from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from .config import MOROCCO_TIMEZONE


# Initial experiment: one Facebook post/day, alternating morning/evening windows.
# After 4 weeks these defaults should be replaced by the page's own metrics.
DEFAULT_SLOTS = {
    0: time(19, 30),  # Monday
    1: time(9, 0),    # Tuesday
    2: time(19, 0),   # Wednesday
    3: time(9, 0),    # Thursday
    4: time(19, 30),  # Friday
    5: time(10, 0),   # Saturday
    6: time(19, 0),   # Sunday
}


def _local(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ZoneInfo(MOROCCO_TIMEZONE))


def recommended_facebook_time(urgency, now=None):
    local_now = _local(now)

    if urgency.get("publish_immediately"):
        return {
            "mode": "immediate",
            "publish_at": local_now.isoformat(),
            "reason": urgency.get("reason") or "urgent verified opportunity",
        }

    target_time = DEFAULT_SLOTS[local_now.weekday()]
    target = datetime.combine(local_now.date(), target_time, tzinfo=local_now.tzinfo)

    # If today's slot already passed, publish at the next day's configured slot.
    if target <= local_now:
        next_day = local_now.date().fromordinal(local_now.date().toordinal() + 1)
        next_weekday = (local_now.weekday() + 1) % 7
        target = datetime.combine(
            next_day,
            DEFAULT_SLOTS[next_weekday],
            tzinfo=local_now.tzinfo,
        )

    return {
        "mode": "scheduled",
        "publish_at": target.isoformat(),
        "reason": "initial Morocco A/B timing window; replace with page metrics after learning period",
    }
