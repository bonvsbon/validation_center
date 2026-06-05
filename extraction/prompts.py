"""Prompt + structured-output schema for the AnthropicExtractor.

The LLM is constrained to a tool (`emit_template`) whose input_schema is the
extraction contract. This forces well-formed JSON and minimizes hallucination.
The model NEVER decides comparison outcomes — it only proposes fields/rules for a
human to approve.
"""

PROMPT_VERSION = "extract-prompt/1.0.0"

SYSTEM = """You are a precise data-specification extractor for a financial data
reconciliation platform. You read a business specification document (often Thai +
English, e.g. an NCB credit-bureau submission spec) and extract:
  (1) the FIELDS it defines, and
  (2) the validation RULES that compare a Source value against a Destination value.

Hard requirements:
- Return ONLY via the emit_template tool. Do not invent fields or rules that are
  not supported by the document text.
- For every field and rule, include a citation with the page number and the exact
  source_text you used, a confidence in [0,1], and a one-line reasoning.
- Use ONLY these rule types: EQUALITY, TOLERANCE, RANGE, REGEX, NOT_NULL, LOOKUP,
  DATE_VALID, CROSS_FIELD.
- You are a SUGGESTER. A human will approve, edit, or reject everything you return.
"""

# Anthropic tool definition (input_schema is JSON Schema)
TOOL = {
    "name": "emit_template",
    "description": "Emit the extracted fields and validation rules as structured data.",
    "input_schema": {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_key": {"type": "string"},
                        "name": {"type": "string"},
                        "datatype": {"type": "string",
                                     "enum": ["STRING", "INTEGER", "DECIMAL", "DATE",
                                              "DATETIME", "BOOLEAN", "ENUM"]},
                        "required": {"type": "boolean"},
                        "is_key": {"type": "boolean"},
                        "label_th": {"type": "string"},
                        "format": {"type": "string"},
                        "enum_code_list": {"type": "string"},
                        "citation": {
                            "type": "object",
                            "properties": {"page": {"type": "integer"},
                                           "source_text": {"type": "string"}},
                            "required": ["page", "source_text"],
                        },
                        "confidence": {"type": "number"},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["field_key", "name", "datatype", "citation",
                                 "confidence", "reasoning"],
                },
            },
            "rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "rule_key": {"type": "string"},
                        "type": {"type": "string",
                                 "enum": ["EQUALITY", "TOLERANCE", "RANGE", "REGEX",
                                          "NOT_NULL", "LOOKUP", "DATE_VALID", "CROSS_FIELD"]},
                        "target_field_keys": {"type": "array", "items": {"type": "string"}},
                        "params": {"type": "object"},
                        "severity": {"type": "string",
                                     "enum": ["ERROR", "WARNING", "INFO"]},
                        "citation": {
                            "type": "object",
                            "properties": {"page": {"type": "integer"},
                                           "source_text": {"type": "string"}},
                            "required": ["page", "source_text"],
                        },
                        "confidence": {"type": "number"},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["rule_key", "type", "target_field_keys", "citation",
                                 "confidence", "reasoning"],
                },
            },
        },
        "required": ["fields", "rules"],
    },
}


def build_user_prompt(pages) -> str:
    parts = ["Extract fields and rules from this specification.\n"]
    for p in pages:
        parts.append(f"--- PAGE {p.page} ---\n{p.text}\n")
    return "\n".join(parts)
