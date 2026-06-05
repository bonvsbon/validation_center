# Validation Center — .NET Port (Rule Engine SQL generator)

A C# port of the reconciliation SQL generator. It produces **the same DuckDB SQL**
as the Python orchestrator from the same Template + Mapping — verified by
cross-validation (the generated scripts are identical modulo whitespace, and both
yield the validated reference summary `7·2·1·1·1·1·1`).

```
dotnet/
├─ ValidationCenter.slnx
├─ ValidationCenter.RuleEngine/      # class library, zero external deps
│  ├─ Quoting.cs       # identifier/literal quoting + expression whitelist (security boundary)
│  ├─ Models.cs        # ResolvedConfig / Binding / RuleDef (System.Text.Json)
│  ├─ Transforms.cs    # transform JSON -> DuckDB expressions
│  ├─ Rules.cs         # the 8 rule-type SQL builders
│  ├─ Resolver.cs      # load + validate Template/Mapping
│  └─ SqlBuilder.cs    # assemble the full pipeline SQL
└─ ValidationCenter.Cli/             # console: emits the SQL (AssemblyName: vc-recon)
   └─ Program.cs
```

## Build & run
```bash
cd dotnet
dotnet build ValidationCenter.slnx

ValidationCenter.Cli/bin/Debug/net10.0/vc-recon \
  --template ../orchestrator/fixtures/template_ncb.json \
  --mapping  ../orchestrator/fixtures/mapping_ncb.json \
  --code-lists ../orchestrator/fixtures/code_lists.json \
  --source   ../orchestrator/fixtures/source.csv \
  --dest     ../orchestrator/fixtures/dest.csv \
  --as-of    2026-06-04 \
  --dump-sql out/generated_dotnet.sql
```

## Why generator-only (no DuckDB dependency)
The library has **zero external NuGet packages**, so it always builds offline and the
security-critical `Quoting` boundary has no third-party surface. The emitted SQL runs on
any DuckDB engine.

### Native in-process execution (optional)
To execute inside .NET, add the DuckDB.NET package to `ValidationCenter.Cli`:
```bash
dotnet add ValidationCenter.Cli package DuckDB.NET.Data.Full
```
then run the generated script on a connection:
```csharp
using DuckDB.NET.Data;
using var con = new DuckDBConnection("Data Source=:memory:");
con.Open();
using (var cmd = con.CreateCommand()) { cmd.CommandText = sql; cmd.ExecuteNonQuery(); }
using var q = con.CreateCommand();
q.CommandText = "SELECT * FROM run_summary";
using var r = q.ExecuteReader();   // read the 7 KPI columns
```

## Parity contract
`Quoting.cs` must stay byte-for-byte semantically identical to
`orchestrator/quoting.py` — it is the SQL-injection boundary. Any change to a rule
template must be mirrored in both `Rules.cs` and `orchestrator/rules.py`, and the
cross-validation (diff of generated SQL + reference summary) must stay green.
