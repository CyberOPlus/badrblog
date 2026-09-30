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
    RECENT_NEWS_MAX_AGE_HOURS,
    SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES,
    SOURCES_CONFIG_PATH,
    JOBS_MODE,
)
from duplicate_utils import canonicalize_url, title_hash, topic_signature
from production_logging import log_event
from job_core import (
    _parse_date as _parse_job_date,
    invalidate_identity_evidence,
    is_application_url_bound_to_job,
    is_foreign_job_detail_url,
)

ALLOWED_STATUSES = {"new", "identity_pending", "skipped", "ready", "selected", "draft_created", "published", "failed"}
FRESHNESS_HARD_MAX_HOURS = 24 * 7


def _smart_freshness_hours(max_age_hours=None):
    if max_age_hours is not None:
        return min(FRESHNESS_HARD_MAX_HOURS, max(0, max_age_hours))
    return FRESHNESS_HARD_MAX_HOURS


def _is_no_date_fallback_article(article):
    return str((article or {}).get("freshness_source") or "").strip() == "fallback_no_date"


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


def _as_utc(parsed):
    if not parsed:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _candidate_retry_after(article):
    for field in ("candidate_retry_after", "enrichment_retry_after"):
        parsed = _as_utc(_parse_iso(article.get(field)))
        if parsed:
            return parsed
    return None


def is_candidate_in_recent_failure(article, now=None):
    retry_after = _candidate_retry_after(article or {})
    if not retry_after:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return retry_after > now.astimezone(timezone.utc)


def _retry_after_iso(minutes=None, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    retry_at = now.astimezone(timezone.utc) + timedelta(
        minutes=max(1, int(minutes or SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES or 1))
    )
    return retry_at.isoformat(timespec="seconds").replace("+00:00", "Z")


def _candidate_failure_fingerprint(stage, reason):
    normalized = re.sub(r"https?://\S+", "<url>", str(reason or "").casefold())
    normalized = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", normalized)
    normalized = re.sub(r"\b\d+\b", "<n>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()[:180]
    seed = f"{str(stage or '').strip().casefold()}|{normalized}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def mark_article_recent_failure(article_id="", article_url="", stage="", reason="", cooldown_minutes=None):
    if not article_id and not article_url:
        return None
    queue = load_article_queue()
    matched = None
    failure_fingerprint = _candidate_failure_fingerprint(stage, reason)
    for article in queue.get("articles", []):
        if article_id and article.get("id") != article_id:
            if not article_url or article.get("url") != article_url:
                continue
        elif article_url and article.get("url") != article_url and article.get("id") != article_id:
            continue
        if article.get("status") == "published":
            return article
        if article.get("status") == "selected":
            article["status"] = "ready"

        same_failure = article.get("candidate_failure_fingerprint") == failure_fingerprint
        repeated_count = (
            int(article.get("candidate_failure_repeat_count") or 0) + 1
            if same_failure
            else 1
        )
        base_minutes = max(
            1,
            int(cooldown_minutes or SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES or 1),
        )
        backoff_minutes = min(24 * 60, base_minutes * (2 ** min(repeated_count - 1, 6)))
        retry_after = _retry_after_iso(backoff_minutes)

        article["candidate_retry_after"] = retry_after
        article["candidate_failure_stage"] = stage
        article["candidate_failure_reason"] = str(reason or "")[:300]
        article["candidate_failure_fingerprint"] = failure_fingerprint
        article["candidate_failure_repeat_count"] = repeated_count
        article["candidate_failure_backoff_minutes"] = backoff_minutes
        article["candidate_failed_at"] = _now_iso()
        article["candidate_failure_count"] = int(article.get("candidate_failure_count") or 0) + 1
        matched = article
        break
    if matched:
        save_article_queue(queue)
    return matched


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

    if isinstance(data.get("categories"), list):
        sources = []
        for category in data.get("categories", []):
            category_key = str(category.get("key") or "").strip()
            category_name = str(category.get("name") or category_key).strip()
            category_label = str(category.get("label") or category_name).strip()
            if JOBS_MODE and not (
                category_key.startswith("jobs-")
                or category_label.startswith("jobs-")
                or category_key == "remote-jobs"
                or category_label == "remote-jobs"
            ):
                continue
            for source in category.get("sources", []):
                if not isinstance(source, dict):
                    continue
                normalized = dict(source)
                normalized["category_key"] = category_key
                normalized["category_name"] = category_name
                normalized["category_label"] = category_label
                normalized["category_hint"] = normalized.get("category_hint") or category_label
                sources.append(normalized)
    else:
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
    articles = queue.get("articles", [])
    notifications = queue.get("notifications", {})
    try:
        if ARTICLE_QUEUE_PATH.exists():
            with open(ARTICLE_QUEUE_PATH, "r", encoding="utf-8-sig") as handle:
                existing = json.load(handle)
            if (
                existing.get("articles", []) == articles
                and existing.get("notifications", {}) == notifications
            ):
                return False
    except (json.JSONDecodeError, OSError, TypeError):
        pass

    data = {
        "updated_at": _now_iso(),
        "articles": articles,
        "notifications": notifications,
    }
    ARTICLE_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = ARTICLE_QUEUE_PATH.with_name(ARTICLE_QUEUE_PATH.name + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    temp_path.replace(ARTICLE_QUEUE_PATH)
    return True


def _misfiled_application_document(row):
    url = str((row or {}).get("url") or "").strip().casefold()
    if not url or url.split("?", 1)[0].endswith(".pdf"):
        return False
    text = " ".join(
        str((row or {}).get(key) or "")
        for key in ("label", "context", "url")
    ).casefold()
    apply_hints = (
        "موقع الإيداع", "التقديم", "الترشيح", "إيداع", "ايداع",
        "apply", "postuler", "candidature", "application form",
    )
    doc_hints = (
        "تحميل", "قرار", "مقرر", "اللائحة", "النتائج", "communiqué",
        "communique", "download", "résultat", "resultat",
    )
    return any(h in text for h in apply_hints) and not any(h in text for h in doc_hints)


def _reset_generated_job_content_for_repair(article):
    """Force a clean regeneration while preserving verified source/enrichment facts."""
    generated_fields = (
        "ai_status",
        "ai_processed_at",
        "ai_quality_status",
        "ai_quality_attempts",
        "ai_total_time_seconds",
        "ai_provider_used",
        "ai_error",
        "ai_input_package",
        "processing_status",
        "processing_error",
        "final_html",
        "blogger_article_html",
        "final_word_count",
        "final_html_chars",
        "final_content_hash",
        "publish_error",
        "publish_blocked_reason",
        "candidate_retry_after",
        "candidate_failure_stage",
        "candidate_failure_reason",
        "candidate_failed_at",
    )
    for field in generated_fields:
        article.pop(field, None)
    article["status"] = "ready"
    article["publish_status"] = "repair_pending"
    article["job_link_repair_pending"] = True
    article["archived"] = False
    article.pop("archived_at", None)
    article.pop("archive_reason", None)


def repair_job_link_bindings():
    stats = {
        "checked": 0,
        "repaired": 0,
        "reopened_published": 0,
        "removed_action_links": 0,
        "removed_document_links": 0,
    }
    if not JOBS_MODE:
        return stats
    queue = load_article_queue()
    changed_any = False
    for article in queue.get("articles", []):
        published_record = (
            article.get("status") == "published"
            or article.get("publish_status") == "published"
            or article.get("archive_reason") == "published_to_blogger"
        )
        if article.get("archived") and not published_record:
            continue
        stats["checked"] += 1
        changed = False
        detail_url = str(
            article.get("job_detail_url")
            or article.get("canonical_url")
            or article.get("url")
            or ""
        ).strip()
        application_url = str(article.get("job_application_url") or "").strip()
        if application_url and not is_application_url_bound_to_job(article, application_url):
            if detail_url and is_application_url_bound_to_job(article, detail_url):
                article["job_application_url"] = detail_url
                article["job_application_link_kind"] = "official_job_page"
                article["job_application_is_specific"] = True
            else:
                article["job_application_url"] = ""
                article["job_application_link_kind"] = ""
                article["job_application_is_specific"] = False
            changed = True

        actions=[]
        for row in article.get("job_action_links") or []:
            if not isinstance(row, dict):
                continue
            url=str(row.get("url") or "").strip()
            kind=str(row.get("kind") or "").strip().lower()
            if is_foreign_job_detail_url(article, url):
                stats["removed_action_links"] += 1; changed=True; continue
            if kind=="apply" and not is_application_url_bound_to_job(article, url):
                stats["removed_action_links"] += 1; changed=True; continue
            if kind=="document" and _misfiled_application_document(row):
                stats["removed_action_links"] += 1; changed=True; continue
            actions.append(row)
        if actions != (article.get("job_action_links") or []):
            article["job_action_links"]=actions

        documents=[]
        for row in article.get("job_document_links") or []:
            if not isinstance(row, dict):
                continue
            url=str(row.get("url") or "").strip()
            if is_foreign_job_detail_url(article, url) or _misfiled_application_document(row):
                stats["removed_document_links"] += 1; changed=True; continue
            documents.append(row)
        if documents != (article.get("job_document_links") or []):
            article["job_document_links"]=documents

        repair_pending = bool(article.get("job_link_repair_pending"))
        if not changed and not repair_pending:
            continue

        changed_any=True
        stats["repaired"] += 1
        article["job_link_binding_repaired_at"]=_now_iso()
        article["job_quality_status"]=""
        article["job_quality_reasons"]=[]
        if published_record or repair_pending:
            _reset_generated_job_content_for_repair(article)
            article["job_identity_action"]="update"
            stats["reopened_published"] += 1
    if changed_any:
        save_article_queue(queue)
        log_event("job_link_bindings_repaired", **stats)
    return stats


def _fresh_queue_cutoff(now=None, max_age_hours=None):
    now = now or datetime.now(timezone.utc)
    max_age_hours = _smart_freshness_hours(max_age_hours)
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
        published_at or datetime.min.replace(tzinfo=timezone.utc),
        discovered_at,
        article.get("url", ""),
    )


def is_article_within_fresh_window(article, now=None, max_age_hours=None):
    if JOBS_MODE:
        deadline = _parse_job_date(article.get("job_deadline"))
        if deadline:
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            return deadline >= current.astimezone(timezone.utc)
        return True
    published_at = _source_published_datetime(article)
    if not published_at:
        return True
    return published_at >= _fresh_queue_cutoff(now=now, max_age_hours=max_age_hours)


def article_age_hours(article, now=None):
    published_at = _source_published_datetime(article)
    if not published_at:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - published_at).total_seconds() / 3600)


def is_article_safe_for_ai(article, now=None):
    if JOBS_MODE:
        return is_article_within_fresh_window(article, now=now)
    published_at = _source_published_datetime(article)
    if not published_at:
        return True
    return published_at >= _fresh_queue_cutoff(now=now, max_age_hours=FRESHNESS_HARD_MAX_HOURS)


def _release_legacy_logo_wait(article):
    if not JOBS_MODE or article.get("publish_status") != "waiting_for_logo":
        return False

    article["publish_status"] = "visual_optional_ready"
    article["logo_resolution_status"] = "unavailable_optional"
    article["job_article_cover_status"] = "skipped_missing_verified_logo"
    article["visual_readiness_status"] = "content_ready_visual_optional"
    article["article_logo_used"] = False

    if (
        article.get("ai_status") == "completed"
        and article.get("final_html")
        and article.get("status") not in {"published", "draft_created"}
    ):
        article["status"] = "ready"

    for field in (
        "logo_first_wait_at",
        "logo_retry_after",
        "candidate_retry_after",
        "publish_block_reason",
    ):
        article.pop(field, None)

    if article.get("candidate_failure_stage") == "company-logo":
        article.pop("candidate_failure_stage", None)
        article.pop("candidate_failure_reason", None)
        article.pop("candidate_failed_at", None)

    if "Verified company logo" in str(article.get("publish_error") or ""):
        article.pop("publish_error", None)

    log_event(
        "job_legacy_logo_wait_released",
        article_id=article.get("id"),
        company=article.get("job_company"),
    )
    return True


def archive_expired_queue_articles(now=None, max_age_hours=None):
    queue = load_article_queue()
    articles = queue.get("articles", [])
    archived_at = _now_iso()
    changed = False
    expired = 0
    missing_date = 0

    released_logo_waits = 0

    for article in articles:
        if article.get("archived"):
            continue
        if _release_legacy_logo_wait(article):
            released_logo_waits += 1
            changed = True
        if article.get("status") in {"published", "draft_created"}:
            continue

        published_at = _source_published_datetime(article)
        if not published_at:
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
        "released_logo_waits": released_logo_waits,
        "total_queued": len(articles),
    }


def get_fresh_queue_candidates(statuses=None, now=None, max_age_hours=None):
    queue = load_article_queue()
    articles = queue.get("articles", [])
    statuses = set(statuses or {"ready", "selected"})
    candidates = []
    for article in articles:
        if article.get("archived") or article.get("status") not in statuses:
            continue
        if article.get("content_fetch_status") != "success":
            continue
        if is_candidate_in_recent_failure(article, now=now):
            log_event(
                "candidate_skipped_recent_failure",
                article_id=article.get("id"),
                source=article.get("source_name"),
                stage=article.get("candidate_failure_stage"),
                retry_after=article.get("candidate_retry_after"),
            )
            continue
        if not is_article_within_fresh_window(article, now=now, max_age_hours=max_age_hours):
            continue
        if not is_article_safe_for_ai(article, now=now):
            continue
        candidates.append(article)
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


IDENTITY_EVIDENCE_MERGE_FIELDS = {
    "ats_reference",
    "job_company",
    "job_location",
    "job_deadline",
    "job_published_at",
    "job_application_url",
    "job_application_link_kind",
    "job_number_of_positions",
}


ATS_QUEUE_MERGE_FIELDS = (
    "source_published_at",
    "published_at_source",
    "article_age_hours",
    "rss_summary",
    "freshness_source",
    "freshness_window_hours",
    "source_priority",
    "official_source",
    "source_country",
    "source_eligibility",
    "source_remote",
    "source_visa_sponsorship",
    "ats_provider",
    "ats_reference",
    "ats_description",
    "phenom_payload",
    "job_company",
    "job_location",
    "job_country",
    "job_deadline",
    "job_published_at",
    "job_application_url",
    "job_application_link_kind",
    "job_contract_type",
    "job_salary",
    "job_remote",
    "job_number_of_positions",
)


def _merge_job_discovery_metadata(existing, discovered):
    """Refresh an existing Jobs queue record with structured ATS discovery data."""
    if not JOBS_MODE or not isinstance(existing, dict) or not isinstance(discovered, dict):
        return False
    changed = False
    identity_evidence_changed = False
    for key in ATS_QUEUE_MERGE_FIELDS:
        value = discovered.get(key)
        if value in (None, "", [], {}):
            continue
        # Listing APIs sometimes expose a request/snapshot timestamp as
        # "postedDate". Once a job has a verified published timestamp, keep it
        # stable instead of rewriting the same queue row every scan.
        if key in {"source_published_at", "job_published_at"} and existing.get(key):
            continue
        if key in {"official_source", "source_remote", "source_visa_sponsorship", "job_remote"}:
            value = bool(value)
        if existing.get(key) != value:
            existing[key] = value
            changed = True
            if key in IDENTITY_EVIDENCE_MERGE_FIELDS:
                identity_evidence_changed = True
    if changed:
        if identity_evidence_changed:
            invalidate_identity_evidence(
                existing,
                reason="structured discovery identity facts changed",
            )
        existing["discovery_metadata_refreshed_at"] = _now_iso()
        # A previously failed generic-HTML enrichment should be retried when
        # structured ATS metadata is now available.
        if existing.get("ats_provider") in {"csod", "phenom", "etalent", "workday"}:
            existing.pop("candidate_retry_after", None)
            existing.pop("candidate_failure_stage", None)
            existing.pop("candidate_failure_reason", None)
            existing.pop("candidate_failure_fingerprint", None)
            existing.pop("candidate_failure_repeat_count", None)
            existing.pop("candidate_failure_backoff_minutes", None)
            existing.pop("candidate_failed_at", None)
            existing.pop("content_fetch_error", None)
            if existing.get("content_fetch_status") == "failed":
                existing.pop("content_fetch_status", None)
            if existing.get("status") in {"failed", "skipped"}:
                existing["status"] = "new"
    return changed


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
    existing_by_url = {
        item.get("canonical_url") or canonicalize_url(item.get("url")): item
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
        category_key = article.get("category_key", "")
        category_name = article.get("category_name", "")
        category_label = article.get("category_label", category_hint)

        if not url or not title:
            continue

        if canonical_url in existing_urls:
            existing = existing_by_url.get(canonical_url)
            if existing is not None:
                _merge_job_discovery_metadata(existing, article)
            duplicate_url += 1
            duplicate_by_category[category_hint] += 1
            continue

        if (not JOBS_MODE) and normalized_title_hash and (
            normalized_title_hash in existing_titles
            or normalized_topic_signature in existing_topics
        ):
            duplicate_title += 1
            duplicate_by_category[category_hint] += 1
            continue

        articles.append(
            {
                "id": make_article_id(f"{url}|{_now_iso()}") if JOBS_MODE else make_article_id(url),
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
                "freshness_source": article.get("freshness_source", ""),
                "freshness_window_hours": article.get("freshness_window_hours", ""),
                "category_key": category_key,
                "category_name": category_name,
                "category_label": category_label,
                "category_hint": category_hint,
                "source_priority": article.get("source_priority", ""),
                "official_source": bool(article.get("official_source", False)),
                "source_country": article.get("source_country", ""),
                "source_eligibility": article.get("source_eligibility", ""),
                "source_remote": bool(article.get("source_remote", False)),
                "source_visa_sponsorship": bool(article.get("source_visa_sponsorship", False)),
                "ats_provider": article.get("ats_provider", ""),
                "ats_reference": article.get("ats_reference", ""),
                "ats_description": article.get("ats_description", ""),
                "phenom_payload": article.get("phenom_payload", {}),
                "job_company": article.get("job_company", ""),
                "job_location": article.get("job_location", ""),
                "job_country": article.get("job_country", ""),
                "job_deadline": article.get("job_deadline", ""),
                "job_published_at": article.get("job_published_at", ""),
                "job_application_url": article.get("job_application_url", ""),
                "job_application_link_kind": article.get("job_application_link_kind", ""),
                "job_contract_type": article.get("job_contract_type", ""),
                "job_salary": article.get("job_salary", ""),
                "job_remote": bool(article.get("job_remote", False)),
                "job_number_of_positions": article.get("job_number_of_positions", 0),
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


JOB_ARCHIVE_FIELDS = (
    "id",
    "canonical_url",
    "url",
    "source_name",
    "source_url",
    "title",
    "seo_title",
    "job_title",
    "job_company",
    "job_location",
    "job_country",
    "job_deadline",
    "job_published_at",
    "job_application_url",
    "job_external_reference",
    "ats_reference",
    "job_campaign_id",
    "job_identity_action",
    "job_score",
    "published_at",
    "blogger_post_id",
    "blogger_post_url",
    "desired_slug",
    "facebook_status",
    "facebook_post_id",
    "facebook_comment_id",
    "facebook_posted_at",
    "facebook_selection_reason",
    "ai_provider_used",
    "archived_at",
    "archive_reason",
)


def _job_archive_record(article):
    return {
        key: article.get(key)
        for key in JOB_ARCHIVE_FIELDS
        if article.get(key) not in (None, "", [], {})
    }


def _compact_job_queue_archive(queue, retention_days=7):
    """Move old terminal Jobs records out of the hot queue into slim monthly shards."""
    if not JOBS_MODE:
        return 0

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(7, int(retention_days or 30)))
    archive_dir = ARTICLE_QUEUE_PATH.parent / "data" / "job_queue_archive"
    keep = []
    buckets = {}

    for article in queue.get("articles", []):
        if not article.get("archived"):
            keep.append(article)
            continue

        archived_at = _as_utc(_parse_iso(article.get("archived_at")))
        if not archived_at or archived_at > cutoff:
            keep.append(article)
            continue

        if article.get("publish_status") == "published":
            facebook_status = str(article.get("facebook_status") or "")
            # Preserve unresolved social delivery indefinitely in the hot queue.
            if facebook_status not in {"posted", "not_selected"}:
                keep.append(article)
                continue

        month = archived_at.strftime("%Y-%m")
        key_seed = str(
            article.get("id")
            or article.get("canonical_url")
            or article.get("url")
            or ""
        )
        key = hashlib.sha256(key_seed.encode("utf-8")).hexdigest()[:24]
        buckets.setdefault(month, {})[key] = _job_archive_record(article)

    compacted = sum(len(rows) for rows in buckets.values())
    if not compacted:
        return 0

    archive_dir.mkdir(parents=True, exist_ok=True)
    for month, rows in buckets.items():
        path = archive_dir / f"{month}.json"
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {"version": 1, "records": {}}
        if not isinstance(existing, dict):
            existing = {"version": 1, "records": {}}
        records = existing.setdefault("records", {})
        if not isinstance(records, dict):
            records = {}
            existing["records"] = records
        records.update(rows)
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)

    queue["articles"] = keep
    log_event(
        "job_queue_compacted",
        records=compacted,
        archive_months=len(buckets),
    )
    return compacted


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
        "archived_stale_logo_wait": 0,
        "released_logo_waits": 0,
        "archived_stale_no_deadline": 0,
        "already_archived": 0,
        "active_count": 0,
        "archived_count": 0,
        "total_queued": len(articles),
    }

    for article in articles:
        if article.get("archived"):
            stats["already_archived"] += 1
            continue

        if _release_legacy_logo_wait(article):
            stats["released_logo_waits"] += 1

        if (
            JOBS_MODE
            and article.get("status") in {"new", "ready", "identity_pending"}
            and not article.get("job_deadline")
        ):
            stale_anchor = _as_utc(
                _parse_iso(
                    article.get("discovered_at")
                    or article.get("source_published_at")
                )
            )
            now_utc = datetime.now(timezone.utc)
            if stale_anchor and stale_anchor < now_utc - timedelta(days=60):
                if _archive_article(
                    article,
                    "no_deadline_unpublished_older_than_60_days",
                    archived_at,
                ):
                    stats["archived_stale_no_deadline"] += 1
                continue

        url = str(article.get("url") or "").strip()
        canonical_url = article.get("canonical_url") or canonicalize_url(url)
        if canonical_url and not article.get("canonical_url"):
            article["canonical_url"] = canonical_url
        if article.get("title") and not article.get("title_hash"):
            article["title_hash"] = title_hash(article.get("title"))

        if canonical_url and not JOBS_MODE:
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

    stats["compacted_archived"] = _compact_job_queue_archive(
        queue,
        retention_days=7,
    )
    if stats["compacted_archived"]:
        articles = queue.get("articles", [])
        stats["archived_count"] = sum(
            1 for article in articles if article.get("archived")
        )
        stats["active_count"] = len(articles) - stats["archived_count"]
        stats["total_queued"] = len(articles)

    if (
        stats["archived_old_skipped"]
        or stats["archived_old_failed"]
        or stats["archived_duplicate_urls"]
        or stats["archived_stale_logo_wait"]
        or stats["released_logo_waits"]
        or stats["archived_stale_no_deadline"]
        or stats["compacted_archived"]
    ):
        save_article_queue(queue)

    return stats
