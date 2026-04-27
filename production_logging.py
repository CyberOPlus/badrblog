# ============================================================
# production_logging.py - Small structured logging helpers
# ============================================================

import json
import re
import time
from datetime import datetime

from bs4 import BeautifulSoup


SECRET_FIELD_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization|bot[_-]?token)"
)


def _redact_mapping(value):
    safe = {}
    for key, item in value.items():
        if SECRET_FIELD_RE.search(str(key)):
            safe[key] = "[redacted]"
        elif isinstance(item, dict):
            safe[key] = _redact_mapping(item)
        else:
            safe[key] = item
    return safe


def _clean_value(value, limit=500):
    if isinstance(value, dict):
        value = json.dumps(_redact_mapping(value), ensure_ascii=False)
    text = " ".join(str(value or "").split())
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(r"access_token=[^&\s]+", "access_token=[redacted]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization|bot[_-]?token)(['\"]?\s*[:=]\s*['\"]?)[^,'\"\s}]+",
        r"\1\2[redacted]",
        text,
    )
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def log_event(event, **fields):
    timestamp = datetime.now().isoformat(timespec="seconds")
    details = " | ".join(
        f"{key}={_clean_value(value)}"
        for key, value in fields.items()
        if value not in (None, "")
    )
    if details:
        print(f"[{timestamp}] {event} | {details}")
    else:
        print(f"[{timestamp}] {event}")


def elapsed_ms(started_at):
    return int((time.perf_counter() - started_at) * 1000)


def html_to_text(html_content):
    soup = BeautifulSoup(html_content or "", "html.parser")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def text_word_count(text):
    return len(re.findall(r"[\w\u0600-\u06FF]+", text or "", flags=re.UNICODE))


def html_word_count(html_content):
    return text_word_count(html_to_text(html_content))
