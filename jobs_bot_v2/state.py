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
    return fingerprint(candidate) in state.get("published", {})


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
    fp = fingerprint(candidate)

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
    """Classify a candidate as new, duplicate, update, or new campaign."""
    state = state or load_state()
    records = state.setdefault("job_records", {})
    exact_key = identity_key(candidate)

    if exact_key in records:
        decision = compare_to_existing(candidate, records[exact_key])
        if decision.action == "update" and not decision.material_update:
            decision.action = "duplicate"
            decision.reason = "same posting with no material fact change"
        return decision, records[exact_key]

    sem = semantic_key(candidate)
    for existing_key in state.setdefault("semantic_index", {}).get(sem, []):
        record = records.get(existing_key)
        if not record:
            continue
        decision = compare_to_existing(candidate, record)
        if decision.action == "update" and not decision.material_update:
            decision.action = "duplicate"
            decision.reason = "semantic duplicate with no material fact change"
        return decision, record

    return compare_to_existing(candidate, None), None


def save_candidate_record(
    candidate,
    *,
    blogger_post_id="",
    blogger_url="",
    desired_slug="",
    state=None,
    now=None,
):
    state = state or load_state()
    now = now or datetime.now(timezone.utc)

    record = snapshot(candidate)
    record.update({
        "blogger_post_id": blogger_post_id,
        "blogger_url": blogger_url,
        "desired_slug": desired_slug,
        "last_seen_at": now.isoformat(),
    })

    key = record["identity_key"]
    records = state.setdefault("job_records", {})
    existing = records.get(key, {})
    if existing:
        for stable in ("blogger_post_id", "blogger_url", "desired_slug", "first_seen_at"):
            if not record.get(stable) and existing.get(stable):
                record[stable] = existing[stable]
    record.setdefault("first_seen_at", existing.get("first_seen_at") or now.isoformat())

    records[key] = record
    sem = record["semantic_key"]
    index = state.setdefault("semantic_index", {}).setdefault(sem, [])
    if key not in index:
        index.append(key)

    save_state(state)
    return record

