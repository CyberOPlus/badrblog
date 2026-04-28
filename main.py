# ============================================================
# main.py - The Main Orchestrator
# ============================================================

import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blogger_client import (
    create_blogger_service,
    get_credentials,
    get_publish_target_name,
    publish_all_articles,
)
from article_backlog import (
    get_pending_articles,
    mark_backlog_published,
    remember_articles,
)
from article_ai_processor import process_one_selected_article_with_ai
from article_draft_publisher import (
    fix_or_update_current_blogger_draft,
    publish_one_blogger_post,
    publish_one_blogger_draft,
)
from article_queue import (
    add_articles_to_queue,
    article_age_hours,
    archive_expired_queue_articles,
    archive_published_queue_article,
    get_fresh_queue_candidates,
    is_article_safe_for_ai,
    load_article_queue,
    load_sources,
    maintain_article_queue,
    save_article_queue,
)
from article_enricher import enrich_ready_articles
from article_processor import prepare_selected_articles_for_ai
from article_scorer import score_new_articles
from article_selector import normalize_category_label, select_next_article, suggest_category
from duplicate_utils import title_hash, topic_signature
from article_quality import (
    get_candidate_fetch_limit,
    print_quality_report,
    select_best_articles,
)
from config import (
    ARTICLE_SELECTION_MULTIPLIER,
    ARTICLE_SELECTION_POOL_MIN,
    ALLOW_UNKNOWN_DATE_IN_FAST_MODE,
    ARTICLE_BACKLOG_PATH,
    ARTICLE_QUEUE_PATH,
    AI_PROVIDER_MEMORY_PATH,
    CHECK_INTERVAL,
    CATEGORY_ROTATION_MODE,
    CRAWL_INTERVAL_MINUTES,
    CRAWL_STATE_PATH,
    FALLBACK_FIRST_RUN_LOOKBACK_HOURS,
    FACEBOOK_AUTO_POST,
    FACEBOOK_STYLE_MEMORY_PATH,
    FAST_NEWS_MODE,
    FRESHNESS_SAFETY_MARGIN_MINUTES,
    FRESH_QUEUE_MODE,
    FIRST_VALID_ARTICLE_MODE,
    INTERNAL_LINK_CACHE_PATH,
    LOGS_DIR,
    MAX_ARTICLES_PER_RUN,
    MAX_AI_ARTICLE_AGE_HOURS,
    MAX_DRAFTS_PER_DAY,
    MAX_LIVE_POSTS_PER_DAY,
    MAX_POSTS_PER_RUN,
    MAX_SOURCES_PER_RUN,
    MIN_MINUTES_BETWEEN_DRAFTS,
    MIN_MINUTES_BETWEEN_LIVE_POSTS,
    SAFE_MODE,
    PUBLISH_MODE,
    PUBLISHED_DB_PATH,
    PROCESS_FULL_CATEGORY_PER_RUN,
    CRAWL_OVERLAP_MINUTES,
    RECENT_NEWS_MAX_AGE_HOURS,
    RECENT_NEWS_ONLY,
    SAFE_CYCLE_DRAFT_ONLY,
    SAFE_CYCLE_MAX_ARTICLES,
    SOURCE_TIMEOUT_SECONDS,
    SOURCE_HEALTH_PATH,
    TELEGRAM_ALERTS_ENABLED,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TOPIC_FINGERPRINTS_PATH,
    validate_config,
)
from facebook_publisher import (
    backfill_facebook_posts,
    get_facebook_limits_status,
    get_facebook_status,
    preview_next_facebook_post,
    post_one_article_to_facebook,
)
from processor import initialize_gemini, process_articles
from publishing_planner import plan_next_article
from published_db import filter_new_articles, load_published_ids, mark_many_as_published
from scraper import discover_first_valid_article_link, discover_fresh_article_links, discover_latest_article_links, get_latest_articles
from runtime_state import (
    add_topic_fingerprint,
    advance_category_rotation,
    load_topic_fingerprints,
    save_crawl_state,
    save_topic_fingerprints,
    select_category_for_rotation,
)
from source_validator import check_sources_config
from notifier import (
    get_notification_status,
    notify_auto_cycle_blocked,
    notify_auto_cycle_summary,
    send_telegram_message,
    telegram_alert_status,
    telegram_debug_probe,
)
from production_logging import html_word_count, log_event


PROBLEM_SOURCE_NAMES = {
    "SANS ISC",
    "AI Trends",
    "AlternativeTo News",
    "Softpedia News",
    "APKMirror",
    "Mandiant Blog",
    "VentureBeat AI",
    "Analytics India Magazine",
    "Perplexity Blog",
    "Google DeepMind Blog",
    "BBC Technology",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def print_banner():
    banner = """
╔══════════════════════════════════════════════════════════╗
║                                                          ║
║          🔐 BLOGGER AUTOMATION BOT v1.0 🔐              ║
║                                                          ║
║   Cybersecurity News → Arabic Translation → Auto Post   ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
    """
    print(banner)


def print_summary(stats):
    print("\n" + "=" * 60)
    print("📊 RUN SUMMARY")
    print("=" * 60)
    print(f"  📰 Articles scraped:     {stats['scraped']}")
    print(f"  ⏭️  Already handled:      {stats['skipped']}")
    print(f"  🧺 Added to backlog:     {stats['backlog_added']}")
    print(f"  🧭 Selected this run:    {stats['selected']}")
    print(f"  🕒 Deferred for later:   {stats['deferred']}")
    print(f"  🌐 Translated:           {stats['translated']}")
    print(f"  📤 Published/Saved:      {stats['published']}")
    print(f"  🎯 Publish target:       {stats['publish_target']}")
    print("=" * 60)


def _source_category_label(source):
    return normalize_category_label(source.get("category_label") or source.get("category_hint") or "")


def _available_category_labels(sources):
    labels = []
    seen = set()
    for source in sources:
        if not source.get("enabled", True):
            continue
        label = _source_category_label(source)
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def _sources_for_category(sources, category_label):
    return [
        source
        for source in sources
        if source.get("enabled", True) and _source_category_label(source) == category_label
    ]


def _category_queue_candidates(category_label):
    return [
        article
        for article in get_fresh_queue_candidates(statuses={"ready", "selected"})
        if normalize_category_label(
            article.get("suggested_category")
            or article.get("category_label")
            or article.get("category_hint")
        )
        == category_label
    ]


def _article_published_sort_value(article):
    value = str(article.get("source_published_at") or "").strip()
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _select_category_for_this_run(sources):
    available = _available_category_labels(sources)
    selection = select_category_for_rotation(available)
    category_label = selection.get("category", "")
    if category_label:
        advance_category_rotation(category_label, available)
    return category_label, selection.get("order") or available


def run_fetch_only():
    """
    Phase 1 command: discover articles and store them in the safe queue only.
    No AI translation, no Blogger API, and no publishing happens here.
    """
    print("\n" + "=" * 60)
    print("PHASE 1: Safe article ingestion")
    print("=" * 60)
    print("Step mode: fetch only. In auto-cycle, AI and publishing continue after this step.\n")

    sources = load_sources()
    enabled_sources = [source for source in sources if source.get("enabled", True)]
    print(f"Configured sources: {len(sources)}")
    print(f"Enabled sources:    {len(enabled_sources)}")
    print("Fetch limit:        per-source fetch_limit_per_run")
    if RECENT_NEWS_ONLY:
        print(f"Recent filter:      last {RECENT_NEWS_MAX_AGE_HOURS} hour(s)", flush=True)
        print(
            "AI freshness cutoff:"
            f" {MAX_AI_ARTICLE_AGE_HOURS:.2f} hour(s)"
            f" (margin {FRESHNESS_SAFETY_MARGIN_MINUTES} min)",
            flush=True,
        )

    published_set = load_published_ids()
    topic_fingerprints = load_topic_fingerprints()
    category_context = {}
    if CATEGORY_ROTATION_MODE and PROCESS_FULL_CATEGORY_PER_RUN:
        existing_queue = load_article_queue()
        selected_category, category_order = _select_category_for_this_run(enabled_sources)
        categories_to_try = [selected_category] if selected_category else []
        if category_order and selected_category in category_order:
            next_category = category_order[(category_order.index(selected_category) + 1) % len(category_order)]
            if next_category and next_category not in categories_to_try:
                categories_to_try.append(next_category)

        discovery = {
            "checked_sources": 0,
            "articles": [],
            "source_results": [],
            "reason": "no category configured",
        }
        category_attempts = []
        for attempt_index, category_label in enumerate(categories_to_try):
            category_sources = _sources_for_category(enabled_sources, category_label)
            queued_candidates = _category_queue_candidates(category_label)
            category_discovery = discover_fresh_article_links(
                category_sources,
                existing_articles=existing_queue.get("articles", []),
                published_urls=published_set,
                published_topic_hashes=topic_fingerprints,
                process_all_sources=True,
            )
            category_discovery["articles"] = sorted(
                category_discovery.get("articles", []),
                key=_article_published_sort_value,
                reverse=True,
            )
            category_attempts.append(
                {
                    "category": category_label,
                    "sources_checked": category_discovery.get("checked_sources", 0),
                    "candidates_found": len(category_discovery.get("articles", [])),
                    "queued_candidates": len(queued_candidates),
                    "fallback": attempt_index > 0,
                    "reason": category_discovery.get("reason", ""),
                }
            )
            discovery = category_discovery
            category_context = {
                "selected_category": category_label,
                "primary_category": selected_category,
                "category_order": category_order,
                "category_attempts": category_attempts,
                "category_fallback_used": attempt_index > 0,
                "queued_candidates": len(queued_candidates),
                "queued_candidate_ids": [
                    article.get("id") for article in queued_candidates if article.get("id")
                ],
            }
            if category_discovery.get("articles") or queued_candidates:
                break
    elif FAST_NEWS_MODE and FRESH_QUEUE_MODE:
        existing_queue = load_article_queue()
        discovery = discover_fresh_article_links(
            enabled_sources,
            existing_articles=existing_queue.get("articles", []),
            published_urls=published_set,
            published_topic_hashes=topic_fingerprints,
        )
    elif FAST_NEWS_MODE and FIRST_VALID_ARTICLE_MODE:
        existing_queue = load_article_queue()
        discovery = discover_first_valid_article_link(
            enabled_sources,
            existing_articles=existing_queue.get("articles", []),
            published_topic_hashes=topic_fingerprints,
        )
    else:
        discovery = discover_latest_article_links(enabled_sources)
    articles = filter_new_articles(discovery["articles"], published_set)
    queue_stats = add_articles_to_queue(articles)
    source_results = discovery.get("source_results", [])
    found_by_category = Counter(
        article.get("category_hint", "Uncategorized") or "Uncategorized"
        for article in articles
    )
    failed_sources = [
        source for source in source_results if source.get("status") == "failed"
    ]
    zero_link_sources = [
        source
        for source in source_results
        if source.get("status") != "failed" and source.get("links_found", 0) == 0
    ]

    print("\n" + "=" * 60)
    print("PHASE 1 FETCH SUMMARY")
    print("=" * 60)
    print(f"Sources checked:          {discovery['checked_sources']}")
    if category_context:
        print(f"Selected category:        {category_context.get('selected_category', '')}")
        print(f"Primary category:         {category_context.get('primary_category', '')}")
        print(f"Queued in category:       {category_context.get('queued_candidates', 0)}")
        for attempt in category_context.get("category_attempts", []):
            print(
                "  Category attempt: "
                f"{attempt.get('category', '')}; "
                f"sources={attempt.get('sources_checked', 0)}; "
                f"candidates={attempt.get('candidates_found', 0)}; "
                f"queued={attempt.get('queued_candidates', 0)}"
            )
    print(f"Articles found:           {len(articles)}")
    print(f"New articles added:       {queue_stats['added']}")
    print(f"Duplicates skipped:       {queue_stats['duplicates']}")
    print(f"  - Same URL:             {queue_stats['duplicate_url']}")
    print(f"  - Same normalized title:{queue_stats['duplicate_title']}")
    print(f"Total queued articles:    {queue_stats['total_queued']}")
    print("Articles found per category:")
    for category, count in found_by_category.items():
        print(f"  - {category}: {count}")
    print("New articles added per category:")
    for category, count in queue_stats.get("added_by_category", {}).items():
        print(f"  - {category}: {count}")
    print(f"Failed/HTTP issue sources:{len(failed_sources)}")
    for source in failed_sources:
        print(
            f"  - {source.get('source_name', '')} "
            f"({source.get('base_url', '')}): {source.get('error', '')}; "
            f"links found: {source.get('links_found', 0)}"
        )
    print(f"Sources with 0 links:     {len(zero_link_sources)}")
    for source in zero_link_sources:
        print(
            f"  - {source.get('source_name', '')} "
            f"({source.get('base_url', '')})"
        )
    print("Publishing:               disabled in Phase 1")
    print("=" * 60)

    return {
        "sources_checked": discovery["checked_sources"],
        "articles_found": len(articles),
        "first_valid_url": articles[0].get("url", "") if articles else "",
        "articles_found_by_category": dict(found_by_category),
        "failed_sources": failed_sources,
        "zero_link_sources": zero_link_sources,
        "reason": discovery.get("reason", ""),
        **category_context,
        **queue_stats,
    }


def reset_runtime_state():
    reset_targets = [
        ARTICLE_QUEUE_PATH,
        ARTICLE_BACKLOG_PATH,
        PUBLISHED_DB_PATH,
        CRAWL_STATE_PATH,
        TOPIC_FINGERPRINTS_PATH,
        SOURCE_HEALTH_PATH,
        AUTO_CYCLE_RUN_LOG,
    ]
    removed = []
    for path in reset_targets:
        try:
            if path.exists():
                path.unlink()
                removed.append(str(path))
        except OSError:
            pass

    for pattern in ("latest-*.txt", "latest-*.err.txt", "*.out.log", "*.err.log"):
        for path in LOGS_DIR.glob(pattern):
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                pass

    save_crawl_state({"sources": {}})
    save_topic_fingerprints(set())
    save_article_queue({"articles": [], "notifications": {}})
    return {"ok": True, "removed": removed}


def run_reset_state_only():
    result = reset_runtime_state()
    print("\n" + "=" * 60)
    print("RESET STATE")
    print("=" * 60)
    print(f"Queue reset: {'yes' if ARTICLE_QUEUE_PATH.exists() else 'no'}")
    print(f"Crawl state reset: {'yes' if CRAWL_STATE_PATH.exists() else 'no'}")
    print(f"Topic fingerprints reset: {'yes' if TOPIC_FINGERPRINTS_PATH.exists() else 'no'}")
    print(f"Published IDs reset: {'yes' if not PUBLISHED_DB_PATH.exists() else 'no'}")
    print(f"Backlog reset: {'yes' if not ARTICLE_BACKLOG_PATH.exists() else 'no'}")
    print("GitHub Actions cache reset: yes (namespace invalidated in workflow)")
    print("=" * 60)
    return result


def _find_article_by_id(article_id):
    if not article_id:
        return None
    queue = load_article_queue()
    for article in queue.get("articles", []):
        if article_id in {article.get("id"), article.get("url")}:
            return article
    return None


def _lock_specific_ready_article(article_url):
    if not article_url:
        return None
    queue = load_article_queue()
    for article in queue.get("articles", []):
        if article.get("url") != article_url:
            continue
        if article.get("status") != "ready" or article.get("content_fetch_status") != "success":
            return None
        if not is_article_safe_for_ai(article):
            age = article_age_hours(article)
            article["status"] = "skipped"
            article["skip_reason"] = (
                "article too close to freshness limit before AI "
                f"(age {age or 0:.2f}h; cutoff {MAX_AI_ARTICLE_AGE_HOURS:.2f}h)"
            )
            save_article_queue(queue)
            return None
        article["status"] = "selected"
        article["selected_at"] = datetime.now().isoformat(timespec="seconds")
        article["suggested_category"] = normalize_category_label(
            article.get("suggested_category")
            or article.get("category_label")
            or article.get("category_hint")
            or suggest_category(article)
        )
        article["selection_reason"] = "first valid article fast mode"
        save_article_queue(queue)
        return article
    return None


def _count_images(article):
    if not article:
        return 0

    package = article.get("ai_input_package") or {}
    urls = set()
    main_image = article.get("main_image") or package.get("main_image")
    if main_image:
        urls.add(str(main_image))

    for image in article.get("article_images") or package.get("article_images") or []:
        if isinstance(image, dict):
            url = image.get("url")
        else:
            url = image
        if url:
            urls.add(str(url))

    return len(urls)


def _count_trusted_references(article):
    if not article:
        return 0
    package = article.get("ai_input_package") or {}
    return len(article.get("trusted_references") or package.get("trusted_references") or [])


def _article_word_count(article):
    if not article:
        return 0
    try:
        stored = int(article.get("final_word_count") or 0)
    except (TypeError, ValueError):
        stored = 0
    return stored or html_word_count(article.get("final_html", ""))


def _source_name_in_final_html(article):
    if not article:
        return False
    source_name = str(article.get("source_name") or "").strip()
    final_html = str(article.get("final_html") or "")
    return bool(source_name and source_name.casefold() in final_html.casefold())


def _parse_local_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _effective_publish_mode():
    if SAFE_MODE:
        return "draft"
    return "live" if PUBLISH_MODE == "live" else "draft"


def _effective_action():
    if (
        not SAFE_MODE
        and PUBLISH_MODE == "live"
        and FAST_NEWS_MODE
        and CATEGORY_ROTATION_MODE
        and PROCESS_FULL_CATEGORY_PER_RUN
        and RECENT_NEWS_ONLY
    ):
        return "LIVE_CATEGORY_ROTATION"
    if (
        not SAFE_MODE
        and PUBLISH_MODE == "live"
        and FAST_NEWS_MODE
        and FRESH_QUEUE_MODE
        and not FIRST_VALID_ARTICLE_MODE
        and RECENT_NEWS_ONLY
    ):
        return "LIVE_FRESH_QUEUE"
    if (
        not SAFE_MODE
        and PUBLISH_MODE == "live"
        and FAST_NEWS_MODE
        and FIRST_VALID_ARTICLE_MODE
        and RECENT_NEWS_ONLY
    ):
        return "LIVE_FAST_RECENT_NEWS"
    if SAFE_MODE:
        return "DRAFT"
    if PUBLISH_MODE == "live":
        return "LIVE"
    return "FETCH_ONLY" if PUBLISH_MODE == "fetch-only" else "DRAFT"


def print_startup_config():
    print("\n" + "=" * 60, flush=True)
    print("STARTUP CONFIG", flush=True)
    print("=" * 60, flush=True)
    print(f"SAFE_MODE:                      {str(SAFE_MODE).lower()}", flush=True)
    print(f"PUBLISH_MODE:                   {PUBLISH_MODE}", flush=True)
    print(f"FAST_NEWS_MODE:                 {str(FAST_NEWS_MODE).lower()}", flush=True)
    print(f"CATEGORY_ROTATION_MODE:         {str(CATEGORY_ROTATION_MODE).lower()}", flush=True)
    print(f"PROCESS_FULL_CATEGORY_PER_RUN:  {str(PROCESS_FULL_CATEGORY_PER_RUN).lower()}", flush=True)
    print(f"FRESH_QUEUE_MODE:               {str(FRESH_QUEUE_MODE).lower()}", flush=True)
    print(f"FIRST_VALID_ARTICLE_MODE:       {str(FIRST_VALID_ARTICLE_MODE).lower()}", flush=True)
    print(f"RECENT_NEWS_ONLY:               {str(RECENT_NEWS_ONLY).lower()}", flush=True)
    print(f"RECENT_NEWS_MAX_AGE_HOURS:      {RECENT_NEWS_MAX_AGE_HOURS}", flush=True)
    print(f"FRESHNESS_SAFETY_MARGIN_MINUTES:{FRESHNESS_SAFETY_MARGIN_MINUTES}", flush=True)
    print(f"MAX_AI_ARTICLE_AGE_HOURS:       {MAX_AI_ARTICLE_AGE_HOURS:.2f}", flush=True)
    print(f"ALLOW_UNKNOWN_DATE_IN_FAST_MODE:{str(ALLOW_UNKNOWN_DATE_IN_FAST_MODE).lower()}", flush=True)
    print(f"MAX_SOURCES_PER_RUN:            {MAX_SOURCES_PER_RUN}", flush=True)
    print(f"SOURCE_TIMEOUT_SECONDS:         {SOURCE_TIMEOUT_SECONDS}", flush=True)
    print(f"Effective action:               {_effective_action()}", flush=True)
    print("=" * 60, flush=True)


def _select_oldest_fresh_ready_article():
    candidates = get_fresh_queue_candidates(statuses={"ready", "selected"})
    if not candidates:
        return None

    candidate = candidates[0]
    if candidate.get("status") != "selected":
        queue = load_article_queue()
        for article in queue.get("articles", []):
            if article.get("id") != candidate.get("id"):
                continue
            article["status"] = "selected"
            article["selected_at"] = datetime.now().isoformat(timespec="seconds")
            article["suggested_category"] = normalize_category_label(
                article.get("suggested_category")
                or article.get("category_label")
                or article.get("category_hint")
                or suggest_category(article)
            )
            article["selection_reason"] = "oldest fresh queued article"
            candidate = article
            break
        save_article_queue(queue)
    return candidate


def _select_newest_fresh_ready_article(category_label="", preferred_ids=None):
    candidates = get_fresh_queue_candidates(statuses={"ready", "selected"})
    if category_label:
        candidates = [
            article
            for article in candidates
        if normalize_category_label(
            article.get("suggested_category")
            or article.get("category_label")
            or article.get("category_hint")
        )
        == category_label
        ]
    if not candidates:
        return None

    preferred_ids = {item for item in (preferred_ids or []) if item}
    if preferred_ids:
        preferred_candidates = [
            article for article in candidates if article.get("id") in preferred_ids
        ]
        if preferred_candidates:
            candidates = preferred_candidates

    candidate = sorted(candidates, key=_article_published_sort_value, reverse=True)[0]
    queue = load_article_queue()
    for article in queue.get("articles", []):
        if article.get("id") != candidate.get("id"):
            continue
        if article.get("status") != "selected":
            article["status"] = "selected"
            article["selected_at"] = datetime.now().isoformat(timespec="seconds")
        article["suggested_category"] = (
            normalize_category_label(article.get("suggested_category"))
            or article.get("category_label")
            or article.get("category_hint")
            or suggest_category(article)
        )
        article["suggested_category"] = normalize_category_label(article["suggested_category"])
        article["selection_reason"] = (
            f"category rotation newest fresh article: {category_label}"
            if category_label
            else "category rotation newest fresh article"
        )
        candidate = article
        break
    save_article_queue(queue)
    return candidate


def get_publish_schedule_status(mode=None, now=None):
    publish_mode = "live" if (mode or _effective_publish_mode()) == "live" else "draft"
    now = now or datetime.now()
    today = now.date().isoformat()
    queue = load_article_queue()
    draft_times = []
    live_times = []

    for article in queue.get("articles", []):
        draft_created_at = article.get("draft_created_at")
        draft_time = _parse_local_datetime(draft_created_at)
        if draft_time:
            draft_times.append(draft_time)

        published_at = article.get("published_at")
        live_time = _parse_local_datetime(published_at)
        if live_time:
            live_times.append(live_time)

    today_draft_times = [
        draft_time for draft_time in draft_times if draft_time.date().isoformat() == today
    ]
    today_live_times = [
        live_time for live_time in live_times if live_time.date().isoformat() == today
    ]
    last_draft_time = max(draft_times) if draft_times else None
    last_live_time = max(live_times) if live_times else None
    minutes_since_last_draft = (
        max(0, int((now - last_draft_time).total_seconds() // 60))
        if last_draft_time
        else None
    )
    minutes_since_last_live = (
        max(0, int((now - last_live_time).total_seconds() // 60))
        if last_live_time
        else None
    )
    last_time = last_live_time if publish_mode == "live" else last_draft_time
    min_minutes = (
        MIN_MINUTES_BETWEEN_LIVE_POSTS
        if publish_mode == "live"
        else MIN_MINUTES_BETWEEN_DRAFTS
    )
    max_per_day = MAX_LIVE_POSTS_PER_DAY if publish_mode == "live" else MAX_DRAFTS_PER_DAY
    created_today = len(today_live_times) if publish_mode == "live" else len(today_draft_times)

    minutes_since_last = None
    interval_next_allowed = now
    if last_time:
        minutes_since_last = max(0, int((now - last_time).total_seconds() // 60))
        interval_next_allowed = last_time + timedelta(minutes=min_minutes)

    daily_next_allowed = now
    limit_reached = created_today >= max_per_day
    if limit_reached:
        daily_next_allowed = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())

    interval_blocked = bool(last_time and interval_next_allowed > now)
    allowed_now = not limit_reached and not interval_blocked
    next_allowed_time = now if allowed_now else max(daily_next_allowed, interval_next_allowed)

    reasons = []
    if limit_reached:
        reasons.append(f"daily {publish_mode} limit reached")
    if interval_blocked:
        reasons.append(f"minimum minutes between {publish_mode} posts has not elapsed")

    return {
        "configured_publish_mode": PUBLISH_MODE,
        "publish_mode": publish_mode,
        "drafts_created_today": len(today_draft_times),
        "live_posts_created_today": len(today_live_times),
        "max_drafts_per_day": MAX_DRAFTS_PER_DAY,
        "max_live_posts_per_day": MAX_LIVE_POSTS_PER_DAY,
        "last_draft_time": last_draft_time,
        "last_live_publish_time": last_live_time,
        "minutes_since_last_draft": minutes_since_last_draft,
        "minutes_since_last_live_publish": minutes_since_last_live,
        "min_minutes_between_drafts": MIN_MINUTES_BETWEEN_DRAFTS,
        "min_minutes_between_live_posts": MIN_MINUTES_BETWEEN_LIVE_POSTS,
        "allowed_now": allowed_now,
        "next_allowed_time": next_allowed_time,
        "reasons": reasons,
    }


def get_safe_cycle_schedule_status(now=None):
    return get_publish_schedule_status(mode="draft", now=now)


def _format_datetime(value):
    return value.isoformat(timespec="seconds") if value else ""


AUTO_CYCLE_RUN_LOG = LOGS_DIR / "auto_cycle_runs.jsonl"
AUTO_CYCLE_WORKFLOW_PATH = Path(".github") / "workflows" / "auto-cycle.yml"


def _workflow_schedule():
    try:
        lines = AUTO_CYCLE_WORKFLOW_PATH.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- cron:"):
            return stripped.split("cron:", 1)[1].strip().strip('"').strip("'")
    return ""


def _new_run_id():
    return datetime.now().strftime("%Y%m%d%H%M%S%f")


def _append_auto_cycle_run_log(record):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    safe_record = {
        "timestamp": record.get("timestamp") or record.get("finished_at", ""),
        "run_id": record.get("run_id", ""),
        "started_at": record.get("started_at", ""),
        "finished_at": record.get("finished_at", ""),
        "mode": record.get("mode", ""),
        "facebook_auto_post": bool(record.get("facebook_auto_post")),
        "target_article_id": record.get("target_article_id", ""),
        "step_reached": record.get("step_reached", ""),
        "selected_article_title": record.get("selected_article_title", ""),
        "source_name": record.get("source_name", ""),
        "category": record.get("category") or record.get("selected_category", ""),
        "selected_category": record.get("selected_category", ""),
        "sources_checked": record.get("sources_checked", 0),
        "candidates_found": record.get("candidates_found", 0),
        "article_word_count": record.get("article_word_count", 0),
        "blogger_status": record.get("blogger_status", ""),
        "published_url": record.get("published_url") or record.get("blogger_post_url", ""),
        "blogger_post_url": record.get("blogger_post_url", ""),
        "facebook_status": record.get("facebook_status", ""),
        "warning": record.get("warning", ""),
        "skip_reason": record.get("skip_reason") or record.get("stopped_reason", ""),
        "stopped_reason": record.get("stopped_reason", ""),
        "execution_seconds": record.get("execution_seconds", 0),
        "success": bool(record.get("success")),
    }
    with open(AUTO_CYCLE_RUN_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe_record, ensure_ascii=False) + "\n")


def _load_auto_cycle_run_logs(limit=None):
    if not AUTO_CYCLE_RUN_LOG.exists():
        return []
    records = []
    with open(AUTO_CYCLE_RUN_LOG, "r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records[-limit:] if limit else records


def _auto_cycle_record_from_result(run_id, started_at, result, error=None):
    finished_at = datetime.now().isoformat(timespec="seconds")
    article = (result or {}).get("article") or {}
    draft = (result or {}).get("draft") or {}
    facebook = (result or {}).get("facebook") or {}
    fetch = (result or {}).get("fetch") or {}
    draft_action = (result or {}).get("draft_action", "")
    facebook_error = facebook.get("error") if facebook and not facebook.get("posted") else ""
    blogger_succeeded = draft_action in {"created", "updated"}
    stopped_reason = "" if blogger_succeeded else str(
        error or (result or {}).get("reason") or draft.get("error") or ""
    )
    if facebook_error and not blogger_succeeded:
        stopped_reason = facebook_error
    skipped = bool((result or {}).get("skipped"))
    success = (bool((result or {}).get("completed")) or skipped) and not error

    category = fetch.get("selected_category") or article.get("suggested_category", "")
    published_url = article.get("blogger_post_url") or article.get("blogger_draft_url") or ""

    return {
        "run_id": run_id,
        "timestamp": finished_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "mode": _effective_publish_mode(),
        "facebook_auto_post": FACEBOOK_AUTO_POST,
        "target_article_id": (result or {}).get("target_article_id") or article.get("id") or "",
        "step_reached": (result or {}).get("step_reached", ""),
        "selected_article_title": article.get("title") or article.get("seo_title") or "",
        "source_name": article.get("source_name", ""),
        "category": category,
        "selected_category": category,
        "sources_checked": fetch.get("sources_checked", 0),
        "candidates_found": fetch.get("articles_found", 0),
        "article_word_count": _article_word_count(article),
        "blogger_status": article.get("publish_status") or draft.get("publishing_mode") or "",
        "published_url": published_url,
        "blogger_post_url": published_url,
        "facebook_status": article.get("facebook_status") or (facebook.get("article") or {}).get("facebook_status", ""),
        "warning": facebook_error if blogger_succeeded and facebook_error else "",
        "skip_reason": stopped_reason,
        "stopped_reason": stopped_reason,
        "execution_seconds": (result or {}).get("execution_seconds", 0),
        "success": success,
    }


def _auto_cycle_alert_message(result, error=None):
    result = result or {}
    article = result.get("article") or {}
    draft = result.get("draft") or {}
    schedule = result.get("schedule") or {}
    step = result.get("step_reached") or "unknown"
    draft_action = result.get("draft_action", "")

    if error:
        return "\n".join(
            [
                "\u274c Auto-cycle failed",
                f"Step: {step}",
                f"Reason: {error}",
            ]
        )

    if result.get("completed"):
        blogger_status = article.get("publish_status") or draft.get("publishing_mode") or draft_action or ""
        post_url = article.get("blogger_post_url") or article.get("blogger_draft_url") or ""
        lines = [
            "\u2705 Auto-cycle success",
            f"Mode: {_effective_publish_mode()}",
            f"Article: {article.get('title') or article.get('seo_title') or ''}",
            f"Category: {article.get('suggested_category', '')}",
            f"Blogger: {blogger_status}",
            f"URL: {post_url}",
        ]
        facebook = result.get("facebook") or {}
        facebook_error = facebook.get("error") if facebook and not facebook.get("posted") else ""
        if facebook_error:
            lines.append(f"Warning: {facebook_error}")
        return "\n".join(lines)

    reason = result.get("reason") or draft.get("error") or "unknown"
    if schedule:
        next_allowed = _format_datetime(schedule.get("next_allowed_time"))
        return "\n".join(
            [
                "\u23f8 Auto-cycle blocked",
                f"Reason: {reason}",
                f"Next allowed time: {next_allowed}",
            ]
        )

    return "\n".join(
        [
            "\u274c Auto-cycle failed",
            f"Step: {step}",
            f"Reason: {reason}",
        ]
    )


def _blogger_draft_alert_message(article, draft_action, draft_result):
    article = article or {}
    draft_result = draft_result or {}
    draft_url = article.get("blogger_draft_url") or article.get("blogger_post_url") or ""
    if draft_action in {"created", "updated"}:
        return "\n".join(
            [
                "\u2705 Blogger draft saved",
                f"Action: {draft_action}",
                f"Article: {article.get('title') or article.get('seo_title') or ''}",
                f"URL: {draft_url}",
            ]
        )

    return "\n".join(
        [
            "\u274c Blogger draft failed",
            f"Article: {article.get('title') or article.get('seo_title') or ''}",
            f"Reason: {draft_result.get('error') or 'unknown'}",
        ]
    )


def _facebook_preview_alert_message(preview):
    preview = preview or {}
    if not preview.get("available"):
        return "\n".join(
            [
                "\u23f8 Facebook preview unavailable",
                f"Reason: {preview.get('error') or 'unknown'}",
            ]
        )

    return "\n".join(
        [
            "\u2139 Facebook preview only",
            f"Post text:\n{preview.get('post_text', '')}",
            f"Hashtags: {preview.get('hashtags', '')}",
            f"First comment: {preview.get('first_comment_text', '')}",
            f"Image URL: {preview.get('image_url', '')}",
        ]
    )


def _send_named_telegram_alert(label, message):
    alert_result = send_telegram_message(message)
    if alert_result.get("sent"):
        print(f"Telegram {label} alert: sent")
    elif alert_result.get("skipped"):
        _print_telegram_config_warning()
        print(f"Telegram {label} alert: skipped")
    else:
        print(f"Telegram {label} alert: failed ({alert_result.get('reason', 'unknown error')})")
    return alert_result


def _send_auto_cycle_alert(result, error=None, run_id=""):
    print("Sending Telegram final report", flush=True)
    alert_result = notify_auto_cycle_summary(result, error=error, run_id=run_id)
    if alert_result.get("sent"):
        print("Telegram final alert: sent")
    elif alert_result.get("skipped"):
        _print_telegram_config_warning()
        print("Telegram final alert: skipped")
    else:
        print(f"Telegram final alert: failed ({alert_result.get('reason', 'unknown error')})")
    return alert_result


def _print_telegram_config_warning(status=None):
    status = status or telegram_alert_status()
    if status["enabled"] and not status["ready"]:
        print("Telegram alerts enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")


RUNTIME_STATE_PATHS = (
    ARTICLE_QUEUE_PATH,
    CRAWL_STATE_PATH,
    SOURCE_HEALTH_PATH,
    AI_PROVIDER_MEMORY_PATH,
    FACEBOOK_STYLE_MEMORY_PATH,
    INTERNAL_LINK_CACHE_PATH,
)


def save_runtime_state_to_git():
    result = {
        "saved": False,
        "git_push_state": "skipped",
        "warning": "",
    }
    try:
        paths = [str(path.relative_to(Path.cwd())) if path.is_absolute() else str(path) for path in RUNTIME_STATE_PATHS if Path(path).exists()]
        if not paths:
            return result
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=False, capture_output=True, text=True)
        subprocess.run(["git", "add", "-f", "--", *paths], check=True, capture_output=True, text=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
        if diff.returncode == 0:
            return result
        commit = subprocess.run(
            ["git", "commit", "-m", "Update bot runtime state [skip ci]"],
            check=False,
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            result["warning"] = "git commit failed"
            result["git_push_state"] = "warning"
            log_event("runtime_state_git_warning", reason=result["warning"])
            return result
        result["saved"] = True
        push = subprocess.run(["git", "push", "origin", "HEAD:main"], check=False, capture_output=True, text=True)
        if push.returncode == 0:
            result["git_push_state"] = "success"
        else:
            result["git_push_state"] = "warning"
            result["warning"] = "git push failed"
            log_event("runtime_state_git_warning", reason=result["warning"])
    except Exception as error:
        result["git_push_state"] = "warning"
        result["warning"] = f"runtime state save failed: {error.__class__.__name__}"
        log_event("runtime_state_git_warning", reason=result["warning"])
    return result


def run_auto_cycle_logged():
    run_id = _new_run_id()
    started_at = datetime.now().isoformat(timespec="seconds")
    started_timer = time.perf_counter()
    result = None
    error = None
    try:
        log_event("auto_cycle_start", run_id=run_id, mode=_effective_publish_mode())
        result = run_safe_cycle_only()
        return result
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        execution_seconds = round(time.perf_counter() - started_timer, 2)
        if isinstance(result, dict):
            result["execution_seconds"] = execution_seconds
        log_event(
            "auto_cycle_end",
            run_id=run_id,
            status="failed" if error else "completed",
            execution_seconds=execution_seconds,
            error=error,
        )
        print(f"Finished in {execution_seconds:.2f} seconds", flush=True)
        runtime_state_result = save_runtime_state_to_git()
        if isinstance(result, dict):
            result["runtime_state"] = runtime_state_result
        print(
            "Runtime state saved: "
            f"{'yes' if runtime_state_result.get('saved') else 'no'} | "
            f"git push: {runtime_state_result.get('git_push_state')}"
        )
        _send_auto_cycle_alert(result or {}, error=error, run_id=run_id)
        _append_auto_cycle_run_log(
            _auto_cycle_record_from_result(run_id, started_at, result or {}, error=error)
        )


def run_health_only():
    records = _load_auto_cycle_run_logs()
    last_10 = records[-10:]
    success_count = sum(1 for record in records if record.get("success"))
    failure_count = sum(1 for record in records if not record.get("success"))
    last_success = next((record for record in reversed(records) if record.get("success")), None)
    last_failure = next((record for record in reversed(records) if not record.get("success")), None)

    queue = load_article_queue()
    articles = queue.get("articles", [])
    archived_count = sum(1 for article in articles if article.get("archived"))
    active_count = len(articles) - archived_count
    status_counts = Counter(
        article.get("status", "unknown") or "unknown"
        for article in articles
        if not article.get("archived")
    )

    print("\n" + "=" * 60)
    print("AUTO-CYCLE HEALTH")
    print("=" * 60)
    print("Last 10 runs:")
    if not last_10:
        print("  No auto-cycle runs logged yet.")
    for record in last_10:
        print(
            f"  - {record.get('started_at', '')} | "
            f"success={record.get('success')} | "
            f"mode={record.get('mode', '')} | "
            f"target={record.get('target_article_id', '')} | "
            f"step={record.get('step_reached', '')} | "
            f"blogger={record.get('blogger_status', '')} | "
            f"facebook={record.get('facebook_status', '')} | "
            f"reason={record.get('stopped_reason', '')}"
        )
    print(f"Success count:       {success_count}")
    print(f"Failure count:       {failure_count}")
    print(f"Last success time:   {last_success.get('finished_at', '') if last_success else ''}")
    print(f"Last failure reason: {last_failure.get('stopped_reason', '') if last_failure else ''}")
    print(f"Active queue count:  {active_count}")
    print(f"Archived count:      {archived_count}")
    print("Queue counts by status:")
    for status, count in sorted(status_counts.items()):
        print(f"  - {status}: {count}")
    print("=" * 60)

    return {
        "last_10": last_10,
        "success_count": success_count,
        "failure_count": failure_count,
        "last_success_time": last_success.get("finished_at", "") if last_success else "",
        "last_failure_reason": last_failure.get("stopped_reason", "") if last_failure else "",
        "active_queue_count": active_count,
        "archived_count": archived_count,
        "queue_counts": dict(status_counts),
    }


def run_24h_status_only():
    publish_status = get_publish_schedule_status(mode="live")
    facebook_limits = get_facebook_limits_status()
    telegram_status = telegram_alert_status()
    records = _load_auto_cycle_run_logs()
    last_success = next((record for record in reversed(records) if record.get("success")), None)
    last_failure = next((record for record in reversed(records) if not record.get("success")), None)

    queue = load_article_queue()
    active_articles = [
        article for article in queue.get("articles", []) if not article.get("archived")
    ]
    ready_articles = [
        article
        for article in active_articles
        if article.get("status") in {"ready", "selected"}
        and article.get("content_fetch_status") == "success"
    ]
    plan = plan_next_article(lock=False)
    next_article = plan.get("selected")
    publish_next = publish_status.get("next_allowed_time")
    facebook_next = facebook_limits.get("next_allowed_time")
    next_allowed = max(
        [value for value in (publish_next, facebook_next) if value],
        default=None,
    )

    print("\n" + "=" * 60)
    print("24H UNATTENDED STATUS")
    print("=" * 60)
    print(f"Workflow schedule:          {_workflow_schedule()}")
    print(f"PUBLISH_MODE:               {PUBLISH_MODE}")
    print(f"FACEBOOK_AUTO_POST:         {'true' if FACEBOOK_AUTO_POST else 'false'}")
    print(f"Telegram ready:             {'yes' if telegram_status['ready'] else 'no'}")
    print(f"Live posts today:           {publish_status['live_posts_created_today']}")
    print(f"Facebook posts today:       {facebook_limits['facebook_posts_today']}")
    print(f"Last successful run:        {last_success.get('finished_at', '') if last_success else ''}")
    print(f"Last failure reason:        {last_failure.get('stopped_reason', '') if last_failure else ''}")
    print(f"Active queue count:         {len(active_articles)}")
    print(f"Ready queue count:          {len(ready_articles)}")
    if next_article:
        print(f"Next eligible article:      {next_article.get('title') or next_article.get('seo_title') or ''}")
        print(f"Next eligible score:        {next_article.get('score', '')}")
    else:
        print("Next eligible article:      ")
    print(f"Next allowed publish time:  {_format_datetime(next_allowed)}")
    print("=" * 60)

    return {
        "workflow_schedule": _workflow_schedule(),
        "publish_mode": PUBLISH_MODE,
        "facebook_auto_post": FACEBOOK_AUTO_POST,
        "telegram_ready": telegram_status["ready"],
        "live_posts_today": publish_status["live_posts_created_today"],
        "facebook_posts_today": facebook_limits["facebook_posts_today"],
        "last_successful_run": last_success.get("finished_at", "") if last_success else "",
        "last_failure_reason": last_failure.get("stopped_reason", "") if last_failure else "",
        "active_queue_count": len(active_articles),
        "ready_queue_count": len(ready_articles),
        "next_eligible_article": next_article,
        "next_allowed_publish_time": _format_datetime(next_allowed),
    }


def run_queue_maintenance_only():
    """
    Archive stale skipped/failed queue records and duplicate URLs.
    Records are not permanently deleted.
    """
    stats = maintain_article_queue(days=7)

    print("\n" + "=" * 60)
    print("QUEUE MAINTENANCE SUMMARY")
    print("=" * 60)
    print(f"Articles checked:           {stats['checked']}")
    print(f"Archived old skipped:       {stats['archived_old_skipped']}")
    print(f"Archived old failed:        {stats['archived_old_failed']}")
    print(f"Archived duplicate URLs:    {stats['archived_duplicate_urls']}")
    print(f"Already archived:           {stats['already_archived']}")
    print(f"Active queue count:         {stats['active_count']}")
    print(f"Archived count:             {stats['archived_count']}")
    print(f"Total queued records:       {stats['total_queued']}")
    print("Permanent deletion:         disabled")
    print("=" * 60)

    return stats


def print_safe_cycle_status(status):
    print("\n" + "=" * 60)
    print("PHASE 11 PUBLISH STATUS")
    print("=" * 60)
    print(f"PUBLISH_MODE:                 {status['configured_publish_mode']}")
    print(f"Effective mode:               {status['publish_mode']}")
    print(f"Drafts created today:        {status['drafts_created_today']}")
    print(f"Live posts created today:    {status['live_posts_created_today']}")
    print(f"Max drafts per day:          {status['max_drafts_per_day']}")
    print(f"Max live posts per day:      {status['max_live_posts_per_day']}")
    print(f"Last draft time:             {_format_datetime(status['last_draft_time'])}")
    draft_minutes = status["minutes_since_last_draft"]
    print(f"Minutes since last draft:    {draft_minutes if draft_minutes is not None else ''}")
    print(f"Last live publish time:      {_format_datetime(status['last_live_publish_time'])}")
    live_minutes = status["minutes_since_last_live_publish"]
    print(f"Minutes since live publish:  {live_minutes if live_minutes is not None else ''}")
    print(f"Min minutes between drafts:  {status['min_minutes_between_drafts']}")
    print(f"Min minutes between live:    {status['min_minutes_between_live_posts']}")
    print(f"Publishing allowed now:      {'yes' if status['allowed_now'] else 'no'}")
    print(f"Next allowed time:           {_format_datetime(status['next_allowed_time'])}")
    if status["reasons"]:
        print(f"Blocked reason:              {'; '.join(status['reasons'])}")
    print(f"Publishing mode:             {status['publish_mode'].upper()}")
    print("=" * 60)


def run_safe_cycle_status_only():
    """
    Phase 10 command: show safe-cycle daily limit and spacing status.
    Read-only: no fetch, AI, Blogger, drafts, or publishing.
    """
    status = get_publish_schedule_status()
    print_safe_cycle_status(status)
    return status


def run_publish_status_only():
    """
    Phase 11 command: show current Blogger publish mode and mode-specific limits.
    Read-only: no fetch, AI, Blogger writes, drafts, live posts, or scheduling.
    """
    status = get_publish_schedule_status()
    print_safe_cycle_status(status)
    return status


def print_facebook_status(status):
    print("\n" + "=" * 60)
    print("PHASE 12 FACEBOOK STATUS")
    print("=" * 60)
    print(f"FACEBOOK_AUTO_POST:                 {'enabled' if status['auto_post_enabled'] else 'disabled'}")
    print(f"Page ID configured:                 {'yes' if status['page_id_configured'] else 'no'}")
    print(f"Page access token configured:       {'yes' if status['token_configured'] else 'no'}")
    print(f"Published articles without Facebook:{status['published_without_facebook']}")
    print(f"Articles already posted to Facebook:{status['posted_to_facebook']}")
    latest = status.get("latest_eligible")
    if latest:
        print(f"Latest eligible title:              {latest.get('title', '')}")
        print(f"Blogger URL:                        {latest.get('blogger_post_url', '')}")
    print("=" * 60)


def run_facebook_status_only():
    """
    Phase 12 command: show Facebook auto-post configuration and queue status.
    Read-only: no Facebook, Blogger, AI, or fetch writes.
    """
    status = get_facebook_status()
    print_facebook_status(status)
    return status


def print_facebook_limits_status(status):
    print("\n" + "=" * 60)
    print("PHASE 13 FACEBOOK LIMITS STATUS")
    print("=" * 60)
    print(f"Facebook posts today:          {status['facebook_posts_today']}")
    print(f"Max Facebook posts per day:    {status['max_facebook_posts_per_day']}")
    print(f"Last Facebook post time:       {_format_datetime(status['last_facebook_post_time'])}")
    minutes = status["minutes_since_last_facebook_post"]
    print(f"Minutes since last post:       {minutes if minutes is not None else ''}")
    print(f"Min minutes between posts:     {status['min_minutes_between_facebook_posts']}")
    print(f"Allowed now:                   {'yes' if status['allowed_now'] else 'no'}")
    print(f"Next allowed time:             {_format_datetime(status['next_allowed_time'])}")
    if status["reasons"]:
        print(f"Blocked reason:                {'; '.join(status['reasons'])}")
    print("=" * 60)


def run_facebook_limits_status_only():
    """
    Phase 13 command: show Facebook daily and spacing limits.
    Read-only: no Facebook, Blogger, AI, or fetch writes.
    """
    status = get_facebook_limits_status()
    print_facebook_limits_status(status)
    return status


def run_alert_status_only():
    status = telegram_alert_status()
    print("\n" + "=" * 60)
    print("PHASE 17 TELEGRAM ALERT STATUS")
    print("=" * 60)
    print(f"Alerts enabled:       {'yes' if status['enabled'] else 'no'}")
    print(f"Bot token configured: {'yes' if status['bot_token_configured'] else 'no'}")
    print(f"Chat ID configured:   {'yes' if status['chat_id_configured'] else 'no'}")
    print(f"Ready to send alerts: {'yes' if status['ready'] else 'no'}")
    _print_telegram_config_warning(status)
    print("=" * 60)
    return status


def run_notification_status_only():
    status = get_notification_status()
    print("\n" + "=" * 60)
    print("TELEGRAM NOTIFICATION STATUS")
    print("=" * 60)
    print(f"Telegram enabled:              {'yes' if status['telegram_enabled'] else 'no'}")
    print(f"Telegram ready:                {'yes' if status['telegram_ready'] else 'no'}")
    print(f"Last notification time:        {status['last_notification_time']}")
    print(f"Published articles not notified:{status['published_articles_not_notified']}")
    print(f"Facebook posts not notified:   {status['facebook_posts_not_notified']}")
    print("=" * 60)
    return status


def run_telegram_config_check_only():
    status = telegram_alert_status()
    print("\n" + "=" * 60)
    print("TELEGRAM CONFIG CHECK")
    print("=" * 60)
    print(f"TELEGRAM_ALERTS_ENABLED: {'yes' if status['enabled'] else 'no'}")
    print(f"TELEGRAM_BOT_TOKEN configured: {'yes' if status['bot_token_configured'] else 'no'}")
    print(f"TELEGRAM_CHAT_ID configured: {'yes' if status['chat_id_configured'] else 'no'}")
    print(f"Ready to send alerts: {'yes' if status['ready'] else 'no'}")
    _print_telegram_config_warning(status)
    print("=" * 60)
    return status


def run_test_alert_only():
    status = telegram_alert_status()
    if not status["enabled"]:
        print("Telegram alert test skipped: TELEGRAM_ALERTS_ENABLED is not true.")
        return {"sent": False, "skipped": True, "reason": "TELEGRAM_ALERTS_ENABLED is not true"}
    _print_telegram_config_warning(status)

    result = send_telegram_message("✅ Telegram alerts are working.")
    if result.get("sent"):
        print("Telegram alert test sent.")
    elif result.get("skipped"):
        print("Telegram alert test skipped: Telegram alerts are not fully configured.")
    else:
        print(f"Telegram alert test failed: {result.get('reason', 'unknown error')}")
    return result


def run_telegram_debug_only():
    result = telegram_debug_probe()
    print("\n" + "=" * 60)
    print("TELEGRAM DEBUG")
    print("=" * 60)
    print(f"getMe HTTP status:       {result.get('get_me_status') or ''}")
    print(f"sendMessage HTTP status: {result.get('send_message_status') or ''}")
    print(f"Telegram working:        {'yes' if result.get('working') else 'no'}")
    if result.get("reason"):
        print(f"Reason:                  {result['reason']}")
    print("=" * 60)
    return result


def print_facebook_preview(preview):
    print("\n" + "=" * 60)
    print("PHASE 13 FACEBOOK PREVIEW")
    print("=" * 60)
    if not preview.get("available"):
        print(preview.get("error", "No eligible Facebook post preview available."))
        print("=" * 60)
        return

    print(f"Selected style: {preview['selected_style']}")
    print("\nPost text:")
    print(preview["post_text"])
    print("\nHashtags:")
    print(preview["hashtags"])
    print("\nFirst comment:")
    print(preview["first_comment_text"])
    print("\nImage URL used:")
    print(preview["image_url"])
    print("=" * 60)


def run_facebook_preview_only():
    """
    Preview the next Facebook post without calling Facebook, Blogger, or publishing.
    """
    preview = preview_next_facebook_post(include_drafts=True)
    print_facebook_preview(preview)
    return preview


def _env_present(name):
    return bool(str(os.getenv(name, "")).strip())


def _safe_bool_env(name):
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _effective_bool_env(name, default):
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return bool(default)
    return raw.lower() in {"1", "true", "yes", "on"}


def _effective_raw_env(name, default):
    raw = str(os.getenv(name, "")).strip()
    return raw if raw else str(default)


def _json_env_valid(name):
    raw = os.getenv(name, "")
    if not raw.strip():
        return False, "missing"
    try:
        json.loads(raw)
        return True, "valid JSON"
    except json.JSONDecodeError:
        return False, "invalid JSON"


def _json_file_valid(path):
    file_path = Path(path)
    if not file_path.exists():
        return False, "missing"
    try:
        json.loads(file_path.read_text(encoding="utf-8-sig"))
        return True, "valid JSON file"
    except (OSError, json.JSONDecodeError):
        return False, "invalid JSON file"


def run_deployment_check_only():
    """
    Read-only deployment validation for GitHub Actions setup.
    Never prints secret values.
    """
    print("\n" + "=" * 60)
    print("DEPLOYMENT CHECK")
    print("=" * 60)

    ai_provider = str(os.getenv("AI_PROVIDER", "auto")).strip().lower()
    publish_mode = str(os.getenv("PUBLISH_MODE", PUBLISH_MODE)).strip().lower()
    facebook_auto_post = _effective_bool_env("FACEBOOK_AUTO_POST", FACEBOOK_AUTO_POST)
    fast_news_mode = _effective_bool_env("FAST_NEWS_MODE", FAST_NEWS_MODE)
    category_rotation_mode = _effective_bool_env("CATEGORY_ROTATION_MODE", CATEGORY_ROTATION_MODE)
    process_full_category = _effective_bool_env("PROCESS_FULL_CATEGORY_PER_RUN", PROCESS_FULL_CATEGORY_PER_RUN)
    fresh_queue_mode = _effective_bool_env("FRESH_QUEUE_MODE", FRESH_QUEUE_MODE)
    first_valid_mode = _effective_bool_env("FIRST_VALID_ARTICLE_MODE", FIRST_VALID_ARTICLE_MODE)
    recent_news_only = _effective_bool_env("RECENT_NEWS_ONLY", RECENT_NEWS_ONLY)
    allow_unknown_date = _effective_bool_env("ALLOW_UNKNOWN_DATE_IN_FAST_MODE", ALLOW_UNKNOWN_DATE_IN_FAST_MODE)
    first_run_lookback_raw = _effective_raw_env("FALLBACK_FIRST_RUN_LOOKBACK_HOURS", FALLBACK_FIRST_RUN_LOOKBACK_HOURS)
    crawl_interval_raw = _effective_raw_env("CRAWL_INTERVAL_MINUTES", CRAWL_INTERVAL_MINUTES)
    crawl_overlap_raw = _effective_raw_env("CRAWL_OVERLAP_MINUTES", CRAWL_OVERLAP_MINUTES)
    recent_hours_raw = _effective_raw_env("RECENT_NEWS_MAX_AGE_HOURS", RECENT_NEWS_MAX_AGE_HOURS)
    freshness_margin_raw = _effective_raw_env("FRESHNESS_SAFETY_MARGIN_MINUTES", FRESHNESS_SAFETY_MARGIN_MINUTES)
    max_posts_raw = _effective_raw_env("MAX_POSTS_PER_RUN", MAX_POSTS_PER_RUN)
    max_articles_raw = _effective_raw_env("MAX_ARTICLES_PER_RUN", MAX_ARTICLES_PER_RUN)
    max_sources_raw = _effective_raw_env("MAX_SOURCES_PER_RUN", MAX_SOURCES_PER_RUN)
    safe_cycle_max_raw = _effective_raw_env("SAFE_CYCLE_MAX_ARTICLES", SAFE_CYCLE_MAX_ARTICLES)
    try:
        workflow_text = AUTO_CYCLE_WORKFLOW_PATH.read_text(encoding="utf-8-sig")
    except OSError:
        workflow_text = ""
    required_env = [
        "BLOG_ID",
    ]
    if publish_mode == "live" and facebook_auto_post:
        required_env.extend(["FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_ACCESS_TOKEN"])
    if ai_provider == "gemini":
        required_env.append("GEMINI_API_KEY")
    elif ai_provider == "openrouter":
        required_env.append("OPENROUTER_API_KEY")
    elif ai_provider == "openai":
        required_env.append("OPENAI_API_KEY")
    else:
        required_env.extend(["GEMINI_API_KEY", "OPENROUTER_API_KEY"])
    missing = [name for name in required_env if not _env_present(name)]
    if ai_provider == "auto":
        has_any_ai_key = any(
            _env_present(name)
            for name in ("GEMINI_API_KEY", "OPENROUTER_API_KEY")
        )
        missing = [
            name
            for name in missing
            if name not in {"GEMINI_API_KEY", "OPENROUTER_API_KEY"}
        ]
        if not has_any_ai_key:
            missing.append("GEMINI_API_KEY or OPENROUTER_API_KEY")

    print("Required environment variables:")
    for name in required_env:
        if ai_provider == "auto" and name in {"GEMINI_API_KEY", "OPENROUTER_API_KEY"}:
            status = "present" if _env_present(name) else "optional-missing"
        else:
            status = "present" if name not in missing else "MISSING"
        print(f"  - {name}: {status}")
    print(f"AI_PROVIDER: {ai_provider}")

    print(f".env required in GitHub Actions: no")
    print(f".env file currently present: {'yes' if Path('.env').exists() else 'no'}")

    client_env_ok, client_env_status = _json_env_valid("BLOGGER_CLIENT_SECRET_JSON")
    token_env_ok, token_env_status = _json_env_valid("BLOGGER_TOKEN_JSON")
    client_file_ok, client_file_status = _json_file_valid("client_secret.json")
    token_file_ok, token_file_status = _json_file_valid(Path("data") / "token.json")

    print("Blogger OAuth material:")
    print(f"  - BLOGGER_CLIENT_SECRET_JSON: {client_env_status}")
    print(f"  - BLOGGER_TOKEN_JSON: {token_env_status}")
    print(f"  - client_secret.json fallback: {client_file_status}")
    print(f"  - data/token.json fallback: {token_file_status}")

    publish_mode_safe = publish_mode in {"draft", "live"}
    print(f"PUBLISH_MODE: {publish_mode if publish_mode else 'MISSING'}")
    print(f"PUBLISH_MODE value safe: {'yes' if publish_mode_safe else 'no'}")
    safe_mode_env = _effective_bool_env("SAFE_MODE", SAFE_MODE)
    print(f"SAFE_MODE: {'true' if safe_mode_env else 'false'}")
    print(f"FAST_NEWS_MODE: {'true' if fast_news_mode else 'false'}")
    print(f"CATEGORY_ROTATION_MODE: {'true' if category_rotation_mode else 'false'}")
    print(f"PROCESS_FULL_CATEGORY_PER_RUN: {'true' if process_full_category else 'false'}")
    print(f"FRESH_QUEUE_MODE: {'true' if fresh_queue_mode else 'false'}")
    print(f"FIRST_VALID_ARTICLE_MODE: {'true' if first_valid_mode else 'false'}")
    print(f"RECENT_NEWS_ONLY: {'true' if recent_news_only else 'false'}")
    print(f"RECENT_NEWS_MAX_AGE_HOURS: {recent_hours_raw or 'MISSING'}")
    print(f"FRESHNESS_SAFETY_MARGIN_MINUTES: {freshness_margin_raw or 'MISSING'}")
    print(f"MAX_AI_ARTICLE_AGE_HOURS: {MAX_AI_ARTICLE_AGE_HOURS:.2f}")
    print(f"FALLBACK_FIRST_RUN_LOOKBACK_HOURS: {first_run_lookback_raw or 'MISSING'}")
    print(f"CRAWL_INTERVAL_MINUTES: {crawl_interval_raw or 'MISSING'}")
    print(f"CRAWL_OVERLAP_MINUTES: {crawl_overlap_raw or 'MISSING'}")
    print(f"ALLOW_UNKNOWN_DATE_IN_FAST_MODE: {'true' if allow_unknown_date else 'false'}")
    print(f"MAX_POSTS_PER_RUN: {max_posts_raw or 'MISSING'}")
    print(f"MAX_ARTICLES_PER_RUN: {max_articles_raw or 'MISSING'}")
    print(f"MAX_SOURCES_PER_RUN: {max_sources_raw or 'MISSING'}")
    print(f"SAFE_CYCLE_MAX_ARTICLES: {safe_cycle_max_raw or 'MISSING'}")
    workflow_schedule = _workflow_schedule()
    print(f"GitHub Actions workflow: {'present' if AUTO_CYCLE_WORKFLOW_PATH.exists() else 'missing'}")
    print(f"GitHub Actions schedule: {workflow_schedule or 'MISSING'}")
    print(f"GitHub Actions concurrency: {'safe' if 'group: auto-cycle-${{ github.ref }}' in workflow_text and 'cancel-in-progress: false' in workflow_text else 'needs attention'}")
    print(f"GitHub Actions permissions: {'actions+contents write' if 'actions: write' in workflow_text and 'contents: write' in workflow_text else 'needs attention'}")
    print(f"GitHub Actions job timeout: {'15 minutes' if 'timeout-minutes: 15' in workflow_text else 'needs attention'}")
    print(f"GitHub Actions auto-cycle timeout: {'10 minutes' if 'Run auto cycle' in workflow_text and 'timeout-minutes: 10' in workflow_text else 'needs attention'}")
    print(f"GitHub Actions self-trigger: {'present' if 'Self trigger next run' in workflow_text and 'self_trigger' in workflow_text and '/dispatches' in workflow_text else 'missing'}")
    print(f"FACEBOOK_AUTO_POST: {'true' if facebook_auto_post else 'false'}")
    print("FACEBOOK_AUTO_POST value safe: yes")
    print("Telegram alerts:")
    print(f"  - TELEGRAM_ALERTS_ENABLED: {'true' if TELEGRAM_ALERTS_ENABLED else 'false'}")
    print(f"  - TELEGRAM_BOT_TOKEN: {'present' if TELEGRAM_BOT_TOKEN else 'missing'}")
    print(f"  - TELEGRAM_CHAT_ID: {'present' if TELEGRAM_CHAT_ID else 'missing'}")

    limit_names = [
        "SAFE_CYCLE_MAX_ARTICLES",
        "MAX_DRAFTS_PER_DAY",
        "MIN_MINUTES_BETWEEN_DRAFTS",
        "MAX_LIVE_POSTS_PER_DAY",
        "MIN_MINUTES_BETWEEN_LIVE_POSTS",
        "MAX_FACEBOOK_POSTS_PER_DAY",
        "MIN_MINUTES_BETWEEN_FACEBOOK_POSTS",
    ]
    effective_limit_values = {
        "SAFE_CYCLE_MAX_ARTICLES": safe_cycle_max_raw,
        "MAX_DRAFTS_PER_DAY": _effective_raw_env("MAX_DRAFTS_PER_DAY", MAX_DRAFTS_PER_DAY),
        "MIN_MINUTES_BETWEEN_DRAFTS": _effective_raw_env("MIN_MINUTES_BETWEEN_DRAFTS", MIN_MINUTES_BETWEEN_DRAFTS),
        "MAX_LIVE_POSTS_PER_DAY": _effective_raw_env("MAX_LIVE_POSTS_PER_DAY", MAX_LIVE_POSTS_PER_DAY),
        "MIN_MINUTES_BETWEEN_LIVE_POSTS": _effective_raw_env("MIN_MINUTES_BETWEEN_LIVE_POSTS", MIN_MINUTES_BETWEEN_LIVE_POSTS),
        "MAX_FACEBOOK_POSTS_PER_DAY": _effective_raw_env("MAX_FACEBOOK_POSTS_PER_DAY", get_facebook_limits_status().get("max_facebook_posts_per_day", "")),
        "MIN_MINUTES_BETWEEN_FACEBOOK_POSTS": _effective_raw_env("MIN_MINUTES_BETWEEN_FACEBOOK_POSTS", get_facebook_limits_status().get("min_minutes_between_facebook_posts", "")),
    }
    missing_limits = [name for name in limit_names if not str(effective_limit_values.get(name, "")).strip()]
    print("Safety limits:")
    for name in limit_names:
        print(f"  - {name}: {'present' if name not in missing_limits else 'MISSING'}")

    warnings = []
    errors = []
    if missing:
        errors.append("Missing required env var(s): " + ", ".join(missing))
    if not (client_env_ok or client_file_ok):
        errors.append("Missing or invalid Blogger client secret JSON source.")
    if not (token_env_ok or token_file_ok):
        errors.append("Missing or invalid Blogger token JSON source.")
    if not publish_mode_safe:
        errors.append("PUBLISH_MODE must be draft or live.")
    if publish_mode == "live" and facebook_auto_post and missing_limits:
        errors.append("Live Blogger plus Facebook requires all rate-limit variables.")
    if publish_mode == "live":
        warnings.append("PUBLISH_MODE is live. Confirm this is intentional before scheduling.")
        effective_single_post = (
            max_posts_raw == "1"
            or max_articles_raw == "1"
            or safe_cycle_max_raw == "1"
        )
        if safe_mode_env:
            errors.append("PUBLISH_MODE=live requires SAFE_MODE=false.")
        if not fast_news_mode:
            errors.append("Live automation requires FAST_NEWS_MODE=true.")
        if not category_rotation_mode:
            errors.append("Live automation requires CATEGORY_ROTATION_MODE=true.")
        if not process_full_category:
            errors.append("Live automation requires PROCESS_FULL_CATEGORY_PER_RUN=true.")
        if fresh_queue_mode:
            warnings.append("FRESH_QUEUE_MODE=true scans a queue; fastest live mode uses FIRST_VALID_ARTICLE_MODE=true.")
        if not recent_news_only:
            errors.append("Live automation requires RECENT_NEWS_ONLY=true.")
        if allow_unknown_date:
            errors.append("Live automation requires ALLOW_UNKNOWN_DATE_IN_FAST_MODE=false.")
        if recent_hours_raw != "2":
            errors.append("Live automation requires RECENT_NEWS_MAX_AGE_HOURS=2.")
        if freshness_margin_raw != "10":
            errors.append("Live automation requires FRESHNESS_SAFETY_MARGIN_MINUTES=10.")
        if max_sources_raw != "999":
            errors.append("Live automation requires MAX_SOURCES_PER_RUN=999.")
        if first_run_lookback_raw != "6":
            errors.append("Live automation requires FALLBACK_FIRST_RUN_LOOKBACK_HOURS=6.")
        if crawl_interval_raw != "5":
            errors.append("Live automation requires CRAWL_INTERVAL_MINUTES=5.")
        if crawl_overlap_raw != "10":
            errors.append("Live automation requires CRAWL_OVERLAP_MINUTES=10.")
        if effective_limit_values["MAX_LIVE_POSTS_PER_DAY"] != "288":
            errors.append("Live automation requires MAX_LIVE_POSTS_PER_DAY=288.")
        if effective_limit_values["MIN_MINUTES_BETWEEN_LIVE_POSTS"] != "1":
            errors.append("Live automation requires MIN_MINUTES_BETWEEN_LIVE_POSTS=1.")
        if effective_limit_values["MAX_FACEBOOK_POSTS_PER_DAY"] != "288":
            errors.append("Live automation requires MAX_FACEBOOK_POSTS_PER_DAY=288.")
        if effective_limit_values["MIN_MINUTES_BETWEEN_FACEBOOK_POSTS"] != "0":
            errors.append("Live automation requires MIN_MINUTES_BETWEEN_FACEBOOK_POSTS=0.")
        if not effective_single_post:
            errors.append("Live automation requires a one-post limit via MAX_POSTS_PER_RUN=1, MAX_ARTICLES_PER_RUN=1, or SAFE_CYCLE_MAX_ARTICLES=1.")
        if not AUTO_CYCLE_WORKFLOW_PATH.exists():
            errors.append("Missing .github/workflows/auto-cycle.yml.")
        elif workflow_schedule != "*/5 * * * *":
            errors.append("GitHub Actions schedule must be */5 * * * *.")
        if "workflow_dispatch:" not in workflow_text:
            errors.append("GitHub Actions workflow_dispatch must remain enabled.")
        if "group: auto-cycle-${{ github.ref }}" not in workflow_text:
            errors.append("GitHub Actions concurrency group must be auto-cycle-${{ github.ref }}.")
        if "cancel-in-progress: false" not in workflow_text:
            errors.append("GitHub Actions cancel-in-progress must be false.")
        if "actions: write" not in workflow_text or "contents: write" not in workflow_text:
            errors.append("GitHub Actions permissions must include actions: write and contents: write.")
        if "timeout-minutes: 15" not in workflow_text:
            errors.append("GitHub Actions job timeout must be 15 minutes.")
        if "Run auto cycle" not in workflow_text or "timeout-minutes: 10" not in workflow_text:
            errors.append("GitHub Actions auto-cycle step timeout must be 10 minutes.")
        if "Self trigger next run" not in workflow_text or "self_trigger" not in workflow_text or "/dispatches" not in workflow_text:
            errors.append("GitHub Actions workflow must include the self-trigger dispatch step.")
        if "branches:" in workflow_text:
            errors.append("GitHub Actions workflow must not restrict scheduled runs away from main.")
    if facebook_auto_post:
        warnings.append("FACEBOOK_AUTO_POST is true. Confirm Facebook limits before scheduling.")
    if Path(".env").exists():
        warnings.append(".env exists locally; ensure it is never committed.")

    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")

    if errors:
        print("Result: FAILED")
        for error in errors:
            print(f"  - {error}")
        print("=" * 60)
        return {"ok": False, "errors": errors, "warnings": warnings}

    print("Result: OK")
    print("=" * 60)
    return {"ok": True, "errors": [], "warnings": warnings}


def print_facebook_post_summary(result):
    article = result.get("article")
    print("\n" + "=" * 60)
    print("PHASE 12 FACEBOOK POST SUMMARY")
    print("=" * 60)
    print(f"Eligible articles checked: {result.get('checked', 0)}")
    print(f"Posted to Facebook:        {'yes' if result.get('posted') else 'no'}")
    if article:
        print(f"Title:                     {article.get('title', '')}")
        print(f"Blogger URL:               {article.get('blogger_post_url', '')}")
        print(f"Facebook status:           {article.get('facebook_status', '')}")
        print(f"Facebook post ID:          {article.get('facebook_post_id', '')}")
        print(f"Facebook comment ID:       {article.get('facebook_comment_id', '')}")
        print(f"Facebook posted at:        {article.get('facebook_posted_at', '')}")
    if result.get("error"):
        print(f"Error:                     {result['error']}")
    print("=" * 60)


def run_post_facebook_only():
    """
    Phase 12 command: post one latest eligible live Blogger article to Facebook.
    Requires FACEBOOK_AUTO_POST=true and configured Page credentials.
    """
    result = post_one_article_to_facebook()
    print_facebook_post_summary(result)
    return result


def print_facebook_backfill_summary(stats):
    print("\n" + "=" * 60)
    print("FACEBOOK BACKFILL SUMMARY")
    print("=" * 60)
    print(f"Backfill checked:             {stats.get('checked', 0)}")
    print(f"Facebook posts created:       {stats.get('created', 0)}")
    print(f"Facebook failures:            {stats.get('failed', 0)}")
    print(f"First comments created:       {stats.get('comments_created', 0)}")
    print(f"Latest Facebook post ID:      {stats.get('latest_facebook_post_id', '')}")
    print(f"Latest Facebook comment ID:   {stats.get('latest_facebook_comment_id', '')}")
    print(
        "First comment created:        "
        f"{'yes' if stats.get('latest_facebook_comment_id') else 'no'}"
    )
    for result in stats.get("results", []):
        article = result.get("article") or {}
        status = article.get("facebook_status") or ("posted" if result.get("posted") else "failed")
        print("-" * 60)
        print(f"Title:     {article.get('title', '')}")
        print(f"Status:    {status}")
        print(f"Post ID:   {article.get('facebook_post_id', '')}")
        print(f"Comment ID:{article.get('facebook_comment_id', '')}")
        if result.get("error"):
            print(f"Error:     {result.get('error')}")
    print("=" * 60)


def run_facebook_backfill_only():
    """
    Post live Blogger articles that are missing Facebook posts.
    Skips articles that already have a Facebook post ID.
    """
    stats = backfill_facebook_posts()
    print_facebook_backfill_summary(stats)
    return stats


def _print_safe_cycle_final_report(
    article,
    draft_action="",
    draft_result=None,
    target_article_id="",
    stopped_reason="",
    source_warnings_count=0,
    enrichment_failed_count=0,
):
    draft_result = draft_result or {}
    publish_mode = draft_result.get("publishing_mode") or _effective_publish_mode()
    print("\n" + "=" * 60)
    print(f"PHASE 9 {_effective_action()} FINAL REPORT")
    print("=" * 60)
    print(f"Target article ID:      {target_article_id or (article.get('id', '') if article else '')}")
    print(f"Selected article title: {article.get('title', '') if article else ''}")
    print(f"Source:                 {article.get('source_name', '') if article else ''}")
    print(f"Category:               {article.get('suggested_category', '') if article else ''}")
    print(f"Score:                  {article.get('score', '') if article else ''}")
    print(f"Article word count:     {_article_word_count(article)}")
    print(f"AI status:              {article.get('ai_status', '') if article else ''}")
    print(f"AI provider used:       {article.get('ai_provider_used', '') if article else ''}")
    print(f"AI quality status:      {article.get('ai_quality_status', '') if article else ''}")
    print(f"AI quality attempts:    {article.get('ai_quality_attempts', '') if article else ''}")
    print(f"Post created/updated:   {draft_action}")
    print(f"Draft ID:               {article.get('blogger_draft_id', '') if article else ''}")
    print(f"Draft URL:              {article.get('blogger_draft_url', '') if article else ''}")
    print(f"Blogger post ID:        {article.get('blogger_post_id', '') if article else ''}")
    print(f"Blogger post URL:       {article.get('blogger_post_url', '') if article else ''}")
    print(f"Facebook status:        {article.get('facebook_status', '') if article else ''}")
    print(f"Facebook image status:  {article.get('facebook_image_status', '') if article else ''}")
    print(f"Facebook post ID:       {article.get('facebook_post_id', '') if article else ''}")
    print(f"Facebook comment ID:    {article.get('facebook_comment_id', '') if article else ''}")
    print(f"Image count:            {_count_images(article)}")
    print(f"Main image found:       {'yes' if article and article.get('main_image') else 'no'}")
    print(f"Image source type:      {article.get('main_image_source_type', '') if article else ''}")
    print(f"Removed source links:   {article.get('removed_source_links_count', 0) if article else 0}")
    print(f"Internal cache loaded:  {article.get('internal_cache_loaded', 0) if article else 0}")
    print(f"Expired links removed:  {article.get('expired_internal_links_removed', 0) if article else 0}")
    print(f"Internal links added:   {article.get('internal_links_inserted_count', 0) if article else 0}")
    print(f"Trusted links added:    {article.get('external_trusted_links_inserted_count', 0) if article else 0}")
    print(f"Internal cache saved:   {'yes' if article and article.get('internal_cache_saved') else 'no'}")
    print(f"Trusted references:     {_count_trusted_references(article)}")
    print(
        "Source name in HTML:    "
        f"{'yes' if _source_name_in_final_html(article) else 'no'}"
    )
    if draft_result.get("error"):
        print(f"Publish error:          {draft_result['error']}")
    if stopped_reason:
        print(f"Stopped reason:         {stopped_reason}")
    print(f"Source warnings:        {source_warnings_count}")
    print(f"Enrichment failures:    {enrichment_failed_count}")
    print(f"Publishing mode:        {publish_mode.upper()}")
    print("=" * 60)


def run_safe_cycle_only():
    """
    Phase 9 command: run one full one-article workflow.
    SAFE_MODE controls whether the effective publish mode is draft or live.
    """
    publish_mode = _effective_publish_mode()
    action_label = _effective_action()
    if action_label == "LIVE_FRESH_QUEUE":
        cycle_label = "LIVE FRESH QUEUE"
    elif action_label == "LIVE_CATEGORY_ROTATION":
        cycle_label = "LIVE CATEGORY ROTATION"
    elif action_label == "LIVE_FAST_RECENT_NEWS":
        cycle_label = "LIVE FAST RECENT NEWS"
    else:
        cycle_label = "DRAFT/SAFE CYCLE"
    print("\n" + "=" * 60)
    print(f"PHASE 9: {cycle_label}")
    print("=" * 60)
    print("Command: python main.py auto-cycle")
    print(f"Effective action: {action_label}")
    print(f"Publishing mode: {publish_mode.upper()}")
    print(f"Safe mode: {'true' if SAFE_MODE else 'false'}")
    if SAFE_MODE and publish_mode == "live":
        reason = "SAFE_MODE=true refuses live publishing"
        print(reason)
        _print_safe_cycle_final_report(None, stopped_reason=reason)
        notify_auto_cycle_blocked(reason, "")
        return {"completed": False, "reason": reason, "step_reached": "safety-check"}
    if publish_mode != "live":
        print("Live publishing is disabled because PUBLISH_MODE is not exactly live.")
    print(f"Cycle max articles: {SAFE_CYCLE_MAX_ARTICLES}")
    print()

    if publish_mode != "live" and not SAFE_CYCLE_DRAFT_ONLY:
        print("SAFE_CYCLE_DRAFT_ONLY is false. Refusing to run safe-cycle.")
        reason = "SAFE_CYCLE_DRAFT_ONLY is false"
        _print_safe_cycle_final_report(None, stopped_reason=reason)
        return {"completed": False, "reason": reason, "step_reached": "safety-check"}

    if SAFE_CYCLE_MAX_ARTICLES != 1:
        print("SAFE_CYCLE_MAX_ARTICLES must be 1. Refusing to process more than one article.")
        reason = "SAFE_CYCLE_MAX_ARTICLES must be 1"
        _print_safe_cycle_final_report(None, stopped_reason=reason)
        notify_auto_cycle_blocked(reason, "")
        return {"completed": False, "reason": reason, "step_reached": "safety-check"}

    schedule_status = get_publish_schedule_status(mode=publish_mode)
    print_safe_cycle_status(schedule_status)
    if not schedule_status["allowed_now"]:
        reason = "; ".join(schedule_status["reasons"]) or "safe-cycle schedule blocked"
        interval_wait_only = (
            publish_mode == "live"
            and schedule_status.get("reasons") == ["minimum minutes between live posts has not elapsed"]
        )
        if interval_wait_only:
            reason = "Waiting for next publishing window"
        print(f"Cycle stopping cleanly before article selection: {reason}.")
        _print_safe_cycle_final_report(None, draft_result={"error": reason}, stopped_reason=reason)
        if not interval_wait_only:
            notify_auto_cycle_blocked(reason, _format_datetime(schedule_status.get("next_allowed_time")))
        result = {
            "completed": False,
            "reason": reason,
            "schedule": schedule_status,
            "step_reached": "publish-limit-check",
        }
        if interval_wait_only:
            result["skipped"] = True
        return result

    print("\n[1/7] fetch")
    fetch_stats = run_fetch_only()
    source_warnings_count = len(fetch_stats.get("failed_sources") or [])
    zero_link_warnings_count = len(fetch_stats.get("zero_link_sources") or [])
    source_warnings_count += zero_link_warnings_count
    if source_warnings_count:
        print(f"Source warnings recorded: {source_warnings_count}")
    if zero_link_warnings_count:
        print(f"Zero-link source warnings recorded: {zero_link_warnings_count}")
    cleanup_stats = archive_expired_queue_articles()
    if cleanup_stats["expired_archived"] or cleanup_stats["missing_date_archived"]:
        print(
            "Fresh queue cleanup: "
            f"expired={cleanup_stats['expired_archived']} | "
            f"missing_date={cleanup_stats['missing_date_archived']}"
        )
    if (
        not CATEGORY_ROTATION_MODE
        and FAST_NEWS_MODE
        and FIRST_VALID_ARTICLE_MODE
        and RECENT_NEWS_ONLY
        and not fetch_stats.get("first_valid_url")
    ):
        reason = fetch_stats.get("reason") or f"no article in last {RECENT_NEWS_MAX_AGE_HOURS} hours"
        print(f"Live fast recent mode stopping before old queue fallback: {reason}.")
        _print_safe_cycle_final_report(
            None,
            stopped_reason=reason,
            source_warnings_count=source_warnings_count,
            enrichment_failed_count=0,
        )
        notify_auto_cycle_blocked(reason, "")
        return {
            "completed": False,
            "skipped": True,
            "reason": reason,
            "fetch": fetch_stats,
            "source_warnings_count": source_warnings_count,
            "enrichment_failed_count": 0,
            "step_reached": "fetch",
        }

    print("\n[2/7] score")
    score_stats = run_score_only()

    print("\n[3/7] enrich")
    enrich_stats = run_enrich_only(force=False)
    enrichment_failed_count = int(enrich_stats.get("failed") or 0)
    enrichment_weak_count = int(enrich_stats.get("weak") or 0)
    enrichment_failed_count += enrichment_weak_count
    if enrichment_failed_count:
        print(f"Enrichment warnings recorded: {enrichment_failed_count}")
    if enrichment_weak_count:
        print(f"Weak enrichment warnings recorded: {enrichment_weak_count}")

    print("\n[4/7] plan-next --lock")
    selected = None
    plan_result = {}
    if CATEGORY_ROTATION_MODE and PROCESS_FULL_CATEGORY_PER_RUN:
        category_label = fetch_stats.get("selected_category", "")
        selected = _select_newest_fresh_ready_article(
            category_label,
            preferred_ids=fetch_stats.get("queued_candidate_ids", []),
        )
        plan_result = {
            "selected": selected,
            "reason": (
                f"newest fresh queued article in {category_label}"
                if selected
                else f"no fresh queued article ready for {category_label}"
            ),
            "lock": True,
            "eligible_count": 1 if selected else 0,
            "selected_category": category_label,
        }
        if selected:
            print(f"Newest fresh article locked for category: {category_label}.")
    elif FAST_NEWS_MODE and FRESH_QUEUE_MODE:
        selected = _select_oldest_fresh_ready_article()
        plan_result = {
            "selected": selected,
            "reason": "oldest fresh queued article" if selected else "no fresh queued article ready for publishing",
            "lock": True,
            "eligible_count": 1 if selected else 0,
        }
        if selected:
            print("Oldest fresh queued article locked for publishing.")
    elif FAST_NEWS_MODE and FIRST_VALID_ARTICLE_MODE:
        selected = _lock_specific_ready_article(fetch_stats.get("first_valid_url", ""))
        plan_result = {
            "selected": selected,
            "reason": "first valid article fast mode" if selected else "first valid article was not ready after enrichment",
            "lock": True,
            "eligible_count": 1 if selected else 0,
        }
        if selected:
            print("First valid article locked for publishing.")
        else:
            reason = "fresh article from this run was not ready after enrichment"
            print(f"Cycle stopping cleanly: {reason}.")
            _print_safe_cycle_final_report(
                None,
                stopped_reason=reason,
                source_warnings_count=source_warnings_count,
                enrichment_failed_count=enrichment_failed_count,
            )
            notify_auto_cycle_blocked(reason, "")
            return {
                "completed": False,
                "reason": reason,
                "fetch": fetch_stats,
                "score": score_stats,
                "enrich": enrich_stats,
                "plan": plan_result,
                "source_warnings_count": source_warnings_count,
                "enrichment_failed_count": enrichment_failed_count,
                "step_reached": "plan-next",
            }
    if not selected:
        if not (
            (FAST_NEWS_MODE and FRESH_QUEUE_MODE)
            or (CATEGORY_ROTATION_MODE and PROCESS_FULL_CATEGORY_PER_RUN)
        ):
            plan_result = run_plan_next_only(lock=True)
            selected = plan_result.get("selected")
    if not selected:
        no_article_reason = fetch_stats.get("reason") or f"no fresh article in the last {RECENT_NEWS_MAX_AGE_HOURS} hours"
        print(f"Cycle stopping cleanly: {no_article_reason}.")
        _print_safe_cycle_final_report(
            None,
            stopped_reason=no_article_reason,
            source_warnings_count=source_warnings_count,
            enrichment_failed_count=enrichment_failed_count,
        )
        notify_auto_cycle_blocked(no_article_reason, "")
        return {
            "completed": False,
            "skipped": True,
            "reason": no_article_reason,
            "fetch": fetch_stats,
            "score": score_stats,
            "enrich": enrich_stats,
            "plan": plan_result,
            "source_warnings_count": source_warnings_count,
            "enrichment_failed_count": enrichment_failed_count,
            "step_reached": "plan-next",
        }

    selected_id = selected.get("id") or selected.get("url")
    print(f"Target article ID: {selected_id}")

    print("\n[5/7] prepare-ai")
    prepare_stats = prepare_selected_articles_for_ai(target_article_id=selected_id)
    article = _find_article_by_id(selected_id)
    print("\n" + "=" * 60)
    print("PHASE 9 PREPARE-AI STEP SUMMARY")
    print("=" * 60)
    print(f"Target articles checked: {prepare_stats['checked']}")
    print(f"Ready for AI:            {prepare_stats['ready_for_ai']}")
    print(f"Failed:                  {prepare_stats['failed']}")
    print("=" * 60)
    if not article or article.get("processing_status") != "ready_for_ai":
        print("Cycle stopping cleanly: selected article could not be prepared for AI.")
        _print_safe_cycle_final_report(
            article,
            target_article_id=selected_id,
            stopped_reason="prepare-ai failed",
            source_warnings_count=source_warnings_count,
            enrichment_failed_count=enrichment_failed_count,
        )
        return {
            "completed": False,
            "reason": "prepare-ai failed",
            "article": article,
            "fetch": fetch_stats,
            "enrich": enrich_stats,
            "source_warnings_count": source_warnings_count,
            "enrichment_failed_count": enrichment_failed_count,
            "target_article_id": selected_id,
            "step_reached": "prepare-ai",
        }

    print("\n[6/7] run-ai", flush=True)
    print("Starting AI rewrite", flush=True)
    ai_started = time.perf_counter()
    ai_stats = process_one_selected_article_with_ai(target_article_id=selected_id)
    ai_elapsed = time.perf_counter() - ai_started
    print(f"AI finished in {ai_elapsed:.1f}s", flush=True)
    if ai_elapsed > 20:
        print(f"Heartbeat: AI rewrite took {ai_elapsed:.1f}s", flush=True)
    article = _find_article_by_id(selected_id)
    print("\n" + "=" * 60)
    print("PHASE 9 RUN-AI STEP SUMMARY")
    print("=" * 60)
    print(f"Processed articles: {ai_stats['processed']}")
    print(f"Success count:      {ai_stats['success']}")
    print(f"Failed count:       {ai_stats['failed']}")
    print(f"Article word count: {_article_word_count(article)}")
    if ai_stats.get("message"):
        print(f"Message:            {ai_stats['message']}")
    print("=" * 60)

    if not article or article.get("ai_status") != "completed" or not article.get("final_html"):
        print("Cycle stopping cleanly: AI failed or no completed AI output is available.")
        _print_safe_cycle_final_report(
            article,
            target_article_id=selected_id,
            stopped_reason="AI failed",
            source_warnings_count=source_warnings_count,
            enrichment_failed_count=enrichment_failed_count,
        )
        return {
            "completed": False,
            "reason": "AI failed",
            "article": article,
            "ai": ai_stats,
            "fetch": fetch_stats,
            "enrich": enrich_stats,
            "source_warnings_count": source_warnings_count,
            "enrichment_failed_count": enrichment_failed_count,
            "target_article_id": selected_id,
            "step_reached": "run-ai",
        }

    print("\n[7/7] publish", flush=True)
    print("Publishing to Blogger", flush=True)
    publish_started = time.perf_counter()
    draft_result = publish_one_blogger_post(target_article_id=selected_id, mode=publish_mode)
    publish_elapsed = time.perf_counter() - publish_started
    print(f"Blogger publish finished in {publish_elapsed:.1f}s", flush=True)
    if publish_elapsed > 20:
        print(f"Heartbeat: Blogger publish took {publish_elapsed:.1f}s", flush=True)
    article = _find_article_by_id(selected_id)
    draft_action = "none"
    if draft_result.get("updated_existing"):
        draft_action = "updated"
    elif draft_result.get("created_new"):
        draft_action = "created"

    print("\n" + "=" * 60)
    print("PHASE 9 BLOGGER PUBLISH STEP SUMMARY")
    print("=" * 60)
    print(f"Eligible articles checked: {draft_result['checked']}")
    print(f"Duplicate posts found:     {draft_result['duplicate_count']}")
    print(f"Existing post updated:     {'yes' if draft_result['updated_existing'] else 'no'}")
    print(f"New post created:          {'yes' if draft_result['created_new'] else 'no'}")
    if draft_result.get("error"):
        print(f"Error:                     {draft_result['error']}")
    print(f"Publishing mode:           {publish_mode.upper()}")
    print("=" * 60)

    facebook_result = None
    facebook_preview = None
    if (
        FACEBOOK_AUTO_POST
        and publish_mode == "live"
        and article
        and article.get("publish_status") == "published"
        and article.get("blogger_post_url")
    ):
        print("\n[8/8] post-facebook", flush=True)
        print("Posting Facebook", flush=True)
        facebook_result = post_one_article_to_facebook(
            target_article_id=selected_id,
            respect_limits=False,
        )
        print_facebook_post_summary(facebook_result)
        article = _find_article_by_id(selected_id)
    elif publish_mode == "draft":
        print("Facebook: skipped because publishing mode is DRAFT.")
    elif not FACEBOOK_AUTO_POST:
        print("Facebook: skipped because FACEBOOK_AUTO_POST is disabled.")

    print("\n[8/8] facebook-preview")
    facebook_preview = preview_next_facebook_post(target_article_id=selected_id, include_drafts=True)
    print_facebook_preview(facebook_preview)

    stopped_reason = ""
    if draft_action not in {"created", "updated"}:
        stopped_reason = draft_result.get("error", "")

    _print_safe_cycle_final_report(
        article,
        draft_action=draft_action,
        draft_result=draft_result,
        target_article_id=selected_id,
        stopped_reason=stopped_reason,
        source_warnings_count=source_warnings_count,
        enrichment_failed_count=enrichment_failed_count,
    )
    if draft_action in {"created", "updated"} and article and article.get("publish_status") == "published":
        published_set = load_published_ids()
        mark_many_as_published([article.get("url") or article.get("canonical_url")], published_set)
        add_topic_fingerprint(
            topic_signature(
                article.get("seo_title")
                or article.get("fetched_title")
                or article.get("title")
                or ""
            )
        )
        archive_published_queue_article(
            article_id=article.get("id", ""),
            article_url=article.get("url", ""),
        )
        article = _find_article_by_id(selected_id)
    return {
        "completed": draft_action in {"created", "updated"},
        "article": article,
        "fetch": fetch_stats,
        "enrich": enrich_stats,
        "draft": draft_result,
        "draft_action": draft_action,
        "facebook": facebook_result,
        "facebook_preview": facebook_preview,
        "source_warnings_count": source_warnings_count,
        "enrichment_failed_count": enrichment_failed_count,
        "target_article_id": selected_id,
        "step_reached": "publish",
        "reason": stopped_reason,
    }


def run_score_only():
    """
    Phase 2 command: score queued articles only.
    No AI translation, no Blogger API, and no publishing happens here.
    """
    print("\n" + "=" * 60)
    print("PHASE 2: Article filtering and scoring")
    print("=" * 60)
    print("Mode: score only. AI translation and Blogger publishing are disabled.\n")

    stats = score_new_articles()

    print("\n" + "=" * 60)
    print("PHASE 2 SCORE SUMMARY")
    print("=" * 60)
    print(f"Total new articles analyzed: {stats['analyzed']}")
    print(f"Ready articles:              {stats['ready']}")
    print(f"Skipped articles:            {stats['skipped']}")
    print(f"High priority:               {stats['high']}")
    print(f"Medium priority:             {stats['medium']}")
    print(f"Low priority:                {stats['low']}")
    print(f"Total queued articles:       {stats['total_queued']}")
    print("Publishing:                  disabled in Phase 2")
    print("=" * 60)

    return stats


def run_enrich_only(force=False):
    """
    Phase 3 command: enrich ready queued articles only.
    No AI translation, no Blogger API, and no publishing happens here.
    """
    print("\n" + "=" * 60)
    print("PHASE 3: Ready article enrichment")
    print("=" * 60)
    print("Mode: enrich only. AI translation and Blogger publishing are disabled.")
    if force:
        print("Force mode: existing successful enrichments will be refreshed.")
    print()

    stats = enrich_ready_articles(force=force)

    print("\n" + "=" * 60)
    print("PHASE 3 ENRICH SUMMARY")
    print("=" * 60)
    print(f"Ready articles checked:          {stats['checked']}")
    print(f"Successfully enriched:           {stats['enriched']}")
    print(f"Weak enrichments skipped:        {stats.get('weak', 0)}")
    print(f"Failed enrichments:              {stats['failed']}")
    print(f"Already enriched skipped:        {stats['already_enriched']}")
    print(f"Total queued articles:           {stats['total_queued']}")
    print("Publishing:                      disabled in Phase 3")
    print("=" * 60)

    return stats


def run_select_next_only():
    """
    Phase 4 command: select one enriched ready article for the next slot.
    No AI translation, no Blogger API, and no publishing happens here.
    """
    print("\n" + "=" * 60)
    print("PHASE 4: Select next article")
    print("=" * 60)
    print("Mode: select only. AI translation and Blogger publishing are disabled.\n")

    selected = select_next_article()
    if not selected:
        print("No eligible ready enriched articles found.")
        return None

    print("\n" + "=" * 60)
    print("PHASE 4 SELECT SUMMARY")
    print("=" * 60)
    print(f"Selected article title: {selected.get('title', '')}")
    print(f"URL:                    {selected.get('url', '')}")
    print(f"Source:                 {selected.get('source_name', '')}")
    print(f"Score:                  {selected.get('score', '')}")
    print(f"Priority:               {selected.get('priority', '')}")
    print(f"Suggested category:     {selected.get('suggested_category', '')}")
    print(f"Selection reason:       {selected.get('selection_reason', '')}")
    print("Publishing:             disabled in Phase 4")
    print("=" * 60)

    return selected


def run_prepare_ai_only():
    """
    Phase 5 command: prepare selected articles for a future AI step.
    No AI translation, no AI API, no Blogger API, and no publishing happens here.
    """
    print("\n" + "=" * 60)
    print("PHASE 5: Prepare selected article for AI")
    print("=" * 60)
    print("Mode: prepare only. AI APIs and Blogger publishing are disabled.\n")

    stats = prepare_selected_articles_for_ai()

    print("\n" + "=" * 60)
    print("PHASE 5 PREPARE-AI SUMMARY")
    print("=" * 60)
    print(f"Selected articles checked: {stats['checked']}")
    print(f"Ready for AI:              {stats['ready_for_ai']}")
    print(f"Failed:                    {stats['failed']}")

    for article in stats["articles"]:
        package = article.get("ai_input_package", {})
        title = package.get("title") or article.get("fetched_title") or article.get("title", "")
        category = article.get("suggested_category", "")
        preview = article.get("content_preview", "")
        has_image = bool(article.get("main_image"))

        print("-" * 60)
        print(f"Selected article title: {title}")
        print(f"Suggested category:     {category}")
        print(f"Preview length:         {len(preview)}")
        print(f"Main image exists:      {'yes' if has_image else 'no'}")
        if article.get("processing_status") == "failed":
            print(f"Processing error:       {article.get('processing_error', '')}")

    print("Publishing:                 disabled in Phase 5")
    print("=" * 60)

    return stats


def run_ai_only(force=False):
    """
    Phase 6 command: call the configured AI provider for one prepared article.
    No Blogger API and no publishing happens here.
    """
    print("\n" + "=" * 60)
    print("PHASE 6: Run AI article processing")
    print("=" * 60)
    print("Mode: AI processing only. Blogger publishing is disabled.\n")
    if force:
        print("Force mode: selected AI-completed article will be regenerated.\n")

    stats = process_one_selected_article_with_ai(force=force)
    article = stats.get("article")

    print("\n" + "=" * 60)
    print("PHASE 6 RUN-AI SUMMARY")
    print("=" * 60)
    print(f"Processed articles: {stats['processed']}")
    print(f"Success count:      {stats['success']}")
    print(f"Failed count:       {stats['failed']}")

    if article:
        print("-" * 60)
        print(f"Title:       {article.get('seo_title') or article.get('title', '')}")
        print(f"Slug:        {article.get('seo_slug', '')}")
        print(f"HTML length: {len(article.get('final_html', ''))}")
        print(f"Word count:  {_article_word_count(article)}")
        if article.get("ai_status") == "failed":
            print(f"AI error:    {article.get('ai_error', '')}")
    else:
        print(stats.get("message", "No selected ready_for_ai article found."))

    print("Publishing:          disabled in Phase 6")
    print("=" * 60)

    return stats


def run_publish_draft_only():
    """
    Phase 7 command: create one Blogger draft only.
    This must never publish a live post.
    """
    print("\n" + "=" * 60)
    print("PHASE 7: Blogger draft publishing")
    print("=" * 60)
    print("Publishing mode: DRAFT ONLY")
    print("Live publishing is disabled for this command.\n")

    result = publish_one_blogger_draft()
    article = result.get("article")

    print("\n" + "=" * 60)
    print("PHASE 7 DRAFT SUMMARY")
    print("=" * 60)
    print(f"Eligible articles checked: {result['checked']}")
    print(f"Draft created:             {'yes' if result['created'] else 'no'}")

    if article:
        print("-" * 60)
        print(f"Title:      {article.get('seo_title') or article.get('title', '')}")
        print(f"Draft ID:   {article.get('blogger_draft_id', '')}")
        print(f"Draft URL:  {article.get('blogger_draft_url', '')}")
        if not result["created"]:
            print(f"Error:      {article.get('publish_error', result.get('error', ''))}")
    else:
        print(result.get("error", "No eligible selected AI-completed article found."))

    print("Publishing mode: DRAFT ONLY")
    print("=" * 60)

    return result


def run_fix_draft_url_only():
    """
    Update an existing Blogger draft for the current article instead of creating
    another numeric-suffix duplicate. Draft-only, never live publish.
    """
    print("\n" + "=" * 60)
    print("PHASE 7.1: Fix/update Blogger draft URL")
    print("=" * 60)
    print("Publishing mode: DRAFT ONLY")
    print("Live publishing is disabled for this command.\n")

    result = fix_or_update_current_blogger_draft()
    article = result.get("article")

    print("\n" + "=" * 60)
    print("PHASE 7.1 FIX-DRAFT-URL SUMMARY")
    print("=" * 60)
    print(f"Eligible articles checked: {result['checked']}")
    print(f"Duplicate drafts found:    {result['duplicate_count']}")
    if result["duplicate_count"]:
        print("Duplicate draft found. Updating existing draft instead.")
    print(f"Existing draft updated:    {'yes' if result['updated_existing'] else 'no'}")
    print(f"New draft created:         {'yes' if result['created_new'] else 'no'}")

    if article:
        print("-" * 60)
        print(f"Title:           {article.get('seo_title') or article.get('title', '')}")
        print(f"Final draft URL: {article.get('blogger_draft_url', '')}")
        print(f"Draft ID:        {article.get('blogger_draft_id', '')}")
        if result.get("error"):
            print(f"Error:           {result['error']}")
    else:
        print(result.get("error", "No eligible selected/draft_created AI-completed article found."))

    print(f"Slug warning: {result.get('slug_warning', '')}")
    print("Publishing mode: DRAFT ONLY")
    print("=" * 60)

    return result


def run_plan_next_only(lock=False):
    """
    Phase 8 command: plan the next article with category balancing.
    Read-only unless --lock is provided.
    """
    print("\n" + "=" * 60)
    print("PHASE 8: Publishing schedule planner")
    print("=" * 60)
    print("Mode: planning only. No AI, no Blogger, no drafts, no publishing.")
    if lock:
        print("Lock mode: selected candidate will be marked as selected.")
    print()

    result = plan_next_article(lock=lock)
    selected = result.get("selected")

    print("\n" + "=" * 60)
    print("PHASE 8 PLAN-NEXT SUMMARY")
    print("=" * 60)
    print(f"Today total drafts/published: {result['today_total']}")
    print("Count per category:")
    for category, count in result["counts"].items():
        print(f"  - {category}: {count}")
    print(f"Target category for next slot: {result['target_category']}")
    print(f"Eligible candidates:          {result['eligible_count']}")

    if selected:
        print("-" * 60)
        print(f"Selected candidate title: {selected.get('title', '')}")
        print(f"Candidate URL:            {selected.get('url', '')}")
        print(f"Score:                    {selected.get('score', '')}")
        print(f"Priority:                 {selected.get('priority', '')}")
        print(f"Suggested category:       {selected.get('suggested_category', '')}")
        print(f"Reason:                   {result['reason']}")
    else:
        print("No eligible ready enriched articles found.")
        print(f"Reason: {result['reason']}")

    print("=" * 60)
    return result


def run_sources_check_only():
    """
    Phase 8.1 command: validate sources.json only.
    No article fetching, AI processing, Blogger calls, drafts, or publishing.
    """
    print("\n" + "=" * 60)
    print("PHASE 8.1: Sources configuration check")
    print("=" * 60)
    print("Mode: validation only. No fetch, no AI, no Blogger, no drafts.\n")

    result = check_sources_config()

    print("\n" + "=" * 60)
    print("PHASE 8.1 SOURCES-CHECK SUMMARY")
    print("=" * 60)
    print(f"Total sources:    {result['total_sources']}")
    print(f"Enabled sources:  {result['enabled_count']}")
    print(f"Disabled sources: {result['disabled_count']}")
    print("Sources per category:")
    for category, count in result["category_counts"].items():
        print(f"  - {category}: {count}")

    print(f"Duplicate base URLs found: {result['duplicate_count']}")
    for url, names in result["duplicates"].items():
        print(f"  - {url}: {', '.join(names)}")

    print(f"Sources missing required fields: {result['missing_required_count']}")
    for item in result["missing_required"]:
        print(
            f"  - #{item['index']} {item['name']}: "
            f"{', '.join(item['missing_fields'])}"
        )

    print(f"Invalid sources found: {result['invalid_count']}")
    for item in result["invalid_sources"]:
        print(f"  - #{item['index']} {item['name']}: {item['reason']}")

    print("=" * 60)
    return result


def run_fetch_test_problem_sources_only(save=False):
    """
    Phase 8.3 command: test only known problematic sources.
    Read-only by default; saves to article_queue.json only with --save.
    """
    print("\n" + "=" * 60)
    print("PHASE 8.3: Problem source fetch test")
    print("=" * 60)
    print("Mode: fetch coverage test only. No AI, no Blogger, no publishing.")
    if save:
        print("Save mode: discovered links will be added to article_queue.json.")
    else:
        print("Read-only mode: article_queue.json will not be modified.")
    print()

    sources = load_sources()
    problem_sources = [
        source
        for source in sources
        if source.get("name") in PROBLEM_SOURCE_NAMES and source.get("enabled", True)
    ]

    discovery = discover_latest_article_links(problem_sources)
    articles = discovery["articles"]
    source_results = discovery.get("source_results", [])
    queue_stats = None
    if save:
        queue_stats = add_articles_to_queue(articles)

    print("\n" + "=" * 60)
    print("PHASE 8.3 PROBLEM SOURCES SUMMARY")
    print("=" * 60)
    print(f"Problem sources checked: {discovery['checked_sources']}")
    print(f"Articles found:          {len(articles)}")
    if queue_stats:
        print(f"New articles added:      {queue_stats['added']}")
        print(f"Duplicates skipped:      {queue_stats['duplicates']}")
        print(f"Total queued articles:   {queue_stats['total_queued']}")
    else:
        print("Queue modified:          no")

    print("Links found per problem source:")
    for source in source_results:
        tried_feeds = source.get("tried_feed_urls", [])
        print(
            f"  - {source.get('source_name', '')}: "
            f"{source.get('links_found', 0)} link(s), "
            f"method={source.get('method_used', '')}, "
            f"html={source.get('normal_links_found', 0)}, "
            f"feed={source.get('feed_links_found', 0)}"
        )
        if source.get("error"):
            print(f"    error: {source.get('error')}")
        if tried_feeds:
            print(f"    feeds tried: {', '.join(tried_feeds)}")

    remaining_zero = [
        source for source in source_results if source.get("links_found", 0) == 0
    ]
    print(f"Remaining 0-link sources: {len(remaining_zero)}")
    for source in remaining_zero:
        print(f"  - {source.get('source_name', '')} ({source.get('base_url', '')})")

    should_disable = [
        source
        for source in remaining_zero
        if source.get("error")
    ]
    needs_custom_extractor = [
        source
        for source in remaining_zero
        if not source.get("error")
    ]
    print(f"Sources that may need disabling: {len(should_disable)}")
    for source in should_disable:
        print(f"  - {source.get('source_name', '')}: {source.get('error')}")

    print(f"Sources that still need a custom extractor: {len(needs_custom_extractor)}")
    for source in needs_custom_extractor:
        print(f"  - {source.get('source_name', '')}: no usable feed/articles found")

    print("=" * 60)
    return {
        "articles_found": len(articles),
        "source_results": source_results,
        "remaining_zero": remaining_zero,
        "queue_stats": queue_stats,
    }


def run_once():
    stats = {
        "scraped": 0,
        "skipped": 0,
        "backlog_added": 0,
        "selected": 0,
        "deferred": 0,
        "translated": 0,
        "published": 0,
        "publish_target": "unknown",
    }

    if not validate_config():
        print("❌ Cannot continue due to configuration errors.")
        return stats

    print("\n" + "-" * 60)
    print("🔧 INITIALIZING SERVICES")
    print("-" * 60)

    gemini_model = initialize_gemini()
    if not gemini_model:
        print("❌ Could not initialize Gemini AI.")
        return stats

    creds = get_credentials()
    publishing_service = create_blogger_service(creds)
    if not publishing_service:
        print("❌ Could not initialize the publishing service.")
        return stats

    stats["publish_target"] = get_publish_target_name(publishing_service)

    published_set = load_published_ids()
    print(f"📋 Loaded {len(published_set)} previously published article(s) from database.")

    publish_limit = MAX_ARTICLES_PER_RUN or 5
    candidate_limit = get_candidate_fetch_limit(
        publish_limit,
        multiplier=ARTICLE_SELECTION_MULTIPLIER,
        minimum=ARTICLE_SELECTION_POOL_MIN,
    )
    print(
        f"🧺 Collecting up to {candidate_limit or 'all'} candidate article(s) "
        f"to fill the publishing backlog and choose {publish_limit} for this hour."
    )

    articles = get_latest_articles(limit=candidate_limit)
    stats["scraped"] = len(articles)

    if not articles:
        print("\n⚠️  No articles found. Nothing to do this run.")
        return stats

    print("\n" + "=" * 60)
    print("🔍 Checking for duplicates...")
    print("=" * 60)

    new_articles = filter_new_articles(articles, published_set)
    stats["skipped"] = stats["scraped"] - len(new_articles)

    if not new_articles:
        print("\n✅ All fetched articles have already been handled.")
        return stats

    print(f"\n📌 Found {len(new_articles)} new candidate article(s).")

    added, updated = remember_articles(new_articles, published_set)
    stats["backlog_added"] = added
    print(f"🧺 Backlog updated: {added} new, {updated} refreshed.")

    pending_articles = get_pending_articles(published_set)
    if not pending_articles:
        print("\n⚠️  No pending backlog articles are available.")
        return stats

    selected_articles, deferred_articles, ranked_articles = select_best_articles(
        pending_articles,
        limit=publish_limit,
    )
    print_quality_report(selected_articles, deferred_articles, ranked_articles)
    stats["selected"] = len(selected_articles)
    stats["deferred"] = len(deferred_articles)

    if not selected_articles:
        print("\n⚠️  No articles were selected from the backlog.")
        return stats

    translated = process_articles(gemini_model, selected_articles)
    stats["translated"] = len(translated)

    if not translated:
        print("\n⚠️  No articles were successfully translated.")
        return stats

    published = publish_all_articles(publishing_service, translated)
    stats["published"] = len(published)

    if published:
        published_urls = {
            trans["original_url"]
            for trans in translated
            if any(result["title"] == trans["title"] for result in published)
        }
        if published_urls:
            mark_many_as_published(published_urls, published_set)
            marked = mark_backlog_published(published_urls)
            print(f"💾 Saved {len(published_urls)} article reference(s) to the database.")
            print(f"🧺 Marked {marked} backlog article(s) as published.")

    return stats


def main():
    print_banner()
    print_startup_config()

    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        run_fetch_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "auto-cycle":
        run_auto_cycle_logged()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "deployment-check":
        raise SystemExit(0 if run_deployment_check_only().get("ok") else 1)

    if len(sys.argv) > 1 and sys.argv[1] == "health":
        run_health_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "24h-status":
        run_24h_status_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "queue-maintenance":
        run_queue_maintenance_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "reset-state":
        run_reset_state_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "safe-cycle":
        run_safe_cycle_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "safe-cycle-status":
        run_safe_cycle_status_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "publish-status":
        run_publish_status_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "facebook-status":
        run_facebook_status_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "facebook-limits-status":
        run_facebook_limits_status_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "alert-status":
        run_alert_status_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "notification-status":
        run_notification_status_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "telegram-config-check":
        run_telegram_config_check_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "test-alert":
        run_test_alert_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "telegram-debug":
        run_telegram_debug_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "facebook-preview":
        run_facebook_preview_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "post-facebook":
        run_post_facebook_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "facebook-backfill":
        run_facebook_backfill_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "score":
        run_score_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "enrich":
        run_enrich_only(force="--force" in sys.argv[2:])
        return

    if len(sys.argv) > 1 and sys.argv[1] == "select-next":
        run_select_next_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "prepare-ai":
        run_prepare_ai_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "run-ai":
        run_ai_only(force="--force" in sys.argv[2:])
        return

    if len(sys.argv) > 1 and sys.argv[1] == "publish-draft":
        run_publish_draft_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "fix-draft-url":
        run_fix_draft_url_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "plan-next":
        run_plan_next_only(lock="--lock" in sys.argv[2:])
        return

    if len(sys.argv) > 1 and sys.argv[1] == "sources-check":
        run_sources_check_only()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "fetch-test-problem-sources":
        run_fetch_test_problem_sources_only(save="--save" in sys.argv[2:])
        return

    run_loop = "--loop" in sys.argv
    if run_loop:
        print("🔁 CONTINUOUS MODE ENABLED")
        print(f"   The bot will check for new articles every {CHECK_INTERVAL} seconds.")
        print("   Press Ctrl+C to stop.\n")

    total_runs = 0

    while True:
        total_runs += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{'🔥' * 20}")
        print(f"📅 Run #{total_runs} started at: {now}")
        print(f"{'🔥' * 20}\n")

        try:
            stats = run_once()
            print_summary(stats)

            if not run_loop:
                print("\n✅ Done! Bot completed one cycle and is now exiting.")
                break

            print(f"\n⏰ Next check in {CHECK_INTERVAL} seconds...")
            print("   (Press Ctrl+C to stop)\n")
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n🛑 Bot stopped by user.")
            break

        except Exception as e:
            print(f"\n❌ UNEXPECTED ERROR: {e}")
            if not run_loop:
                break
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
