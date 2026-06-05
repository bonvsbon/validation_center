using System.Globalization;
using System.Text.Json;

namespace ValidationCenter.RuleEngine;

/// <summary>Compile transform JSON into DuckDB SQL expressions. Mirrors
/// orchestrator/transforms.py.</summary>
public static class Transforms
{
    public static string Compile(string expr, JsonElement? transform)
    {
        if (transform is null) return expr;
        var t = transform.Value;
        if (t.ValueKind == JsonValueKind.Null) return expr;
        if (t.ValueKind == JsonValueKind.Array)
        {
            foreach (var op in t.EnumerateArray()) expr = Apply(expr, op);
            return expr;
        }
        return Apply(expr, t);
    }

    private static string S(JsonElement op, string name) => op.GetProperty(name).GetString()!;
    private static JsonElement Arg(JsonElement op, int i) => op.GetProperty("args")[i];
    private static int ArgI(JsonElement op, int i) => Arg(op, i).GetInt32();
    private static string ArgS(JsonElement op, int i) => Arg(op, i).GetString()!;
    private static string ArgN(JsonElement op, int i) =>
        Arg(op, i).GetDouble().ToString("R", CultureInfo.InvariantCulture);

    private static string Apply(string expr, JsonElement op)
    {
        var name = op.GetProperty("op").GetString();
        return name switch
        {
            "trim" => $"trim({expr})",
            "upper" => $"upper({expr})",
            "lower" => $"lower({expr})",
            "round" => $"round(TRY_CAST({expr} AS DOUBLE), {ArgI(op, 0)})",
            "scale" => $"(TRY_CAST({expr} AS DOUBLE) / {ArgN(op, 0)})",
            "substr" => $"substr({expr}, {ArgI(op, 0)}, {ArgI(op, 1)})",
            "pad_left" => $"lpad({expr}, {ArgI(op, 0)}, {Quoting.Lit(ArgS(op, 1))})",
            "replace" => $"replace({expr}, {Quoting.Lit(S(op, "from"))}, {Quoting.Lit(S(op, "to"))})",
            "null_if" => $"nullif({expr}, {Quoting.Lit(ArgS(op, 0))})",
            "date_format" =>
                $"strftime(try_strptime({expr}, {Quoting.Lit(S(op, "from"))}), {Quoting.Lit(S(op, "to"))})",
            _ => throw new ArgumentException($"unknown transform op: {name}")
        };
    }
}
