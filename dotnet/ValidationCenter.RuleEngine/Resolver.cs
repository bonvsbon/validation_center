using System.Text.Json;

namespace ValidationCenter.RuleEngine;

/// <summary>Flatten Template + Mapping (+ code lists) into a ResolvedConfig and
/// validate it. Mirrors orchestrator/resolver.py.</summary>
public static class Resolver
{
    private static readonly HashSet<string> SingleFieldRules = new()
        { "EQUALITY", "TOLERANCE", "RANGE", "REGEX", "NOT_NULL", "LOOKUP", "DATE_VALID" };
    private static readonly HashSet<string> BothSideRules = new() { "EQUALITY", "TOLERANCE" };

    private static readonly JsonSerializerOptions Opts = new()
    {
        PropertyNameCaseInsensitive = true
    };

    public static T Load<T>(string path) =>
        JsonSerializer.Deserialize<T>(File.ReadAllText(path), Opts)
        ?? throw new InvalidOperationException($"failed to parse {path}");

    public static ResolvedConfig Resolve(
        TemplateDoc template, MappingDoc mapping,
        Dictionary<string, List<CodeItem>>? codeLists = null,
        string asOfDate = "2026-06-04",
        string ruleEngineVersion = "rule-engine/1.0.0")
    {
        var single = template.Rules.Where(r => r.Type != "CROSS_FIELD")
            .OrderBy(r => r.RuleId).ToList();
        var cross = template.Rules.Where(r => r.Type == "CROSS_FIELD")
            .OrderBy(r => r.RuleId).ToList();

        var cfg = new ResolvedConfig
        {
            ExpectedSide = mapping.ExpectedSide,
            KeyPairs = mapping.KeyPairs,
            FieldBindings = mapping.FieldBindings,
            Rules = single,
            CrossRules = cross,
            CodeLists = codeLists ?? new(),
            AsOfDate = asOfDate,
            RuleEngineVersion = ruleEngineVersion
        };
        Validate(cfg);
        return cfg;
    }

    private static void Validate(ResolvedConfig cfg)
    {
        if (cfg.KeyPairs.Count == 0)
            throw new ArgumentException("mapping has no key_pairs — cannot match records");
        var src = cfg.SrcFields();
        var dst = cfg.DstFields();

        foreach (var r in cfg.Rules)
        {
            if (!SingleFieldRules.Contains(r.Type))
                throw new ArgumentException($"rule {r.RuleId}: unknown single-field type {r.Type}");
            if (r.TargetFieldKeys.Count == 0)
                throw new ArgumentException($"rule {r.RuleId}: missing target_field_keys");
            var k = r.TargetFieldKeys[0];
            if (!dst.ContainsKey(k))
                throw new ArgumentException($"rule {r.RuleId}: field '{k}' has no destination binding");
            if (BothSideRules.Contains(r.Type) && !src.ContainsKey(k))
                throw new ArgumentException($"rule {r.RuleId}: field '{k}' needs a source binding for {r.Type}");
            if (r.Type == "LOOKUP")
            {
                var cl = r.StrParam("code_list");
                if (!cfg.CodeLists.ContainsKey(cl))
                    throw new ArgumentException($"rule {r.RuleId}: code_list '{cl}' not provided");
            }
        }

        foreach (var r in cfg.CrossRules)
            foreach (var p in new[] { "lhs", "rhs", "tolerance" })
                if (r.Params == null || !r.Params.ContainsKey(p))
                    throw new ArgumentException($"cross rule {r.RuleId}: missing param '{p}'");
    }
}
