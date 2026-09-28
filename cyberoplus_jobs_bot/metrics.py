from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import DATA_DIR


METRICS_PATH = DATA_DIR / "metrics.jsonl"


def record_event(event, **fields):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    with METRICS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def publication_dimensions(candidate, quality, urgency):
    return {
        "job_title": candidate.title,
        "company": candidate.company,
        "city": candidate.location,
        "country": candidate.country,
        "source": candidate.source_name,
        "source_priority": candidate.source_priority,
        "number_of_positions": candidate.number_of_positions,
        "deadline": candidate.deadline,
        "quality_score": quality.get("score"),
        "urgency": urgency.get("level"),
        "remote": candidate.remote,
        "visa_sponsorship": candidate.visa_sponsorship,
        "eligibility": candidate.eligibility,
    }
