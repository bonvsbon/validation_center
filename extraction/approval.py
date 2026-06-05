"""Human approval gate for AI-suggested templates.

A draft template (from PDF extraction) must be reviewed before it can be used in a
reconciliation run. This enforces the platform invariant: *AI suggests, humans
approve.* `require_approved` raises if anything is still SUGGESTED, so the run path
can call it as a guard.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, List


def _items(template: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(template.get("fields", [])) + list(template.get("rules", []))


def pending_review(template: Dict[str, Any]) -> Dict[str, Any]:
    fields_pending = [f["field_key"] for f in template.get("fields", [])
                      if f.get("review_status", "APPROVED") == "SUGGESTED"]
    rules_pending = [r.get("rule_key", r.get("rule_id")) for r in template.get("rules", [])
                     if r.get("review_status", "APPROVED") == "SUGGESTED"]
    return {
        "fields_pending": fields_pending,
        "rules_pending": rules_pending,
        "total_pending": len(fields_pending) + len(rules_pending),
        "low_confidence": [x for x in _low_conf(template)],
    }


def _low_conf(template: Dict[str, Any], threshold: float = 0.7) -> List[str]:
    out = []
    for f in template.get("fields", []):
        if (f.get("confidence") or 1.0) < threshold:
            out.append(f"field:{f['field_key']}")
    for r in template.get("rules", []):
        if (r.get("confidence") or 1.0) < threshold:
            out.append(f"rule:{r.get('rule_key', r.get('rule_id'))}")
    return out


def is_fully_approved(template: Dict[str, Any]) -> bool:
    return all(it.get("review_status", "APPROVED") != "SUGGESTED" for it in _items(template))


def require_approved(template: Dict[str, Any]) -> None:
    if not is_fully_approved(template):
        p = pending_review(template)
        raise ValueError(
            f"template not fully approved: {p['total_pending']} item(s) still SUGGESTED "
            f"(fields={p['fields_pending']}, rules={p['rules_pending']})")


def approve_all(template: Dict[str, Any], approver: str = "system",
                publish: bool = True) -> Dict[str, Any]:
    """Approve every SUGGESTED field/rule. Optionally publish (lock) the version."""
    now = datetime.now(timezone.utc).isoformat()
    for it in _items(template):
        if it.get("review_status", "APPROVED") == "SUGGESTED":
            it["review_status"] = "APPROVED"
            it["approved_by"] = approver
            it["approved_at"] = now
    if publish:
        template["status"] = "PUBLISHED"
        template["published_by"] = approver
        template["published_at"] = now
        template["locked"] = True
    return template


def reject(template: Dict[str, Any], item_key: str, kind: str = "rule") -> Dict[str, Any]:
    coll = template.get("rules" if kind == "rule" else "fields", [])
    key_attr = "rule_key" if kind == "rule" else "field_key"
    for it in coll:
        if it.get(key_attr) == item_key or it.get("rule_id") == item_key:
            it["review_status"] = "REJECTED"
    return template
