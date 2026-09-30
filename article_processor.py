# ============================================================
# article_processor.py - Phase 5 AI Input Preparation
# ============================================================

from datetime import datetime

from article_queue import load_article_queue, save_article_queue
from article_selector import normalize_category_label
from config import MIN_EXTRACTED_CHARS, JOBS_MODE
from internal_link_cache import load_internal_link_cache, select_internal_link_candidates
from job_document_renderer import extract_job_document_texts
from job_core import classify_identity


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
    if not JOBS_MODE and not _has_value(article.get("suggested_category")):
        missing.append("suggested_category")
    if article.get("content_fetch_status") != "success":
        missing.append("successful content extraction")
    return missing


def _build_ai_input_package(article):
    full_text = article.get("full_article_text") or article.get("content_full") or article.get("content_preview", "")
    return {
        "title": article.get("fetched_title") or article.get("title", ""),
        "url": article.get("url", ""),
        "source_name": article.get("source_name", ""),
        "official_source": bool(article.get("official_source") or article.get("job_official_source")),
        "job_official_source": bool(article.get("job_official_source") or article.get("official_source")),
        "suggested_category": normalize_category_label(
            article.get("suggested_category")
            or (article.get("category_label") if JOBS_MODE else "")
        ),
        "main_image": "" if JOBS_MODE else article.get("main_image", ""),
        "article_images": [] if JOBS_MODE else article.get("article_images", []),
        "extra_article_images": [] if JOBS_MODE else article.get("extra_article_images", []),
        "meta_description": article.get("meta_description", ""),
        "content_preview": article.get("content_preview", ""),
        "content_preview_chars": len(article.get("content_preview", "")),
        "rss_summary": article.get("rss_summary", ""),
        "full_article_text": full_text,
        "full_article_text_chars": len(full_text),
        "source_tables": article.get("source_tables", []),
        "source_tables_count": article.get("source_tables_count", 0),
        "source_tables_truncated": bool(article.get("source_tables_truncated", False)),
        "enrichment_status": article.get("enrichment_status", ""),
        "source_url": article.get("source_url", ""),
        "source_published_at": article.get("source_published_at", ""),
        "published_at_source": article.get("published_at_source", ""),
        "article_age_hours": article.get("article_age_hours"),
        "trusted_references": article.get("trusted_references", []),
        "related_posts": [] if JOBS_MODE else _related_posts_for(article),
        "labels": article.get("labels", []),
        "job_title": article.get("job_title", ""),
        "job_company": article.get("job_company", ""),
        "job_location": article.get("job_location", ""),
        "job_country": article.get("job_country", ""),
        "job_contract_type": article.get("job_contract_type", ""),
        "job_salary": article.get("job_salary", ""),
        "job_deadline": article.get("job_deadline", ""),
        "job_deadline_display": article.get("job_deadline_display", ""),
        "job_exam_date": article.get("job_exam_date", ""),
        "job_exam_date_display": article.get("job_exam_date_display", ""),
        "job_notice_type": article.get("job_notice_type", "vacancy"),
        "job_notice_type_hint": article.get("job_notice_type", "vacancy"),
        "job_notice_type_source": article.get("job_notice_type_source", "heuristic"),
        "job_notice_status": article.get("job_notice_status", ""),
        "job_published_at": article.get("job_published_at", ""),
        "job_published_at_display": article.get("job_published_at_display", ""),
        "job_external_reference": article.get("job_external_reference", ""),
        "ats_reference": article.get("ats_reference", ""),
        "job_application_url": article.get("job_application_url", ""),
        "job_application_link_kind": article.get("job_application_link_kind", ""),
        "job_application_is_specific": bool(article.get("job_application_is_specific", False)),
        "job_application_is_official_channel": bool(article.get("job_application_is_official_channel", False)),
        "job_detail_url": article.get("job_detail_url", ""),
        "job_action_links": article.get("job_action_links", []),
        "job_document_links": article.get("job_document_links", []),
        "job_document_texts": article.get("job_document_texts", []),
        "job_document_text_pages": article.get("job_document_text_pages", 0),
        "job_document_text_chars": article.get("job_document_text_chars", 0),
        "job_document_text_truncated": bool(article.get("job_document_text_truncated", False)),
        "job_number_of_positions": article.get("job_number_of_positions", 0),
        "job_diploma": article.get("job_diploma", ""),
        "job_experience": article.get("job_experience", ""),
        "job_entry_level": bool(article.get("job_entry_level", False)),
        "job_remote": bool(article.get("job_remote", False)),
        "job_visa_sponsorship": bool(article.get("job_visa_sponsorship", False)),
        "job_eligibility": article.get("job_eligibility", ""),
        "job_score": article.get("job_score", 0),
        "desired_slug": article.get("desired_slug", ""),
        "company_logo_url": article.get("company_logo_url", ""),
        "company_logo_verified": bool(article.get("company_logo_verified", False)),
        "company_logo_confidence": article.get("company_logo_confidence", 0),
        "company_logo_source": article.get("company_logo_source", ""),
        "company_official_domain": article.get("company_official_domain", ""),
    }


def _related_posts_for(article):
    cache_data, _stats = load_internal_link_cache(save=True)
    return [
        {"title": candidate.get("title", ""), "url": candidate.get("url", "")}
        for candidate in select_internal_link_candidates(article, cache_data.get("links", []))
    ]


def resolve_identity_pending_articles(target_article_id=None):
    """
    Resolve ambiguous Jobs identities without AI or publishing.

    identity_pending is temporary: gather/cache official PDF text evidence, then
    re-run identity classification. Only a confirmed duplicate becomes skipped.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])

    checked = 0
    resolved_ready = 0
    duplicates = 0
    still_pending = 0
    evidence_errors = 0

    for article in articles:
        if target_article_id and target_article_id not in {article.get("id"), article.get("url")}:
            continue
        if article.get("status") != "identity_pending":
            continue
        if article.get("content_fetch_status") != "success":
            continue

        checked += 1
        article["identity_pending_last_checked_at"] = _now_iso()

        if article.get("job_document_links"):
            try:
                extract_job_document_texts(article)
                article.pop("identity_pending_evidence_error", None)
            except Exception as error:
                # Identity uncertainty must never be converted into a permanent
                # failure because one official PDF is temporarily unavailable.
                article["identity_pending_evidence_error"] = str(error)
                evidence_errors += 1

        article["identity_pending_evidence_checked_at"] = _now_iso()
        article["identity_pending_evidence_status"] = (
            "documents_checked"
            if article.get("job_document_links")
            else "no_official_documents"
        )

        decision = classify_identity(article)
        article["job_identity_action"] = decision["action"]
        article["job_identity_reason"] = decision["reason"]

        if decision["action"] == "duplicate":
            article["status"] = "skipped"
            article["skip_reason"] = f"job duplicate confirmed: {decision['reason']}"
            article["job_identity_final"] = True
            article["job_duplicate_confirmed_at"] = _now_iso()
            duplicates += 1
            continue

        if decision["action"] == "hold":
            article["status"] = "identity_pending"
            article["job_identity_final"] = False
            article.pop("skip_reason", None)
            still_pending += 1
            continue

        # new/new_campaign/update are publishable identity outcomes. Return the
        # candidate to the normal ready queue; the regular selector will score,
        # schedule and prepare it again before any AI/Blogger work.
        article["status"] = "ready"
        article["job_identity_final"] = False
        article["identity_pending_resolved_at"] = _now_iso()
        article.pop("skip_reason", None)
        article.pop("job_duplicate_confirmed_at", None)
        resolved_ready += 1

    if checked:
        save_article_queue(queue)

    return {
        "checked": checked,
        "resolved_ready": resolved_ready,
        "duplicates": duplicates,
        "still_pending": still_pending,
        "evidence_errors": evidence_errors,
    }


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
            if JOBS_MODE and article.get("job_document_links"):
                try:
                    extract_job_document_texts(article)
                    article.pop("job_document_text_error", None)
                except Exception as error:
                    # PDF evidence is enrichment. A temporary PDF failure must not
                    # erase the already verified source-page evidence.
                    article["job_document_text_error"] = str(error)
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
