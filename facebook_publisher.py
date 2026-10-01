# ============================================================
# facebook_publisher.py - Phase 12 Facebook Page Auto-Posting
# ============================================================

import hashlib
import re
import json
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlparse
from state_io import atomic_write_json

import requests

from article_queue import load_article_queue, save_article_queue, archive_published_queue_article
from config import (
    FACEBOOK_AUTO_POST,
    FACEBOOK_GRAPH_API_URL,
    FACEBOOK_IMAGE_OUTPUT_DIR,
    FACEBOOK_STYLE_MEMORY_PATH,
    JOB_VISUAL_STATE_PATH,
    FACEBOOK_PAGE_ACCESS_TOKEN,
    FACEBOOK_PAGE_ID,
    WHATSAPP_CHANNEL_URL,
    JOBS_MODE,
    JOBS_FACEBOOK_FOLLOW_ARTICLE,
    JOBS_FACEBOOK_MAX_POSTS_PER_DAY,
    JOBS_FACEBOOK_MIN_INTERVAL_MINUTES,
)
from production_logging import elapsed_ms, log_event
from internal_link_cache import load_internal_link_cache
from job_visual_policy import choose_job_template
from company_logo_resolver import refresh_company_logo, verified_company_logo
from utils.facebook_image_generator import generate_facebook_image
from job_core import (
    facebook_slot_status,
    _local as jobs_local_time,
    _parse_date as parse_job_date,
    classify_urgency,
    job_deadline_time,
    list_active_job_campaign_records,
    record_job_social_state,
)
from social_ai_processor import generate_jobs_facebook_post
JOBS_CAPTION_STYLE = "jobs"

FORBIDDEN_CAPTION_PHRASES = (
    "مقال",
    "اقرأ المزيد",
    "في هذا المقال",
    "اضغط على الرابط",
    "افتح الرابط",
)

FACEBOOK_LINK_MODE_ENFORCED = "comment"


class FacebookDeliveryUncertain(RuntimeError):
    """Remote Facebook outcome is unknown; never auto-retry the same post."""


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _is_configured():
    return bool(FACEBOOK_AUTO_POST and FACEBOOK_PAGE_ID and FACEBOOK_PAGE_ACCESS_TOKEN)


def _has_blogger_live_publish(article):
    return (
        article.get("status") == "published"
        and article.get("publish_status") == "published"
        and bool(_blogger_post_url(article))
    )


def _valid_public_blogger_url(url):
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return ""
    parsed = urlparse(url)
    if not parsed.netloc or parsed.path.strip("/") == "":
        return ""
    return url


def _blogger_post_url(article):
    return _valid_public_blogger_url((article or {}).get("blogger_post_url"))


def _is_jobs_image_generation_failure(error):
    text = re.sub(r"\s+", "", str(error or "").casefold())
    return "jobsfacebookimagegenerationfailed" in text


def _facebook_retry_ready(article, now_epoch=None):
    try:
        retry_after = float(article.get("facebook_retry_after_epoch") or 0)
    except (TypeError, ValueError):
        retry_after = 0
    now_value = float(now_epoch if now_epoch is not None else time.time())
    if retry_after <= now_value:
        return True

    # Rendering happens locally before any Graph request. Older queue entries may
    # carry the former 30m/1h exponential delay, which only postpones a fix that
    # is already deployed and creates no Facebook API pressure. Recompute those
    # legacy local-render retries from their recorded failure time and use the
    # normal Jobs social pacing instead. Graph/API failures keep their original
    # longer backoff below.
    if _is_jobs_image_generation_failure(article.get("facebook_error")):
        try:
            previous_delay = float(article.get("facebook_retry_delay_seconds") or 0)
        except (TypeError, ValueError):
            previous_delay = 0
        if previous_delay > 0:
            failed_at = retry_after - previous_delay
            local_delay = max(5 * 60, int(JOBS_FACEBOOK_MIN_INTERVAL_MINUTES) * 60)
            return failed_at + local_delay <= now_value
    return False


def _facebook_comment_retry_ready(article, now_epoch=None):
    try:
        retry_after = float(article.get("facebook_comment_retry_after_epoch") or 0)
    except (TypeError, ValueError):
        retry_after = 0
    return retry_after <= float(now_epoch if now_epoch is not None else time.time())


def _job_facebook_expired(article, now=None):
    return classify_urgency(article, now=now).get("level") == "expired"


def _mark_facebook_pending(article, now=None, reason="published_to_blogger"):
    if not _has_blogger_live_publish(article) or article.get('facebook_post_id'):
        return False
    if _job_facebook_expired(article, now=now):
        return _mark_facebook_expired(article, now=now)

    current = str(article.get("facebook_status") or "").strip()
    if current in {
        "posted",
        "posted_comment_failed",
        "posted_comment_uncertain",
        "delivery_uncertain",
    }:
        return False
    if current == "failed" and not _facebook_retry_ready(article):
        return False

    changed = current != "facebook_pending"
    article["facebook_status"] = "facebook_pending"
    article["facebook_queued_at"] = (
        article.get("facebook_queued_at")
        or (now.isoformat() if hasattr(now, "isoformat") else _now_iso())
    )
    article["facebook_queue_reason"] = str(reason or "published_to_blogger")
    # A live Blogger article awaiting Facebook must not remain in the generic
    # published archive; archived items can be pruned before social backfill.
    for key in ("archived", "archived_at", "archive_reason", "archive_deferred_reason", "archive_deferred_at"):
        article.pop(key, None)
    article.pop("facebook_selection_reason", None)
    article.pop("facebook_error", None)
    article.pop("facebook_expired_at", None)
    article.pop("facebook_expired_reason", None)
    if current in {"failed", "not_selected", "facebook_expired"}:
        # Once a failed item is explicitly re-opened, its old cooldown no longer
        # belongs to the pending state. Keeping retry_after here can make the
        # item immediately ineligible again (especially after a renderer fix).
        _clear_facebook_failure_state(article)
    if changed:
        _persist_jobs_social_state(article)
    return changed


def _mark_facebook_expired(article, now=None):
    if article.get('facebook_post_id'):
        return False
    current = str(article.get("facebook_status") or "").strip()
    changed = current != "facebook_expired"
    article["facebook_status"] = "facebook_expired"
    article["facebook_expired_at"] = (
        now.isoformat() if hasattr(now, "isoformat") else _now_iso()
    )
    article["facebook_expired_reason"] = "job expired before Facebook queue turn"
    article.pop("facebook_selection_reason", None)
    article.pop("facebook_error", None)
    _clear_facebook_failure_state(article)
    if changed:
        _persist_jobs_social_state(article)
    return changed


def _persist_jobs_social_state(article):
    if not isinstance(article, dict):
        return {}
    try:
        return record_job_social_state(article)
    except Exception as error:
        log_event(
            "facebook_social_state_persist_failed",
            article_id=article.get("id"),
            error=error.__class__.__name__,
        )
        return {}


def _recovered_application_link_kind(record):
    """Recover only explicit/strong application semantics from durable memory."""
    stored = str((record or {}).get("application_link_kind") or "").strip().lower()
    if stored:
        return stored

    url = str((record or {}).get("application_url") or "").strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""

    path = str(parsed.path or "").casefold().rstrip("/")
    # Legacy records predate application_link_kind persistence. Only classify
    # unmistakable job-bound application endpoints as direct apply; ordinary
    # detail pages must not receive the red Apply visual.
    if (
        path.endswith("/job/login")
        or path.endswith("/job/apply")
        or path.endswith("/apply")
        or "/apply/" in path
    ):
        return "direct_apply"
    return "official_job_page"


def _recover_jobs_facebook_queue_from_memory(queue, now=None, max_age_days=30):
    """Recover Blogger-published Jobs that vanished from the volatile queue."""

    now = now or datetime.now(timezone.utc)
    articles = queue.setdefault("articles", [])
    existing_urls = {
        str(article.get("blogger_post_url") or "").strip()
        for article in articles
        if str(article.get("blogger_post_url") or "").strip()
    }
    existing_campaigns = {
        str(article.get("job_campaign_id") or "").strip()
        for article in articles
        if str(article.get("job_campaign_id") or "").strip()
    }

    try:
        cache, _stats = load_internal_link_cache(save=False)
        cache_by_url = {
            str(entry.get("url") or "").strip(): entry
            for entry in (cache.get("links") or [])
            if isinstance(entry, dict) and str(entry.get("url") or "").strip()
        }
    except Exception:
        cache_by_url = {}

    recovered = 0
    skipped_terminal = 0
    for record in list_active_job_campaign_records():
        blogger_url = str(record.get("blogger_url") or "").strip()
        campaign_id = str(record.get("campaign_id") or "").strip()
        if not blogger_url or not campaign_id:
            continue
        if blogger_url in existing_urls or campaign_id in existing_campaigns:
            continue

        facebook_status = str(record.get("facebook_status") or "").strip()
        facebook_post_id = str(record.get("facebook_post_id") or "").strip()
        facebook_comment_id = str(record.get("facebook_comment_id") or "").strip()

        if facebook_post_id and facebook_comment_id and facebook_status == "posted":
            skipped_terminal += 1
            continue
        if facebook_status == "facebook_expired":
            skipped_terminal += 1
            continue

        updated_at = parse_job_date(record.get("updated_at"))
        if (
            not facebook_post_id
            and updated_at
            and (now - updated_at).total_seconds() > max(1, int(max_age_days)) * 86400
        ):
            continue

        cache_entry = cache_by_url.get(blogger_url) or {}
        source_url = str(record.get("source_url") or "").strip()
        stable_seed = blogger_url or source_url or campaign_id
        article_id = hashlib.sha1(stable_seed.encode("utf-8")).hexdigest()[:16]

        article = {
            "id": article_id,
            "url": source_url or blogger_url,
            "canonical_url": source_url or blogger_url,
            "source_url": source_url,
            "source_name": record.get("source_name", ""),
            "source_priority": record.get("source_priority", ""),
            "status": "published",
            "publish_status": "published",
            "published_at": cache_entry.get("published_at") or record.get("updated_at") or _now_iso(),
            "blogger_post_id": record.get("blogger_post_id", ""),
            "blogger_post_url": blogger_url,
            "seo_title": cache_entry.get("title") or record.get("title") or "",
            "title": cache_entry.get("title") or record.get("title") or "",
            "suggested_category": cache_entry.get("category") or "jobs-morocco",
            "job_campaign_id": campaign_id,
            "job_company": record.get("company", ""),
            "job_title": record.get("title", ""),
            "company_logo_url": record.get("company_logo_url", ""),
            "company_logo_verified": bool(record.get("company_logo_verified")),
            "company_logo_confidence": int(record.get("company_logo_confidence") or 0),
            "company_logo_source": record.get("company_logo_source", ""),
            "company_official_domain": record.get("company_official_domain", ""),
            "company_logo_checksum": record.get("company_logo_checksum", ""),
            "job_location": record.get("location", ""),
            "job_deadline": record.get("deadline", ""),
            "job_number_of_positions": record.get("number_of_positions", 0),
            "job_salary": record.get("salary", ""),
            "job_contract_type": record.get("contract_type", ""),
            "job_application_url": record.get("application_url", ""),
            "job_application_link_kind": _recovered_application_link_kind(record),
            "job_application_is_specific": bool(record.get("application_is_specific")),
            "job_notice_type": record.get("notice_type") or "vacancy",
            "job_notice_status": record.get("notice_status", ""),
            "job_external_reference": record.get("external_reference", ""),
            "facebook_status": facebook_status or "facebook_pending",
            "facebook_post_id": facebook_post_id,
            "facebook_posted_at": record.get("facebook_posted_at", ""),
            "facebook_comment_id": facebook_comment_id,
            "facebook_queued_at": record.get("facebook_queued_at") or record.get("updated_at") or _now_iso(),
            "facebook_retry_after_epoch": record.get("facebook_retry_after_epoch") or 0,
            "facebook_comment_retry_after_epoch": record.get("facebook_comment_retry_after_epoch") or 0,
            "facebook_delivery_uncertain_at": record.get("facebook_delivery_uncertain_at", ""),
            "facebook_image_status": record.get("facebook_image_status", ""),
            "facebook_error": record.get("facebook_error", ""),
            "facebook_queue_reason": "campaign_memory_recovery",
            "facebook_queue_recovered": True,
        }
        articles.append(article)
        # Recovery itself is a durable social-state transition. Persist the
        # reconstructed pending/retry state immediately so a second queue loss
        # cannot erase the fact that this Blogger post still needs Facebook.
        _persist_jobs_social_state(article)
        existing_urls.add(blogger_url)
        existing_campaigns.add(campaign_id)
        recovered += 1

    if recovered:
        log_event(
            "facebook_jobs_queue_recovered",
            recovered=recovered,
            queue_size=len(articles),
        )
    return {"recovered": recovered, "skipped_terminal": skipped_terminal}


def _sync_jobs_facebook_queue(queue, now=None):
    """Migrate every live unpublished Jobs article into the real Facebook queue."""

    now = now or datetime.now(timezone.utc)
    recovery = _recover_jobs_facebook_queue_from_memory(queue, now=now)
    recovered = int(recovery.get("recovered") or 0)
    queued = 0
    expired = 0
    revived = 0
    changed = bool(recovered)

    for article in queue.get("articles", []):
        if not _has_blogger_live_publish(article) or article.get("facebook_post_id"):
            continue

        current = str(article.get("facebook_status") or "").strip()
        if _job_facebook_expired(article, now=now):
            if _mark_facebook_expired(article, now=now):
                expired += 1
                changed = True
            continue

        if current == "facebook_expired":
            revived += 1

        # Legacy selective-policy records are deliberately re-opened. A real
        # Blogger job must not remain buried because its old score was low.
        if current in {"", "not_selected", "facebook_expired"} or current is None:
            if _mark_facebook_pending(
                article,
                now=now,
                reason="legacy_requeued" if current in {"not_selected", "facebook_expired"} else "published_to_blogger",
            ):
                queued += 1
                changed = True
        elif current == "failed" and _facebook_retry_ready(article):
            if _mark_facebook_pending(article, now=now, reason="retry_ready"):
                queued += 1
                changed = True

    if changed:
        save_article_queue(queue)
        log_event(
            "facebook_jobs_queue_synced",
            queued=queued,
            recovered=recovered,
            expired=expired,
            revived=revived,
        )

    return {
        "queued": queued,
        "recovered": recovered,
        "expired": expired,
        "revived": revived,
    }


def _facebook_job_priority(article, now=None):
    """Deadline-aware, aging-safe queue priority; score ranks but never filters."""
    now = now or datetime.now(timezone.utc)
    deadline = job_deadline_time(article)

    deadline_boost = 0.0
    hours_remaining = None
    if deadline:
        hours_remaining = max(0.0, (deadline - now).total_seconds() / 3600.0)
        if hours_remaining <= 24:
            deadline_boost = 30.0
        elif hours_remaining <= 48:
            deadline_boost = 26.0
        elif hours_remaining <= 72:
            deadline_boost = 22.0
        elif hours_remaining <= 7 * 24:
            deadline_boost = 16.0
        elif hours_remaining <= 14 * 24:
            deadline_boost = 10.0
        else:
            deadline_boost = 4.0

    queued_time = parse_job_date(
        article.get("facebook_queued_at")
        or article.get("published_at")
        or article.get("selected_at")
    )
    if queued_time:
        age_hours = max(0.0, (now - queued_time).total_seconds() / 3600.0)
    else:
        age_hours = 0.0

    # Aging prevents starvation without allowing very old/no-deadline work to
    # jump ahead of jobs that are about to close.
    age_days_boost = min(age_hours / 24.0, 14.0)

    urgency = (classify_urgency(article, now=now).get("level"))
    urgency_boost = {
        "critical": 4.0,
        "high": 3.0,
        "elevated": 1.5,
        "normal": 0.0,
    }.get(urgency, 0.0)

    try:
        score = max(0, min(100, int(article.get("job_score") or 0)))
    except (TypeError, ValueError):
        score = 0
    try:
        positions = max(0, int(article.get("job_number_of_positions") or 0))
    except (TypeError, ValueError):
        positions = 0

    fifo_priority = -queued_time.timestamp() if queued_time else 0.0
    priority_points = deadline_boost + age_days_boost + urgency_boost

    return (
        priority_points,
        deadline_boost,
        age_days_boost,
        score,
        positions,
        fifo_priority,
    )


def _eligible_for_facebook(article):
    if not _has_blogger_live_publish(article) or article.get("facebook_post_id"):
        return False
    if _job_facebook_expired(article):
        return False
    status = str(article.get("facebook_status") or "").strip()
    return (
        status in {"facebook_pending", "failed"}
        and _facebook_retry_ready(article)
    )


def _find_latest_eligible_article(articles):
    eligible = [article for article in articles if _eligible_for_facebook(article)]
    if not eligible:
        return None
    return max(eligible, key=_facebook_job_priority)


def _target_article(articles, target_article_id=None):
    if target_article_id:
        for article in articles:
            if target_article_id in {article.get("id"), article.get("url")}:
                return article
        return None
    return _find_latest_eligible_article(articles)


def _has_blogger_draft(article):
    return (
        article.get("status") == "draft_created"
        and article.get("publish_status") == "draft_created"
        and bool(_valid_public_blogger_url(article.get("blogger_draft_url")) or _blogger_post_url(article))
    )


def _eligible_for_preview(article, include_drafts=False):
    if _has_blogger_live_publish(article):
        return True
    return bool(include_drafts and _has_blogger_draft(article))


def _find_latest_preview_article(articles, include_drafts=False):
    eligible = [
        article
        for article in articles
        if _eligible_for_preview(article, include_drafts=include_drafts)
        and not article.get("facebook_post_id")
        and (
            article.get("facebook_status") in {None, "", "facebook_pending", "failed"}
            or (include_drafts and _has_blogger_draft(article))
        )
    ]
    if not eligible:
        return None
    live_pending = [
        article
        for article in eligible
        if _has_blogger_live_publish(article) and _eligible_for_facebook(article)
    ]
    if live_pending:
        return max(live_pending, key=_facebook_job_priority)
    return max(
        eligible,
        key=lambda article: (
            article.get("published_at", ""),
            article.get("draft_created_at", ""),
            article.get("selected_at", ""),
            article.get("discovered_at", ""),
        ),
    )


def _empty_style_memory():
    return {
        "global_styles": [],
        "recent": {},
        "recent_hooks": [],
        "recent_structures": [],
        "recent_ctas": [],
        "recent_hashtag_sets": [],
        "recent_fingerprints": [],
        "stats": {},
    }


def _load_style_memory():
    try:
        if FACEBOOK_STYLE_MEMORY_PATH.exists():
            with FACEBOOK_STYLE_MEMORY_PATH.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("global_styles", [])
                data.setdefault("recent", {})
                data.setdefault("recent_hooks", [])
                data.setdefault("recent_structures", [])
                data.setdefault("recent_ctas", [])
                data.setdefault("recent_hashtag_sets", [])
                data.setdefault("recent_fingerprints", [])
                data.setdefault("stats", {})
                return data
    except Exception as error:
        log_event("facebook_style_memory_load_failed", error=error.__class__.__name__)
    return _empty_style_memory()


def _save_style_memory(memory):
    try:
        atomic_write_json(FACEBOOK_STYLE_MEMORY_PATH, memory, sort_keys=True)
    except Exception as error:
        log_event("facebook_style_memory_save_failed", error=error.__class__.__name__)


def _caption_memory_category(article):
    return str(article.get("suggested_category") or "general").strip() or "general"


def _normalize_memory_text(value):
    return re.sub(r"[^\w\u0600-\u06FF]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _caption_fingerprint(caption):
    normalized = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", str(caption or ""))
    normalized = re.sub(r"\s+", " ", normalized.casefold()).strip()
    normalized = re.sub(r"https?://\S+", "", normalized)
    normalized = re.sub(r"#[\w\u0600-\u06FF_]+", "", normalized, flags=re.UNICODE)
    return hashlib.sha256(_normalize_memory_text(normalized).encode("utf-8")).hexdigest()[:20]


def _remember_caption_pattern(article, pattern, posted, structure_id="", hook="", cta="", hashtags=None, fingerprint=""):
    if pattern != JOBS_CAPTION_STYLE:
        return
    memory = _load_style_memory()
    category = _caption_memory_category(article)

    # Recency memory represents content that may actually be visible on the
    # Page. Definite pre-publish/API failures must not poison the fingerprint
    # cache or make a safe retry look like a duplicate.
    if posted:
        recent = list(memory.setdefault("recent", {}).get(category, []))
        recent.append(pattern)
        memory["recent"][category] = recent[-8:]
        global_styles = list(memory.get("global_styles", []))
        global_styles.append(pattern)
        memory["global_styles"] = global_styles[-8:]
        if hook:
            recent_hooks = list(memory.get("recent_hooks", []))
            recent_hooks.append(_normalize_memory_text(hook))
            memory["recent_hooks"] = recent_hooks[-20:]
        if cta:
            recent_ctas = list(memory.get("recent_ctas", []))
            recent_ctas.append(_normalize_memory_text(cta))
            memory["recent_ctas"] = recent_ctas[-12:]
        if hashtags:
            recent_hashtag_sets = list(memory.get("recent_hashtag_sets", []))
            recent_hashtag_sets.append("|".join(sorted(str(tag).casefold() for tag in hashtags)))
            memory["recent_hashtag_sets"] = recent_hashtag_sets[-12:]
        if fingerprint:
            recent_fingerprints = list(memory.get("recent_fingerprints", []))
            recent_fingerprints.append(fingerprint)
            memory["recent_fingerprints"] = recent_fingerprints[-30:]
        if structure_id:
            recent_structures = list(memory.get("recent_structures", []))
            recent_structures.append(structure_id)
            memory["recent_structures"] = recent_structures[-12:]

    category_stats = memory.setdefault("stats", {}).setdefault(category, {})
    stats = category_stats.setdefault(pattern, {})
    stats["used"] = int(stats.get("used", 0)) + 1
    if structure_id:
        structure_stats = stats.setdefault("structures", {}).setdefault(structure_id, {})
        structure_stats["used"] = int(structure_stats.get("used", 0)) + 1
    if posted:
        stats["posted"] = int(stats.get("posted", 0)) + 1
        stats["last_success_at"] = _now_iso()
        if structure_id:
            structure_stats["posted"] = int(structure_stats.get("posted", 0)) + 1
            structure_stats["last_success_at"] = _now_iso()
    else:
        stats["failed"] = int(stats.get("failed", 0)) + 1
        stats["last_failure_at"] = _now_iso()
        if structure_id:
            structure_stats["failed"] = int(structure_stats.get("failed", 0)) + 1
            structure_stats["last_failure_at"] = _now_iso()
    _save_style_memory(memory)


def _clean_caption_line(line):
    cleaned = unescape(str(line or "")).strip()
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    for phrase in FORBIDDEN_CAPTION_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"[*_`]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:،")
    return cleaned


def _short_title(article):
    title = article.get("seo_title") or article.get("fetched_title") or article.get("title", "")
    return _clean_caption_line(title)


_VISUAL_LOCATION_ALIASES = {
    "casablanca": ("Casablanca", "الدار البيضاء"),
    "rabat": ("Rabat", "الرباط"),
    "marrakech": ("Marrakech", "Marrakesh", "مراكش"),
    "tanger": ("Tanger", "Tangier", "طنجة"),
    "tangier": ("Tanger", "Tangier", "طنجة"),
    "agadir": ("Agadir", "أكادير"),
    "fes": ("Fès", "Fes", "Fez", "فاس"),
    "fez": ("Fès", "Fes", "Fez", "فاس"),
    "meknes": ("Meknès", "Meknes", "مكناس"),
    "kenitra": ("Kénitra", "Kenitra", "القنيطرة"),
    "oujda": ("Oujda", "وجدة"),
    "tetouan": ("Tétouan", "Tetouan", "تطوان"),
    "el jadida": ("El Jadida", "الجديدة"),
    "settat": ("Settat", "سطات"),
}

_GENERIC_JOB_TITLES = {
    "job",
    "jobs",
    "vacancy",
    "vacancies",
    "career",
    "careers",
    "recruitment",
    "recrutement",
    "offre",
    "offres",
    "offres d emploi",
    "فرص عمل",
    "وظائف",
}


def _visual_location_aliases(article):
    value = _clean_caption_line(article.get("job_location") or "")
    aliases = set()
    if value:
        aliases.add(value)
        for part in re.split(r"[,/|؛]+", value):
            part = part.strip()
            if part:
                aliases.add(part)
        lowered = value.casefold()
        for key, values in _VISUAL_LOCATION_ALIASES.items():
            if key in lowered:
                aliases.update(values)
    return sorted(aliases, key=len, reverse=True)


def _is_generic_job_title(value):
    normalized = re.sub(
        r"[^\w\u0600-\u06ff]+",
        " ",
        str(value or "").casefold(),
        flags=re.UNICODE,
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return not normalized or normalized in _GENERIC_JOB_TITLES


def _compact_visual_role(value):
    value = _clean_caption_line(value)

    # Official vacancy titles often append grade, contract duration, reference
    # numbers and geography after a comma. When the first segment is already a
    # clear role and the suffix is substantial, keep the role for the image and
    # leave the administrative detail to the caption/article.
    comma_head = re.split(r"[,،]", value, maxsplit=1)[0].strip(" -–—:")
    if (
        len(comma_head) >= 10
        and len(value) >= 64
        and len(value) - len(comma_head) >= 20
    ):
        return comma_head

    if len(value) <= 112:
        return value

    # Remove a trailing parenthetical qualifier only when the role remains clear.
    no_tail = re.sub(r"\s*\([^()]{8,}\)\s*$", "", value).strip(" -–—:")
    if 10 <= len(no_tail) < len(value):
        return no_tail
    return value


def _job_visual_title(article):
    notice_type = str(article.get("job_notice_type") or "vacancy").strip().lower()
    company = _clean_caption_line(
        article.get("job_company") or article.get("source_name") or ""
    )
    seo_title = _short_title(article)
    raw_role = _clean_caption_line(article.get("job_title") or "")

    if notice_type == "candidate_list" and not seo_title:
        return "لوائح المدعوين لاجتياز مباراة التوظيف"
    if notice_type == "final_results" and not seo_title:
        return "النتائج النهائية لمباراة التوظيف"
    if notice_type == "results" and not seo_title:
        return "نتائج مباراة التوظيف"

    visual = seo_title or raw_role
    if not visual:
        return "فرصة عمل جديدة"

    if company:
        escaped = re.escape(company)
        # Remove the employer together with grammatical glue first so we do not
        # leave fragments such as "لدى في" after a plain name replacement.
        patterns = (
            rf"^\s*{escaped}\s+(?:توظف|تعلن\s+عن\s+توظيف|تعلن\s+توظيف|recrute|recrutement|is\s+hiring|hiring)\s*[:\-–—]*\s*",
            rf"\s+(?:لدى|عند|مع|chez|at)\s+{escaped}\b",
            rf"\s*[-–—:]?\s*{escaped}\s*$",
            rf"^\s*{escaped}\s*[-–—:]\s*",
        )
        for pattern in patterns:
            visual = re.sub(pattern, " ", visual, flags=re.I)
        visual = re.sub(escaped, " ", visual, flags=re.I)

    if notice_type == "vacancy":
        visual = re.sub(
            r"^\s*(?:وظيفة|فرصة\s+عمل|فرصة\s+توظيف|إعلان\s+توظيف|"
            r"offre\s+d['’]?emploi|job\s+opening)\s*[:\-–—]*\s*",
            "",
            visual,
            flags=re.I,
        )
        visual = re.sub(
            r"^\s*(?:توظف|recrute|recrutement|hiring)\s*[:\-–—]*\s*",
            "",
            visual,
            flags=re.I,
        )

    for alias in _visual_location_aliases(article):
        escaped = re.escape(alias)
        visual = re.sub(
            rf"(?:\s+(?:في|بمدينة|à|a|in|at)\s+|\s+ب){escaped}\s*$",
            "",
            visual,
            flags=re.I,
        )
        visual = re.sub(
            rf"\s*[-–—,:،]\s*{escaped}\s*$",
            "",
            visual,
            flags=re.I,
        )

    visual = re.sub(r"\s+", " ", visual).strip(" -–—:،,")
    visual = _compact_visual_role(visual)

    # If cleanup still leaves a very long marketing/SEO sentence, prefer the
    # official role when it is specific and materially shorter.
    if (
        len(visual) > 112
        and raw_role
        and not _is_generic_job_title(raw_role)
        and len(raw_role) <= 105
    ):
        visual = _compact_visual_role(raw_role)

    if not visual or _is_generic_job_title(visual):
        if raw_role and not _is_generic_job_title(raw_role):
            visual = _compact_visual_role(raw_role)
        elif notice_type == "candidate_list":
            visual = "لوائح المدعوين لاجتياز مباراة التوظيف"
        elif notice_type == "final_results":
            visual = "النتائج النهائية لمباراة التوظيف"
        elif notice_type == "results":
            visual = "نتائج مباراة التوظيف"
        else:
            visual = "فرصة عمل جديدة"

    return _clean_caption_line(visual)


def _repair_job_facebook_application_semantics(article):
    """Restore only strong legacy application semantics before template selection."""
    if not isinstance(article, dict):
        return False

    current = str(article.get("job_application_link_kind") or "").strip().lower()
    if current:
        return False

    inferred = _recovered_application_link_kind({
        "application_url": article.get("job_application_url") or "",
    })
    if inferred != "direct_apply":
        return False

    article["job_application_link_kind"] = "direct_apply"
    article["job_application_is_specific"] = True
    package = article.get("ai_input_package")
    if isinstance(package, dict):
        package["job_application_link_kind"] = "direct_apply"
        package["job_application_is_specific"] = True

    log_event(
        "facebook_job_application_semantics_repaired",
        article_id=article.get("id"),
        application_kind="direct_apply",
    )
    _persist_jobs_social_state(article)
    return True


def _refresh_job_logo_before_facebook(article):
    """Late verified-logo recovery immediately before the Facebook visual step."""

    current = verified_company_logo(article)
    article["facebook_logo_refresh_attempted_at"] = _now_iso()
    if current.get("company_logo_verified") and current.get("company_logo_url"):
        article["facebook_logo_refresh_status"] = "already_verified"
        article.pop("facebook_logo_refresh_error", None)
        return current

    refreshed = refresh_company_logo(article)
    if isinstance(refreshed, dict):
        article.update(refreshed)
        package = article.get("ai_input_package")
        if isinstance(package, dict):
            package.update(refreshed)
    if refreshed.get("company_logo_verified") and refreshed.get("company_logo_url"):
        article["facebook_logo_refresh_status"] = "verified"
        article.pop("facebook_logo_refresh_error", None)
        log_event(
            "facebook_job_logo_late_refresh_ready",
            article_id=article.get("id"),
            company=article.get("job_company"),
            confidence=refreshed.get("company_logo_confidence", 0),
        )
        return refreshed

    article["facebook_logo_refresh_status"] = "unavailable"
    article["facebook_logo_refresh_error"] = (
        "Verified employer logo is still unavailable after late refresh."
    )
    log_event(
        "facebook_job_logo_late_refresh_unavailable",
        article_id=article.get("id"),
        company=article.get("job_company"),
    )
    return refreshed or current


def _main_image_url(article):
    logo = verified_company_logo(article)
    if logo.get("company_logo_verified") and logo.get("company_logo_url"):
        return str(logo["company_logo_url"]).strip()
    # Jobs visuals never substitute the generated article cover, a source
    # hero image, or plain employer text for a missing verified logo.
    return ""


def _redact_facebook_error(text):
    text = str(text or "")
    for secret in (FACEBOOK_PAGE_ACCESS_TOKEN,):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _post_to_graph(path, payload):
    url = f"{FACEBOOK_GRAPH_API_URL.rstrip('/')}/{path.lstrip('/')}"
    started = time.perf_counter()
    log_event("facebook_graph_start", path=path)
    try:
        response = requests.post(url, data=payload, timeout=60)
    except (requests.Timeout, requests.ConnectionError) as error:
        log_event(
            "facebook_graph_end",
            path=path,
            status="network-uncertain",
            error=error.__class__.__name__,
            elapsed_ms=elapsed_ms(started),
        )
        raise FacebookDeliveryUncertain(
            f"Facebook delivery outcome is uncertain after {error.__class__.__name__}."
        ) from error
    if response.status_code >= 500 or response.status_code == 408:
        error_text = _redact_facebook_error(response.text[:200])
        log_event(
            "facebook_graph_end",
            path=path,
            status=response.status_code,
            error=error_text,
            elapsed_ms=elapsed_ms(started),
        )
        raise FacebookDeliveryUncertain(
            f"Facebook delivery outcome is uncertain after HTTP {response.status_code}."
        )
    if response.status_code >= 400:
        error_text = _redact_facebook_error(response.text[:200])
        log_event(
            "facebook_graph_end",
            path=path,
            status=response.status_code,
            error=error_text,
            elapsed_ms=elapsed_ms(started),
        )
        raise RuntimeError(f"Facebook Graph API error {response.status_code}: {_redact_facebook_error(response.text[:500])}")
    data = response.json()
    if not isinstance(data, dict):
        raise FacebookDeliveryUncertain("Facebook Graph API returned an unexpected response after delivery.")
    log_event(
        "facebook_graph_end",
        path=path,
        status=response.status_code,
        elapsed_ms=elapsed_ms(started),
    )
    return data

def _post_photo_file(path, payload, image_path):
    url = f"{FACEBOOK_GRAPH_API_URL.rstrip('/')}/{path.lstrip('/')}"
    started = time.perf_counter()
    log_event("facebook_graph_start", path=path, upload="photo")
    try:
        with open(image_path, "rb") as handle:
            response = requests.post(
                url,
                data=payload,
                files={"source": handle},
                timeout=60,
            )
    except (requests.Timeout, requests.ConnectionError) as error:
        log_event(
            "facebook_graph_end",
            path=path,
            status="network-uncertain",
            error=error.__class__.__name__,
            elapsed_ms=elapsed_ms(started),
        )
        raise FacebookDeliveryUncertain(
            f"Facebook photo delivery outcome is uncertain after {error.__class__.__name__}."
        ) from error
    if response.status_code >= 500 or response.status_code == 408:
        error_text = _redact_facebook_error(response.text[:200])
        log_event(
            "facebook_graph_end",
            path=path,
            status=response.status_code,
            error=error_text,
            elapsed_ms=elapsed_ms(started),
        )
        raise FacebookDeliveryUncertain(
            f"Facebook photo delivery outcome is uncertain after HTTP {response.status_code}."
        )
    if response.status_code >= 400:
        error_text = _redact_facebook_error(response.text[:200])
        log_event(
            "facebook_graph_end",
            path=path,
            status=response.status_code,
            error=error_text,
            elapsed_ms=elapsed_ms(started),
        )
        raise RuntimeError(f"Facebook Graph API error {response.status_code}: {_redact_facebook_error(response.text[:500])}")
    data = response.json()
    if not isinstance(data, dict):
        raise FacebookDeliveryUncertain("Facebook Graph API returned an unexpected photo response after delivery.")
    log_event(
        "facebook_graph_end",
        path=path,
        status=response.status_code,
        elapsed_ms=elapsed_ms(started),
    )
    return data

def _facebook_image_output_path(article):
    article_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(article.get("id") or article.get("url") or "post")).strip("-")
    if not article_id:
        article_id = "post"
    return FACEBOOK_IMAGE_OUTPUT_DIR / f"{article_id[:80]}.jpg"


def _split_caption_parts(caption):
    lines = [line for line in str(caption or "").splitlines() if line.strip()]
    hashtags = lines[-1] if lines and lines[-1].startswith("#") else ""
    post_text = "\n".join(lines[:-1]) if hashtags else "\n".join(lines)
    return post_text.strip(), hashtags.strip()


def _jobs_facebook_blueprint(article, blogger_url):
    """Generate/use independent social AI copy after Blogger succeeds, then force RTL display."""
    raw_caption = str(article.get("facebook_post_text") or "").strip()
    source = str(article.get("facebook_post_source") or "").strip().lower()

    if source != "social_ai" or not raw_caption:
        social_result = generate_jobs_facebook_post(article)
        raw_caption = str(social_result.get("facebook_post_text") or "").strip()
        if not raw_caption:
            raise RuntimeError("Social AI returned an empty Jobs Facebook post.")
        article["facebook_post_text"] = raw_caption
        article["facebook_post_source"] = "social_ai"
        article["facebook_ai_provider_used"] = str(social_result.get("provider") or "")
        article["facebook_ai_attempts"] = int(social_result.get("attempts") or 1)
        article["facebook_ai_generated_at"] = _now_iso()
        article.pop("facebook_ai_error", None)
        log_event(
            "facebook_social_ai_attached",
            article_id=article.get("id"),
            provider=article.get("facebook_ai_provider_used"),
            attempts=article.get("facebook_ai_attempts"),
        )

    # AI writes the language; the publisher owns display direction. Strip any
    # model-supplied bidi controls, then force every visible line to start RTL.
    raw_caption = re.sub(
        r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]",
        "",
        raw_caption,
    ).strip()
    raw_caption = re.sub(r"\n{3,}", "\n\n", raw_caption)

    plain_lines = raw_caption.splitlines()
    nonempty_lines = [line.strip() for line in plain_lines if line.strip()]
    if not nonempty_lines:
        raise RuntimeError("AI-generated Jobs Facebook post is empty after cleanup.")

    hook = nonempty_lines[0]
    cta = next((line for line in nonempty_lines if "أول تعليق" in line), "")
    hashtags = re.findall(r"#[\w\u0600-\u06FF_]+", raw_caption, flags=re.UNICODE)

    rtl_mark = "\u200f"
    caption = "\n".join(
        (rtl_mark + line.strip()) if line.strip() else ""
        for line in plain_lines
    ).strip()

    return {
        "caption": caption,
        "hashtags": hashtags,
        "hook": hook,
        "cta": cta,
        "fingerprint": _caption_fingerprint(caption),
        "lead": "",
        "sections": [],
        "style": "jobs",
        "structure": "jobs_ai",
        "blogger_url": blogger_url,
        "source": "ai",
    }

def _prepare_facebook_post(article, articles, blogger_url):
    memory = _load_style_memory()
    blueprint = _jobs_facebook_blueprint(article, blogger_url)
    _validate_facebook_caption(
        blueprint["caption"],
        blogger_url=blogger_url,
        style="",
        hook=blueprint["hook"],
        structure_id="",
        title=_short_title(article),
        memory=memory,
        allow_simple=True,
    )
    return blueprint


def _publish_facebook_post(article, blueprint):
    blogger_url = _blogger_post_url(article)
    if not blogger_url:
        raise RuntimeError("Missing live Blogger URL for Facebook post.")

    caption = blueprint["caption"]
    visual_title = (_job_visual_title(article))
    article["facebook_visual_title"] = visual_title

    image_result = generate_facebook_image(
        visual_title,
        _main_image_url(article),
        _facebook_image_output_path(article),
        hook_text=blueprint.get("hook", ""),
        template_key=article.get("facebook_template_key", ""),
        employer_name=(
            (str(article.get("job_company") or article.get("source_name") or "").strip())
        ),
    )
    if image_result.get("ok"):
        image_result["url"] = _main_image_url(article)
        article["facebook_logo_used"] = image_result["url"]
        verified = verified_company_logo(article)
        article["facebook_logo_checksum"] = str(
            verified.get("company_logo_checksum") or ""
        )
        article["facebook_visual_layout"] = {
            "title": visual_title,
            "font_size": image_result.get("title_font_size"),
            "font_width": image_result.get("title_font_width"),
            "lines": image_result.get("title_lines"),
            "truncated": bool(image_result.get("title_truncated")),
            "title_bbox": image_result.get("title_bbox"),
            "logo_kind": image_result.get("logo_kind"),
            "logo_bbox": image_result.get("logo_bbox"),
        }
    base_payload = {
        "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
    }

    if image_result.get("ok") and Path(image_result["path"]).exists():
        payload = {
            **base_payload,
            "caption": caption,
            "published": "true",
        }
        data = _post_photo_file(f"{FACEBOOK_PAGE_ID}/photos", payload, image_result["path"])
        return data.get("post_id") or data.get("id") or "", "photo", image_result

    log_event(
        "facebook_job_image_required",
        article_id=article.get("id"),
        template_key=article.get("facebook_template_key", ""),
        error=image_result.get("error", ""),
    )
    raise RuntimeError(
        "Jobs Facebook image generation failed; refusing text-only publish."
    )


def _post_first_comment(facebook_post_id, blogger_post_url):
    comment = _first_comment_text(blogger_post_url)
    data = _post_to_graph(
        f"{facebook_post_id}/comments",
        {
            "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
            "message": comment,
        },
    )
    return data.get("id") or ""


def _first_comment_text(blogger_post_url):
    lines = [
        "🔗 رابط التفاصيل:",
        blogger_post_url,
    ]
    if WHATSAPP_CHANNEL_URL:
        lines.extend([
            "",
            "📲 تابع قناة واتساب للعروض الجديدة:",
            WHATSAPP_CHANNEL_URL,
        ])
    return "\n".join(lines)


def _validate_facebook_caption(caption, blogger_url="", style="", hook="", structure_id="", title="", memory=None, allow_simple=False):
    if not str(caption or "").strip():
        raise RuntimeError("Facebook caption is empty.")

    raw_caption = str(caption)
    plain_caption = re.sub(
        r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]",
        "",
        raw_caption,
    )

    visible_lines = [line for line in raw_caption.splitlines() if line.strip()]
    if any(not line.startswith("\u200f") for line in visible_lines):
        raise RuntimeError("Jobs Facebook caption is not forced to RTL on every visible line.")

    if "```" in plain_caption or re.search(r'"\s*(title|description|html_content|facebook_post_text)\s*"\s*:', plain_caption):
        raise RuntimeError("Facebook caption contains visible JSON/markdown.")
    if re.search(r"https?://\S+", plain_caption):
        raise RuntimeError("Facebook caption contains a URL.")
    if re.search(r"^\s*[-*]\s+", plain_caption, flags=re.MULTILINE):
        raise RuntimeError("Facebook caption contains markdown bullets.")

    hashtags = re.findall(r"#[\w\u0600-\u06FF_]+", plain_caption, flags=re.UNICODE)
    if len(set(hashtags)) != len(hashtags):
        raise RuntimeError("Facebook caption contains duplicate hashtags.")
    if not (3 <= len(hashtags) <= 5):
        raise RuntimeError("Jobs Facebook caption must contain 3 to 5 hashtags.")

    if blogger_url and "أول تعليق" not in plain_caption:
        raise RuntimeError("Facebook caption must say the link is in the first comment.")
    if not hook or _normalize_memory_text(hook) == _normalize_memory_text(title):
        raise RuntimeError("Facebook caption hook is missing or identical to the title.")

    first_line = next((line.strip() for line in plain_caption.splitlines() if line.strip()), "")
    if len(first_line) < 18 or first_line.startswith("#"):
        raise RuntimeError("Facebook caption hook is too weak.")

    arabic_chars = len(re.findall(r"[\u0600-\u06FF]", plain_caption))
    if arabic_chars < 40:
        raise RuntimeError("Facebook caption is not Arabic enough.")

    latin_chars = len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]", plain_caption))
    if latin_chars > max(80, int(arabic_chars * 0.60)):
        raise RuntimeError("Jobs Facebook caption must remain Arabic-first even with foreign terms.")

    if len(plain_caption) > 1200 or len(plain_caption) < 120:
        raise RuntimeError("Facebook caption length is outside the expected range.")
    fingerprint = _caption_fingerprint(plain_caption)
    if fingerprint in set((memory or {}).get("recent_fingerprints", [])):
        raise RuntimeError("Facebook caption is too similar to a recent post.")
    if not allow_simple and len([line for line in plain_caption.splitlines() if line.strip()]) < 5:
        raise RuntimeError("Facebook caption is too thin.")

def _facebook_failure_delay_seconds(error, failure_count):
    text = str(error or "").casefold().replace(" ", "")
    # A Jobs image render failure occurs before any Graph request. Retrying at
    # the normal social interval recovers quickly after a renderer/code fix
    # without increasing Facebook API traffic.
    if _is_jobs_image_generation_failure(error):
        return max(5 * 60, int(JOBS_FACEBOOK_MIN_INTERVAL_MINUTES) * 60)
    # Authentication/permission failures need configuration changes; hammering
    # Graph every scheduled run cannot fix them.
    if any(token in text for token in (
        "oauthexception",
        '"code":190',
        '"code":10',
        '"code":200',
        "accesstoken",
        "permission",
        "notauthorized",
    )):
        return 6 * 3600
    if any(token in text for token in (
        "ratelimit",
        "toomanyrequests",
        '"code":4',
        '"code":17',
        '"code":32',
        '"code":613',
        "http429",
    )):
        return 60 * 60
    # Definite local/API failures can be retried, but with bounded exponential
    # spacing rather than every 15-minute workflow tick.
    step = max(0, min(int(failure_count or 1) - 1, 4))
    return min(6 * 3600, 30 * 60 * (2 ** step))


def _apply_failure(article, error):
    article["facebook_status"] = "failed"
    article["facebook_error"] = str(error)
    count = int(article.get("facebook_failure_count") or 0) + 1
    article["facebook_failure_count"] = count
    article["facebook_last_failure_at"] = _now_iso()
    delay = _facebook_failure_delay_seconds(error, count)
    article["facebook_retry_after_epoch"] = int(time.time()) + delay
    article["facebook_retry_delay_seconds"] = delay


def _clear_facebook_failure_state(article):
    for key in (
        "facebook_failure_count",
        "facebook_last_failure_at",
        "facebook_retry_after_epoch",
        "facebook_retry_delay_seconds",
    ):
        article.pop(key, None)


def _schedule_comment_retry(article, error):
    count = int(article.get("facebook_comment_failure_count") or 0) + 1
    article["facebook_comment_failure_count"] = count
    delay = _facebook_failure_delay_seconds(error, count)
    article["facebook_comment_retry_after_epoch"] = int(time.time()) + delay
    article["facebook_comment_retry_delay_seconds"] = delay


def _clear_comment_failure_state(article):
    for key in (
        "facebook_comment_failure_count",
        "facebook_comment_retry_after_epoch",
        "facebook_comment_retry_delay_seconds",
    ):
        article.pop(key, None)


def _failure_result(queue, article, error, checked=1, extra=None):
    if article:
        _apply_failure(article, error)
        _persist_jobs_social_state(article)
        save_article_queue(queue)
        result = {
            "checked": checked,
            "posted": False,
            "article": article,
            "error": article.get("facebook_error", ""),
        }
        if extra:
            result.update(extra)
        return result

    result = {
        "checked": checked,
        "posted": False,
        "article": None,
        "error": str(error),
    }
    if extra:
        result.update(extra)
    return result


def _deferred_result(article, reason, extra=None):
    result = {
        "checked": 1 if article else 0,
        "posted": False,
        "deferred": True,
        "article": article,
        "error": str(reason or ""),
    }
    if extra:
        result.update(extra)
    return result


def get_facebook_limits_status(now=None, urgent=False):
    queue = load_article_queue()
    posted_times = []
    for article in queue.get("articles", []):
        status = article.get("facebook_status")
        if status in {"posted", "posted_comment_failed", "posted_comment_uncertain"}:
            value = article.get("facebook_posted_at")
        elif status == "delivery_uncertain":
            # Conservatively count an uncertain upload as if it may have landed.
            value = article.get("facebook_delivery_uncertain_at")
        else:
            value = None
        if value:
            posted_times.append(value)

    local_now = jobs_local_time(now)
    local_posts = []
    for value in posted_times:
        parsed = parse_job_date(value)
        if parsed:
            local_posts.append(jobs_local_time(parsed))
    today_posts = [value for value in local_posts if value.date() == local_now.date()]
    last_post_time = max(local_posts) if local_posts else None

    hard_daily_limit = (JOBS_FACEBOOK_MAX_POSTS_PER_DAY)
    normal_daily_limit = (
        (hard_daily_limit)
    )
    effective_daily_limit = min(hard_daily_limit, normal_daily_limit + (1 if urgent else 0))
    daily_blocked = len(today_posts) >= effective_daily_limit

    safe_interval = (
        (JOBS_FACEBOOK_MIN_INTERVAL_MINUTES)
    )
    minutes_since_last = (
        max(0, int((local_now - last_post_time).total_seconds() // 60))
        if last_post_time
        else None
    )
    interval_blocked = bool(
        last_post_time
        and (local_now - last_post_time).total_seconds() < safe_interval * 60
    )

    slot = facebook_slot_status(posted_times=posted_times, now=now, urgent=urgent)
    allowed_now = bool(slot.get("allowed_now")) and not daily_blocked and not interval_blocked
    reasons = []
    if daily_blocked:
        reasons.append("Facebook hard daily safety limit reached")
    if interval_blocked:
        reasons.append("Facebook safety interval has not elapsed")
    if not slot.get("allowed_now") and not urgent:
        reasons.append("waiting for Morocco Facebook publishing slot")

    next_allowed = slot.get("next_slot", "")
    if interval_blocked and last_post_time:
        next_allowed = (last_post_time + timedelta(minutes=safe_interval)).isoformat()

    return {
        "facebook_posts_today": len(today_posts),
        "max_facebook_posts_per_day": normal_daily_limit,
        "effective_facebook_posts_per_day": effective_daily_limit,
        "hard_max_facebook_posts_per_day": hard_daily_limit,
        "last_facebook_post_time": last_post_time.isoformat() if last_post_time else None,
        "minutes_since_last_facebook_post": minutes_since_last,
        "min_minutes_between_facebook_posts": safe_interval,
        "allowed_now": allowed_now,
        "next_allowed_time": next_allowed,
        "reasons": reasons,
        "jobs_slot_mode": slot.get("mode", "scheduled"),
        "jobs_slot": slot.get("slot", ""),
    }


def post_one_article_to_facebook(target_article_id=None, respect_limits=True):
    """
    Post exactly one live Blogger article to a Facebook Page.
    This is a no-op unless Facebook auto-posting is explicitly enabled.
    """
    queue = load_article_queue()
    _sync_jobs_facebook_queue(queue)
    articles = queue.get("articles", [])
    article = _target_article(articles, target_article_id=target_article_id)

    if not FACEBOOK_AUTO_POST:
        return _deferred_result(article, "FACEBOOK_AUTO_POST is disabled.")

    if not FACEBOOK_PAGE_ID:
        return _deferred_result(article, "FACEBOOK_PAGE_ID is not configured.")

    if not FACEBOOK_PAGE_ACCESS_TOKEN:
        return _deferred_result(article, "FACEBOOK_PAGE_ACCESS_TOKEN is not configured.")

    if not article:
        return {
            "checked": 0,
            "posted": False,
            "article": None,
            "error": "No eligible published article without Facebook post found.",
        }

    if _job_facebook_expired(article):
        _mark_facebook_expired(article)
        save_article_queue(queue)
        return {
            "checked": 1,
            "posted": False,
            "deferred": False,
            "dropped_from_social_queue": True,
            "article": article,
            "error": "Job expired before its Facebook queue turn.",
        }

    if (
        not article.get('facebook_post_id') and str(article.get('facebook_status') or '').strip() not in {'facebook_pending', 'failed'}
    ):
        return _deferred_result(
            article,
            "Job is not pending in the Facebook queue.",
            extra={"facebook_status": article.get("facebook_status", "")},
        )

    if article.get("facebook_post_id"):
        return {
            "checked": 1,
            "posted": False,
            "article": article,
            "error": "Article already has facebook_post_id; refusing duplicate post.",
        }

    if article.get("facebook_status") == "failed" and not _facebook_retry_ready(article):
        return _deferred_result(
            article,
            "Facebook retry cooldown has not elapsed.",
            extra={"retry_after_epoch": article.get("facebook_retry_after_epoch")},
        )

    if not _has_blogger_live_publish(article):
        return _failure_result(
            queue,
            article,
            "Article is not a successful live Blogger publish with blogger_post_url.",
        )

    blogger_url = _blogger_post_url(article)
    if not blogger_url:
        return _failure_result(queue, article, "Missing valid live Blogger URL for Facebook.")

    if respect_limits:
        limits = get_facebook_limits_status(
            urgent=bool(JOBS_MODE and article.get("job_publish_immediately"))
        )
        if not limits["allowed_now"]:
            return _deferred_result(
                article,
                "; ".join(limits["reasons"]) or "Facebook posting limits blocked this run.",
                extra={"limits": limits},
            )

    _repair_job_facebook_application_semantics(article)
    logo = _refresh_job_logo_before_facebook(article)
    if not (
        logo.get("company_logo_verified")
        and str(logo.get("company_logo_url") or "").strip()
    ):
        # Do not consume a Facebook slot with a text-only or employer-name
        # fallback. Keep the real Blogger job in the retryable social queue.
        return _failure_result(
            queue,
            article,
            article.get("facebook_logo_refresh_error")
            or "Verified employer logo unavailable for Jobs Facebook visual.",
            extra={"facebook_logo_refresh_status": article.get("facebook_logo_refresh_status", "")},
        )

    selection = choose_job_template(article, JOB_VISUAL_STATE_PATH)
    if not selection.get("pinned"):
        save_article_queue(queue)
    log_event(
        "facebook_job_template_selected",
        article_id=article.get("id"),
        template_key=selection.get("key", ""),
        reason=selection.get("reason", ""),
        pinned=selection.get("pinned", False),
    )

    try:
        blueprint = _prepare_facebook_post(article, articles, blogger_url)
        caption_pattern = blueprint["style"]
        log_event(
            "facebook_post_start",
            article_id=article.get("id"),
            blogger_url=blogger_url,
            pattern=caption_pattern,
        )
        log_event(
            "facebook_style_used",
            article_id=article.get("id"),
            style=blueprint["style"],
            structure=blueprint["structure"],
        )
        log_event(
            "facebook_hook_generated",
            article_id=article.get("id"),
            hook=blueprint["hook"],
        )
        log_event(
            "facebook_hashtags_count",
            article_id=article.get("id"),
            count=len(blueprint["hashtags"]),
        )
        facebook_post_id, post_type, image_result = _publish_facebook_post(article, blueprint)
        if not facebook_post_id:
            raise RuntimeError("Facebook Graph API did not return a post id.")

        article["facebook_status"] = "posted"
        article["facebook_post_id"] = facebook_post_id
        article["facebook_posted_at"] = _now_iso()
        _clear_facebook_failure_state(article)
        article["facebook_post_type"] = post_type
        article["facebook_image_status"] = "posted" if image_result.get("ok") else "failed_text_only"
        article["facebook_image_path"] = image_result.get("path", "")
        article["facebook_image_url"] = image_result.get("url", "")
        article["facebook_image_used_fallback"] = False
        if image_result.get("error"):
            article["facebook_image_error"] = image_result.get("error", "")[:300]
        else:
            article.pop("facebook_image_error", None)
        article["facebook_caption_pattern"] = caption_pattern
        article["facebook_style_used"] = blueprint["style"]
        article["facebook_hook_generated"] = blueprint["hook"]
        article["facebook_structure_used"] = blueprint["structure"]
        article["facebook_hashtags_count"] = len(blueprint["hashtags"])
        article["facebook_cta_used"] = blueprint.get("cta", "")
        article["facebook_caption_fingerprint"] = blueprint.get("fingerprint", "")
        article["facebook_link_mode"] = FACEBOOK_LINK_MODE_ENFORCED
        article["facebook_post_text"] = blueprint["caption"]
        article.pop("facebook_error", None)

        # Persist the acknowledged remote ID before the separate comment call.
        # A comment failure must never cause a second photo post, even if the
        # volatile queue is lost between the photo and comment requests.
        _persist_jobs_social_state(article)
        save_article_queue(queue)

        try:
            comment_id = _post_first_comment(facebook_post_id, blogger_url)
            if not comment_id:
                raise RuntimeError("Facebook first comment did not return a comment id.")
            article["facebook_comment_id"] = comment_id
            _clear_comment_failure_state(article)
            log_event(
                "facebook_comment_success",
                article_id=article.get("id"),
                success=True,
                comment_id=comment_id,
            )
        except FacebookDeliveryUncertain as comment_error:
            article["facebook_status"] = "posted_comment_uncertain"
            article["facebook_error"] = f"First comment delivery uncertain: {comment_error}"
            log_event(
                "facebook_comment_success",
                article_id=article.get("id"),
                success=False,
                uncertain=True,
                error=comment_error.__class__.__name__,
            )
        except Exception as comment_error:
            article["facebook_status"] = "posted_comment_failed"
            article["facebook_error"] = f"First comment failed: {comment_error}"
            _schedule_comment_retry(article, comment_error)
            log_event(
                "facebook_comment_success",
                article_id=article.get("id"),
                success=False,
                error=comment_error.__class__.__name__,
            )

        _remember_caption_pattern(
            article,
            caption_pattern,
            posted=bool(article.get("facebook_post_id")),
            structure_id=blueprint["structure"],
            hook=blueprint["hook"],
            cta=blueprint.get("cta", ""),
            hashtags=blueprint.get("hashtags", []),
            fingerprint=blueprint.get("fingerprint", ""),
        )
        _persist_jobs_social_state(article)
        save_article_queue(queue)
        if (
            article.get('facebook_status') == 'posted' and article.get('facebook_post_id') and article.get('facebook_comment_id')
        ):
            archive_published_queue_article(
                article_id=article.get("id", ""),
                article_url=article.get("url", ""),
                reason="facebook_post_and_comment_completed",
            )
        result = {
            "checked": 1,
            "posted": bool(article.get("facebook_post_id")),
            "comment_posted": bool(article.get("facebook_comment_id")),
            "image_posted": article.get("facebook_image_status") == "posted",
            "article": article,
            "error": article.get("facebook_error", ""),
        }
        log_event(
            "facebook_post_result",
            status=article.get("facebook_status"),
            article_id=article.get("id"),
            facebook_post_id=article.get("facebook_post_id"),
            comment_id=article.get("facebook_comment_id"),
            blogger_url=blogger_url,
            error=article.get("facebook_error", ""),
        )
        log_event(
            "facebook_post_success",
            article_id=article.get("id"),
            success=article.get("facebook_status") in {"posted", "posted_comment_failed", "posted_comment_uncertain"},
            post_id=article.get("facebook_post_id"),
        )
        return result

    except FacebookDeliveryUncertain as error:
        article["facebook_status"] = "delivery_uncertain"
        article["facebook_error"] = str(error)
        article["facebook_delivery_uncertain_at"] = _now_iso()
        _persist_jobs_social_state(article)
        save_article_queue(queue)
        log_event(
            "facebook_post_result",
            status="delivery_uncertain",
            article_id=article.get("id"),
            blogger_url=blogger_url,
            error=error.__class__.__name__,
        )
        return {
            "checked": 1,
            "posted": False,
            "delivery_uncertain": True,
            "article": article,
            "error": article.get("facebook_error", ""),
        }

    except Exception as error:
        _apply_failure(article, error)
        if "caption_pattern" in locals():
            _remember_caption_pattern(
                article,
                caption_pattern,
                posted=False,
                structure_id=blueprint.get("structure", "") if "blueprint" in locals() else "",
                hook=blueprint.get("hook", "") if "blueprint" in locals() else "",
                cta=blueprint.get("cta", "") if "blueprint" in locals() else "",
                hashtags=blueprint.get("hashtags", []) if "blueprint" in locals() else [],
                fingerprint=blueprint.get("fingerprint", "") if "blueprint" in locals() else "",
            )
        _persist_jobs_social_state(article)
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": False,
            "article": article,
            "error": article.get("facebook_error", ""),
        }
        log_event(
            "facebook_post_success",
            article_id=article.get("id"),
            success=False,
            error=error.__class__.__name__,
        )
        log_event(
            "facebook_post_result",
            status="failed",
            article_id=article.get("id"),
            blogger_url=blogger_url,
            error=article.get("facebook_error", ""),
        )
        return result


def _facebook_backfill_candidates(articles):
    new_post_candidates = [
        article
        for article in articles
        if _eligible_for_facebook(article)
    ]
    comment_retry_candidates = [
        article
        for article in articles
        if _has_blogger_live_publish(article)
        and article.get("facebook_post_id")
        and not article.get("facebook_comment_id")
        and article.get("facebook_status") == "posted_comment_failed"
        and _facebook_comment_retry_ready(article)
    ]
    def new_post_sort_key(article):
        return _facebook_job_priority(article)

    comment_sort_key = lambda article: (
        article.get("published_at", ""),
        article.get("selected_at", ""),
        article.get("discovered_at", ""),
    )
    return (
        sorted(new_post_candidates, key=new_post_sort_key, reverse=True),
        sorted(comment_retry_candidates, key=comment_sort_key),
    )


def retry_facebook_first_comment(target_article_id):
    queue = load_article_queue()
    articles = queue.get("articles", [])
    article = _target_article(articles, target_article_id=target_article_id)
    if not article:
        return {
            "checked": 0,
            "posted": False,
            "article": None,
            "error": "No matching article found for Facebook comment retry.",
        }
    if not article.get("facebook_post_id"):
        return _failure_result(queue, article, "Article has no Facebook post ID for comment retry.")
    blogger_url = _blogger_post_url(article)
    if not blogger_url:
        return _failure_result(queue, article, "Article has no real Blogger URL for comment retry.")

    try:
        comment_id = _post_first_comment(article["facebook_post_id"], blogger_url)
        article["facebook_comment_id"] = comment_id
        article["facebook_status"] = "posted"
        article.pop("facebook_error", None)
        _clear_comment_failure_state(article)
        _persist_jobs_social_state(article)
        save_article_queue(queue)
        archive_published_queue_article(
            article_id=article.get("id", ""),
            article_url=article.get("url", ""),
            reason="facebook_comment_retry_completed",
        )
        result = {
            "checked": 1,
            "posted": True,
            "article": article,
            "error": "",
            "comment_retry": True,
        }
        log_event("facebook_comment_success", article_id=article.get("id"), success=True, comment_id=comment_id)
    except FacebookDeliveryUncertain as error:
        article["facebook_status"] = "posted_comment_uncertain"
        article["facebook_error"] = f"First comment delivery uncertain: {error}"
        _persist_jobs_social_state(article)
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": False,
            "delivery_uncertain": True,
            "article": article,
            "error": article.get("facebook_error", ""),
            "comment_retry": True,
        }
        log_event("facebook_comment_success", article_id=article.get("id"), success=False, uncertain=True, error=error.__class__.__name__)
    except Exception as error:
        article["facebook_status"] = "posted_comment_failed"
        article["facebook_error"] = f"First comment failed: {error}"
        _schedule_comment_retry(article, error)
        _persist_jobs_social_state(article)
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": False,
            "article": article,
            "error": article.get("facebook_error", ""),
            "comment_retry": True,
        }
        log_event("facebook_comment_success", article_id=article.get("id"), success=False, error=error.__class__.__name__)
    return result


def drain_scheduled_facebook():
    """Retry pending social delivery independently of new Blogger generation.

    Never bypass daily/slot limits. Each cycle can create at most one photo and
    retry at most one missing first comment; actual Graph failures are reported.
    """
    stats = {"created": 0, "comments_created": 0, "failed": 0, "skipped": 0}
    queue = load_article_queue()
    sync_stats = (
        (_sync_jobs_facebook_queue(queue))
    )
    stats["queued"] = sync_stats.get("queued", 0)
    stats["recovered"] = sync_stats.get("recovered", 0)
    stats["expired"] = sync_stats.get("expired", 0)
    stats["revived"] = sync_stats.get("revived", 0)
    pending, comments = _facebook_backfill_candidates(queue.get("articles", []))
    stats["pending"] = len(pending)

    if not _is_configured():
        stats["skipped"] = 1
        stats["configuration_missing"] = True
        return stats
    if comments:
        result = retry_facebook_first_comment(comments[0].get("id") or comments[0].get("url"))
        stats["comments_created"] = int(bool(result.get("posted")))
        stats["failed"] += int(
            not result.get("posted")
            and not result.get("deferred")
            and not result.get("delivery_uncertain")
        )
    for article in pending:
        if _job_facebook_expired(article):
            if _mark_facebook_expired(article):
                save_article_queue(queue)
                stats["expired"] += 1
            continue
        limits = get_facebook_limits_status(urgent=bool(JOBS_MODE and article.get("job_publish_immediately")))
        if not limits["allowed_now"]:
            stats["skipped"] += 1
            continue
        result = post_one_article_to_facebook(article.get("id") or article.get("url"), respect_limits=True)
        stats["created"] += int(bool(result.get("posted")))
        result_article = result.get("article") or {}
        stats["failed"] += int(
            not result.get("posted")
            and not result.get("deferred")
            and not result.get("delivery_uncertain")
            and not result.get("dropped_from_social_queue")
        )
        break
    return stats


def backfill_facebook_posts():
    """Safely drain old Facebook work without bypassing Page pacing.

    A manual backfill may create at most one new feed post per invocation. It
    still honors slots, daily limits and the safety interval, and retries at
    most one known-failed first comment.
    """
    queue = load_article_queue()
    sync_stats = (_sync_jobs_facebook_queue(queue))
    new_post_candidates, comment_retry_candidates = _facebook_backfill_candidates(
        queue.get("articles", [])
    )
    stats = {
        "checked": len(new_post_candidates) + len(comment_retry_candidates),
        "created": 0,
        "failed": 0,
        "skipped": 0,
        "comments_created": 0,
        "latest_facebook_post_id": "",
        "latest_facebook_comment_id": "",
        "results": [],
        "queued": sync_stats.get("queued", 0),
        "expired": sync_stats.get("expired", 0),
        "revived": sync_stats.get("revived", 0),
        "pending": len(new_post_candidates),
    }

    if comment_retry_candidates:
        article = comment_retry_candidates[0]
        result = retry_facebook_first_comment(article.get("id") or article.get("url"))
        stats["results"].append(result)
        result_article = result.get("article") or {}
        if result_article.get("facebook_comment_id"):
            stats["comments_created"] = 1
            stats["latest_facebook_comment_id"] = result_article.get("facebook_comment_id", "")
        elif not result.get("delivery_uncertain"):
            stats["failed"] += 1

    for article in new_post_candidates:
        if _job_facebook_expired(article):
            if _mark_facebook_expired(article):
                save_article_queue(queue)
                stats["expired"] += 1
            continue
        limits = get_facebook_limits_status(
            urgent=bool(JOBS_MODE and article.get("job_publish_immediately"))
        )
        if not limits.get("allowed_now"):
            stats["skipped"] += 1
            continue
        result = post_one_article_to_facebook(
            target_article_id=article.get("id") or article.get("url"),
            respect_limits=True,
        )
        stats["results"].append(result)
        result_article = result.get("article") or {}
        if result_article.get("facebook_post_id"):
            stats["created"] = 1
            stats["latest_facebook_post_id"] = result_article.get("facebook_post_id", "")
            stats["latest_facebook_comment_id"] = result_article.get("facebook_comment_id", "")
        elif not result.get("deferred") and not result.get("delivery_uncertain"):
            stats["failed"] += 1
        break

    return stats


def preview_next_facebook_post(target_article_id=None, include_drafts=False):
    """
    Build a read-only preview for an eligible Facebook article.
    This never calls Facebook, Blogger, or any publishing API.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])
    if target_article_id:
        article = _target_article(articles, target_article_id=target_article_id)
        if article and not _eligible_for_preview(article, include_drafts=include_drafts):
            article = None
    else:
        article = _find_latest_preview_article(articles, include_drafts=include_drafts)

    if not article:
        return {
            "available": False,
            "article": None,
            "error": "No eligible Blogger article for Facebook preview found.",
            "preview_status": "unavailable",
        }

    blogger_url = _blogger_post_url(article) or _valid_public_blogger_url(article.get("blogger_draft_url"))
    if not blogger_url:
        return {
            "available": False,
            "article": article,
            "error": "Selected Blogger article does not have a usable post permalink.",
            "preview_status": "unavailable",
        }
    preview_status = "ok"
    try:
        blueprint = _prepare_facebook_post(article, articles, blogger_url=blogger_url)
    except Exception as error:
        return {
            "available": False,
            "article": article,
            "error": _redact_facebook_error(str(error)),
            "preview_status": "unavailable",
        }
    caption_pattern = blueprint["style"]
    post_text, hashtags = _split_caption_parts(blueprint["caption"])

    return {
        "available": True,
        "preview_status": preview_status,
        "article": article,
        "selected_style": caption_pattern,
        "selected_structure": blueprint["structure"],
        "hook": blueprint["hook"],
        "post_text": post_text,
        "hashtags": hashtags,
        "first_comment_text": _first_comment_text(blogger_url),
        "link_mode": FACEBOOK_LINK_MODE_ENFORCED,
        "image_url": _main_image_url(article),
        "visual_title": (_job_visual_title(article)),
        "template_key": (article.get("facebook_template_key", "")),
        "template_file": (article.get("facebook_template_file", "")),
        "template_reason": (article.get("facebook_template_reason", "")),
        "error": "",
    }


def get_facebook_status():
    queue = load_article_queue()
    _sync_jobs_facebook_queue(queue)
    articles = queue.get("articles", [])
    published = [article for article in articles if _has_blogger_live_publish(article)]
    without_post = [
        article
        for article in published
        if not article.get("facebook_post_id")
        and article.get("facebook_status") in {"facebook_pending", "failed"}
    ]
    pending = [
        article
        for article in published
        if not article.get("facebook_post_id")
        and article.get("facebook_status") == "facebook_pending"
    ]
    social_expired = [
        article
        for article in published
        if not article.get("facebook_post_id")
        and article.get("facebook_status") == "facebook_expired"
    ]
    posted = [article for article in published if article.get("facebook_post_id")]
    delivery_uncertain = [
        article for article in published
        if article.get("facebook_status") == "delivery_uncertain"
    ]
    comment_uncertain = [
        article for article in published
        if article.get("facebook_status") == "posted_comment_uncertain"
    ]

    return {
        "auto_post_enabled": FACEBOOK_AUTO_POST,
        "page_id_configured": bool(FACEBOOK_PAGE_ID),
        "token_configured": bool(FACEBOOK_PAGE_ACCESS_TOKEN),
        "published_without_facebook": len(without_post),
        "facebook_pending_count": len(pending),
        "facebook_expired_count": len(social_expired),
        "posted_to_facebook": len(posted),
        "delivery_uncertain_count": len(delivery_uncertain),
        "comment_uncertain_count": len(comment_uncertain),
        "latest_eligible": _find_latest_eligible_article(articles),
    }
