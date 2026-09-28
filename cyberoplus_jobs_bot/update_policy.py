from __future__ import annotations

from datetime import datetime, timezone


def _parse(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def update_actions(candidate, existing_record, urgency):
    """Choose how to handle a verified material update to an existing job page."""
    changed = []
    old_positions = _int(existing_record.get("number_of_positions"))
    new_positions = _int(candidate.number_of_positions)

    if old_positions != new_positions and (old_positions or new_positions):
        changed.append("number_of_positions")

    for field in ("deadline", "salary", "contract_type", "location", "application_url"):
        if str(existing_record.get(field) or "").strip() != str(getattr(candidate, field, "") or "").strip():
            changed.append(field)

    social_reason = ""
    social_action = "none"

    # Accuracy correction: a large position-count revision deserves a social update.
    if old_positions and new_positions:
        ratio = abs(new_positions - old_positions) / max(old_positions, 1)
        if ratio >= 0.50:
            social_action = "correction_or_update"
            social_reason = "position count changed by at least 50%"

    # A new application URL changes the user's action path and must be surfaced.
    if "application_url" in changed:
        social_action = "correction_or_update"
        social_reason = "application URL changed"

    # A near deadline is time-sensitive.
    if urgency.get("publish_immediately"):
        social_action = "correction_or_update"
        social_reason = urgency.get("reason") or "urgent update"

    # Deadline extensions/shortenings are meaningful when the date really changed.
    old_deadline = _parse(existing_record.get("deadline"))
    new_deadline = _parse(candidate.deadline)
    if old_deadline and new_deadline and old_deadline.date() != new_deadline.date():
        delta = abs((new_deadline - old_deadline).total_seconds()) / 86400
        if delta >= 1:
            social_action = "correction_or_update"
            social_reason = "deadline changed"

    return {
        "blogger_action": "update_same_post",
        "count_as_new_article": False,
        "changed_fields": list(dict.fromkeys(changed)),
        "social_action": social_action,
        "social_reason": social_reason,
    }
