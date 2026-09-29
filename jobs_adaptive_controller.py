from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from config import (
    JOBS_ADAPTIVE_MAX_DAILY_CAP,
    JOBS_ADAPTIVE_MIN_DAILY_CAP,
    JOBS_ADAPTIVE_PUBLISHING,
    JOBS_TIMEZONE,
)

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "data" / "jobs_adaptive_state.json"
CAP_STAGES = (3, 4, 6, 8, 10, 12)
GREEN_THRESHOLDS = (0, 2, 4, 7, 10, 14)
HISTORY_DAYS = 120


def _local_now(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ZoneInfo(JOBS_TIMEZONE))


def _default_state():
    return {
        "version": 1,
        "green_score": 0,
        "current_day": "",
        "days": {},
        "last_evaluated_day": "",
    }


def load_state():
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _default_state()
    state = _default_state()
    if isinstance(data, dict):
        state.update(data)
    if not isinstance(state.get("days"), dict):
        state["days"] = {}
    return state


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(STATE_PATH)


def _day_row(state, day):
    return state.setdefault("days", {}).setdefault(
        day,
        {
            "blogger_success": 0,
            "blogger_failure": 0,
            "blogger_rate_limit": 0,
            "deterministic_fallback": 0,
            "ai_provider_failure": 0,
            "facebook_failure": 0,
            "source_warning": 0,
            "logo_wait": 0,
        },
    )


def _evaluate_day(state, day):
    if not day or state.get("last_evaluated_day") == day:
        return False
    row = state.get("days", {}).get(day) or {}
    success = int(row.get("blogger_success") or 0)
    failures = int(row.get("blogger_failure") or 0)
    rate_limits = int(row.get("blogger_rate_limit") or 0)
    score = int(state.get("green_score") or 0)

    if rate_limits:
        score -= 4
    elif failures >= 2:
        score -= 3
    elif failures == 1:
        score -= 1
    elif success:
        score += 1

    state["green_score"] = max(0, min(30, score))
    state["last_evaluated_day"] = day
    return True


def _roll_day(state, now=None):
    local = _local_now(now)
    today = local.date().isoformat()
    current = str(state.get("current_day") or "")
    changed = False
    if current and current != today:
        changed = _evaluate_day(state, current) or changed
    if current != today:
        state["current_day"] = today
        _day_row(state, today)
        changed = True

    days = state.setdefault("days", {})
    if len(days) > HISTORY_DAYS:
        for day in sorted(days)[:-HISTORY_DAYS]:
            days.pop(day, None)
        changed = True
    return changed


def current_policy(now=None):
    state = load_state()
    if _roll_day(state, now=now):
        save_state(state)

    if not JOBS_ADAPTIVE_PUBLISHING:
        cap = JOBS_ADAPTIVE_MIN_DAILY_CAP
        return {"enabled": False, "stage": 0, "green_score": 0, "daily_cap": cap}

    score = int(state.get("green_score") or 0)
    stage = 0
    for index, threshold in enumerate(GREEN_THRESHOLDS):
        if score >= threshold:
            stage = index
    cap = CAP_STAGES[min(stage, len(CAP_STAGES) - 1)]
    cap = max(JOBS_ADAPTIVE_MIN_DAILY_CAP, min(JOBS_ADAPTIVE_MAX_DAILY_CAP, cap))
    return {
        "enabled": True,
        "stage": stage,
        "green_score": score,
        "daily_cap": cap,
    }


def record_cycle_result(result=None, error=""):
    state = load_state()
    _roll_day(state)
    day = state.get("current_day") or _local_now().date().isoformat()
    row = _day_row(state, day)
    result = result or {}
    article = result.get("article") or {}
    draft = result.get("draft") or {}
    facebook = result.get("facebook") or {}
    reason = " ".join(
        str(x or "")
        for x in (
            error,
            result.get("reason"),
            draft.get("error") if isinstance(draft, dict) else "",
            article.get("publish_error"),
        )
    ).casefold()

    logo_deferred = bool(
        (isinstance(draft, dict) and draft.get("deferred"))
        or article.get("publish_status") == "waiting_for_logo"
    )
    if result.get("draft_action") in {"created", "updated"} and article.get("publish_status") == "published":
        row["blogger_success"] = int(row.get("blogger_success") or 0) + 1
    elif result.get("step_reached") == "publish" and reason and not logo_deferred:
        row["blogger_failure"] = int(row.get("blogger_failure") or 0) + 1
        if any(token in reason for token in ("429", "403", "rate limit", "quota", "too many requests")):
            row["blogger_rate_limit"] = int(row.get("blogger_rate_limit") or 0) + 1

    provider = str(article.get("ai_provider_used") or "")
    if provider.startswith("deterministic:"):
        row["deterministic_fallback"] = int(row.get("deterministic_fallback") or 0) + 1
    if article.get("ai_rotation_exhausted") or article.get("ai_time_budget_exceeded"):
        row["ai_provider_failure"] = int(row.get("ai_provider_failure") or 0) + 1
    if article.get("publish_block_reason") in {
        "verified_company_logo_required",
        "verified_company_logo_render_failed",
    }:
        row["logo_wait"] = int(row.get("logo_wait") or 0) + 1
    if isinstance(facebook, dict) and facebook.get("error") and not facebook.get("posted"):
        row["facebook_failure"] = int(row.get("facebook_failure") or 0) + 1
    row["source_warning"] = int(row.get("source_warning") or 0) + int(result.get("source_warnings_count") or 0)
    save_state(state)
    return current_policy()
