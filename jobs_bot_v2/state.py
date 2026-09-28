from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from .config import DATA_DIR, DAILY_PUBLISH_LIMIT, STATE_PATH


def _default_state():
    return {
        "published": {},
        "candidates": {},
        "daily_publish_count": {},
        "last_publish_at": "",
    }


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
    now = now or datetime.now(timezone.utc)
    key = now.date().isoformat()
    return int(state.get("daily_publish_count", {}).get(key, 0)) < DAILY_PUBLISH_LIMIT


def mark_published(candidate, article_url="", state=None, now=None):
    state = state or load_state()
    now = now or datetime.now(timezone.utc)
    key = now.date().isoformat()
    fp = fingerprint(candidate)
    state.setdefault("published", {})[fp] = {
        "title": candidate.title,
        "company": candidate.company,
        "source_url": candidate.source_url,
        "canonical_url": candidate.canonical_url,
        "article_url": article_url,
        "published_at": now.isoformat(),
    }
    counts = state.setdefault("daily_publish_count", {})
    counts[key] = int(counts.get(key, 0)) + 1
    state["last_publish_at"] = now.isoformat()
    save_state(state)
    return state
