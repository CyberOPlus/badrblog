# ============================================================
# article_processor.py - Phase 5 AI Input Preparation
# ============================================================

from datetime import datetime

from article_queue import load_article_queue, save_article_queue


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _has_value(value):
    return bool(str(value or "").strip())


def _validate_selected_article(article):
    missing = []

    if not (_has_value(article.get("title")) or _has_value(article.get("fetched_title"))):
        missing.append("title or fetched_title")
    if not _has_value(article.get("url")):
        missing.append("url")
    if not _has_value(article.get("content_preview")):
        missing.append("content_preview")
    if not _has_value(article.get("suggested_category")):
        missing.append("suggested_category")

    return missing


def _build_ai_input_package(article):
    return {
        "title": article.get("fetched_title") or article.get("title", ""),
        "url": article.get("url", ""),
        "source_name": article.get("source_name", ""),
        "suggested_category": article.get("suggested_category", ""),
        "main_image": article.get("main_image", ""),
        "article_images": article.get("article_images", []),
        "meta_description": article.get("meta_description", ""),
        "content_preview": article.get("content_preview", ""),
        "source_url": article.get("source_url", ""),
        "trusted_references": article.get("trusted_references", []),
        "related_posts": _related_posts_for(article),
    }


def _related_posts_for(article):
    queue = load_article_queue()
    related = []
    current_url = article.get("url", "")
    category = article.get("suggested_category", "")

    for candidate in queue.get("articles", []):
        if candidate.get("url") == current_url:
            continue
        if candidate.get("status") not in {"draft_created", "published"}:
            continue
        if category and candidate.get("suggested_category") != category:
            continue

        link = candidate.get("blogger_draft_url") or candidate.get("blogger_url") or candidate.get("url")
        title = candidate.get("seo_title") or candidate.get("fetched_title") or candidate.get("title")
        if not link or not title:
            continue
        related.append({"title": title, "url": link})
        if len(related) >= 3:
            break

    return related


def prepare_selected_articles_for_ai(target_article_id=None):
    """
    Prepare selected articles for a future AI phase without calling any AI API.
    The article status remains selected.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])

    checked = 0
    ready_for_ai = 0
    failed = 0
    prepared_articles = []

    for article in articles:
        if target_article_id and target_article_id not in {article.get("id"), article.get("url")}:
            continue
        if article.get("status") not in {"selected", "draft_created"}:
            continue

        checked += 1
        missing_fields = _validate_selected_article(article)

        if missing_fields:
            article["processing_status"] = "failed"
            article["processing_error"] = "Missing required field(s): " + ", ".join(missing_fields)
            failed += 1
        else:
            package = _build_ai_input_package(article)
            article["processing_status"] = "ready_for_ai"
            article["processing_prepared_at"] = _now_iso()
            article["ai_input_package"] = package
            article.pop("processing_error", None)
            ready_for_ai += 1

        prepared_articles.append(article)

    if checked:
        save_article_queue(queue)

    return {
        "checked": checked,
        "ready_for_ai": ready_for_ai,
        "failed": failed,
        "articles": prepared_articles,
    }
