"""Semantic Facebook template selection for Jobs posts.

This module intentionally knows nothing about employer-logo retrieval. It only
maps verified job facts to one of the owner-supplied visual templates and keeps
rotation state small and durable.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from job_core import job_deadline_time

JOB_TEMPLATE_FILES_BY_KEY = {
    "new": "job-new-orange.png",
    "deadline": "job-deadline-yellow.png",
    "alert": "job-alert-blue.png",
    "apply": "job-apply-red.png",
}
JOB_TEMPLATE_KEYS = tuple(JOB_TEMPLATE_FILES_BY_KEY)
DEFAULT_JOB_TEMPLATE_KEY = "new"
RECENT_TEMPLATE_MEMORY = 2
DEADLINE_TEMPLATE_WINDOW_HOURS = 72


def _load_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _has_application_path(article):
    """Only mark Apply when the source explicitly exposes a direct apply action."""
    link_kind = str(article.get("job_application_link_kind") or "").strip().lower()
    url = str(article.get("job_application_url") or "").strip()
    if link_kind in {"direct_apply", "apply"} and url.startswith(("http://", "https://")):
        return True
    for row in article.get("job_action_links") or []:
        if not isinstance(row, dict):
            continue
        value = str(row.get("url") or "").strip()
        kind = str(row.get("kind") or "").strip().lower()
        if kind == "apply" and value.startswith(("http://", "https://")):
            return True
    return False


def semantic_template_candidates(article, now=None):
    """Return truthful template candidates, strongest semantic signal first."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    notice_type = str(article.get("job_notice_type") or "vacancy").strip().lower()
    if notice_type != "vacancy":
        return ("alert",), f"employment-notice:{notice_type}"

    deadline = job_deadline_time(article)
    if deadline:
        hours_left = (deadline - now).total_seconds() / 3600
        if 0 <= hours_left <= DEADLINE_TEMPLATE_WINDOW_HOURS:
            return ("deadline",), "verified-deadline-within-72h"

    if _has_application_path(article):
        # Both are truthful because a direct apply action is verified. Rotation
        # decides which one is used so the feed does not become repetitive.
        return ("new", "apply"), "active-vacancy-with-direct-apply"

    return ("new",), "active-vacancy"


def _stable_tiebreak(article, key):
    seed = "|".join(
        [
            str(article.get("job_campaign_id") or ""),
            str(article.get("id") or ""),
            str(article.get("url") or ""),
            str(article.get("job_company") or ""),
            str(article.get("job_title") or article.get("title") or ""),
            key,
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def choose_job_template(article, state_path, now=None):
    """Choose once per article, then pin that key for every retry."""
    pinned = str(article.get("facebook_template_key") or "").strip().lower()
    if pinned in JOB_TEMPLATE_FILES_BY_KEY:
        return {
            "key": pinned,
            "file": JOB_TEMPLATE_FILES_BY_KEY[pinned],
            "reason": str(article.get("facebook_template_reason") or "pinned"),
            "pinned": True,
        }

    candidates, reason = semantic_template_candidates(article, now=now)
    state = _load_state(state_path)
    recent = [
        key for key in state.get("recent_template_keys", [])
        if key in JOB_TEMPLATE_FILES_BY_KEY
    ][-RECENT_TEMPLATE_MEMORY:]
    usage = state.get("usage_counts") if isinstance(state.get("usage_counts"), dict) else {}
    usage = {
        key: max(0, int(usage.get(key, 0) or 0))
        for key in JOB_TEMPLATE_FILES_BY_KEY
    }

    fresh = [key for key in candidates if key not in recent]
    pool = fresh or list(candidates)
    selected = min(
        pool,
        key=lambda key: (usage.get(key, 0), _stable_tiebreak(article, key)),
    )

    usage[selected] = usage.get(selected, 0) + 1
    recent.append(selected)
    recent = recent[-6:]
    file_name = JOB_TEMPLATE_FILES_BY_KEY[selected]
    state.update(
        {
            "version": 2,
            "last_template_key": selected,
            "last_background_file": file_name,
            "last_background_index": JOB_TEMPLATE_KEYS.index(selected),
            "recent_template_keys": recent,
            "usage_counts": usage,
        }
    )
    _save_state(state_path, state)

    article["facebook_template_key"] = selected
    article["facebook_template_file"] = file_name
    article["facebook_template_reason"] = reason
    return {
        "key": selected,
        "file": file_name,
        "reason": reason,
        "pinned": False,
    }


def template_filename(template_key):
    return JOB_TEMPLATE_FILES_BY_KEY.get(
        str(template_key or "").strip().lower(),
        JOB_TEMPLATE_FILES_BY_KEY[DEFAULT_JOB_TEMPLATE_KEY],
    )
