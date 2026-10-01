# ============================================================
# main.py - The Main Orchestrator
# ============================================================

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from article_ai_processor import ai_circuit_status, process_one_selected_article_with_ai
from article_draft_publisher import (
    fix_or_update_current_blogger_draft,
    publish_one_blogger_post,
    publish_one_blogger_draft,
    retry_pending_job_document_renders,
)
from article_queue import (
    add_articles_to_queue,
    article_queue_storage_status,
    archive_expired_queue_articles,
    archive_published_queue_article,
    repair_job_link_bindings,
    load_article_queue,
    load_sources,
    maintain_article_queue,
    mark_article_recent_failure,
    save_article_queue,
)
from article_enricher import enrich_ready_articles
from article_processor import prepare_selected_articles_for_ai, resolve_identity_pending_articles
from article_scorer import score_new_articles
from config import (
    ARTICLE_QUEUE_PATH,
    AI_PROVIDER_MEMORY_PATH,
    CHECK_INTERVAL,
    CRAWL_STATE_PATH,
    FACEBOOK_AUTO_POST,
    JOBS_FACEBOOK_FOLLOW_ARTICLE,
    FACEBOOK_STYLE_MEMORY_PATH,
    INTERNAL_LINK_CACHE_PATH,
    LOGS_DIR,
    MAX_ARTICLES_PER_RUN,
    MAX_DRAFTS_PER_DAY,
    MAX_LIVE_POSTS_PER_DAY,
    MAX_SOURCES_PER_RUN,
    MIN_MINUTES_BETWEEN_DRAFTS,
    MIN_MINUTES_BETWEEN_LIVE_POSTS,
    SAFE_MODE,
    PUBLISH_MODE,
    SAFE_CYCLE_DRAFT_ONLY,
    SAFE_CYCLE_MAX_ARTICLES,
    SOURCE_HEALTH_PATH,
    SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES,
    TARGET_LIVE_POSTS_PER_DAY,
    JOBS_MODE,
    JOBS_MAX_PUBLISH_AGE_HOURS,
    JOBS_AI_CROSS_CANDIDATE_RETRIES,
)
from facebook_publisher import (
    backfill_facebook_posts,
    drain_scheduled_facebook,
    get_facebook_limits_status,
    get_facebook_status,
    preview_next_facebook_post,
    post_one_article_to_facebook,
)
from scraper import discover_latest_article_links
from runtime_state import record_source_cooldown, reset_job_discovery_state
from source_validator import check_sources_config
from production_logging import html_word_count, log_event
from job_core import record_job_publish, select_best_job_from_queue, job_status_snapshot, maintain_job_memory
from jobs_adaptive_controller import record_cycle_result as record_jobs_cycle_result


def _jobs_one_shot_force_run():
    return (
        JOBS_MODE
        and os.getenv("JOBS_ONE_SHOT_FORCE_RUN", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )


def _jobs_one_shot_candidate_articles(articles, attempted_ids=None):
    attempted_ids = {item for item in (attempted_ids or set()) if item}
    candidates = [
        article
        for article in (articles or [])
        if (article.get("id") or article.get("url")) not in attempted_ids
        and not article.get("blogger_post_id")
        and str(article.get("publish_status") or "").strip().lower() != "published"
    ]
    with_documents = [
        article
        for article in candidates
        if any(
            isinstance(item, dict) and str(item.get("url") or "").strip()
            for item in (article.get("job_document_links") or [])
        )
    ]
    return with_documents or candidates


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def print_banner():
    banner = """
╔══════════════════════════════════════════════════════════╗
║                                                          ║
║             💼 JOBS AUTOMATION 💼                 ║
║                                                          ║
║   Jobs → Verify → Arabic Article → Blogger → Facebook   ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
    """
    print(banner)


def _cooldown_sources_after_candidate_failures(category_label, enrich_stats):
    failed_articles = enrich_stats.get("failed_articles") or []
    if not failed_articles:
        return []
    successful_sources = {
        str(article.get("source_url") or "").strip()
        for article in (enrich_stats.get("successful_articles") or [])
        if str(article.get("source_url") or "").strip()
    }
    failures_by_source = defaultdict(list)
    source_names = {}
    for article in failed_articles:
        source_key = str(article.get("source_url") or "").strip()
        if not source_key:
            continue
        failures_by_source[source_key].append(article)
        source_names[source_key] = str(article.get("source_name") or source_key)

    rotated = []
    for source_key, failures in failures_by_source.items():
        if source_key in successful_sources:
            continue
        source_name = source_names.get(source_key, source_key)
        record_source_cooldown(
            source_key,
            source_name=source_name,
            error=f"all candidate enrichments failed ({len(failures)})",
            minutes=SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES,
        )
        log_event(
            "source_cooled_down_after_all_candidates_failed",
            category=category_label,
            source=source_name,
            source_url=source_key,
            failed_candidates=len(failures),
            cooldown_minutes=SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES,
        )
        rotated.append({"source_url": source_key, "source_name": source_name, "failed_candidates": len(failures)})
    return rotated


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
    print("Discovery:          paginated/cursor; fetch_limit_per_run is a page-size hint")

    category_context = {}
    queue_storage = article_queue_storage_status()
    recovery_required = bool(
        not queue_storage.get("valid")
        or queue_storage.get("recovery_required")
    )
    if recovery_required:
        recovery_reason = (
            queue_storage.get("reason")
            or "recovery_reset_after_queue_storage_loss"
        )
        recovery = reset_job_discovery_state(
            reason="recovery_reset_after_queue_storage_loss"
        )
        log_event(
            "job_discovery_state_recovered",
            queue_reason=recovery_reason,
            changed_sources=recovery.get("changed_sources", 0),
            forgotten_ids=recovery.get("forgotten_ids", 0),
        )
        print(
            "Jobs discovery recovery: reset seen IDs/cursors after "
            f"{recovery_reason}."
        )

        # A repaired-but-valid empty queue can carry a one-shot recovery
        # marker. Clear it only after discovery state has been reopened.
        if queue_storage.get("recovery_required"):
            recovery_queue = load_article_queue()
            notifications = recovery_queue.setdefault("notifications", {})
            notifications.pop("queue_recovery_required", None)
            notifications.pop("queue_recovery_reason", None)
            save_article_queue(recovery_queue)

    # Jobs discovery is exhaustive and stateful. Do not route it through
    # recent-news/category first-valid shortcuts that can hide lower listing
    # pages or defer whole sources indefinitely.
    discovery = discover_latest_article_links(enabled_sources)
    articles = (discovery["articles"])
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
        if source.get("status") != "failed"
        and source.get("links_found", 0) == 0
        and not source.get("empty_ok")
    ]

    print("\n" + "=" * 60)
    print("PHASE 1 FETCH SUMMARY")
    print("=" * 60)
    print(f"Sources checked:          {discovery['checked_sources']}")
    if category_context:
        print(f"Selected category:        {category_context.get('selected_category', '')}")
        print(f"Selected source:          {category_context.get('selected_source_name', '')}")
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
    print(f"Known stale Jobs rejected:{queue_stats.get('stale_jobs_rejected', 0)}")
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


def _find_article_by_id(article_id):
    if not article_id:
        return None
    queue = load_article_queue()
    for article in queue.get("articles", []):
        if article_id in {article.get("id"), article.get("url")}:
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
    if SAFE_MODE:
        return "DRAFT"
    return "LIVE" if PUBLISH_MODE == "live" else "FETCH_ONLY" if PUBLISH_MODE == "fetch-only" else "DRAFT"


def print_startup_config():
    print("Jobs-only publishing pipeline", flush=True)
    print(f"Publishing mode: {_effective_action()}", flush=True)
    print(f"Verified publication age: at most {JOBS_MAX_PUBLISH_AGE_HOURS} hours", flush=True)
    print(f"Sources per pass: {MAX_SOURCES_PER_RUN or 'all'}", flush=True)
    print("Discovery → verified job → Blogger → Facebook queue", flush=True)


def _category_label_for_article(article):
    return str(article.get("category_label") or article.get("category_hint") or "jobs").strip()


def get_publish_schedule_status(mode=None, now=None):
    publish_mode = "live" if (mode or _effective_publish_mode()) == "live" else "draft"
    now = now or datetime.now()
    if publish_mode == 'live':
        snapshot = job_status_snapshot(now)
        return {
            "configured_publish_mode": PUBLISH_MODE,
            "publish_mode": publish_mode,
            "posts_today": snapshot["published_today"],
            "drafts_created_today": 0,
            "live_posts_created_today": snapshot["published_today"],
            "max_drafts_per_day": MAX_DRAFTS_PER_DAY,
            "max_live_posts_per_day": snapshot["daily_cap"],
            "target_live_posts_per_day": snapshot["daily_cap"],
            "soft_target_reached": snapshot["published_today"] >= snapshot["daily_cap"],
            "last_draft_time": None,
            "last_live_publish_time": None,
            "minutes_since_last_draft": None,
            "minutes_since_last_live_publish": None,
            "min_minutes_between_drafts": MIN_MINUTES_BETWEEN_DRAFTS,
            "min_minutes_between_live_posts": snapshot.get("min_interval_minutes", 0),
            "allowed_now": bool(snapshot.get("allowed_now", True)),
            "next_allowed_time": snapshot.get("next_allowed_time") or now,
            "reasons": list(snapshot.get("reasons") or []),
            "jobs_policy": snapshot,
        }
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

    interval_blocked = bool(min_minutes > 0 and last_time and interval_next_allowed > now)
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
        "posts_today": created_today,
        "drafts_created_today": len(today_draft_times),
        "live_posts_created_today": len(today_live_times),
        "max_drafts_per_day": MAX_DRAFTS_PER_DAY,
        "max_live_posts_per_day": MAX_LIVE_POSTS_PER_DAY,
        "target_live_posts_per_day": TARGET_LIVE_POSTS_PER_DAY,
        "soft_target_reached": publish_mode == "live" and created_today >= TARGET_LIVE_POSTS_PER_DAY,
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
AUTO_CYCLE_LOG_MAX_RECORDS = 672
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


def _next_auto_cycle_tick(now=None):
    now = now or datetime.now()
    ticks = (1, 7, 13, 19, 25, 31, 37, 43, 49, 55)
    base = now.replace(second=0, microsecond=0)
    for minute in ticks:
        candidate = base.replace(minute=minute)
        if candidate > now:
            return candidate
    return (base.replace(minute=ticks[0]) + timedelta(hours=1))


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
    records = _load_auto_cycle_run_logs(
        limit=max(0, AUTO_CYCLE_LOG_MAX_RECORDS - 1)
    )
    records.append(safe_record)
    temp_path = AUTO_CYCLE_RUN_LOG.with_name(AUTO_CYCLE_RUN_LOG.name + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        for row in records[-AUTO_CYCLE_LOG_MAX_RECORDS:]:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temp_path.replace(AUTO_CYCLE_RUN_LOG)


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


RUNTIME_STATE_PATHS = (
    ARTICLE_QUEUE_PATH,
    CRAWL_STATE_PATH,
    SOURCE_HEALTH_PATH,
    AI_PROVIDER_MEMORY_PATH,
    FACEBOOK_STYLE_MEMORY_PATH,
    INTERNAL_LINK_CACHE_PATH,
    Path("data/job_state.json"),
    Path("data/job_memory"),
    Path("data/job_visual_state.json"),
    Path("data/jobs_adaptive_state.json"),
    Path("data/job_queue_archive"),
)


def _safe_git_command_output(*values):
    text = "\n".join(str(value or "") for value in values if value)
    text = text.replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    text = re.sub(r"https://[^@\s]+@", "https://***@", text)
    text = re.sub(r"(token|secret|password|key)[=:]\s*\S+", r"\1=***", text, flags=re.IGNORECASE)
    return text[:500] or "no git output"


def save_runtime_state_to_git():
    result = {
        "saved": False,
        "git_push_state": "skipped",
        "warning": "",
    }
    if os.getenv("JOBS_RUNTIME_PERSIST_BY_WORKFLOW", "").lower() == "true":
        result["git_push_state"] = "workflow-managed"
        return result
    if os.getenv("GITHUB_ACTIONS", "").strip().lower() != "true":
        result["warning"] = "runtime state git save skipped outside GitHub Actions"
        return result
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
            result["warning"] = f"git commit failed: {_safe_git_command_output(commit.stderr, commit.stdout)}"
            result["git_push_state"] = "warning"
            log_event("runtime_state_git_warning", reason=result["warning"], returncode=commit.returncode)
            return result
        result["saved"] = True
        push = subprocess.run(["git", "push", "origin", "HEAD:main"], check=False, capture_output=True, text=True)
        if push.returncode == 0:
            result["git_push_state"] = "success"
        else:
            result["git_push_state"] = "warning"
            result["warning"] = f"git push failed: {_safe_git_command_output(push.stderr, push.stdout)}"
            log_event("runtime_state_git_warning", reason=result["warning"], returncode=push.returncode)
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
        # The independent Facebook queue still drains when discovery/AI fails.
        # Queue priority (deadline/urgency/score/FIFO) decides which pending
        # Blogger article is promoted; social errors never fail Blogger work.
        if FACEBOOK_AUTO_POST and _effective_publish_mode() == 'live':
            try:
                social = drain_scheduled_facebook()
            except Exception as social_error:
                social = {"created": 0, "failed": 1, "error_type": social_error.__class__.__name__}
                log_event("jobs_social_drain_warning", error_type=social_error.__class__.__name__)
            print("Scheduled Facebook: " + json.dumps(social, ensure_ascii=False), flush=True)
            if isinstance(result, dict):
                result["scheduled_facebook"] = social
        execution_seconds = round(time.perf_counter() - started_timer, 2)
        if isinstance(result, dict):
            result["execution_seconds"] = execution_seconds
            result["lightweight_run"] = SAFE_CYCLE_MAX_ARTICLES == 1 and MAX_ARTICLES_PER_RUN == 1
            result["next_run_expected_at"] = _next_auto_cycle_tick().isoformat(timespec="seconds")
        log_event(
            "auto_cycle_end",
            run_id=run_id,
            status="failed" if error else "completed",
            execution_seconds=execution_seconds,
            error=error,
        )
        print(f"Finished in {execution_seconds:.2f} seconds", flush=True)
        try:
            adaptive_policy = record_jobs_cycle_result(result or {}, error=error or "")
            if isinstance(result, dict):
                result["adaptive_policy"] = adaptive_policy
        except Exception as adaptive_error:
            log_event("jobs_adaptive_state_warning", reason=adaptive_error.__class__.__name__)
        runtime_state_result = save_runtime_state_to_git()
        if isinstance(result, dict):
            result["runtime_state"] = runtime_state_result
        print(
            "Runtime state saved: "
            f"{'yes' if runtime_state_result.get('saved') else 'no'} | "
            f"git push: {runtime_state_result.get('git_push_state')}"
        )
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
    plan = run_plan_next_only(lock=False)
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
    memory_stats = (maintain_job_memory())

    print("\n" + "=" * 60)
    print("QUEUE MAINTENANCE SUMMARY")
    print("=" * 60)
    print(f"Articles checked:           {stats['checked']}")
    print(f"Archived old skipped:       {stats['archived_old_skipped']}")
    print(f"Archived old failed:        {stats['archived_old_failed']}")
    print(f"Archived duplicate URLs:    {stats['archived_duplicate_urls']}")
    print(f"Released legacy logo waits: {stats.get('released_logo_waits', 0)}")
    print(f"Archived stale no-deadline: {stats.get('archived_stale_no_deadline', 0)}")
    if memory_stats:
        print(f"Old campaigns pruned:       {memory_stats.get('campaigns_pruned', 0)}")
    print(f"Already archived:           {stats['already_archived']}")
    print(f"Active queue count:         {stats['active_count']}")
    print(f"Archived count:             {stats['archived_count']}")
    print(f"Total queued records:       {stats['total_queued']}")
    print("Permanent deletion:         disabled")
    print("=" * 60)

    if memory_stats:
        stats["job_memory"] = memory_stats
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
    print(f"Facebook pending queue:             {status.get('facebook_pending_count', 0)}")
    print(f"Expired from social queue only:     {status.get('facebook_expired_count', 0)}")
    print(f"Articles already posted to Facebook:{status['posted_to_facebook']}")
    latest = status.get("latest_eligible")
    if latest:
        print(f"Next Facebook queue title:          {latest.get('title', '')}")
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


def print_facebook_preview(preview):
    print("\n" + "=" * 60)
    print("PHASE 13 FACEBOOK PREVIEW")
    print("=" * 60)
    if not preview.get("available"):
        print(f"Preview status: {preview.get('preview_status', 'unavailable')}")
        print(preview.get("error", "No eligible Facebook post preview available."))
        print("=" * 60)
        return

    print(f"Preview status: {preview.get('preview_status', 'ok')}")
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
    """Check the actual Jobs deployment without network requests or secret output."""
    errors, warnings = [], []
    provider_names = ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "CLOUDFLARE_API_TOKEN")
    if not any(_env_present(name) for name in provider_names):
        warnings.append("No AI key found locally; GitHub Actions injects its configured secrets at runtime.")
    for name in ("BLOGGER_CLIENT_SECRET_JSON", "BLOGGER_TOKEN_JSON"):
        if _env_present(name) and not _json_env_valid(name)[0]:
            errors.append(f"{name} is invalid JSON.")
    workflow = Path(__file__).parent / ".github/workflows/auto-cycle.yml"
    text = workflow.read_text(encoding="utf-8") if workflow.exists() else ""
    for required in ("workflow_dispatch:", "jobs-production-refs/heads/main", "cancel-in-progress: false", "Continue production cycle directly", "Persist Jobs runtime state"):
        if required not in text:
            errors.append(f"Production workflow missing {required}.")
    if ARTICLE_QUEUE_PATH.name != "jobs_article_queue.json":
        errors.append("Only the durable Jobs queue may be used.")
    if not load_sources():
        errors.append("No Jobs sources configured.")
    print("Deployment: Jobs only; runtime credentials supplied by GitHub Actions.")
    for message in warnings:
        print("Warning: " + message)
    for message in errors:
        print("Error: " + message)
    return {"ok": not errors, "errors": errors, "warnings": warnings}


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


def _record_successful_publish(article):
    if not article or article.get("publish_status") != "published":
        return

    record_job_publish(article)

    archive_published_queue_article(
        article_id=article.get("id", ""),
        article_url=article.get("url", ""),
    )


def _mark_candidate_failure_for_retry(article, stage, reason):
    article = article or {}
    if (
        stage == "run-ai"
        and str(article.get("ai_failure_scope") or "").strip().lower() == "retry_backoff"
    ):
        log_event(
            "ai_retry_backoff_preserved",
            article_id=article.get("id"),
            failure_fingerprint=article.get("ai_failure_fingerprint", ""),
            retry_after=article.get("ai_retry_after", ""),
        )
        return article
    cooldown_minutes = SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES
    if stage == "run-ai" and article.get("ai_retry_after"):
        try:
            retry_at = datetime.fromisoformat(
                str(article.get("ai_retry_after")).replace("Z", "+00:00")
            )
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            remaining_seconds = (
                retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)
            ).total_seconds()
            if remaining_seconds > 0:
                cooldown_minutes = max(
                    cooldown_minutes,
                    int((remaining_seconds + 59) // 60),
                )
        except ValueError:
            pass

    failed = mark_article_recent_failure(
        article_id=article.get("id", ""),
        article_url=article.get("url", ""),
        stage=stage,
        reason=reason,
        cooldown_minutes=cooldown_minutes,
    )
    log_event(
        "candidate_skipped_recent_failure",
        article_id=article.get("id"),
        source=article.get("source_name"),
        stage=stage,
        reason=reason,
        retry_after=(failed or {}).get("candidate_retry_after"),
        failure_scope=article.get("ai_failure_scope", "") if stage == "run-ai" else "",
        failure_fingerprint=article.get("ai_failure_fingerprint", "") if stage == "run-ai" else "",
    )
    return failed


def _select_retry_candidate(fetch_stats, attempted_ids):
    attempted_ids = {item for item in (attempted_ids or set()) if item}
    selected = None
    resolve_identity_pending_articles()
    queue = load_article_queue()
    candidate_articles = [
        article for article in queue.get("articles", [])
        if (article.get("id") or article.get("url")) not in attempted_ids
    ]
    if _jobs_one_shot_force_run():
        candidate_articles = _jobs_one_shot_candidate_articles(
            queue.get("articles", []),
            attempted_ids=attempted_ids,
        )
    candidates = {"articles": candidate_articles}
    selected = select_best_job_from_queue(candidates)
    save_article_queue(queue)
    if not selected:
        pending_stats = resolve_identity_pending_articles()
        if pending_stats.get("resolved_ready"):
            queue = load_article_queue()
            candidate_articles = [
                article for article in queue.get("articles", [])
                if (article.get("id") or article.get("url")) not in attempted_ids
            ]
            if _jobs_one_shot_force_run():
                candidate_articles = _jobs_one_shot_candidate_articles(
                    queue.get("articles", []),
                    attempted_ids=attempted_ids,
                )
            candidates = {"articles": candidate_articles}
            selected = select_best_job_from_queue(candidates)
            save_article_queue(queue)
    if selected:
        queue = load_article_queue()
        for article in queue.get("articles", []):
            if (article.get("id") or article.get("url")) != (selected.get("id") or selected.get("url")):
                continue
            article["status"] = "selected"
            article["selected_at"] = datetime.now().isoformat(timespec="seconds")
            selected = article
            break
        save_article_queue(queue)

    if selected and (selected.get("id") or selected.get("url")) in attempted_ids:
        return None
    return selected


def _log_retry_next_candidate(failed_article, next_article, stage, reason):
    if not next_article:
        return
    log_event(
        "candidate_retry_next_source",
        failed_article_id=(failed_article or {}).get("id"),
        failed_source=(failed_article or {}).get("source_name"),
        next_article_id=next_article.get("id"),
        next_source=next_article.get("source_name"),
        stage=stage,
        reason=reason,
    )


def _is_retryable_publish_deferral(reason, article=None):
    """Return True for a clean Blogger retry that should wait for the next cycle."""
    text = str(reason or "").strip().casefold()
    numeric_permalink_retry = (
        "blogger generated a numeric jobs permalink" in text
        and "deleted and will retry" in text
    )
    if not numeric_permalink_retry:
        return False
    if isinstance(article, dict):
        try:
            return int(article.get("permalink_attempt") or 0) > 0
        except (TypeError, ValueError):
            return bool(article.get("blogger_numeric_permalink_rejected"))
    return True


def _retry_after_single_candidate_failure(
    failed_article,
    stage,
    reason,
    publish_mode,
    fetch_stats,
    attempted_ids,
    max_extra_attempts=3,
):
    initial_failure_scope = str(
        (failed_article or {}).get("ai_failure_scope") or ""
    ).strip().lower()

    if stage == "publish" and _is_retryable_publish_deferral(reason, failed_article):
        # Blogger occasionally appends digits to a requested Jobs permalink.
        # The publisher deletes that unwanted post and advances an alphabetic
        # permalink seed. Keep this already-generated article intact and retry it
        # on the next scheduled cycle; rotating through other candidates here
        # wastes AI/publish budget and can push the run into its timeout.
        log_event(
            "blogger_publish_deferred_to_next_cycle",
            article_id=(failed_article or {}).get("id"),
            permalink_attempt=(failed_article or {}).get("permalink_attempt", 0),
            reason=reason,
        )
        return None, []

    # A retry-backoff result means the article was intentionally not sent to
    # any provider. Do not count it as another failure and do not rotate to more
    # candidates in the same cycle.
    if stage == "run-ai" and initial_failure_scope == "retry_backoff":
        log_event(
            "ai_candidate_rotation_stopped",
            failed_article_id=(failed_article or {}).get("id"),
            failure_scope="retry_backoff",
            failure_fingerprint=(failed_article or {}).get("ai_failure_fingerprint", ""),
            retry_after=(failed_article or {}).get("ai_retry_after", ""),
            reason=reason,
        )
        return None, []

    marked_failed = _mark_candidate_failure_for_retry(failed_article, stage, reason)
    retry_results = []
    last_failed = marked_failed or failed_article or {}

    if stage == "run-ai":
        failure_scope = str(
            (last_failed or {}).get("ai_failure_scope")
            or (failed_article or {}).get("ai_failure_scope")
            or ""
        ).strip().lower()
        circuit = (ai_circuit_status())
        if failure_scope in {"global_outage", "cycle_budget"} or circuit.get("global_open"):
            log_event(
                "ai_candidate_rotation_stopped",
                failed_article_id=(failed_article or {}).get("id"),
                failure_scope=failure_scope or "global_outage",
                failure_fingerprint=(
                    (failed_article or {}).get("ai_failure_fingerprint", "")
                    or circuit.get("global_fingerprint", "")
                ),
                retry_after=(
                    (failed_article or {}).get("ai_retry_after", "")
                    or circuit.get("global_retry_after", "")
                ),
                reason=reason,
            )
            return None, retry_results

        # Provider rotation already happens inside one article. After an
        # article-specific AI failure, allow at most one fresh candidate in this
        # cycle so a quality/input problem cannot fan out into four AI jobs.
        max_extra_attempts = min(
            max(0, int(max_extra_attempts or 0)),
            JOBS_AI_CROSS_CANDIDATE_RETRIES,
        )

    for _ in range(max(0, max_extra_attempts)):
        next_selected = _select_retry_candidate(fetch_stats, attempted_ids)
        if not next_selected:
            return None, retry_results
        next_id = next_selected.get("id") or next_selected.get("url")
        attempted_ids.add(next_id)
        _log_retry_next_candidate(last_failed, next_selected, stage, reason)
        item_result = _process_job_target(next_selected, publish_mode)
        retry_results.append(item_result)
        if item_result.get("completed"):
            return item_result, retry_results
        if item_result.get("waiting_for_publish_retry"):
            return None, retry_results
        last_failed = item_result.get("article") or next_selected
    return None, retry_results


def _process_job_target(selected, publish_mode):
    selected_id = selected.get("id") or selected.get("url")
    result = {
        "target_article_id": selected_id,
        "category": _category_label_for_article(selected),
        "source_name": selected.get("source_name", ""),
        "completed": False,
        "reason": "",
    }

    try:
        prepare_stats = prepare_selected_articles_for_ai(target_article_id=selected_id)
        article = _find_article_by_id(selected_id)
    except Exception as error:
        log_event(
            "article_skipped_after_prepare_failure",
            article_id=selected_id,
            reason=error.__class__.__name__,
        )
        _mark_candidate_failure_for_retry(selected, "prepare-ai", str(error))
        result.update({"article": None, "reason": str(error), "step_reached": "prepare-ai"})
        return result
    result["prepare"] = prepare_stats
    if not article or article.get("processing_status") != "ready_for_ai":
        _mark_candidate_failure_for_retry(article or selected, "prepare-ai", "prepare-ai failed")
        result.update({"article": article, "reason": "prepare-ai failed", "step_reached": "prepare-ai"})
        return result

    try:
        ai_stats = process_one_selected_article_with_ai(target_article_id=selected_id)
        article = _find_article_by_id(selected_id)
    except Exception as error:
        log_event(
            "ai_article_skipped_after_ai_failure",
            article_id=selected_id,
            reason=error.__class__.__name__,
        )
        _mark_candidate_failure_for_retry(article or selected, "run-ai", str(error))
        result.update({"article": None, "reason": str(error), "step_reached": "run-ai"})
        return result
    result["ai"] = ai_stats
    if not article or article.get("ai_status") != "completed" or not article.get("final_html"):
        log_event(
            "ai_article_skipped_after_ai_failure",
            article_id=selected_id,
            reason=(ai_stats or {}).get("message") or "AI failed",
        )
        _mark_candidate_failure_for_retry(article or selected, "run-ai", (ai_stats or {}).get("message") or "AI failed")
        result.update({
            "article": article,
            "reason": "AI failed",
            "step_reached": "run-ai",
            "failure_scope": (ai_stats or {}).get("failure_scope") or (article or {}).get("ai_failure_scope", ""),
            "failure_fingerprint": (ai_stats or {}).get("failure_fingerprint") or (article or {}).get("ai_failure_fingerprint", ""),
            "retry_after": (ai_stats or {}).get("retry_after") or (article or {}).get("ai_retry_after", ""),
        })
        return result

    try:
        draft_result = publish_one_blogger_post(target_article_id=selected_id, mode=publish_mode)
        article = _find_article_by_id(selected_id)
    except Exception as error:
        log_event(
            "article_skipped_after_publish_failure",
            article_id=selected_id,
            reason=error.__class__.__name__,
        )
        _mark_candidate_failure_for_retry(article or selected, "publish", str(error))
        result.update({"article": article, "reason": str(error), "step_reached": "publish"})
        return result
    draft_action = "none"
    if draft_result.get("updated_existing"):
        draft_action = "updated"
    elif draft_result.get("created_new"):
        draft_action = "created"
    result.update({"draft": draft_result, "draft_action": draft_action})
    if draft_action not in {"created", "updated"}:
        publish_error = draft_result.get("error") or "Blogger failed"
        if _is_retryable_publish_deferral(publish_error, article or selected):
            result.update(
                {
                    "article": article,
                    "reason": publish_error,
                    "step_reached": "publish",
                    "skipped": True,
                    "waiting_for_publish_retry": True,
                }
            )
            log_event(
                "blogger_publish_deferred_to_next_cycle",
                article_id=(article or selected).get("id"),
                permalink_attempt=(article or selected).get("permalink_attempt", 0),
                reason=publish_error,
            )
            return result
        _mark_candidate_failure_for_retry(article or selected, "publish", publish_error)
        result.update(
            {
                "article": article,
                "reason": publish_error,
                "step_reached": "publish",
            }
        )
        return result

    facebook_result = None
    if (
        FACEBOOK_AUTO_POST
        and publish_mode == "live"
        and article
        and article.get("publish_status") == "published"
        and article.get("blogger_post_url")
    ):
        try:
            facebook_result = post_one_article_to_facebook(
                target_article_id=selected_id,
                respect_limits=True,
            )
            article = _find_article_by_id(selected_id)
        except Exception as error:
            facebook_result = {"posted": False, "error": str(error)}
            log_event(
                "facebook_post_failed_after_blogger_success",
                article_id=selected_id,
                reason=error.__class__.__name__,
            )

    if article and article.get("publish_status") == "published":
        _record_successful_publish(article)
        article = _find_article_by_id(selected_id)

    result.update(
        {
            "completed": True,
            "article": article,
            "facebook": facebook_result,
            "step_reached": "publish",
            "reason": "",
        }
    )
    return result


def run_safe_cycle_only():
    """
    Phase 9 command: run one full one-article workflow.
    SAFE_MODE controls whether the effective publish mode is draft or live.
    """
    publish_mode = _effective_publish_mode()
    action_label = _effective_action()
    cycle_label = "JOBS LIVE CYCLE" if publish_mode == "live" else "JOBS DRAFT CYCLE"
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


    repair_stats = repair_job_link_bindings()
    if repair_stats.get("repaired"):
        print(
            "Jobs link repair: "
            f"repaired={repair_stats['repaired']} | "
            f"reopened_published={repair_stats['reopened_published']} | "
            f"removed_actions={repair_stats['removed_action_links']} | "
            f"removed_documents={repair_stats['removed_document_links']}"
        )

    schedule_status = get_publish_schedule_status(mode=publish_mode)
    if _jobs_one_shot_force_run() and publish_mode == "live":
        original_reasons = list(schedule_status.get("reasons") or [])
        if not schedule_status.get("allowed_now"):
            log_event(
                "jobs_one_shot_publish_pacing_override",
                reasons=" | ".join(original_reasons),
            )
        schedule_status = dict(schedule_status)
        schedule_status["allowed_now"] = True
        schedule_status["reasons"] = []
        schedule_status["one_shot_override"] = True
        schedule_status["original_reasons"] = original_reasons
    print_safe_cycle_status(schedule_status)
    if not schedule_status["allowed_now"]:
        reason = "; ".join(schedule_status["reasons"]) or "safe-cycle schedule blocked"
        interval_wait_only = publish_mode == "live" and any(
            "spacing" in item or "minimum minutes" in item
            for item in schedule_status.get("reasons", [])
        )
        ingest_stats = None
        print(f"Publishing is paced ({reason}); continuing Jobs ingestion.")
        fetch_stats = run_fetch_only()
        cleanup_stats = archive_expired_queue_articles()
        visual_retry_stats = retry_pending_job_document_renders(max_articles=1)
        score_stats = run_score_only()
        enrich_stats = run_enrich_only(force=False)
        identity_stats = resolve_identity_pending_articles()
        ingest_stats = {
            "fetch": fetch_stats,
            "cleanup": cleanup_stats,
            "visual_retry": visual_retry_stats,
            "score": score_stats,
            "enrich": enrich_stats,
            "identity_pending": identity_stats,
        }
        _print_safe_cycle_final_report(None, draft_result={"error": reason}, stopped_reason=reason)
        result = {
            "completed": False,
            "skipped": True,
            "reason": reason,
            "schedule": schedule_status,
            "ingest": ingest_stats,
            "step_reached": "publish-limit-check",
        }
        if interval_wait_only:
            result["waiting_for_window"] = True
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
    visual_retry_stats = (
        (retry_pending_job_document_renders(max_articles=1))
    )
    if cleanup_stats["expired_archived"] or cleanup_stats["missing_date_archived"]:
        print(
            "Fresh queue cleanup: "
            f"expired={cleanup_stats['expired_archived']} | "
            f"missing_date={cleanup_stats['missing_date_archived']}"
        )

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
    _cooldown_sources_after_candidate_failures(
        fetch_stats.get("selected_category", ""),
        enrich_stats,
    )

    identity_stats = {}
    identity_stats = resolve_identity_pending_articles()
    circuit = ai_circuit_status()
    if circuit.get("global_open"):
        reason = (
            "AI circuit open; ingestion/enrichment continued without AI calls "
            f"until {circuit.get('global_retry_after') or 'later'}"
        )
        log_event(
            "ai_cycle_deferred_by_global_circuit",
            failure_fingerprint=circuit.get("global_fingerprint", ""),
            category=circuit.get("global_category", ""),
            retry_after=circuit.get("global_retry_after", ""),
            mode="safe-cycle",
        )
        _print_safe_cycle_final_report(
            None,
            stopped_reason=reason,
            source_warnings_count=source_warnings_count,
            enrichment_failed_count=enrichment_failed_count,
        )
        return {
            "completed": False,
            "skipped": True,
            "reason": reason,
            "fetch": fetch_stats,
            "score": score_stats,
            "enrich": enrich_stats,
            "identity_pending": identity_stats,
            "schedule": schedule_status,
            "waiting_for_ai_circuit": True,
            "ai_circuit": circuit,
            "source_warnings_count": source_warnings_count,
            "enrichment_failed_count": enrichment_failed_count,
            "step_reached": "ai-circuit-check",
        }

    print("\n[4/7] plan-next --lock")
    selected = None
    plan_result = {}
    queue = load_article_queue()
    selection_queue = queue
    if _jobs_one_shot_force_run():
        selection_queue = {
            "articles": _jobs_one_shot_candidate_articles(
                queue.get("articles", [])
            )
        }
    selected = select_best_job_from_queue(selection_queue)
    save_article_queue(queue)

    # A first identity pass can discover a brand-new ambiguous candidate.
    # If no other publishable job exists, gather its official PDF evidence
    # immediately and retry identity once in the same cycle.
    if not selected:
        second_identity_stats = resolve_identity_pending_articles()
        for key, value in second_identity_stats.items():
            if isinstance(value, int):
                identity_stats[key] = int(identity_stats.get(key) or 0) + value
        if second_identity_stats.get("resolved_ready"):
            queue = load_article_queue()
            selection_queue = queue
            if _jobs_one_shot_force_run():
                selection_queue = {
                    "articles": _jobs_one_shot_candidate_articles(
                        queue.get("articles", [])
                    )
                }
            selected = select_best_job_from_queue(selection_queue)
            save_article_queue(queue)

    if selected:
        queue = load_article_queue()
        for article in queue.get("articles", []):
            if (article.get("id") or article.get("url")) != (selected.get("id") or selected.get("url")):
                continue
            article["status"] = "selected"
            article["selected_at"] = datetime.now().isoformat(timespec="seconds")
            article["selection_reason"] = (
                f"jobs quality {article.get('job_score', 0)}/100; "
                f"identity={article.get('job_identity_action', '')}; "
                f"urgency={(article.get('job_urgency') or {}).get('level', 'normal')}"
            )
            selected = article
            break
        save_article_queue(queue)
    plan_result = {
        "selected": selected,
        "reason": selected.get("selection_reason", "") if selected else "no verified job passed quality/identity/daily policy",
        "lock": True,
        "eligible_count": 1 if selected else 0,
    }
    if selected:
        print(
            "Best verified job locked: "
            f"{selected.get('job_company', '')} | {selected.get('job_title') or selected.get('title', '')} | "
            f"score={selected.get('job_score', 0)}"
        )
    if not selected:
        pass
    if not selected:
        no_article_reason = (
            plan_result.get("reason")
            or fetch_stats.get("reason")
            or "no verified job ready after discovery, enrichment and identity checks"
        )
        print(f"Cycle stopping cleanly: {no_article_reason}.")
        _print_safe_cycle_final_report(
            None,
            stopped_reason=no_article_reason,
            source_warnings_count=source_warnings_count,
            enrichment_failed_count=enrichment_failed_count,
        )
        return {
            "completed": False,
            "skipped": True,
            "reason": no_article_reason,
            "fetch": fetch_stats,
            "schedule": schedule_status,
            "score": score_stats,
            "enrich": enrich_stats,
            "plan": plan_result,
            "source_warnings_count": source_warnings_count,
            "enrichment_failed_count": enrichment_failed_count,
            "step_reached": "plan-next",
        }

    selected_id = selected.get("id") or selected.get("url")
    attempted_candidate_ids = {selected_id}
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
        retry_success, retry_results = _retry_after_single_candidate_failure(
            article or selected,
            "prepare-ai",
            "prepare-ai failed",
            publish_mode,
            fetch_stats,
            attempted_candidate_ids,
        )
        if retry_success:
            retry_article = retry_success.get("article")
            _print_safe_cycle_final_report(
                retry_article,
                draft_action=retry_success.get("draft_action", "created"),
                draft_result=retry_success.get("draft"),
                target_article_id=retry_success.get("target_article_id"),
                source_warnings_count=source_warnings_count,
                enrichment_failed_count=enrichment_failed_count,
            )
            return {
                "completed": True,
                "article": retry_article,
                "fetch": fetch_stats,
                "schedule": schedule_status,
                "score": score_stats,
                "enrich": enrich_stats,
                "draft": retry_success.get("draft"),
                "draft_action": retry_success.get("draft_action"),
                "facebook": retry_success.get("facebook"),
                "retry_results": retry_results,
                "source_warnings_count": source_warnings_count,
                "enrichment_failed_count": enrichment_failed_count,
                "target_article_id": retry_success.get("target_article_id"),
                "step_reached": "publish",
                "reason": "",
            }
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
            "schedule": schedule_status,
            "enrich": enrich_stats,
            "retry_results": retry_results,
            "source_warnings_count": source_warnings_count,
            "enrichment_failed_count": enrichment_failed_count,
            "target_article_id": selected_id,
            "step_reached": "prepare-ai",
        }

    print("\n[6/7] run-ai", flush=True)
    print("Starting AI rewrite", flush=True)
    ai_started = time.perf_counter()
    try:
        ai_stats = process_one_selected_article_with_ai(target_article_id=selected_id)
    except Exception as error:
        ai_stats = {"processed": 1, "success": 0, "failed": 1, "message": str(error)}
        log_event(
            "ai_article_skipped_after_ai_failure",
            article_id=selected_id,
            reason=error.__class__.__name__,
        )
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
        retry_success, retry_results = _retry_after_single_candidate_failure(
            article or selected,
            "run-ai",
            ai_stats.get("message") or "AI failed",
            publish_mode,
            fetch_stats,
            attempted_candidate_ids,
        )
        if retry_success:
            retry_article = retry_success.get("article")
            _print_safe_cycle_final_report(
                retry_article,
                draft_action=retry_success.get("draft_action", "created"),
                draft_result=retry_success.get("draft"),
                target_article_id=retry_success.get("target_article_id"),
                source_warnings_count=source_warnings_count,
                enrichment_failed_count=enrichment_failed_count,
            )
            return {
                "completed": True,
                "article": retry_article,
                "ai": ai_stats,
                "fetch": fetch_stats,
                "schedule": schedule_status,
                "score": score_stats,
                "enrich": enrich_stats,
                "draft": retry_success.get("draft"),
                "draft_action": retry_success.get("draft_action"),
                "facebook": retry_success.get("facebook"),
                "retry_results": retry_results,
                "source_warnings_count": source_warnings_count,
                "enrichment_failed_count": enrichment_failed_count,
                "target_article_id": retry_success.get("target_article_id"),
                "step_reached": "publish",
                "reason": "",
            }
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
            "schedule": schedule_status,
            "enrich": enrich_stats,
            "retry_results": retry_results,
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
        print("Promoting the newly published Jobs article", flush=True)
        try:
            facebook_result = post_one_article_to_facebook(
                target_article_id=selected_id,
                respect_limits=True,
            )
        except Exception as social_error:
            facebook_result = {"posted": False, "deferred": True, "error_type": social_error.__class__.__name__}
            log_event("jobs_social_after_publish_warning", article_id=selected_id,
                      error_type=social_error.__class__.__name__)
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
    publish_retry_deferred = False
    if draft_action not in {"created", "updated"}:
        stopped_reason = draft_result.get("error", "")
        publish_retry_deferred = _is_retryable_publish_deferral(
            stopped_reason,
            article or selected,
        )
        retry_success, retry_results = _retry_after_single_candidate_failure(
            article or selected,
            "publish",
            stopped_reason or "Blogger failed",
            publish_mode,
            fetch_stats,
            attempted_candidate_ids,
        )
        if retry_success:
            retry_article = retry_success.get("article")
            _print_safe_cycle_final_report(
                retry_article,
                draft_action=retry_success.get("draft_action", "created"),
                draft_result=retry_success.get("draft"),
                target_article_id=retry_success.get("target_article_id"),
                source_warnings_count=source_warnings_count,
                enrichment_failed_count=enrichment_failed_count,
            )
            return {
                "completed": True,
                "article": retry_article,
                "fetch": fetch_stats,
                "schedule": schedule_status,
                "score": score_stats,
                "enrich": enrich_stats,
                "draft": retry_success.get("draft"),
                "draft_action": retry_success.get("draft_action"),
                "facebook": retry_success.get("facebook"),
                "facebook_preview": facebook_preview,
                "retry_results": retry_results,
                "source_warnings_count": source_warnings_count,
                "enrichment_failed_count": enrichment_failed_count,
                "target_article_id": retry_success.get("target_article_id"),
                "step_reached": "publish",
                "reason": "",
            }
    else:
        retry_results = []

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
        _record_successful_publish(article)
        article = _find_article_by_id(selected_id)
    return {
        "completed": draft_action in {"created", "updated"},
        "skipped": bool(publish_retry_deferred),
        "waiting_for_publish_retry": bool(publish_retry_deferred),
        "article": article,
        "fetch": fetch_stats,
        "schedule": schedule_status,
        "enrich": enrich_stats,
        "draft": draft_result,
        "draft_action": draft_action,
        "facebook": facebook_result,
        "facebook_preview": facebook_preview,
        "retry_results": retry_results,
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
    return run_plan_next_only(lock=True)


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
    """Preview or lock a job through the same identity/freshness gate as production."""
    queue = load_article_queue()
    selected = select_best_job_from_queue(queue)
    if selected and lock:
        selected["status"] = "selected"
        selected["selected_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        selected["selection_reason"] = "verified Jobs priority, freshness and identity"
    if lock:
        save_article_queue(queue)
    result = {"selected": selected, "lock": lock, "eligible_count": int(bool(selected)),
              "reason": "verified job selected" if selected else "no verified fresh job eligible"}
    print(result["reason"])
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


def main():
    """Run only Jobs commands; the default entrypoint is the production cycle."""
    parser = argparse.ArgumentParser(description="Verified Jobs → Blogger → Facebook")
    commands = {
        "fetch": run_fetch_only, "auto-cycle": run_auto_cycle_logged,
        "safe-cycle": run_auto_cycle_logged, "health": run_health_only,
        "24h-status": run_24h_status_only, "queue-maintenance": run_queue_maintenance_only,
        "safe-cycle-status": run_safe_cycle_status_only, "publish-status": run_publish_status_only,
        "facebook-status": run_facebook_status_only, "facebook-limits-status": run_facebook_limits_status_only,
        "facebook-preview": run_facebook_preview_only, "post-facebook": run_post_facebook_only,
        "facebook-backfill": run_facebook_backfill_only, "score": run_score_only,
        "select-next": run_select_next_only, "prepare-ai": run_prepare_ai_only,
        "publish-draft": run_publish_draft_only, "fix-draft-url": run_fix_draft_url_only,
        "sources-check": run_sources_check_only, "deployment-check": run_deployment_check_only,
    }
    parser.add_argument("command", nargs="?", default="auto-cycle", choices=sorted([*commands, "enrich", "run-ai", "plan-next"]))
    parser.add_argument("--force", action="store_true", help="Refresh extraction or AI for the selected job")
    parser.add_argument("--lock", action="store_true", help="Lock the verified Jobs candidate")
    parser.add_argument("--loop", action="store_true", help="Repeat Jobs cycles locally")
    args = parser.parse_args()
    if args.loop and args.command not in {"auto-cycle", "safe-cycle"}:
        parser.error("--loop is supported only for Jobs cycles")
    print_banner()
    print_startup_config()
    if args.command == "enrich":
        return run_enrich_only(force=args.force)
    if args.command == "run-ai":
        return run_ai_only(force=args.force)
    if args.command == "plan-next":
        return run_plan_next_only(lock=args.lock)
    while True:
        try:
            result = commands[args.command]()
            if args.command == "deployment-check":
                raise SystemExit(0 if result.get("ok") else 1)
            if not args.loop:
                if args.command in {"auto-cycle", "safe-cycle"} and not result.get("completed") and not result.get("skipped"):
                    if result.get("step_reached") in {"run-ai", "prepare-ai", "publish", "safety-check"}:
                        raise SystemExit(1)
                return result
        except KeyboardInterrupt:
            return
        except Exception as error:
            if not args.loop:
                raise
            log_event("jobs_loop_retry", error=error.__class__.__name__)
        time.sleep(max(60, CHECK_INTERVAL))


if __name__ == "__main__":
    main()
