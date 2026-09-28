from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import (
    DATA_DIR,
    DAILY_PUBLISH_LIMIT,
    MOROCCO_TIMEZONE,
    STATE_PATH,
    URGENT_EXTRA_DAILY_LIMIT,
)

from .job_identity import (
    compare_to_existing,
    identity_key,
    semantic_key,
    snapshot,
)

from .memory_store import (
    get_by_identity,
    get_semantic_candidates,
    upsert_campaign,
)


def _default_state():
    return {
        "published": {},
        "candidates": {},
        "job_records": {},
        "semantic_index": {},
        "daily_publish_count": {},
        "daily_urgent_override_count": {},
        "last_publish_at": "",
    }


def _local_day_key(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        local = now.astimezone(ZoneInfo(MOROCCO_TIMEZONE))
    except Exception:
        local = now.astimezone(timezone.utc)
    return local.date().isoformat()


def load_state():
    if not STATE_PATH.exists():
        return _default_state()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        base = _default_state()
        base.update(data if isinstance(data, dict) else {})
        return base
    except Exception:
        return _default_state()


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def normalize_text(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    return re.sub(r"[^\w\u0600-\u06ff]+", " ", text).strip()


def fingerprint(candidate):
    base = "|".join([
        normalize_text(candidate.company),
        normalize_text(candidate.title),
        normalize_text(candidate.location),
    ])
    if not base.strip("|"):
        base = normalize_text(candidate.canonical_url or candidate.source_url)
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def already_published(candidate, state=None):
    state = state or load_state()
    key = identity_key(candidate)
    return key in state.get("published", {})


def can_publish_today(state=None, now=None):
    state = state or load_state()
    key = _local_day_key(now)
    return int(state.get("daily_publish_count", {}).get(key, 0)) < DAILY_PUBLISH_LIMIT


def can_use_urgent_override(state=None, now=None):
    state = state or load_state()
    key = _local_day_key(now)
    return (
        int(state.get("daily_urgent_override_count", {}).get(key, 0))
        < URGENT_EXTRA_DAILY_LIMIT
    )


def mark_published(candidate, article_url="", state=None, now=None, urgent_override=False):
    state = state or load_state()
    now = now or datetime.now(timezone.utc)
    key = _local_day_key(now)
    fp = identity_key(candidate)

    state.setdefault("published", {})[fp] = {
        "title": candidate.title,
        "company": candidate.company,
        "source_url": candidate.source_url,
        "canonical_url": candidate.canonical_url,
        "article_url": article_url,
        "published_at": now.isoformat(),
        "urgent_override": bool(urgent_override),
    }

    counts = state.setdefault("daily_publish_count", {})
    counts[key] = int(counts.get(key, 0)) + 1

    if urgent_override:
        urgent = state.setdefault("daily_urgent_override_count", {})
        urgent[key] = int(urgent.get(key, 0)) + 1

    state["last_publish_at"] = now.isoformat()
    save_state(state)
    return state

def classify_candidate(candidate, state=None):
    """Classify against durable sharded memory, with legacy-state fallback."""
    state = state or load_state()
    exact_key = identity_key(candidate)

    record = get_by_identity(exact_key)
    if record:
        decision = compare_to_existing(candidate, record)
        if decision.action == "update" and not decision.material_update:
            decision.action = "duplicate"
            decision.reason = "same posting with no material fact change"
        return decision, record

    for record in get_semantic_candidates(semantic_key(candidate)):
        decision = compare_to_existing(candidate, record)
        if decision.action == "update" and not decision.material_update:
            decision.action = "duplicate"
            decision.reason = "semantic duplicate with no material fact change"
        return decision, record

    # Legacy compatibility for records written before sharded memory existed.
    records = state.setdefault("job_records", {})
    if exact_key in records:
        record = records[exact_key]
        decision = compare_to_existing(candidate, record)
        if decision.action == "update" and not decision.material_update:
            decision.action = "duplicate"
            decision.reason = "legacy duplicate with no material fact change"
        return decision, record

    sem = semantic_key(candidate)
    for existing_key in state.setdefault("semantic_index", {}).get(sem, []):
        record = records.get(existing_key)
        if record:
            return compare_to_existing(candidate, record), record

    return compare_to_existing(candidate, None), None


def save_candidate_record(
    candidate,
    *,
    blogger_post_id="",
    blogger_url="",
    desired_slug="",
    campaign_id="",
    status="active",
    state=None,
    now=None,
):
    """Persist the campaign in long-term sharded memory.

    Only compact daily counters remain in state.json; campaign history lives in
    memory shards so thousands of articles do not turn one JSON file into a
    bottleneck.
    """
    return upsert_campaign(
        candidate,
        campaign_id=campaign_id,
        blogger_post_id=blogger_post_id,
        blogger_url=blogger_url,
        desired_slug=desired_slug,
        status=status,
        now=now,
    )

