using System.Globalization;
using System.Text.RegularExpressions;
using static ValidationCenter.RuleEngine.Quoting;

namespace ValidationCenter.RuleEngine;

/// <summary>Generate the per-rule DuckDB SELECT for each rule type. Executable
/// mirror of orchestrator/rules.py and db/rule-engine/rule_templates.sql.</summary>
public static class Rules
{
    private static readonly Regex FieldToken = new(@"\bf_[A-Za-z0-9_]+\b", RegexOptions.Compiled);

    private static string NumericExpr(string expr) =>
        FieldToken.Replace(expr, m => $"TRY_CAST({m.Value} AS DECIMAL(18,4))");

    private static string Select(string fieldKey, string ruleId, string severity,
        string expected, string actual, string category, string verdict,
        string fromClause, string recordKey = "record_key") =>
        "SELECT\n" +
        $"  {recordKey} AS record_key,\n" +
        $"  {fieldKey} AS field_key,\n" +
        $"  {ruleId} AS rule_id,\n" +
        $"  {severity} AS severity,\n" +
        $"  {expected} AS expected,\n" +
        $"  {actual} AS actual,\n" +
        $"  {category} AS category,\n" +
        $"  {verdict} AS verdict\n" +
        fromClause;

    public static string BuildSingle(RuleDef r, string asOfDate = "2026-06-04")
    {
        var k = r.TargetFieldKeys[0];
        string s = $"s_{SafeName(k)}", d = $"d_{SafeName(k)}";
        string fk = Lit(k), rid = Lit(r.RuleId), sev = Lit(r.Severity);

        switch (r.Type)
        {
            case "EQUALITY":
            {
                var eq = $"{s} IS NOT DISTINCT FROM {d}";
                return Select(fk, rid, sev,
                    $"CAST({s} AS VARCHAR)", $"CAST({d} AS VARCHAR)",
                    $"CASE WHEN {eq} THEN 'MATCH' ELSE 'MISMATCH' END",
                    $"CASE WHEN {eq} THEN 'equal' ELSE 'not_equal' END",
                    "FROM matched");
            }
            case "TOLERANCE":
            {
                int scale = r.IntParam("scale", 6);
                string tol = Num(r.NumParam("tolerance"));
                string cs = $"TRY_CAST({s} AS DECIMAL(38,{scale}))";
                string cd = $"TRY_CAST({d} AS DECIMAL(38,{scale}))";
                string nul = $"{cs} IS NULL OR {cd} IS NULL";
                return Select(fk, rid, sev,
                    $"CAST({s} AS VARCHAR)", $"CAST({d} AS VARCHAR)",
                    $"CASE WHEN {nul} THEN 'EXCEPTION' WHEN abs({cs} - {cd}) <= {tol} THEN 'MATCH' ELSE 'MISMATCH' END",
                    $"CASE WHEN {nul} THEN 'uncastable' ELSE 'diff=' || CAST(abs({cs} - {cd}) AS VARCHAR) END",
                    "FROM matched");
            }
            case "RANGE":
            {
                string lo = Num(r.NumParam("min")), hi = Num(r.NumParam("max"));
                string cd = $"TRY_CAST({d} AS DOUBLE)";
                return Select(fk, rid, sev,
                    Lit($"[{lo},{hi}]"), $"CAST({d} AS VARCHAR)",
                    $"CASE WHEN {cd} IS NULL THEN 'EXCEPTION' WHEN {cd} BETWEEN {lo} AND {hi} THEN 'MATCH' ELSE 'MISMATCH' END",
                    $"CASE WHEN {cd} IS NULL THEN 'uncastable' WHEN {cd} BETWEEN {lo} AND {hi} THEN 'in_range' ELSE 'out_of_range' END",
                    "FROM matched");
            }
            case "REGEX":
            {
                string pat = Lit(r.StrParam("pattern"));
                string m = $"regexp_full_match(CAST({d} AS VARCHAR), {pat})";
                return Select(fk, rid, sev,
                    pat, $"CAST({d} AS VARCHAR)",
                    $"CASE WHEN {d} IS NULL THEN 'MISMATCH' WHEN {m} THEN 'MATCH' ELSE 'MISMATCH' END",
                    $"CASE WHEN {d} IS NULL THEN 'empty' WHEN {m} THEN 'format_ok' ELSE 'format_bad' END",
                    "FROM matched");
            }
            case "NOT_NULL":
            {
                string blank = $"{d} IS NULL OR trim(CAST({d} AS VARCHAR)) = ''";
                return Select(fk, rid, sev,
                    Lit("NOT_NULL"), $"CAST({d} AS VARCHAR)",
                    $"CASE WHEN {blank} THEN 'MISMATCH' ELSE 'MATCH' END",
                    $"CASE WHEN {blank} THEN 'missing' ELSE 'present' END",
                    "FROM matched");
            }
            case "LOOKUP":
            {
                string clName = r.StrParam("code_list");
                string cl = SafeName(clName);
                string join = $"FROM matched LEFT JOIN cl_{cl} cl ON cl.item_code = CAST({d} AS VARCHAR) AND cl.active";
                return Select(Lit(k), rid, sev,
                    Lit($"in:{clName}"), $"CAST({d} AS VARCHAR)",
                    $"CASE WHEN {d} IS NULL THEN 'MISMATCH' WHEN cl.item_code IS NOT NULL THEN 'MATCH' ELSE 'MISMATCH' END",
                    $"CASE WHEN {d} IS NULL THEN 'empty' WHEN cl.item_code IS NOT NULL THEN 'found' ELSE 'not_in_list' END",
                    join);
            }
            case "DATE_VALID":
            {
                string fmt = Lit(r.StrParam("format", "%Y-%m-%d"));
                bool notFuture = r.BoolParam("not_future");
                string asOf = $"DATE {Lit(asOfDate)}";
                string parsed = $"try_strptime(CAST({d} AS VARCHAR), {fmt})";
                string future = notFuture ? $"{parsed}::DATE > {asOf}" : "FALSE";
                return Select(Lit(k), rid, sev,
                    Lit($"valid_date({r.StrParam("format", "%Y-%m-%d")})"), $"CAST({d} AS VARCHAR)",
                    $"CASE WHEN {parsed} IS NULL THEN 'MISMATCH' WHEN {future} THEN 'MISMATCH' ELSE 'MATCH' END",
                    $"CASE WHEN {parsed} IS NULL THEN 'unparseable' WHEN {future} THEN 'future_date' ELSE 'valid' END",
                    "FROM matched");
            }
            default:
                throw new ArgumentException($"unsupported single rule type {r.Type}");
        }
    }

    public static string BuildCross(RuleDef r)
    {
        string lhs = CheckExpr(r.StrParam("lhs"));
        string rhs = CheckExpr(r.StrParam("rhs"));
        string tol = Num(r.NumParam("tolerance"));
        string clhs = $"({NumericExpr(lhs)})";
        string crhs = $"({NumericExpr(rhs)})";
        string nul = $"{clhs} IS NULL OR {crhs} IS NULL";
        return Select(Lit($"cross:{r.Key}"), Lit(r.RuleId), Lit(r.Severity),
            $"CAST({crhs} AS VARCHAR)", $"CAST({clhs} AS VARCHAR)",
            $"CASE WHEN {nul} THEN 'EXCEPTION' WHEN abs({clhs} - {crhs}) <= {tol} THEN 'MATCH' ELSE 'MISMATCH' END",
            $"CASE WHEN {nul} THEN 'uncastable' ELSE 'diff=' || CAST(abs({clhs} - {crhs}) AS VARCHAR) END",
            "FROM matched_wide");
    }

    public static string BuildFieldEvalUnion(ResolvedConfig cfg)
    {
        var members = new List<string>();
        foreach (var r in cfg.Rules) members.Add(BuildSingle(r, cfg.AsOfDate));
        foreach (var r in cfg.CrossRules) members.Add(BuildCross(r));
        if (members.Count == 0) throw new InvalidOperationException("no rules to evaluate");
        return string.Join("\nUNION ALL\n", members);
    }
}
