# ============================================================
# article_queue.py - Safe Article Ingestion Queue
# ============================================================

import hashlib
import json
import re
from collections import Counter
from datetime import datetime

from config import ARTICLE_QUEUE_PATH, SOURCES_CONFIG_PATH

ALLOWED_STATUSES = {"new", "skipped", "ready", "selected", "published", "failed"}


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def normalize_title(title):
    """
    Normalize titles for duplicate detection across sources.
    """
    cleaned = re.sub(r"\s+", " ", (title or "").strip()).casefold()
    cleaned = re.sub(r"[^\w\u0600-\u06FF ]+", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def make_article_id(url):
    """
    Stable short ID based on URL, safe for future database migration.
    """
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]


def load_sources():
    """
    Load Phase 1 source configuration from sources.json.
    """
    if not SOURCES_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing source configuration: {SOURCES_CONFIG_PATH}")

    with open(SOURCES_CONFIG_PATH, "r", encoding="utf-8-sig") as handle:
        data = json.load(handle)

    sources = data.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("sources.json must contain a top-level 'sources' list.")

    return sources


def load_article_queue():
    if not ARTICLE_QUEUE_PATH.exists():
        return {"updated_at": "", "articles": []}

    try:
        with open(ARTICLE_QUEUE_PATH, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, IOError) as error:
        print(f"Warning: Could not read article queue: {error}")
        return {"updated_at": "", "articles": []}

    articles = data.get("articles", [])
    if not isinstance(articles, list):
        articles = []

    return {
        "updated_at": data.get("updated_at", ""),
        "articles": articles,
    }


def save_article_queue(queue):
    data = {
        "updated_at": _now_iso(),
        "articles": queue.get("articles", []),
    }
    with open(ARTICLE_QUEUE_PATH, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def add_articles_to_queue(discovered_articles):
    """
    Add newly discovered articles while preventing URL and title duplicates.
    """
    queue = load_article_queue()
    articles = queue["articles"]

    existing_urls = {item.get("url") for item in articles if item.get("url")}
    existing_titles = {
        normalize_title(item.get("title", ""))
        for item in articles
        if normalize_title(item.get("title", ""))
    }

    added = 0
    duplicate_url = 0
    duplicate_title = 0
    added_by_category = Counter()
    duplicate_by_category = Counter()

    for article in discovered_articles:
        url = (article.get("url") or "").strip()
        title = (article.get("title") or "").strip()
        normalized_title = normalize_title(title)
        category_hint = article.get("category_hint", "")

        if not url or not title:
            continue

        if url in existing_urls:
            duplicate_url += 1
            duplicate_by_category[category_hint] += 1
            continue

        if normalized_title and normalized_title in existing_titles:
            duplicate_title += 1
            duplicate_by_category[category_hint] += 1
            continue

        articles.append(
            {
                "id": make_article_id(url),
                "title": title,
                "url": url,
                "source_name": article.get("source_name", ""),
                "source_url": article.get("source_url", ""),
                "category_hint": category_hint,
                "discovered_at": _now_iso(),
                "status": "new",
            }
        )
        existing_urls.add(url)
        existing_titles.add(normalized_title)
        added += 1
        added_by_category[category_hint] += 1

    save_article_queue(queue)
    return {
        "added": added,
        "duplicate_url": duplicate_url,
        "duplicate_title": duplicate_title,
        "duplicates": duplicate_url + duplicate_title,
        "added_by_category": dict(added_by_category),
        "duplicate_by_category": dict(duplicate_by_category),
        "total_queued": len(articles),
    }
