# ============================================================
# article_processor.py - Phase 5 AI Input Preparation
# ============================================================

from datetime import datetime

from article_queue import article_age_hours, is_article_safe_for_ai, load_article_queue, save_article_queue
from article_selector import normalize_category_label
from config import FRESHNESS_SAFETY_MARGIN_MINUTES, MAX_AI_ARTICLE_AGE_HOURS, MIN_EXTRACTED_CHARS


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
    full_text = article.get("full_article_text") or article.get("content_full") or article.get("content_preview", "")
    if not _has_value(full_text):
        missing.append("main content")
    elif len(str(full_text).strip()) < MIN_EXTRACTED_CHARS:
        missing.append(f"main content below {MIN_EXTRACTED_CHARS} characters")
    if not _has_value(article.get("suggested_category")):
        missing.append("suggested_category")
    if article.get("content_fetch_status") != "success":
        missing.append("successful content extraction")
    if not is_article_safe_for_ai(article):
        age = article_age_hours(article)
        age_text = f"{age:.2f}h" if age is not None else "unknown"
        missing.append(
            "article too close to freshness limit "
            f"(age {age_text}; AI cutoff {MAX_AI_ARTICLE_AGE_HOURS:.2f}h; "
            f"margin {FRESHNESS_SAFETY_MARGIN_MINUTES}m)"
        )

    return missing


def _build_ai_input_package(article):
    full_text = article.get("full_article_text") or article.get("content_full") or article.get("content_preview", "")
    return {
        "title": article.get("fetched_title") or article.get("title", ""),
        "url": article.get("url", ""),
        "source_name": article.get("source_name", ""),
        "suggested_category": normalize_category_label(article.get("suggested_category", "")),
        "main_image": article.get("main_image", ""),
        "article_images": article.get("article_images", []),
        "meta_description": article.get("meta_description", ""),
        "content_preview": article.get("content_preview", ""),
        "content_preview_chars": len(article.get("content_preview", "")),
        "rss_summary": article.get("rss_summary", ""),
        "full_article_text": full_text,
        "full_article_text_chars": len(full_text),
        "enrichment_status": article.get("enrichment_status", ""),
        "source_url": article.get("source_url", ""),
        "source_published_at": article.get("source_published_at", ""),
        "published_at_source": article.get("published_at_source", ""),
        "article_age_hours": article.get("article_age_hours"),
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
