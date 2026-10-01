import json
from datetime import datetime, timedelta, timezone
from state_io import atomic_write_json

from config import (
    CRAWL_STATE_PATH,
    SOURCE_FAILURE_COOLDOWN_MINUTES,
    SOURCE_FAILURE_THRESHOLD,
    SOURCE_HEALTH_ENABLED,
    SOURCE_HEALTH_PATH,
)


def _read_json(path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path, data):
    atomic_write_json(path, data)


def load_crawl_state():
    data = _read_json(CRAWL_STATE_PATH, {})
    if not isinstance(data, dict):
        return {"sources": {}, "updated_at": ""}
    sources = data.get("sources", {})
    if not isinstance(sources, dict):
        sources = {}
    category_rotation = data.get("category_rotation", {})
    if not isinstance(category_rotation, dict):
        category_rotation = {}
    source_rotation = data.get("source_rotation", {})
    if not isinstance(source_rotation, dict):
        source_rotation = {}
    return {
        "sources": sources,
        "category_rotation": category_rotation,
        "source_rotation": source_rotation,
        "updated_at": str(data.get("updated_at", "")),
    }


def save_crawl_state(state):
    data = {
        "sources": state.get("sources", {}),
        "category_rotation": state.get("category_rotation", {}),
        "source_rotation": state.get("source_rotation", {}),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(CRAWL_STATE_PATH, data)


def reset_job_discovery_state(reason="queue_state_mismatch"):
    """Forget discovery cursors/seen IDs after durable Jobs queue loss."""
    state = load_crawl_state()
    sources = state.setdefault("sources", {})
    changed_sources = 0
    forgotten_ids = 0

    for record in sources.values():
        if not isinstance(record, dict):
            continue
        seen_ids = record.get("job_seen_ids") or []
        resume = record.get("job_discovery_resume") or {}
        has_discovery_state = bool(
            seen_ids
            or resume
            or int(record.get("job_seen_ids_count") or 0)
            or int(record.get("discovery_pages_scanned") or 0)
        )
        if not has_discovery_state:
            continue

        forgotten_ids += len(seen_ids) if isinstance(seen_ids, list) else 0
        record["job_seen_ids"] = []
        record["job_seen_ids_count"] = 0
        record["discovery_last_new_count"] = 0
        record["job_discovery_resume"] = {}
        record["discovery_stop_reason"] = str(reason or "queue_state_mismatch")
        record["discovery_pages_scanned"] = 0
        changed_sources += 1

    if changed_sources:
        save_crawl_state(state)

    return {
        "changed_sources": changed_sources,
        "forgotten_ids": forgotten_ids,
        "reason": str(reason or "queue_state_mismatch"),
    }

def source_crawl_record(source_key):
    state = load_crawl_state()
    return state.get("sources", {}).get(source_key, {})


def update_source_crawl(source_key, **fields):
    state = load_crawl_state()
    sources = state.setdefault("sources", {})
    record = sources.setdefault(source_key, {})
    record.update({key: value for key, value in fields.items() if value not in (None, "")})
    save_crawl_state(state)


def _utc_now():
    return datetime.now(timezone.utc)


def _parse_utc(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value=None):
    return (value or _utc_now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_source_health():
    data = _read_json(SOURCE_HEALTH_PATH, {})
    if not isinstance(data, dict):
        return {"sources": {}, "updated_at": ""}
    sources = data.get("sources", {})
    if not isinstance(sources, dict):
        sources = {}
    return {"sources": sources, "updated_at": str(data.get("updated_at", ""))}


def save_source_health(state):
    data = {
        "sources": state.get("sources", {}),
        "updated_at": _utc_iso(),
    }
    _write_json(SOURCE_HEALTH_PATH, data)


def source_health_record(source_key):
    state = load_source_health()
    return state.get("sources", {}).get(source_key, {})


def is_source_cooled_down(source_key, now=None):
    if not SOURCE_HEALTH_ENABLED:
        return False, ""
    record = source_health_record(source_key)
    cooldown_until = _parse_utc(record.get("cooldown_until"))
    now = now or _utc_now()
    if cooldown_until and cooldown_until > now:
        return True, cooldown_until.isoformat(timespec="seconds").replace("+00:00", "Z")
    return False, ""


def record_source_success(source_key, source_name=""):
    if not SOURCE_HEALTH_ENABLED or not source_key:
        return
    state = load_source_health()
    record = state.setdefault("sources", {}).setdefault(source_key, {})
    record.update(
        {
            "source_name": source_name or record.get("source_name", ""),
            "failure_count": 0,
            "last_success_at": _utc_iso(),
            "cooldown_until": "",
            "last_error": "",
        }
    )
    save_source_health(state)


def record_source_failure(source_key, source_name="", error=""):
    if not SOURCE_HEALTH_ENABLED or not source_key:
        return {}
    state = load_source_health()
    record = state.setdefault("sources", {}).setdefault(source_key, {})
    failure_count = int(record.get("failure_count") or 0) + 1
    record.update(
        {
            "source_name": source_name or record.get("source_name", ""),
            "failure_count": failure_count,
            "last_failure_at": _utc_iso(),
            "last_error": str(error or "")[:300],
        }
    )
    if failure_count >= SOURCE_FAILURE_THRESHOLD:
        cooldown_until = _utc_now() + timedelta(minutes=max(1, SOURCE_FAILURE_COOLDOWN_MINUTES))
        record["cooldown_until"] = _utc_iso(cooldown_until)
    save_source_health(state)
    return dict(record)


def record_source_cooldown(source_key, source_name="", error="", minutes=None):
    if not SOURCE_HEALTH_ENABLED or not source_key:
        return {}
    cooldown_minutes = SOURCE_FAILURE_COOLDOWN_MINUTES if minutes is None else minutes
    cooldown_until = _utc_now() + timedelta(minutes=max(1, int(cooldown_minutes or 1)))
    state = load_source_health()
    record = state.setdefault("sources", {}).setdefault(source_key, {})
    failure_count = int(record.get("failure_count") or 0) + 1
    record.update(
        {
            "source_name": source_name or record.get("source_name", ""),
            "failure_count": failure_count,
            "last_failure_at": _utc_iso(),
            "last_error": str(error or "")[:300],
            "cooldown_until": _utc_iso(cooldown_until),
        }
    )
    save_source_health(state)
    return dict(record)
