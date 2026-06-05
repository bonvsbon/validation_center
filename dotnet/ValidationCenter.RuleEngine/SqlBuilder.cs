using System.Text;
using static ValidationCenter.RuleEngine.Quoting;

namespace ValidationCenter.RuleEngine;

/// <summary>Assemble the full DuckDB reconciliation script. Mirrors
/// orchestrator/sql_builder.py — output is byte-comparable in structure.</summary>
public static class SqlBuilder
{
    private static List<string> CanonProjection(
        IReadOnlyDictionary<string, Binding> fields, bool src)
    {
        var outp = new List<string>();
        foreach (var (key, b) in fields)
        {
            var col = src ? b.SrcCol! : b.DstCol!;
            var tf = src ? b.SrcTransform : b.DstTransform;
            var expr = Transforms.Compile(Ident(col), tf);
            outp.Add($"{expr} AS f_{SafeName(key)}");
        }
        return outp;
    }

    private static List<string> KeyExprs(ResolvedConfig cfg, bool src)
    {
        var outp = new List<string>();
        foreach (var kp in cfg.KeyPairs)
        {
            var col = src ? kp.SrcCol! : kp.DstCol!;
            var tf = src ? kp.SrcTransform : kp.DstTransform;
            var expr = Transforms.Compile(Ident(col), tf);
            outp.Add($"CAST({expr} AS VARCHAR)");
        }
        return outp;
    }

    private static string CodeListTables(ResolvedConfig cfg)
    {
        var sb = new StringBuilder();
        foreach (var (name, items) in cfg.CodeLists)
        {
            var tbl = $"cl_{SafeName(name)}";
            if (items.Count > 0)
            {
                var rows = string.Join(", ",
                    items.Select(it => $"({Lit(it.ItemCode)}, {Bool(it.Active)})"));
                sb.AppendLine($"CREATE OR REPLACE TEMP TABLE {tbl} AS " +
                              $"SELECT * FROM (VALUES {rows}) AS t(item_code, active);");
            }
            else
            {
                sb.AppendLine($"CREATE OR REPLACE TEMP TABLE {tbl} AS " +
                              "SELECT NULL::VARCHAR AS item_code, NULL::BOOLEAN AS active WHERE FALSE;");
            }
        }
        return sb.ToString();
    }

    public static string BuildSetupSql(ResolvedConfig cfg, string srcCsv, string dstCsv)
    {
        var srcFields = cfg.SrcFields();
        var dstFields = cfg.DstFields();

        var srcProj = string.Join(",\n       ", CanonProjection(srcFields, true));
        var dstProj = string.Join(",\n       ", CanonProjection(dstFields, false));
        var srcKey = string.Join(", ", KeyExprs(cfg, true));
        var dstKey = string.Join(", ", KeyExprs(cfg, false));

        var sCols = string.Join(", ", srcFields.Keys.Select(k => $"s.f_{SafeName(k)} AS s_{SafeName(k)}"));
        var dCols = string.Join(", ", dstFields.Keys.Select(k => $"d.f_{SafeName(k)} AS d_{SafeName(k)}"));
        var wideCols = string.Join(", ", dstFields.Keys.Select(k => $"d_{SafeName(k)} AS f_{SafeName(k)}"));

        var fieldUnion = Rules.BuildFieldEvalUnion(cfg);
        var codeLists = CodeListTables(cfg);

        return $$"""

-- ===== Step 1: STAGE =====
CREATE OR REPLACE TEMP TABLE src_raw AS
  SELECT * FROM read_csv_auto({{Lit(srcCsv)}}, header=true, all_varchar=true);
CREATE OR REPLACE TEMP TABLE dst_raw AS
  SELECT * FROM read_csv_auto({{Lit(dstCsv)}}, header=true, all_varchar=true);

-- ===== Step 2: CANONIZE =====
CREATE OR REPLACE TEMP TABLE src_keyed AS
  SELECT concat_ws('|', {{srcKey}}) AS record_key,
       {{srcProj}}
  FROM src_raw;
CREATE OR REPLACE TEMP TABLE dst_keyed AS
  SELECT concat_ws('|', {{dstKey}}) AS record_key,
       {{dstProj}}
  FROM dst_raw;

-- ===== Step 3: DUPLICATES =====
CREATE OR REPLACE TEMP TABLE dup_keys AS
  SELECT record_key, 'SOURCE' AS side FROM src_keyed GROUP BY record_key HAVING COUNT(*) > 1
  UNION ALL
  SELECT record_key, 'DEST'   FROM dst_keyed GROUP BY record_key HAVING COUNT(*) > 1;
CREATE OR REPLACE TEMP TABLE src_u AS
  SELECT * FROM src_keyed WHERE record_key NOT IN (SELECT record_key FROM dup_keys WHERE side='SOURCE');
CREATE OR REPLACE TEMP TABLE dst_u AS
  SELECT * FROM dst_keyed WHERE record_key NOT IN (SELECT record_key FROM dup_keys WHERE side='DEST');

-- ===== Step 4: MATCHED =====
CREATE OR REPLACE TEMP TABLE matched AS
  SELECT s.record_key, {{sCols}}, {{dCols}}
  FROM src_u s JOIN dst_u d USING (record_key);
CREATE OR REPLACE TEMP TABLE matched_wide AS
  SELECT record_key, {{wideCols}} FROM matched;

-- ===== Step 5: MISSING (full presence) =====
CREATE OR REPLACE TEMP TABLE rec_missing AS
  SELECT record_key, 'MISSING_IN_DEST' AS category
    FROM (SELECT DISTINCT record_key FROM src_keyed)
   WHERE record_key NOT IN (SELECT DISTINCT record_key FROM dst_keyed)
  UNION ALL
  SELECT record_key, 'MISSING_IN_SOURCE'
    FROM (SELECT DISTINCT record_key FROM dst_keyed)
   WHERE record_key NOT IN (SELECT DISTINCT record_key FROM src_keyed);

-- ===== Code lists for LOOKUP =====
{{codeLists}}

-- ===== Step 6: FIELD EVALUATION =====
CREATE OR REPLACE TEMP TABLE field_results AS
{{fieldUnion}};

-- ===== Step 7: RECORD ROLLUP =====
CREATE OR REPLACE TEMP TABLE record_rollup AS
WITH base AS (
  SELECT DISTINCT record_key FROM src_keyed
  UNION SELECT DISTINCT record_key FROM dst_keyed
),
agg AS (
  SELECT record_key,
         BOOL_OR(category='EXCEPTION') AS has_exc,
         BOOL_OR(category='MISMATCH' AND severity='ERROR') AS has_err_mismatch
  FROM field_results GROUP BY record_key
)
SELECT b.record_key,
  CASE WHEN m.category IS NOT NULL THEN m.category
       WHEN dk.record_key IS NOT NULL THEN 'DUPLICATE'
       WHEN COALESCE(a.has_exc, false) THEN 'EXCEPTION'
       WHEN COALESCE(a.has_err_mismatch, false) THEN 'MISMATCH'
       ELSE 'MATCH' END AS category
FROM base b
LEFT JOIN rec_missing m USING (record_key)
LEFT JOIN (SELECT DISTINCT record_key FROM dup_keys) dk USING (record_key)
LEFT JOIN agg a USING (record_key);

-- ===== Step 8: SUMMARY =====
CREATE OR REPLACE TEMP TABLE run_summary AS
SELECT
  COUNT(*) AS total_records,
  COUNT(*) FILTER (WHERE category='MATCH') AS match_count,
  COUNT(*) FILTER (WHERE category='MISMATCH') AS mismatch_count,
  COUNT(*) FILTER (WHERE category='MISSING_IN_SOURCE') AS missing_src,
  COUNT(*) FILTER (WHERE category='MISSING_IN_DEST') AS missing_dst,
  COUNT(*) FILTER (WHERE category='DUPLICATE') AS duplicate_count,
  COUNT(*) FILTER (WHERE category='EXCEPTION') AS exception_count
FROM record_rollup;
""";
    }
}
