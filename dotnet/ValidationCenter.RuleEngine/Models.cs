using System.Text.Json;
using System.Text.Json.Serialization;

namespace ValidationCenter.RuleEngine;

public sealed class Binding
{
    [JsonPropertyName("field_key")] public string FieldKey { get; set; } = "";
    [JsonPropertyName("src_col")] public string? SrcCol { get; set; }
    [JsonPropertyName("dst_col")] public string? DstCol { get; set; }
    [JsonPropertyName("src_transform")] public JsonElement? SrcTransform { get; set; }
    [JsonPropertyName("dst_transform")] public JsonElement? DstTransform { get; set; }
}

public sealed class RuleDef
{
    [JsonPropertyName("rule_id")] public string RuleId { get; set; } = "";
    [JsonPropertyName("rule_key")] public string? RuleKey { get; set; }
    [JsonPropertyName("type")] public string Type { get; set; } = "";
    [JsonPropertyName("severity")] public string Severity { get; set; } = "ERROR";
    [JsonPropertyName("params")] public Dictionary<string, JsonElement>? Params { get; set; }
    [JsonPropertyName("target_field_keys")] public List<string> TargetFieldKeys { get; set; } = new();

    public string Key => RuleKey ?? RuleId;

    // typed param helpers
    public double NumParam(string name) => Params![name].GetDouble();
    public int IntParam(string name, int dflt) =>
        Params != null && Params.TryGetValue(name, out var v) ? v.GetInt32() : dflt;
    public string StrParam(string name, string dflt = "") =>
        Params != null && Params.TryGetValue(name, out var v) ? (v.GetString() ?? dflt) : dflt;
    public bool BoolParam(string name, bool dflt = false) =>
        Params != null && Params.TryGetValue(name, out var v) && v.GetBoolean();
    public string RawParam(string name) => Params![name].GetRawText();
}

public sealed class CodeItem
{
    [JsonPropertyName("item_code")] public string ItemCode { get; set; } = "";
    [JsonPropertyName("active")] public bool Active { get; set; } = true;
}

public sealed class TemplateDoc
{
    [JsonPropertyName("version")] public string? Version { get; set; }
    [JsonPropertyName("rules")] public List<RuleDef> Rules { get; set; } = new();
}

public sealed class MappingDoc
{
    [JsonPropertyName("expected_side")] public string ExpectedSide { get; set; } = "SOURCE";
    [JsonPropertyName("key_pairs")] public List<Binding> KeyPairs { get; set; } = new();
    [JsonPropertyName("field_bindings")] public List<Binding> FieldBindings { get; set; } = new();
}

public sealed class ResolvedConfig
{
    public string ExpectedSide { get; init; } = "SOURCE";
    public List<Binding> KeyPairs { get; init; } = new();
    public List<Binding> FieldBindings { get; init; } = new();
    public List<RuleDef> Rules { get; init; } = new();        // single-field
    public List<RuleDef> CrossRules { get; init; } = new();   // CROSS_FIELD
    public Dictionary<string, List<CodeItem>> CodeLists { get; init; } = new();
    public string AsOfDate { get; init; } = "2026-06-04";
    public string RuleEngineVersion { get; init; } = "rule-engine/1.0.0";

    /// <summary>field_key -> Binding (deduped) for fields with a source column.</summary>
    public IReadOnlyDictionary<string, Binding> SrcFields()
    {
        var o = new Dictionary<string, Binding>();
        foreach (var b in KeyPairs.Concat(FieldBindings))
            if (b.SrcCol != null && !o.ContainsKey(b.FieldKey)) o[b.FieldKey] = b;
        return o;
    }

    public IReadOnlyDictionary<string, Binding> DstFields()
    {
        var o = new Dictionary<string, Binding>();
        foreach (var b in KeyPairs.Concat(FieldBindings))
            if (b.DstCol != null && !o.ContainsKey(b.FieldKey)) o[b.FieldKey] = b;
        return o;
    }
}
