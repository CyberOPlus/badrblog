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


def expiry_status(candidate, now=None):
    deadline = _parse(candidate.deadline)
    if not deadline:
        return {
            "expired": False,
            "known": False,
            "visible_message": "",
            "disable_apply_cta": False,
            "indexing_action": "",
        }

    now = now or datetime.now(timezone.utc)
    expired = deadline < now
    return {
        "expired": expired,
        "known": True,
        "visible_message": "انتهى أجل الترشيح." if expired else "",
        "disable_apply_cta": expired,
        # Keep the useful article live, update validThrough/visible status, then recrawl.
        "indexing_action": "URL_UPDATED" if expired else "",
    }
