# ============================================================
# article_backlog.py - Pending Article Backlog
# ============================================================

import json
from datetime import datetime

from config import ARTICLE_BACKLOG_PATH
from state_io import atomic_write_json


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def load_article_backlog():
    """
    Load fetched articles that are waiting for publication.
    """
    if not ARTICLE_BACKLOG_PATH.exists():
        return {}

    try:
        with open(ARTICLE_BACKLOG_PATH, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, IOError) as error:
        print(f"⚠️  Warning: Could not read article backlog: {error}")
        return {}

    articles = data.get("articles", {})
    if not isinstance(articles, dict):
        return {}
    return articles


def save_article_backlog(backlog):
    data = {
        "updated_at": _now_iso(),
        "articles": backlog,
    }
    atomic_write_json(ARTICLE_BACKLOG_PATH, data)


def remember_articles(articles, published_set):
    """
    Store every newly fetched usable article so later runs do not forget it.
    """
    backlog = load_article_backlog()
    added = 0
    updated = 0

    for article in articles:
        url = article.get("url")
        if not url or url in published_set:
            continue

        existing = backlog.get(url, {})
        status = existing.get("status", "pending")
        if status == "published":
            continue

        record = {
            "status": "pending",
            "title": article.get("title", ""),
            "url": url,
            "source_url": article.get("source_url", ""),
            "body": article.get("body", ""),
            "image": article.get("image"),
            "trusted_sources": article.get("trusted_sources", []),
            "first_seen_at": existing.get("first_seen_at") or _now_iso(),
            "last_seen_at": _now_iso(),
        }

        if url in backlog:
            updated += 1
        else:
            added += 1
        backlog[url] = record

    save_article_backlog(backlog)
    return added, updated


def get_pending_articles(published_set):
    """
    Return all pending backlog articles that have not been published yet.
    """
    backlog = load_article_backlog()
    pending = []

    for url, record in backlog.items():
        if url in published_set:
            continue
        if record.get("status", "pending") != "pending":
            continue
        if not record.get("body"):
            continue

        pending.append(
            {
                "title": record.get("title", ""),
                "url": url,
                "source_url": record.get("source_url", ""),
                "body": record.get("body", ""),
                "image": record.get("image"),
                "trusted_sources": record.get("trusted_sources", []),
                "first_seen_at": record.get("first_seen_at", ""),
                "last_seen_at": record.get("last_seen_at", ""),
            }
        )

    return pending


def mark_backlog_published(urls):
    backlog = load_article_backlog()
    changed = 0

    for url in urls:
        if url not in backlog:
            continue
        backlog[url]["status"] = "published"
        backlog[url]["published_at"] = _now_iso()
        changed += 1

    if changed:
        save_article_backlog(backlog)
    return changed
