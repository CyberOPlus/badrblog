from __future__ import annotations

import json

from .ai_engine import generate_json


def _validator(data):
    decision = str(data.get("decision") or "")
    if decision not in {"same_posting", "new_campaign", "uncertain"}:
        raise ValueError("invalid identity decision")
    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        raise ValueError("invalid confidence")
    evidence = data.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list")


def review_ambiguous_identity(candidate, existing_record):
    """AI assists only when deterministic signals are inconclusive.

    It cannot invent facts, browse, or make a low-confidence merge. A merge/new
    campaign result is accepted only at >=0.90 confidence; otherwise the item is held.
    """
    payload = {
        "new_candidate": candidate.to_dict(),
        "existing_record": existing_record,
    }
    prompt = f"""
You are a strict job-posting identity reviewer.

Decide whether NEW_CANDIDATE is:
- same_posting: the same real-world recruitment posting, with updated mutable facts;
- new_campaign: a genuinely separate recruitment campaign that deserves a new page;
- uncertain: evidence is insufficient.

Rules:
- Use ONLY the JSON facts below.
- Never infer missing facts.
- A changed number of positions alone (for example 100 then 20) is normally an UPDATE,
  not a new campaign, if company/title/location and campaign/application identity align.
- A different requisition/reference ID is strong evidence of a new campaign.
- A clearly different application URL is strong evidence of a new campaign.
- A repost around 30+ days later with a new deadline can be a new campaign.
- Same title alone is never enough.
- Return uncertain whenever evidence conflicts or is weak.
- confidence must reflect evidence quality, not intuition.
- evidence must cite exact fields from the input.

Return JSON only:
{{
  "decision": "same_posting|new_campaign|uncertain",
  "confidence": 0.0,
  "evidence": ["field-based reason"],
  "material_changes": ["changed fact"]
}}

INPUT:
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()

    data = generate_json(prompt, validator=_validator)
    confidence = float(data["confidence"])
    accepted = confidence >= 0.90 and data["decision"] in {"same_posting", "new_campaign"}
    return {
        **data,
        "accepted": accepted,
        "action": (
            "update" if accepted and data["decision"] == "same_posting"
            else "new_campaign" if accepted and data["decision"] == "new_campaign"
            else "hold"
        ),
    }
