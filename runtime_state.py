import json
from datetime import datetime, timedelta, timezone

from config import (
    CRAWL_STATE_PATH,
    SOURCE_FAILURE_COOLDOWN_MINUTES,
    SOURCE_FAILURE_THRESHOLD,
    SOURCE_HEALTH_ENABLED,
    SOURCE_HEALTH_PATH,
    TOPIC_FINGERPRINTS_PATH,
)

TOPIC_COOLDOWN_HOURS = 24
CATEGORY_ROTATION_ORDER = ["Cyber-Security", "AI-Tools", "Tech-News", "Apps-Programs"]


def _read_json(path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


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


def source_crawl_record(source_key):
    state = load_crawl_state()
    return state.get("sources", {}).get(source_key, {})


def update_source_crawl(source_key, **fields):
    state = load_crawl_state()
    sources = state.setdefault("sources", {})
    record = sources.setdefault(source_key, {})
    record.update({key: value for key, value in fields.items() if value not in (None, "")})
    save_crawl_state(state)


def category_rotation_record():
    state = load_crawl_state()
    record = state.get("category_rotation", {})
    return record if isinstance(record, dict) else {}


def _rotation_order(available_categories=None):
    available = [str(category) for category in (available_categories or []) if category]
    if not available:
        return list(CATEGORY_ROTATION_ORDER)
    ordered = [category for category in CATEGORY_ROTATION_ORDER if category in available]
    ordered.extend(category for category in available if category not in ordered)
    return ordered


def select_category_for_rotation(available_categories=None):
    order = _rotation_order(available_categories)
    if not order:
        return {"category": "", "index": 0, "order": []}
    record = category_rotation_record()
    try:
        index = int(record.get("next_index") or 0)
    except (TypeError, ValueError):
        index = 0
    index = index % len(order)
    return {"category": order[index], "index": index, "order": order}


def advance_category_rotation(selected_category, available_categories=None):
    order = _rotation_order(available_categories)
    if not order:
        return {}
    try:
        index = order.index(selected_category)
    except ValueError:
        index = -1
    next_index = (index + 1) % len(order)
    state = load_crawl_state()
    state["category_rotation"] = {
        "order": order,
        "last_category": selected_category,
        "next_category": order[next_index],
        "next_index": next_index,
        "updated_at": _utc_iso(),
    }
    save_crawl_state(state)
    return dict(state["category_rotation"])


def source_rotation_record(category_label):
    state = load_crawl_state()
    rotations = state.get("source_rotation", {})
    if not isinstance(rotations, dict):
        return {}
    record = rotations.get(str(category_label or ""), {})
    return record if isinstance(record, dict) else {}


def _source_key(source):
    if isinstance(source, dict):
        return str(source.get("base_url") or source.get("url") or source.get("name") or "").strip()
    return str(source or "").strip()


def order_sources_for_rotation(category_label, sources):
    sources = [source for source in (sources or []) if source]
    if len(sources) < 2:
        return sources
    keys = [_source_key(source) for source in sources]
    record = source_rotation_record(category_label)
    start_key = str(record.get("next_source_key") or "").strip()
    if not start_key:
        last_key = str(record.get("last_source_key") or "").strip()
        if last_key in keys:
            start_key = keys[(keys.index(last_key) + 1) % len(keys)]
    if start_key in keys:
        index = keys.index(start_key)
        return sources[index:] + sources[:index]
    return sources


def advance_source_rotation(category_label, selected_source_key, selected_source_name="", available_source_keys=None):
    selected_source_key = str(selected_source_key or "").strip()
    category_label = str(category_label or "").strip()
    if not category_label or not selected_source_key:
        return {}
    keys = [str(key or "").strip() for key in (available_source_keys or []) if str(key or "").strip()]
    if selected_source_key in keys and keys:
        next_key = keys[(keys.index(selected_source_key) + 1) % len(keys)]
    elif keys:
        next_key = keys[0]
    else:
        next_key = ""
    state = load_crawl_state()
    rotations = state.setdefault("source_rotation", {})
    rotations[category_label] = {
        "last_source_key": selected_source_key,
        "last_source_name": selected_source_name,
        "next_source_key": next_key,
        "updated_at": _utc_iso(),
    }
    save_crawl_state(state)
    return dict(rotations[category_label])


def load_topic_fingerprints():
    data = _read_json(TOPIC_FINGERPRINTS_PATH, {})
    raw = data.get("fingerprints", [])
    now = _utc_now()
    if isinstance(raw, dict):
        fingerprints = set()
        for fingerprint, stored_at in raw.items():
            parsed = _parse_utc(stored_at)
            if not parsed or (now - parsed).total_seconds() <= TOPIC_COOLDOWN_HOURS * 3600:
                fingerprints.add(str(fingerprint))
        return fingerprints
    if not isinstance(raw, list):
        raw = []
    return {str(item) for item in raw if item}


def save_topic_fingerprints(fingerprints):
    existing = _read_json(TOPIC_FINGERPRINTS_PATH, {}).get("fingerprints", {})
    if not isinstance(existing, dict):
        existing = {}
    now = _utc_iso()
    active = {str(item) for item in fingerprints if item}
    data = {
        "fingerprints": {
            fingerprint: existing.get(fingerprint) or now
            for fingerprint in sorted(active)
        },
        "cooldown_hours": TOPIC_COOLDOWN_HOURS,
        "updated_at": now,
    }
    _write_json(TOPIC_FINGERPRINTS_PATH, data)


def add_topic_fingerprint(fingerprint):
    if not fingerprint:
        return False
    fingerprints = load_topic_fingerprints()
    before = len(fingerprints)
    fingerprints.add(str(fingerprint))
    save_topic_fingerprints(fingerprints)
    return len(fingerprints) != before


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
