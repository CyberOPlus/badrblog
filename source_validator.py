# ============================================================
# source_validator.py - Sources configuration checks
# ============================================================

from collections import Counter, defaultdict

from article_queue import load_sources


ALLOWED_CATEGORY_HINTS = [
    "أدوات الذكاء الاصطناعي",
    "الأمن السيبراني",
    "أخبار التقنية",
    "برامج وتطبيقات",
]

REQUIRED_SOURCE_FIELDS = {
    "name",
    "base_url",
    "enabled",
    "category_hint",
    "fetch_limit_per_run",
}


def _normalize_base_url(url):
    return (url or "").strip().rstrip("/").lower()


def check_sources_config():
    """
    Validate sources.json without fetching articles.
    """
    sources = load_sources()
    category_counts = Counter()
    duplicate_groups = defaultdict(list)
    missing_required = []
    invalid_sources = []
    enabled_count = 0
    disabled_count = 0

    for index, source in enumerate(sources, start=1):
        name = source.get("name") or f"source #{index}"
        base_url = source.get("base_url", "")
        category = source.get("category_hint", "")
        enabled = source.get("enabled")
        fetch_limit = source.get("fetch_limit_per_run")

        missing_fields = sorted(
            field for field in REQUIRED_SOURCE_FIELDS if field not in source
        )
        if missing_fields:
            missing_required.append(
                {
                    "index": index,
                    "name": name,
                    "missing_fields": missing_fields,
                }
            )

        if enabled is True:
            enabled_count += 1
        elif enabled is False:
            disabled_count += 1
        else:
            invalid_sources.append(
                {
                    "index": index,
                    "name": name,
                    "reason": "enabled must be true or false",
                }
            )

        if category in ALLOWED_CATEGORY_HINTS:
            category_counts[category] += 1
        else:
            invalid_sources.append(
                {
                    "index": index,
                    "name": name,
                    "reason": "invalid category_hint",
                }
            )

        if not isinstance(fetch_limit, int) or fetch_limit < 1:
            invalid_sources.append(
                {
                    "index": index,
                    "name": name,
                    "reason": "fetch_limit_per_run must be a positive integer",
                }
            )

        normalized_url = _normalize_base_url(base_url)
        if normalized_url:
            duplicate_groups[normalized_url].append(name)
        else:
            invalid_sources.append(
                {
                    "index": index,
                    "name": name,
                    "reason": "base_url is empty",
                }
            )

    duplicates = {
        url: names for url, names in duplicate_groups.items() if len(names) > 1
    }

    return {
        "total_sources": len(sources),
        "enabled_count": enabled_count,
        "disabled_count": disabled_count,
        "category_counts": {
            category: category_counts.get(category, 0)
            for category in ALLOWED_CATEGORY_HINTS
        },
        "duplicate_count": len(duplicates),
        "duplicates": duplicates,
        "missing_required_count": len(missing_required),
        "missing_required": missing_required,
        "invalid_count": len(invalid_sources),
        "invalid_sources": invalid_sources,
    }
