"""Read-only Facebook performance samples for the CyberOPlus Jobs page.

This module never creates, edits, boosts or deletes a Facebook post. A lack
of Meta read permissions cannot interrupt Blogger or the Facebook queue.
Counters are NOT reach, impressions, link clicks or application conversions.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from config import (
    FACEBOOK_GRAPH_API_URL,
    FACEBOOK_PAGE_ACCESS_TOKEN,
)
from state_io import atomic_write_json

BASE = Path(__file__).resolve().parent
QUEUE_PATH = BASE / "jobs_article_queue.json"
STATE_PATH = BASE / "data" / "facebook_performance.json"
MOROCCO = ZoneInfo("Africa/Casablanca")
POST_ID = re.compile(r"^[0-9]+(?:_[0-9]+)?$")
POLL_SECONDS = 20 * 60
MAX_POSTS_PER_RUN = 2
# Lower and upper ages ensure we never relabel a 6-day count as "24h".
SAMPLE_WINDOWS = {
    "early": (1, 8),
    "day": (24, 48),
    "week": (168, 216),
}


def _time(value):
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _state():
    data = _load_json(STATE_PATH)
    return {
        "version": 1,
        "posts": data.get("posts") if isinstance(data.get("posts"), dict) else {},
        "last_poll_at": data.get("last_poll_at") or "",
        "last_error_code": data.get("last_error_code") or "",
        "blocked_until": data.get("blocked_until") or "",
    }


def _parse_count(payload, field):
    item = payload.get(field) or {}
    if not isinstance(item, dict):
        return None
    summary = item.get("summary") or {}
    count = summary.get("total_count") if isinstance(summary, dict) else None
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return None
    return max(0, int(count))


def _extract_counts(payload, first_comment_known):
    """Page's own first comment is excluded from reader comment count."""
    reactions = _parse_count(payload, "reactions")
    comments = _parse_count(payload, "comments")
    shares_obj = payload.get("shares") or {}
    shares = shares_obj.get("count") if isinstance(shares_obj, dict) else None
    if isinstance(shares, bool) or not isinstance(shares, (int, float)):
        shares = None
    else:
        shares = max(0, int(shares))
    comments_readers = max(0, comments - int(first_comment_known)) if comments is not None else None
    result = {
        "reactions": reactions,
        "comments_total": comments,
        "comments_excluding_first_comment": comments_readers,
        "shares": shares,
    }
    known = (reactions, comments_readers, shares)
    result["known_interactions"] = sum(v for v in known if v is not None) if any(v is not None for v in known) else None
    return result


def _candidate_samples(queue, state, now):
    """Prioritize 24h observations over baseline; do not replay old history."""
    ranked = []
    for article in queue.get("articles") or []:
        if not isinstance(article, dict):
            continue
        post_id = str(article.get("facebook_post_id") or "").strip()
        posted = _time(article.get("facebook_posted_at"))
        if not POST_ID.fullmatch(post_id) or not posted:
            continue
        age_hours = (now - posted).total_seconds() / 3600
        if age_hours < 0 or age_hours > 216:
            continue
        existing = (state.get("posts") or {}).get(post_id) or {}
        snapshots = existing.get("snapshots") or {}
        for stage, (minimum, maximum) in SAMPLE_WINDOWS.items():
            if stage in snapshots or not minimum <= age_hours <= maximum:
                continue
            # The 24-hour comparison is most important for picking future
            # audience slots. No multiple requests for the same post/run.
            priority = {"day": 0, "early": 1, "week": 2}[stage]
            ranked.append((priority, max(0.0, age_hours - minimum), post_id, stage, article, posted, age_hours))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    seen = set()
    for item in ranked:
        if item[2] in seen:
            continue
        seen.add(item[2])
        yield item


def _request_engagement(post_id, token, session=requests):
    """Use basic Graph post edges; deprecated Insights metrics are not queried."""
    response = session.get(
        f"{FACEBOOK_GRAPH_API_URL.rstrip('/')}/{post_id}",
        params={
            "access_token": token,
            "fields": "id,created_time,shares,reactions.limit(0).summary(true),comments.limit(0).summary(true)",
        },
        timeout=(3, 5),
    )
    if response.status_code >= 400:
        # Never print URLs, token, response body or request config.
        return None, f"http_{response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return None, "invalid_json"
    if not isinstance(payload, dict) or payload.get("error"):
        error = payload.get("error") if isinstance(payload, dict) else {}
        code = error.get("code") if isinstance(error, dict) else None
        return None, "graph_error_" + str(code if isinstance(code, int) else "unknown")
    if str(payload.get("id") or "") != post_id:
        return None, "post_id_mismatch"
    return payload, ""


def collect(now=None, session=requests):
    """Maximum 2 read requests per 20 minutes; independent of publishing."""
    enabled = str(os.getenv("JOBS_FACEBOOK_METRICS_ENABLED", "false")).strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return {"status": "disabled", "queried": 0}
    if not FACEBOOK_PAGE_ACCESS_TOKEN:
        return {"status": "missing_page_token", "queried": 0}

    now = _time(now) or datetime.now(timezone.utc)
    state = _state()
    if _time(state.get("blocked_until")) and now < _time(state["blocked_until"]):
        return {"status": "read_permission_cooldown", "queried": 0}
    previous = _time(state.get("last_poll_at"))
    if previous and (now - previous).total_seconds() < POLL_SECONDS:
        return {"status": "rate_guard", "queried": 0}
    queue = _load_json(QUEUE_PATH)
    candidates = list(_candidate_samples(queue, state, now))[:MAX_POSTS_PER_RUN]
    state["last_poll_at"] = now.isoformat()
    queried = saved = 0
    for _, _, post_id, stage, article, posted, age in candidates:
        queried += 1
        try:
            payload, error = _request_engagement(post_id, FACEBOOK_PAGE_ACCESS_TOKEN, session=session)
        except (requests.RequestException, ValueError):
            payload, error = None, "transport_error"
        if error:
            state["last_error_code"] = error
            if error in {"http_400", "http_401", "http_403", "http_429"} or error.startswith("graph_error_"):
                state["blocked_until"] = (now + timedelta(hours=6)).isoformat()
                break
            continue
        sample = _extract_counts(payload, bool(article.get("facebook_comment_id")))
        posted_local = posted.astimezone(MOROCCO)
        entry = state["posts"].setdefault(post_id, {
            "facebook_post_id": post_id,
            "posted_at": posted.isoformat(),
            "morocco_weekday": posted_local.weekday(),
            "morocco_hour": posted_local.hour,
            "notice_type": str(article.get("job_notice_type") or ""),
            "snapshots": {},
        })
        entry.setdefault("snapshots", {})[stage] = {
            "collected_at": now.isoformat(),
            "age_hours": round(age, 2),
            "counts": sample,
            "first_comment_confirmed": bool(article.get("facebook_comment_id")),
        }
        state["last_error_code"] = ""
        saved += 1

    # Keep bounded public-repo analytics; source posts stay untouched.
    posts = state["posts"]
    if len(posts) > 180:
        old = sorted(posts, key=lambda key: str(posts[key].get("posted_at") or ""))
        for key in old[:-180]:
            posts.pop(key, None)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(STATE_PATH, state, sort_keys=True)
    return {"status": "ok" if not state["last_error_code"] else "degraded",
            "queried": queried, "saved_samples": saved, "error_code": state["last_error_code"]}


def performance_snapshot(data=None):
    """No 'best slot' is asserted until several comparable-age samples exist."""
    state = data if isinstance(data, dict) else _state()
    groups = {}
    samples = 0
    for row in (state.get("posts") or {}).values():
        snapshot = (row.get("snapshots") or {}).get("day") or {}
        age = snapshot.get("age_hours")
        counts = snapshot.get("counts") or {}
        interactions = counts.get("known_interactions")
        if not isinstance(age, (int, float)) or not 24 <= age <= 36:
            continue
        if not isinstance(interactions, (int, float)):
            continue
        weekday, hour = row.get("morocco_weekday"), row.get("morocco_hour")
        if not isinstance(weekday, int) or not isinstance(hour, int):
            continue
        bucket = ("weekday" if weekday < 5 else "weekend", "morning" if hour < 12 else "afternoon" if hour < 17 else "evening")
        groups.setdefault(bucket, []).append(int(interactions))
        samples += 1
    cells = [{
        "day_type": day_type,
        "period": period,
        "sample_count": len(values),
        "average_known_interactions": round(sum(values) / len(values), 2),
        "enough_to_compare": len(values) >= 4,
    } for (day_type, period), values in sorted(groups.items())]
    return {
        "comparable_24h_samples": samples,
        "cells": cells,
        "can_recommend_slots": sum(c["enough_to_compare"] for c in cells) >= 2,
        "note": "These are engagement counters, not reach, link clicks or job applications.",
    }


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False))
    print(json.dumps(performance_snapshot(), ensure_ascii=False))
