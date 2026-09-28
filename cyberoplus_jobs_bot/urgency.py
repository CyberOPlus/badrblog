from __future__ import annotations

from datetime import datetime, timezone


def _parse_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def days_until_deadline(candidate, now=None):
    deadline = _parse_date(candidate.deadline)
    if not deadline:
        return None
    now = now or datetime.now(timezone.utc)
    return (deadline - now).total_seconds() / 86400


def classify_urgency(candidate, now=None):
    """Return a conservative urgency classification.

    Critical/high urgency can bypass the normal Facebook slot only when the
    opportunity is official and already passed the quality/eligibility gate.
    """
    now = now or datetime.now(timezone.utc)
    days = days_until_deadline(candidate, now=now)
    try:
        positions = max(0, int(candidate.number_of_positions or 0))
    except (TypeError, ValueError):
        positions = 0

    if days is not None and days < 0:
        return {
            "level": "expired",
            "publish_immediately": False,
            "allow_daily_override": False,
            "reason": "deadline passed",
            "deadline_days": round(days, 2),
        }

    if candidate.official_source and days is not None and days <= 2:
        return {
            "level": "critical",
            "publish_immediately": True,
            "allow_daily_override": True,
            "reason": "official opportunity closes within 48 hours",
            "deadline_days": round(days, 2),
        }

    if candidate.official_source and positions >= 100 and (days is None or days <= 7):
        return {
            "level": "high",
            "publish_immediately": True,
            "allow_daily_override": True,
            "reason": "large official hiring campaign",
            "deadline_days": None if days is None else round(days, 2),
        }

    if candidate.official_source and positions >= 20:
        return {
            "level": "elevated",
            "publish_immediately": False,
            "allow_daily_override": False,
            "reason": "notable official hiring campaign",
            "deadline_days": None if days is None else round(days, 2),
        }

    return {
        "level": "normal",
        "publish_immediately": False,
        "allow_daily_override": False,
        "reason": "",
        "deadline_days": None if days is None else round(days, 2),
    }
