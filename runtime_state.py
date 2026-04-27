import json
from datetime import datetime

from config import CRAWL_STATE_PATH, TOPIC_FINGERPRINTS_PATH


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
    return {
        "sources": sources,
        "updated_at": str(data.get("updated_at", "")),
    }


def save_crawl_state(state):
    data = {
        "sources": state.get("sources", {}),
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


def load_topic_fingerprints():
    data = _read_json(TOPIC_FINGERPRINTS_PATH, {})
    fingerprints = data.get("fingerprints", [])
    if not isinstance(fingerprints, list):
        fingerprints = []
    return {str(item) for item in fingerprints if item}


def save_topic_fingerprints(fingerprints):
    data = {
        "fingerprints": sorted({str(item) for item in fingerprints if item}),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
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
