using System.Globalization;
using System.Text.RegularExpressions;

namespace ValidationCenter.RuleEngine;

/// <summary>
/// SQL quoting / literal helpers — the single choke point that prevents SQL
/// injection from config-derived identifiers, literals and expressions.
/// Mirrors orchestrator/quoting.py exactly (the security boundary).
/// </summary>
public static class Quoting
{
    public static string Ident(string name) => "\"" + name.Replace("\"", "\"\"") + "\"";

    public static string Lit(string value) => "'" + value.Replace("'", "''") + "'";

    public static string Num(double value) =>
        value.ToString("R", CultureInfo.InvariantCulture);

    public static string Num(int value) => value.ToString(CultureInfo.InvariantCulture);

    public static string Bool(bool value) => value ? "TRUE" : "FALSE";

    public static string SafeName(string name)
    {
        var s = Regex.Replace(name, @"\W", "_");
        if (s.Length == 0 || char.IsDigit(s[0])) s = "_" + s;
        return s;
    }

    private static readonly Regex ExprOk =
        new(@"^[A-Za-z0-9_+\-*/().,\s]+$", RegexOptions.Compiled);

    private static readonly string[] Bad =
        { "select", "insert", "update", "delete", "drop", ";", "--", "/*" };

    public static string CheckExpr(string expr)
    {
        if (!ExprOk.IsMatch(expr))
            throw new ArgumentException($"unsafe expression: {expr}");
        var lowered = expr.ToLowerInvariant();
        foreach (var b in Bad)
            if (lowered.Contains(b))
                throw new ArgumentException($"unsafe expression contains '{b}': {expr}");
        return expr;
    }
}
