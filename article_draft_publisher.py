# ============================================================
# article_draft_publisher.py - Phase 7 Blogger Draft Publishing
# ============================================================

import hashlib
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from googleapiclient.errors import HttpError

from article_queue import load_article_queue, save_article_queue
from article_ai_processor import MIN_PUBLISHABLE_WORDS, format_phase3_article_html, validate_phase3_article_quality
from article_selector import normalize_category_label
from blogger_client import create_blogger_service, get_credentials, is_local_publisher
from config import (
    BLOG_ID,
    MAX_RETRIES,
    PUBLISH_MODE,
    RETRY_DELAY,
    SAFE_MODE,
    JOBS_MODE,
    JOBS_TEST_MODE,
    JOBS_EXPECTED_BLOG_HOST,
)
from notifier import notify_blogger_result
from production_logging import html_word_count, log_event
from quality_gate import validate_before_publish
from internal_link_cache import apply_link_enrichment, record_published_article
from source_sanitizer import sanitize_source_links
from jobposting import append_jobposting
from utils.facebook_image_generator import generate_job_article_cover

TEMPORARY_BLOGGER_HTTP_STATUSES = {429, 500, 502, 503, 504}
JOB_ARTICLE_COVER_DIR = Path("assets/generated/job-articles")
JOB_ARTICLE_RAW_BASE = "https://raw.githubusercontent.com/CyberOPlus/badrblog/main"


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _log_notification_result(label, result):
    if result.get("sent") or result.get("skipped"):
        return
    print(f"Telegram {label} notification failed: {result.get('reason', 'unknown error')}")


def _effective_publish_mode(mode=None):
    if SAFE_MODE:
        return "draft"
    requested_mode = mode or PUBLISH_MODE
    return "live" if PUBLISH_MODE == "live" and requested_mode == "live" else "draft"


def _execute_blogger_request(request, operation, safe_to_retry=True):
    for attempt in range(MAX_RETRIES + 1):
        try:
            return request.execute()
        except HttpError as error:
            status = getattr(error.resp, "status", None)
            can_retry = (
                safe_to_retry
                and status in TEMPORARY_BLOGGER_HTTP_STATUSES
                and attempt < MAX_RETRIES
            )
            log_event(
                "blogger_api_error",
                operation=operation,
                status=status,
                attempt=attempt + 1,
                retry="yes" if can_retry else "no",
                error=error._get_reason().strip(),
            )
            if not can_retry:
                raise
            time.sleep(RETRY_DELAY * (attempt + 1))

    raise RuntimeError(f"Blogger API operation failed after retries: {operation}")


def _eligible_for_publish(article):
    return (
        article.get("status") in {"selected", "draft_created", "published"}
        and article.get("processing_status") == "ready_for_ai"
        and article.get("ai_status") == "completed"
        and bool(article.get("final_html"))
    )


def _article_word_count(article):
    try:
        stored = int(article.get("final_word_count") or 0)
    except (TypeError, ValueError):
        stored = 0
    return stored or html_word_count(article.get("final_html", ""))


def _publish_quality_error(article, articles):
    words = _article_word_count(article)
    minimum_publishable_words = 100 if JOBS_MODE else MIN_PUBLISHABLE_WORDS
    if words < minimum_publishable_words:
        log_event(
            "article_skipped_too_short",
            article_id=article.get("id"),
            words=words,
            reason=f"minimum {minimum_publishable_words}",
        )
        article["final_word_count"] = words
        return f"article too short ({words} words; minimum {minimum_publishable_words})"

    result = validate_before_publish(article, existing_articles=articles)
    article["pre_publish_quality"] = result.to_dict()
    article["final_word_count"] = result.word_count or _article_word_count(article)
    if not result.passed:
        return result.reason
    phase3_reason = validate_phase3_article_quality(article)
    if phase3_reason:
        return phase3_reason
    if result.warnings:
        article["pre_publish_warnings"] = list(result.warnings)
    else:
        article.pop("pre_publish_warnings", None)
    return ""


def _block_publish(queue, article, error, result_shape):
    article["publish_status"] = "failed"
    article["publish_error"] = f"Publish blocked: {error}"
    article["publish_blocked_reason"] = error
    article.pop("telegram_blogger_notified", None)
    article.pop("telegram_blogger_event_key", None)
    save_article_queue(queue)
    log_event(
        "blogger_publish_blocked",
        article_id=article.get("id"),
        source=article.get("source_name"),
        reason=error,
        words=_article_word_count(article),
    )
    result = dict(result_shape)
    result.update({"article": article, "error": article["publish_error"]})
    _log_notification_result("Blogger", notify_blogger_result(queue, article, result, stage="quality gate"))
    return result


def _ensure_post_url_for_mode(post, mode):
    if _effective_publish_mode(mode) != "live":
        return
    post_url = str(post.get("url") or "").strip()
    parsed = urlparse(post_url)
    if not post_url or not parsed.netloc or parsed.path.strip("/") == "":
        raise RuntimeError("Blogger did not return a live post URL; refusing downstream promotion.")
    if JOBS_MODE and JOBS_TEST_MODE:
        host = parsed.netloc.casefold().removeprefix("www.")
        expected = str(JOBS_EXPECTED_BLOG_HOST or "").casefold().removeprefix("www.")
        if expected and host != expected:
            raise RuntimeError(
                f"Jobs test safety blocked unexpected Blogger host: {host}; expected {expected}."
            )


def _ensure_jobs_target_blog(service):
    """Block any Jobs test write before posts.insert/update if BLOG_ID points elsewhere."""
    if not (JOBS_MODE and JOBS_TEST_MODE):
        return
    request = service.blogs().get(blogId=BLOG_ID)
    blog = _execute_blogger_request(request, "verify jobs target blog", safe_to_retry=True)
    blog_url = str((blog or {}).get("url") or "").strip()
    parsed = urlparse(blog_url)
    host = parsed.netloc.casefold().removeprefix("www.")
    expected = str(JOBS_EXPECTED_BLOG_HOST or "").casefold().removeprefix("www.")
    if not expected or host != expected:
        raise RuntimeError(
            f"Jobs test safety blocked BLOG_ID target: {host or 'unknown'}; expected {expected}."
        )


def _build_post_body(article):
    content = article.get("final_html", "")
    labels = (
        [str(label).strip() for label in (article.get("labels") or []) if str(label).strip()]
        if JOBS_MODE
        else [normalize_category_label(article.get("suggested_category", ""))]
    )
    if JOBS_MODE and "jobs" not in labels:
        labels.insert(0, "jobs")
    body = {
        "kind": "blogger#post",
        "title": article.get("seo_title") or article.get("title", ""),
        "content": content,
        "labels": list(dict.fromkeys(labels)),
    }

    if article.get("seo_description"):
        body["customMetaData"] = article["seo_description"]

    return body


def _source_domain_for_article(article):
    for key in ("original_url", "url", "source_url", "canonical_url"):
        parsed = urlparse(str(article.get(key) or "").strip())
        if parsed.netloc:
            return parsed.netloc
    return ""


def _job_cover_key(article):
    candidate = str(
        article.get("desired_slug")
        or article.get("seo_slug")
        or article.get("job_campaign_id")
        or ""
    ).strip().casefold()
    candidate = re.sub(r"[^a-z0-9-]+", "-", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-")
    if candidate:
        return candidate[:100]
    seed = str(
        article.get("canonical_url")
        or article.get("url")
        or article.get("job_title")
        or article.get("title")
        or "job"
    )
    return "job-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _persist_generated_job_cover(path):
    """Commit the generated binary before Blogger references its public raw URL."""
    if os.getenv("GITHUB_ACTIONS", "").strip().lower() != "true":
        return False

    path = Path(path)
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(
        ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        check=True,
    )
    subprocess.run(["git", "add", "-f", "--", path.as_posix()], check=True)
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", path.as_posix()],
        check=False,
    )
    if diff.returncode == 0:
        return False
    subprocess.run(
        [
            "git", "commit", "-m",
            f"Add job article cover {path.stem} [skip ci]",
            "--", path.as_posix(),
        ],
        check=True,
    )
    subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
    return True


def _prepare_job_article_cover(article):
    if not JOBS_MODE:
        return ""

    package = article.get("ai_input_package")
    if not isinstance(package, dict):
        package = {}
        article["ai_input_package"] = package

    job_title = str(
        article.get("job_title")
        or package.get("job_title")
        or article.get("fetched_title")
        or article.get("title")
        or ""
    ).strip()
    employer = str(
        article.get("job_company")
        or package.get("job_company")
        or article.get("source_name")
        or ""
    ).strip()
    logo_url = str(
        article.get("company_logo_url")
        or package.get("company_logo_url")
        or ""
    ).strip()

    cover_key = _job_cover_key(article)
    output_path = JOB_ARTICLE_COVER_DIR / f"{cover_key}.jpg"
    result = generate_job_article_cover(
        job_title,
        logo_url,
        output_path,
        employer_name=employer,
    )
    if not result.get("ok"):
        raise RuntimeError(
            "Could not generate the required job article cover: "
            + str(result.get("error") or "unknown error")
        )

    _persist_generated_job_cover(output_path)
    public_url = f"{JOB_ARTICLE_RAW_BASE}/{quote(output_path.as_posix(), safe='/')}"
    cover_alt = " - ".join(part for part in (job_title, employer) if part) or "فرصة عمل"

    article["job_article_cover_path"] = output_path.as_posix()
    article["job_article_cover_url"] = public_url
    article["main_image"] = public_url
    article["article_images"] = [
        {
            "url": public_url,
            "alt": cover_alt,
            "source": "generated_job_template",
        }
    ]
    article["extra_article_images"] = []
    article["main_image_source_type"] = "generated_job_template"
    article["main_image_extraction_method"] = "generated_job_template"

    package["main_image"] = public_url
    package["article_images"] = list(article["article_images"])
    package["extra_article_images"] = []
    package["job_article_cover_url"] = public_url
    package["cover_alt"] = cover_alt
    package["cover_width"] = result.get("width") or ""
    package["cover_height"] = result.get("height") or ""

    log_event(
        "job_article_cover_ready",
        article_id=article.get("id"),
        path=output_path.as_posix(),
        url=public_url,
        logo_loaded="yes" if result.get("logo_loaded") else "no",
    )
    return public_url


def _sanitize_article_final_html(article):
    source_domain = _source_domain_for_article(article)
    if JOBS_MODE:
        _prepare_job_article_cover(article)
    cleaned = format_phase3_article_html(
        article.get("final_html", ""),
        article.get("ai_input_package") or article,
    )

    if JOBS_MODE:
        removed_count = 0
        link_stats = {
            "internal_cache_loaded": 0,
            "expired_internal_links_removed": 0,
            "internal_links_inserted_count": 0,
            "external_trusted_links_inserted_count": 0,
            "internal_cache_saved": False,
        }
    else:
        cleaned, removed_count = sanitize_source_links(cleaned, source_domain)
        cleaned, link_stats = apply_link_enrichment(cleaned, article, source_domain=source_domain)
        cleaned, post_link_removed_count = sanitize_source_links(cleaned, source_domain)
        removed_count += post_link_removed_count

    article["final_html"] = cleaned
    article["blogger_article_html"] = cleaned
    article["removed_source_links_count"] = removed_count
    article["internal_cache_loaded"] = link_stats.get("internal_cache_loaded", 0)
    article["expired_internal_links_removed"] = link_stats.get("expired_internal_links_removed", 0)
    article["internal_links_inserted_count"] = link_stats.get("internal_links_inserted_count", 0)
    article["external_trusted_links_inserted_count"] = link_stats.get("external_trusted_links_inserted_count", 0)
    article["internal_cache_saved"] = link_stats.get("internal_cache_saved", False)
    article["final_word_count"] = html_word_count(cleaned)
    return removed_count

def _list_posts_by_status(service, status):
    try:
        response = (
            service.posts()
            .list(blogId=BLOG_ID, status=status, fetchBodies=False, maxResults=50)
        )
        response = _execute_blogger_request(response, f"list {status}", safe_to_retry=True)
    except HttpError:
        return []
    return response.get("items", []) or []


def _find_matching_blogger_posts(service, article):
    title = (article.get("seo_title") or article.get("title") or "").strip().casefold()
    draft_id = str(article.get("blogger_draft_id") or "").strip()
    saved_post_id = str(article.get("blogger_post_id") or "").strip()
    matches = []
    seen_ids = set()

    for status in ("DRAFT", "LIVE"):
        for post in _list_posts_by_status(service, status):
            post_id = str(post.get("id") or "")
            post_title = (post.get("title") or "").strip().casefold()

            is_match = False
            if saved_post_id and post_id == saved_post_id:
                is_match = True
            if draft_id and post_id == draft_id:
                is_match = True
            if title and post_title == title:
                is_match = True

            if is_match and post_id not in seen_ids:
                seen_ids.add(post_id)
                post = dict(post)
                post["_matched_status"] = status
                matches.append(post)

    return matches


def _get_saved_post_by_id(service, article, mode=None):
    publish_mode = _effective_publish_mode(mode)
    saved_id = str(article.get("blogger_post_id") or "").strip()
    if not saved_id:
        saved_id = str(article.get("blogger_draft_id") or "").strip()
    if not saved_id:
        return None
    try:
        request = service.posts().get(blogId=BLOG_ID, postId=saved_id)
        post = _execute_blogger_request(request, "get saved post", safe_to_retry=True)
    except HttpError:
        return None
    post = dict(post)
    post["_matched_status"] = post.get("status", "DRAFT")
    return post


def _choose_post_to_update(matches, article, mode=None):
    publish_mode = _effective_publish_mode(mode)
    saved_id = str(article.get("blogger_post_id") or "").strip()
    if not saved_id and publish_mode == "draft":
        saved_id = str(article.get("blogger_draft_id") or "").strip()

    if saved_id:
        for post in matches:
            if str(post.get("id") or "") == saved_id:
                return post

    if publish_mode == "live":
        for post in matches:
            if post.get("_matched_status") == "LIVE":
                return post
        for post in matches:
            if post.get("_matched_status") == "DRAFT":
                return post
        return None

    for post in matches:
        if post.get("_matched_status") == "DRAFT":
            return post
    return None


def _ensure_returned_post_url(service, post):
    post = dict(post or {})
    if post.get("url") or not post.get("id"):
        return post

    request = service.posts().get(blogId=BLOG_ID, postId=post["id"])
    refreshed = _execute_blogger_request(request, "refresh post url", safe_to_retry=True)
    post.update(refreshed or {})
    return post


def _publish_if_live(service, post, mode):
    if _effective_publish_mode(mode) != "live":
        return post
    if post.get("status") == "LIVE" and post.get("url"):
        return post
    request = service.posts().publish(blogId=BLOG_ID, postId=post["id"])
    published = _execute_blogger_request(request, "publish existing post", safe_to_retry=True)
    return _ensure_returned_post_url(service, published)


def _apply_jobposting_schema(service, post, article, mode):
    """Append one idempotent JobPosting block after Blogger returns the real URL."""
    if not JOBS_MODE or _effective_publish_mode(mode) != "live":
        return post
    post_url = str(post.get("url") or "").strip()
    if not post_url:
        return post

    content = append_jobposting(article.get("final_html", ""), article, post_url)
    if content == article.get("final_html", ""):
        return post

    article["final_html"] = content
    article["blogger_article_html"] = content
    body = _build_post_body(article)
    request = service.posts().update(blogId=BLOG_ID, postId=post["id"], body=body)
    updated = _execute_blogger_request(request, "append JobPosting schema", safe_to_retry=True)
    updated = _ensure_returned_post_url(service, updated)
    _ensure_post_url_for_mode(updated, mode)
    return updated


def _apply_success(article, post, mode):
    publish_mode = _effective_publish_mode(mode)
    now = _now_iso()
    article["blogger_post_id"] = post.get("id", "")
    article["blogger_post_url"] = post.get("url", "")
    article["final_word_count"] = _article_word_count(article)

    if publish_mode == "live":
        article["status"] = "published"
        article["published_at"] = now
        article["publish_status"] = "published"
        cache_stats = record_published_article(article, article.get("blogger_post_url", ""))
        article["internal_cache_saved"] = bool(cache_stats.get("saved"))
    else:
        article["status"] = "draft_created"
        article["draft_created_at"] = now
        article["blogger_draft_id"] = post.get("id", "")
        article["blogger_draft_url"] = post.get("url", "")
        article["publish_status"] = "draft_created"

    article.pop("publish_error", None)
    article.pop("telegram_blogger_notified", None)
    article.pop("telegram_blogger_event_key", None)
    log_event(
        "blogger_publish_result",
        status="success",
        mode=publish_mode,
        article_id=article.get("id"),
        post_id=article.get("blogger_post_id"),
        url=article.get("blogger_post_url") or article.get("blogger_draft_url"),
        words=article.get("final_word_count"),
        source=article.get("source_name"),
    )


def _apply_failure(article, error):
    article["publish_status"] = "failed"
    article["publish_error"] = str(error)
    article.pop("telegram_blogger_notified", None)
    article.pop("telegram_blogger_event_key", None)
    log_event(
        "blogger_publish_result",
        status="failed",
        article_id=article.get("id"),
        error=error,
        source=article.get("source_name"),
    )


def _custom_slug_warning(article):
    if article.get("seo_slug"):
        article["custom_slug_warning"] = "Custom permalink is not supported by this Blogger API method."
    return article.get("custom_slug_warning", "")


def publish_one_blogger_draft(target_article_id=None):
    """
    Create exactly one Blogger draft from a fully AI-processed selected article.
    This never creates a live post.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])
    eligible = [article for article in articles if _eligible_for_publish(article)]
    if target_article_id:
        eligible = [
            article
            for article in eligible
            if target_article_id in {article.get("id"), article.get("url")}
        ]

    if not eligible:
        return {
            "checked": 0,
            "created": False,
            "article": None,
            "error": "No eligible selected AI-completed article found.",
        }

    article = eligible[0]
    _sanitize_article_final_html(article)
    quality_error = _publish_quality_error(article, articles)
    if quality_error:
        return _block_publish(
            queue,
            article,
            quality_error,
            {"checked": 1, "created": False},
        )

    try:
        creds = get_credentials()
        if not creds:
            raise RuntimeError("Blogger credentials are not available.")

        service = create_blogger_service(creds)
        if not service or is_local_publisher(service):
            raise RuntimeError("Blogger service is not available; refusing local fallback for drafts.")

        title = article.get("seo_title") or article.get("title", "")
        matches = _find_matching_blogger_posts(service, article)
        if matches:
            raise RuntimeError("Duplicate draft found. Updating existing draft instead.")

        _custom_slug_warning(article)

        request = service.posts().insert(blogId=BLOG_ID, body=_build_post_body(article), isDraft=True)
        post = _execute_blogger_request(request, "insert draft", safe_to_retry=False)
        post = _ensure_returned_post_url(service, post)
        _ensure_post_url_for_mode(post, "draft")
        _apply_success(article, post, "draft")
        save_article_queue(queue)
        result = {
            "checked": 1,
            "created": True,
            "article": article,
            "error": "",
        }
        _log_notification_result("Blogger", notify_blogger_result(queue, article, result, stage="create draft"))
        return result

    except HttpError as error:
        details = error._get_reason().strip()
        _apply_failure(article, f"Blogger API error {error.resp.status}: {details}")
    except Exception as error:
        _apply_failure(article, error)

    save_article_queue(queue)
    result = {
        "checked": 1,
        "created": False,
        "article": article,
        "error": article.get("publish_error", ""),
    }
    _log_notification_result("Blogger", notify_blogger_result(queue, article, result, stage="create draft"))
    return result


def fix_or_update_current_blogger_draft(target_article_id=None):
    """
    Update an existing matching Blogger draft instead of creating duplicates.
    Creates a new draft only when no matching draft/post exists.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])
    eligible = [article for article in articles if _eligible_for_publish(article)]
    if target_article_id:
        eligible = [
            article
            for article in eligible
            if target_article_id in {article.get("id"), article.get("url")}
        ]

    if not eligible:
        return {
            "checked": 0,
            "duplicate_count": 0,
            "updated_existing": False,
            "created_new": False,
            "article": None,
            "error": "No eligible selected/draft_created AI-completed article found.",
            "slug_warning": "Custom permalink is not supported by this Blogger API method.",
        }

    article = eligible[0]
    slug_warning = _custom_slug_warning(article)
    _sanitize_article_final_html(article)
    quality_error = _publish_quality_error(article, articles)
    if quality_error:
        return _block_publish(
            queue,
            article,
            quality_error,
            {
                "checked": 1,
                "duplicate_count": 0,
                "updated_existing": False,
                "created_new": False,
                "slug_warning": slug_warning,
            },
        )

    try:
        creds = get_credentials()
        if not creds:
            raise RuntimeError("Blogger credentials are not available.")

        service = create_blogger_service(creds)
        if not service or is_local_publisher(service):
            raise RuntimeError("Blogger service is not available; refusing local fallback for drafts.")

        body = _build_post_body(article)
        saved_draft = _get_saved_post_by_id(service, article, mode="draft")
        if saved_draft and saved_draft.get("status") != "LIVE":
            request = service.posts().update(blogId=BLOG_ID, postId=saved_draft["id"], body=body)
            post = _execute_blogger_request(request, "update saved draft", safe_to_retry=True)
            post = _ensure_returned_post_url(service, post)
            _ensure_post_url_for_mode(post, "draft")
            _apply_success(article, post, "draft")
            article["draft_update_status"] = "updated_existing"
            save_article_queue(queue)
            result = {
                "checked": 1,
                "duplicate_count": 1,
                "updated_existing": True,
                "created_new": False,
                "article": article,
                "error": "",
                "slug_warning": slug_warning,
            }
            _log_notification_result("Blogger", notify_blogger_result(queue, article, result, stage="update draft"))
            return result

        matches = _find_matching_blogger_posts(service, article)
        duplicate_count = len(matches)

        if matches:
            post_to_update = _choose_post_to_update(matches, article, mode="draft")
            if not post_to_update:
                raise RuntimeError("Matching live post found, but no matching draft is safe to update.")
            request = service.posts().update(blogId=BLOG_ID, postId=post_to_update["id"], body=body)
            post = _execute_blogger_request(request, "update matching draft", safe_to_retry=True)
            post = _ensure_returned_post_url(service, post)
            _ensure_post_url_for_mode(post, "draft")
            _apply_success(article, post, "draft")
            article["draft_update_status"] = "updated_existing"
            save_article_queue(queue)
            result = {
                "checked": 1,
                "duplicate_count": duplicate_count,
                "updated_existing": True,
                "created_new": False,
                "article": article,
                "error": "",
                "slug_warning": slug_warning,
            }
            _log_notification_result("Blogger", notify_blogger_result(queue, article, result, stage="update draft"))
            return result

        request = service.posts().insert(blogId=BLOG_ID, body=body, isDraft=True)
        post = _execute_blogger_request(request, "insert draft", safe_to_retry=False)
        post = _ensure_returned_post_url(service, post)
        _ensure_post_url_for_mode(post, "draft")
        _apply_success(article, post, "draft")
        article["draft_update_status"] = "created_new"
        save_article_queue(queue)
        result = {
            "checked": 1,
            "duplicate_count": 0,
            "updated_existing": False,
            "created_new": True,
            "article": article,
            "error": "",
            "slug_warning": slug_warning,
        }
        _log_notification_result("Blogger", notify_blogger_result(queue, article, result, stage="create draft"))
        return result

    except HttpError as error:
        details = error._get_reason().strip()
        _apply_failure(article, f"Blogger API error {error.resp.status}: {details}")
    except Exception as error:
        _apply_failure(article, error)

    save_article_queue(queue)
    result = {
        "checked": 1,
        "duplicate_count": 0,
        "updated_existing": False,
        "created_new": False,
        "article": article,
        "error": article.get("publish_error", ""),
        "slug_warning": slug_warning,
    }
    _log_notification_result("Blogger", notify_blogger_result(queue, article, result, stage="update draft"))
    return result


def publish_one_blogger_post(target_article_id=None, mode=None):
    """
    Create or update exactly one Blogger post in draft or live mode.
    Live mode is used only when mode/PUBLISH_MODE is exactly "live".
    """
    publish_mode = _effective_publish_mode(mode)
    queue = load_article_queue()
    articles = queue.get("articles", [])
    eligible = [article for article in articles if _eligible_for_publish(article)]
    if target_article_id:
        eligible = [
            article
            for article in eligible
            if target_article_id in {article.get("id"), article.get("url")}
        ]

    if not eligible:
        return {
            "checked": 0,
            "duplicate_count": 0,
            "updated_existing": False,
            "created_new": False,
            "article": None,
            "error": "No eligible selected/draft_created AI-completed article found.",
            "publishing_mode": publish_mode,
        }

    article = eligible[0]
    _custom_slug_warning(article)
    _sanitize_article_final_html(article)
    quality_error = _publish_quality_error(article, articles)
    if quality_error:
        return _block_publish(
            queue,
            article,
            quality_error,
            {
                "checked": 1,
                "duplicate_count": 0,
                "updated_existing": False,
                "created_new": False,
                "publishing_mode": publish_mode,
            },
        )

    try:
        creds = get_credentials()
        if not creds:
            raise RuntimeError("Blogger credentials are not available.")

        service = create_blogger_service(creds)
        if not service or is_local_publisher(service):
            raise RuntimeError("Blogger service is not available; refusing local fallback for Blogger publishing.")

        _ensure_jobs_target_blog(service)
        body = _build_post_body(article)
        saved_post = _get_saved_post_by_id(service, article, mode=publish_mode)
        if saved_post and (publish_mode == "live" or saved_post.get("status") != "LIVE"):
            request = service.posts().update(blogId=BLOG_ID, postId=saved_post["id"], body=body)
            post = _execute_blogger_request(request, f"update saved {publish_mode}", safe_to_retry=True)
            post = _ensure_returned_post_url(service, post)
            post = _publish_if_live(service, post, publish_mode)
            _ensure_post_url_for_mode(post, publish_mode)
            post = _apply_jobposting_schema(service, post, article, publish_mode)
            _apply_success(article, post, publish_mode)
            article["draft_update_status" if publish_mode == "draft" else "live_update_status"] = "updated_existing"
            save_article_queue(queue)
            result = {
                "checked": 1,
                "duplicate_count": 1,
                "updated_existing": True,
                "created_new": False,
                "article": article,
                "error": "",
                "publishing_mode": publish_mode,
            }
            _log_notification_result("Blogger", notify_blogger_result(queue, article, result, stage=f"update {publish_mode}"))
            return result

        matches = _find_matching_blogger_posts(service, article)
        duplicate_count = len(matches)
        if matches:
            post_to_update = _choose_post_to_update(matches, article, mode=publish_mode)
            if not post_to_update:
                raise RuntimeError("Matching Blogger post found, but no post is safe to update for this mode.")
            request = service.posts().update(blogId=BLOG_ID, postId=post_to_update["id"], body=body)
            post = _execute_blogger_request(request, f"update matching {publish_mode}", safe_to_retry=True)
            post = _ensure_returned_post_url(service, post)
            post = _publish_if_live(service, post, publish_mode)
            _ensure_post_url_for_mode(post, publish_mode)
            post = _apply_jobposting_schema(service, post, article, publish_mode)
            _apply_success(article, post, publish_mode)
            article["draft_update_status" if publish_mode == "draft" else "live_update_status"] = "updated_existing"
            save_article_queue(queue)
            result = {
                "checked": 1,
                "duplicate_count": duplicate_count,
                "updated_existing": True,
                "created_new": False,
                "article": article,
                "error": "",
                "publishing_mode": publish_mode,
            }
            _log_notification_result("Blogger", notify_blogger_result(queue, article, result, stage=f"update {publish_mode}"))
            return result

        request = service.posts().insert(blogId=BLOG_ID, body=body, isDraft=(publish_mode != "live"))
        post = _execute_blogger_request(request, f"insert {publish_mode}", safe_to_retry=False)
        post = _ensure_returned_post_url(service, post)
        _ensure_post_url_for_mode(post, publish_mode)
        post = _apply_jobposting_schema(service, post, article, publish_mode)
        _apply_success(article, post, publish_mode)
        article["draft_update_status" if publish_mode == "draft" else "live_update_status"] = "created_new"
        save_article_queue(queue)
        result = {
            "checked": 1,
            "duplicate_count": 0,
            "updated_existing": False,
            "created_new": True,
            "article": article,
            "error": "",
            "publishing_mode": publish_mode,
        }
        _log_notification_result("Blogger", notify_blogger_result(queue, article, result, stage=f"create {publish_mode}"))
        return result

    except HttpError as error:
        details = error._get_reason().strip()
        _apply_failure(article, f"Blogger API error {error.resp.status}: {details}")
    except Exception as error:
        _apply_failure(article, error)

    save_article_queue(queue)
    result = {
        "checked": 1,
        "duplicate_count": 0,
        "updated_existing": False,
        "created_new": False,
        "article": article,
        "error": article.get("publish_error", ""),
        "publishing_mode": publish_mode,
    }
    _log_notification_result("Blogger", notify_blogger_result(queue, article, result, stage=f"publish {publish_mode}"))
    return result
