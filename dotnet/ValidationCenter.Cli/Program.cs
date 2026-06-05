using ValidationCenter.RuleEngine;

// Minimal CLI: read template/mapping/code-lists + dataset paths, generate the
// deterministic DuckDB reconciliation SQL, and write it out.
//
// Usage:
//   vc-recon --template t.json --mapping m.json --code-lists c.json \
//            --source s.csv --dest d.csv [--as-of 2026-06-04] [--dump-sql out.sql]
//
// Execution: pipe the generated SQL into any DuckDB engine. For native in-process
// execution add the DuckDB.NET.Data.Full NuGet package and run the script on a
// DuckDBConnection (see dotnet/README.md). This CLI keeps zero external
// dependencies so it always builds; the SQL it emits is identical to the Python
// orchestrator's, which is cross-validated in CI.

static string? Arg(string[] a, string name)
{
    var i = Array.IndexOf(a, name);
    return (i >= 0 && i + 1 < a.Length) ? a[i + 1] : null;
}

string Req(string name)
{
    var v = Arg(args, name);
    if (v is null) throw new ArgumentException($"missing required argument {name}");
    return v;
}

try
{
    var template = Resolver.Load<TemplateDoc>(Req("--template"));
    var mapping = Resolver.Load<MappingDoc>(Req("--mapping"));

    Dictionary<string, List<CodeItem>> codeLists = new();
    var clPath = Arg(args, "--code-lists");
    if (clPath is not null)
        codeLists = Resolver.Load<Dictionary<string, List<CodeItem>>>(clPath);

    var asOf = Arg(args, "--as-of") ?? "2026-06-04";
    var cfg = Resolver.Resolve(template, mapping, codeLists, asOf);

    var sql = SqlBuilder.BuildSetupSql(cfg, Req("--source"), Req("--dest"));

    var dump = Arg(args, "--dump-sql");
    if (dump is not null)
    {
        File.WriteAllText(dump, sql);
        Console.WriteLine($"wrote {sql.Length} chars of SQL to {dump}");
    }
    else
    {
        Console.WriteLine(sql);
    }

    Console.Error.WriteLine($"engine={cfg.RuleEngineVersion} as_of={cfg.AsOfDate} " +
                            $"rules={cfg.Rules.Count}+{cfg.CrossRules.Count}cross");
    return 0;
}
catch (Exception e)
{
    Console.Error.WriteLine($"ERROR: {e.Message}");
    return 1;
}
