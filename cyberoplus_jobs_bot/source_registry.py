from __future__ import annotations

import json

from .config import JOBS_SOURCE_REGISTRY


def load_registry():
    data = json.loads(JOBS_SOURCE_REGISTRY.read_text(encoding="utf-8"))
    sources = []
    for source in data.get("sources", []):
        if source.get("enabled", True):
            sources.append(source)
    return data, sources
