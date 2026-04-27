# ============================================================
# article_queue.py - Safe Article Ingestion Queue
# ============================================================

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from config import (
    ARTICLE_QUEUE_PATH,
    MAX_AI_ARTICLE_AGE_HOURS,
    RECENT_NEWS_MAX_AGE_HOURS,
    SOURCES_CONFIG_PATH,
)
from duplicate_utils import canonicalize_url, title_hash, topic_signature

ALLOWED_STATUSES = {"new", "skipped", "ready", "selected", "draft_created", "published", "failed"}


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _article_age_anchor(article):
    for field in (
        "updated_at",
        "scored_at",
        "content_fetched_at",
        "discovered_at",
        "selected_at",
    ):
        parsed = _parse_iso(article.get(field))
        if parsed:
            return parsed
    return None


def _archive_article(article, reason, archived_at):
    if article.get("archived"):
        return False
    article["archived"] = True
    article["archived_at"] = archived_at
    article["archive_reason"] = reason
    return True


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
    return hashlib.sha1(canonicalize_url(url).encode("utf-8")).hexdigest()[:16]


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
        "notifications": data.get("notifications", {}) if isinstance(data.get("notifications", {}), dict) else {},
    }


def save_article_queue(queue):
    data = {
        "updated_at": _now_iso(),
        "articles": queue.get("articles", []),
        "notifications": queue.get("notifications", {}),
    }
    with open(ARTICLE_QUEUE_PATH, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def _fresh_queue_cutoff(now=None, max_age_hours=None):
    now = now or datetime.now(timezone.utc)
    max_age_hours = RECENT_NEWS_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    return now - timedelta(hours=max(0, max_age_hours))


def _source_published_datetime(article):
    published_at = _parse_iso(article.get("source_published_at"))
    if not published_at:
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return published_at.astimezone(timezone.utc)


def _fresh_queue_sort_key(article):
    published_at = _source_published_datetime(article)
    discovered_at = _parse_iso(article.get("discovered_at")) or datetime.max.replace(tzinfo=None)
    if discovered_at.tzinfo is not None:
        discovered_at = discovered_at.astimezone(timezone.utc).replace(tzinfo=None)
    return (
        published_at or datetime.max.replace(tzinfo=timezone.utc),
        discovered_at,
        article.get("url", ""),
    )


def is_article_within_fresh_window(article, now=None, max_age_hours=None):
    published_at = _source_published_datetime(article)
    if not published_at:
        return False
    return published_at >= _fresh_queue_cutoff(now=now, max_age_hours=max_age_hours)


def article_age_hours(article, now=None):
    published_at = _source_published_datetime(article)
    if not published_at:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - published_at).total_seconds() / 3600)


def is_article_safe_for_ai(article, now=None):
    published_at = _source_published_datetime(article)
    if not published_at:
        return False
    return published_at >= _fresh_queue_cutoff(now=now, max_age_hours=MAX_AI_ARTICLE_AGE_HOURS)


def archive_expired_queue_articles(now=None, max_age_hours=None):
    queue = load_article_queue()
    articles = queue.get("articles", [])
    archived_at = _now_iso()
    changed = False
    expired = 0
    missing_date = 0

    for article in articles:
        if article.get("archived"):
            continue
        if article.get("status") in {"published", "draft_created"}:
            continue

        published_at = _source_published_datetime(article)
        if not published_at:
            if article.get("status") in {"new", "ready", "selected", "failed", "skipped"}:
                if _archive_article(article, "missing_reliable_publish_date", archived_at):
                    missing_date += 1
                    changed = True
            continue

        if not is_article_within_fresh_window(article, now=now, max_age_hours=max_age_hours):
            if _archive_article(article, "expired_recent_window", archived_at):
                expired += 1
                changed = True

    if changed:
        save_article_queue(queue)

    return {
        "expired_archived": expired,
        "missing_date_archived": missing_date,
        "changed": changed,
        "total_queued": len(articles),
    }


def get_fresh_queue_candidates(statuses=None, now=None, max_age_hours=None):
    queue = load_article_queue()
    articles = queue.get("articles", [])
    statuses = set(statuses or {"ready", "selected"})
    candidates = [
        article
        for article in articles
        if not article.get("archived")
        and article.get("status") in statuses
        and article.get("content_fetch_status") == "success"
        and is_article_within_fresh_window(article, now=now, max_age_hours=max_age_hours)
        and is_article_safe_for_ai(article, now=now)
    ]
    return sorted(candidates, key=_fresh_queue_sort_key)


def archive_published_queue_article(article_id="", article_url="", reason="published_to_blogger"):
    if not article_id and not article_url:
        return False

    queue = load_article_queue()
    for article in queue.get("articles", []):
        if article.get("id") == article_id or article.get("url") == article_url:
            if article.get("archived"):
                return False
            changed = _archive_article(article, reason, _now_iso())
            if changed:
                save_article_queue(queue)
            return changed
    return False


def add_articles_to_queue(discovered_articles):
    """
    Add newly discovered articles while preventing URL and title duplicates.
    """
    queue = load_article_queue()
    articles = queue["articles"]

    existing_urls = {
        item.get("canonical_url") or canonicalize_url(item.get("url"))
        for item in articles
        if item.get("url") and not item.get("archived")
    }
    existing_titles = {
        item.get("title_hash") or title_hash(item.get("title", ""))
        for item in articles
        if normalize_title(item.get("title", "")) and not item.get("archived")
    }
    existing_topics = {
        item.get("topic_signature") or topic_signature(item.get("title", ""))
        for item in articles
        if normalize_title(item.get("title", "")) and not item.get("archived")
    }

    added = 0
    duplicate_url = 0
    duplicate_title = 0
    added_by_category = Counter()
    duplicate_by_category = Counter()

    for article in discovered_articles:
        url = (article.get("url") or "").strip()
        title = (article.get("title") or "").strip()
        canonical_url = canonicalize_url(url)
        normalized_title_hash = title_hash(title)
        normalized_topic_signature = topic_signature(title)
        category_hint = article.get("category_hint", "")

        if not url or not title:
            continue

        if canonical_url in existing_urls:
            duplicate_url += 1
            duplicate_by_category[category_hint] += 1
            continue

        if normalized_title_hash and (
            normalized_title_hash in existing_titles
            or normalized_topic_signature in existing_topics
        ):
            duplicate_title += 1
            duplicate_by_category[category_hint] += 1
            continue

        articles.append(
            {
                "id": make_article_id(url),
                "source_url_hash": make_article_id(url),
                "canonical_url": canonical_url,
                "title_hash": normalized_title_hash,
                "topic_signature": normalized_topic_signature,
                "title": title,
                "url": url,
                "source_name": article.get("source_name", ""),
                "source_url": article.get("source_url", ""),
                "source_published_at": article.get("source_published_at", ""),
                "published_at_source": article.get("published_at_source", ""),
                "article_age_hours": article.get("article_age_hours"),
                "rss_summary": article.get("rss_summary", ""),
                "category_hint": category_hint,
                "discovered_at": _now_iso(),
                "status": "new",
            }
        )
        existing_urls.add(canonical_url)
        existing_titles.add(normalized_title_hash)
        existing_topics.add(normalized_topic_signature)
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


def maintain_article_queue(days=7):
    """
    Non-destructive queue cleanup. Records are archived, not deleted.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])
    now = datetime.now()
    archived_at = _now_iso()
    cutoff = now - timedelta(days=days)
    seen_urls = {}

    stats = {
        "checked": len(articles),
        "archived_old_skipped": 0,
        "archived_old_failed": 0,
        "archived_duplicate_urls": 0,
        "already_archived": 0,
        "active_count": 0,
        "archived_count": 0,
        "total_queued": len(articles),
    }

    for article in articles:
        if article.get("archived"):
            stats["already_archived"] += 1
            continue

        url = str(article.get("url") or "").strip()
        canonical_url = article.get("canonical_url") or canonicalize_url(url)
        if canonical_url and not article.get("canonical_url"):
            article["canonical_url"] = canonical_url
        if article.get("title") and not article.get("title_hash"):
            article["title_hash"] = title_hash(article.get("title"))

        if canonical_url:
            if canonical_url in seen_urls:
                if _archive_article(article, "duplicate_url", archived_at):
                    stats["archived_duplicate_urls"] += 1
                continue
            seen_urls[canonical_url] = article

        status = article.get("status")
        if status not in {"skipped", "failed"}:
            continue

        anchor = _article_age_anchor(article)
        if anchor and anchor < cutoff:
            reason = f"{status}_older_than_{days}_days"
            if _archive_article(article, reason, archived_at):
                if status == "skipped":
                    stats["archived_old_skipped"] += 1
                else:
                    stats["archived_old_failed"] += 1

    stats["archived_count"] = sum(1 for article in articles if article.get("archived"))
    stats["active_count"] = len(articles) - stats["archived_count"]

    if (
        stats["archived_old_skipped"]
        or stats["archived_old_failed"]
        or stats["archived_duplicate_urls"]
    ):
        save_article_queue(queue)

    return stats
