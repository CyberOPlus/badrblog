from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import MEMORY_DIR
from .job_identity import identity_key, semantic_key, snapshot


def _shard(key):
    value = str(key or "00")
    return (value[:2] if len(value) >= 2 else value.ljust(2, "0")).lower()


def _path(kind, key):
    return MEMORY_DIR / kind / f"{_shard(key)}.json"


def _load(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def new_campaign_id():
    # Random, persistent campaign identity. It contains no date, vacancy count,
    # deadline, salary, or other mutable job fact.
    return uuid.uuid4().hex[:12]


def _get_campaign(campaign_id):
    if not campaign_id:
        return None
    path = _path("campaigns", campaign_id)
    data = _load(path)
    row = data.get(campaign_id)
    return dict(row) if isinstance(row, dict) else None


def get_by_identity(job_identity):
    if not job_identity:
        return None
    path = _path("identity", job_identity)
    index = _load(path)
    campaign_id = index.get(job_identity)
    return _get_campaign(campaign_id)


def get_semantic_candidates(job_semantic):
    if not job_semantic:
        return []
    path = _path("semantic", job_semantic)
    index = _load(path)
    ids = index.get(job_semantic) or []
    rows = []
    for campaign_id in reversed(ids):
        row = _get_campaign(campaign_id)
        if row:
            rows.append(row)
    return rows


def assign_campaign_id(candidate, existing=None):
    if existing and existing.get("campaign_id"):
        return existing["campaign_id"]

    raw = candidate.raw if isinstance(candidate.raw, dict) else {}
    existing_id = str(raw.get("_campaign_id") or "").strip()
    return existing_id or new_campaign_id()


def upsert_campaign(
    candidate,
    *,
    campaign_id="",
    blogger_post_id="",
    blogger_url="",
    desired_slug="",
    status="active",
    now=None,
):
    now = now or datetime.now(timezone.utc)
    campaign_id = campaign_id or assign_campaign_id(candidate)

    current = _get_campaign(campaign_id) or {}
    row = snapshot(candidate)
    row.update({
        "campaign_id": campaign_id,
        "status": status or current.get("status") or "active",
        "blogger_post_id": blogger_post_id or current.get("blogger_post_id", ""),
        "blogger_url": blogger_url or current.get("blogger_url", ""),
        "desired_slug": desired_slug or current.get("desired_slug", ""),
        "first_seen_at": current.get("first_seen_at") or now.isoformat(),
        "last_seen_at": now.isoformat(),
    })

    # Preserve stable publication identity forever.
    for stable in ("blogger_post_id", "blogger_url", "desired_slug"):
        if not row.get(stable) and current.get(stable):
            row[stable] = current[stable]

    campaign_path = _path("campaigns", campaign_id)
    campaign_data = _load(campaign_path)
    campaign_data[campaign_id] = row
    _save(campaign_path, campaign_data)

    # Every observed identity becomes an alias to the same campaign. This is
    # what lets an application URL change without losing the old relationship.
    ident = identity_key(candidate)
    identity_path = _path("identity", ident)
    identity_data = _load(identity_path)
    identity_data[ident] = campaign_id
    _save(identity_path, identity_data)

    sem = semantic_key(candidate)
    semantic_path = _path("semantic", sem)
    semantic_data = _load(semantic_path)
    ids = list(semantic_data.get(sem) or [])
    if campaign_id not in ids:
        ids.append(campaign_id)
    semantic_data[sem] = ids
    _save(semantic_path, semantic_data)

    return row


def mark_expired(campaign_id, now=None):
    row = _get_campaign(campaign_id)
    if not row:
        return None
    now = now or datetime.now(timezone.utc)
    row["status"] = "expired"
    row["expired_at"] = now.isoformat()
    row["last_seen_at"] = now.isoformat()

    path = _path("campaigns", campaign_id)
    data = _load(path)
    data[campaign_id] = row
    _save(path, data)
    return row


def stats():
    result = {"campaigns": 0, "identity_aliases": 0, "semantic_keys": 0}
    for kind, key in (("campaigns","campaigns"),("identity","identity_aliases"),("semantic","semantic_keys")):
        root = MEMORY_DIR / kind
        if not root.exists():
            continue
        for path in root.glob("*.json"):
            result[key] += len(_load(path))
    return result
