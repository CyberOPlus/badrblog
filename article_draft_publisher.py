# ============================================================
# article_draft_publisher.py - Phase 7 Blogger Draft Publishing
# ============================================================

import hashlib
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
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
from production_logging import html_word_count, log_event
from quality_gate import validate_before_publish
from internal_link_cache import (
    apply_link_enrichment,
    record_published_article,
)
from jobposting import append_jobposting, jobposting_validation_errors
from google_indexing import notify_job_url
from job_document_renderer import render_job_document_pages
from source_sanitizer import sanitize_source_links
from company_logo_resolver import refresh_company_logo, verified_company_logo
from utils.facebook_image_generator import generate_job_article_cover

TEMPORARY_BLOGGER_HTTP_STATUSES = {429, 500, 502, 503, 504}


class JobLogoPendingError(RuntimeError):
    """A verified employer logo is not ready yet; retry the job later."""

JOB_ARTICLE_COVER_DIR = Path("assets/generated/job-articles")
JOB_ARTICLE_RAW_BASE = "https://raw.githubusercontent.com/CyberOPlus/badrblog/main"


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")



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
    ai_provider = str(article.get("ai_provider_used") or "").strip().lower()
    return (
        article.get("status") in {"selected", "draft_created", "published"}
        and article.get("processing_status") == "ready_for_ai"
        and article.get("ai_status") == "completed"
        and article.get("ai_quality_status") == "passed"
        and bool(ai_provider)
        and not ai_provider.startswith("deterministic:")
        and not article.get("ai_deterministic_fallback")
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
    if (not JOBS_MODE) and words < MIN_PUBLISHABLE_WORDS:
        log_event(
            "article_skipped_too_short",
            article_id=article.get("id"),
            words=words,
            reason=f"minimum {MIN_PUBLISHABLE_WORDS}",
        )
        article["final_word_count"] = words
        return f"article too short ({words} words; minimum {MIN_PUBLISHABLE_WORDS})"

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


def _jobs_quality_error_is_ai_repairable(error):
    reason = str(error or "").casefold()
    backend_only_hints = (
        "generated cover image",
        "missing, reordered, duplicated, or unverified images",
        "rendered official pdf page is missing",
        "rendered official pdf pages are duplicated or out of order",
        "logo",
    )
    return not any(hint in reason for hint in backend_only_hints)


def _block_publish(queue, article, error, result_shape):
    article["publish_status"] = "failed"
    article["publish_error"] = f"Publish blocked: {error}"
    article["publish_blocked_reason"] = error

    if JOBS_MODE and _jobs_quality_error_is_ai_repairable(error):
        provider_used = str(article.get("ai_provider_used") or "").strip()
        provider_family = provider_used.split(":", 1)[0].strip().lower()
        article["ai_status"] = "failed"
        article["ai_quality_status"] = "pre_publish_failed_retry_pending"
        article["ai_retry_pending"] = True
        article["ai_retry_origin"] = "pre_publish_quality"
        article["ai_retry_reason"] = str(error)
        if provider_family:
            article["ai_retry_provider"] = provider_family
        log_event(
            "jobs_pre_publish_quality_returned_to_ai",
            article_id=article.get("id"),
            provider=provider_family,
            reason=error,
        )

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


def _alpha_letters(seed, length=8):
    digest = hashlib.sha256(str(seed or "job").encode("utf-8")).digest()
    return "".join(chr(ord("a") + (byte % 26)) for byte in digest[:length])


def _attempt_letters(value):
    try:
        number = max(0, int(value or 0))
    except (TypeError, ValueError):
        number = 0
    if number <= 0:
        return ""
    letters = ""
    while number:
        number -= 1
        letters = chr(ord("a") + (number % 26)) + letters
        number //= 26
    return letters


def _permalink_seed_title(article):
    slug = str(article.get("seo_slug") or article.get("desired_slug") or "").strip().casefold()
    slug = re.sub(r"[^a-z-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        seed = (
            article.get("job_campaign_id")
            or article.get("canonical_url")
            or article.get("url")
            or article.get("job_title")
            or "job"
        )
        slug = "job-" + _alpha_letters(seed)
    attempt = _attempt_letters(article.get("permalink_attempt"))
    if attempt:
        slug = f"{slug}-{attempt}"
    return slug.replace("-", " ").strip()


def _job_permalink_stem(url):
    path = urlparse(str(url or "")).path.rstrip("/")
    filename = path.rsplit("/", 1)[-1]
    return re.sub(r"\.html?$", "", filename, flags=re.I)


def _reject_numeric_new_job_permalink(service, post, article):
    if not JOBS_MODE:
        return
    url = str((post or {}).get("url") or "").strip()
    stem = _job_permalink_stem(url)
    if not stem or not re.search(r"\d", stem):
        return

    article["blogger_numeric_permalink_rejected"] = url
    article["permalink_attempt"] = int(article.get("permalink_attempt") or 0) + 1
    delete_error = ""
    try:
        request = service.posts().delete(blogId=BLOG_ID, postId=post["id"])
        _execute_blogger_request(request, "delete numeric-permalink Jobs post", safe_to_retry=True)
    except Exception as error:
        delete_error = str(error)
    for key in ("blogger_post_id", "blogger_post_url", "blogger_draft_id", "blogger_draft_url"):
        article.pop(key, None)
    log_event(
        "job_numeric_permalink_rejected",
        article_id=article.get("id"),
        url=url,
        stem=stem,
        delete_error=delete_error,
    )
    if delete_error:
        raise RuntimeError(
            "Blogger generated a numeric Jobs permalink and cleanup failed: "
            + delete_error
        )
    raise RuntimeError(
        "Blogger generated a numeric Jobs permalink; it was deleted and will retry "
        "with a new alphabetic permalink seed."
    )


def _build_post_body(article, permalink_seed=False):
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
        "title": (
            _permalink_seed_title(article)
            if JOBS_MODE and permalink_seed
            else article.get("seo_title") or article.get("title", "")
        ),
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
        article.get("seo_slug")
        or article.get("desired_slug")
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


def _persist_generated_job_assets(paths, commit_label="job assets"):
    """Commit generated binary assets before Blogger references their raw URLs."""
    if os.getenv("GITHUB_ACTIONS", "").strip().lower() != "true":
        return False

    paths = [Path(path) for path in (paths or []) if Path(path).exists()]
    if not paths:
        return False

    path_args = [path.as_posix() for path in paths]
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(
        ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        check=True,
    )
    subprocess.run(["git", "add", "-f", "--", *path_args], check=True)
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", *path_args],
        check=False,
    )
    if diff.returncode == 0:
        return False
    subprocess.run(
        [
            "git", "commit", "-m",
            f"Add {commit_label} [skip ci]",
            "--", *path_args,
        ],
        check=True,
    )

    last_error = ""
    for attempt in range(1, 4):
        push = subprocess.run(
            ["git", "push", "origin", "HEAD:main"],
            check=False,
            capture_output=True,
            text=True,
        )
        if push.returncode == 0:
            return True

        last_error = (push.stderr or push.stdout or "git push failed").strip()[:500]
        log_event(
            "job_asset_git_push_retry",
            attempt=attempt,
            reason=last_error,
        )
        if attempt >= 3:
            break

        rebase = subprocess.run(
            [
                "git",
                "-c", "rebase.autoStash=true",
                "pull", "--rebase", "origin", "main",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if rebase.returncode != 0:
            last_error = (rebase.stderr or rebase.stdout or "git rebase failed").strip()[:500]
            log_event(
                "job_asset_git_rebase_failed",
                attempt=attempt,
                reason=last_error,
            )
            break

    raise RuntimeError(
        "Could not persist generated job assets after Git retries: "
        + (last_error or "unknown Git error")
    )


def _persist_generated_job_cover(path):
    return _persist_generated_job_assets(
        [path],
        commit_label=f"job article cover {Path(path).stem}",
    )


def _document_render_retry_at(hours=6):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _mark_document_render_retry(article, package, error, *, reason="render_failed"):
    article["job_document_render_status"] = "document_render_retry"
    article["job_document_render_retry_count"] = int(
        article.get("job_document_render_retry_count") or 0
    ) + 1
    article["job_document_render_retry_after"] = _document_render_retry_at()
    article["job_document_render_error"] = str(error or reason)[:1000]
    article["job_document_render_retry_reason"] = reason
    package["job_document_render_status"] = "document_render_retry"
    package["job_document_render_retry_after"] = article["job_document_render_retry_after"]
    article["ai_input_package"] = package
    log_event(
        "job_document_render_deferred",
        article_id=article.get("id"),
        retry_after=article["job_document_render_retry_after"],
        reason=reason,
        error=article["job_document_render_error"],
    )


def _prepare_job_document_page_images(article, force_retry=False):
    if not JOBS_MODE:
        return []

    package = article.get("ai_input_package")
    if not isinstance(package, dict):
        package = {}
        article["ai_input_package"] = package

    existing = (
        article.get("job_document_page_images")
        or package.get("job_document_page_images")
        or []
    )
    previous_pages = list(existing) if isinstance(existing, list) else []
    if existing and not force_retry:
        return existing

    document_links = article.get("job_document_links") or package.get("job_document_links") or []
    if not document_links:
        article["job_document_render_status"] = "not_available"
        package["job_document_render_status"] = "not_available"
        article["ai_input_package"] = package
        return []

    try:
        pages = render_job_document_pages(
            article,
            raw_base=JOB_ARTICLE_RAW_BASE,
        )
    except Exception as error:
        _mark_document_render_retry(
            article,
            package,
            error,
            reason="renderer_exception",
        )
        return []

    failures = int(article.get("job_document_render_failures") or 0)
    attempted = int(article.get("job_document_render_attempted_documents") or 0)

    if not pages:
        if previous_pages:
            article["job_document_page_images"] = list(previous_pages)
            package["job_document_page_images"] = list(previous_pages)
            package["job_document_rendered_pages"] = len(previous_pages)
        if failures > 0 or attempted > 0:
            _mark_document_render_retry(
                article,
                package,
                "Official document rendering produced no publishable page images.",
                reason="no_rendered_pages",
            )
        else:
            article["job_document_render_status"] = "not_available"
            package["job_document_render_status"] = "not_available"
            article["ai_input_package"] = package
        return list(previous_pages)

    try:
        _persist_generated_job_assets(
            [row.get("path") for row in pages],
            commit_label=f"job document pages {_job_cover_key(article)}",
        )
    except Exception as error:
        article["job_document_page_images"] = list(previous_pages)
        package["job_document_page_images"] = list(previous_pages)
        package["job_document_rendered_pages"] = len(previous_pages)
        _mark_document_render_retry(
            article,
            package,
            error,
            reason="asset_persist_failed",
        )
        return list(previous_pages)

    article["job_document_page_images"] = pages
    package["job_document_page_images"] = list(pages)
    package["job_document_rendered_pages"] = len(pages)
    article["ai_input_package"] = package

    if failures > 0:
        article["job_document_render_status"] = "document_render_retry"
        package["job_document_render_status"] = "document_render_retry"
        article["job_document_render_retry_count"] = int(
            article.get("job_document_render_retry_count") or 0
        ) + 1
        article["job_document_render_retry_after"] = _document_render_retry_at()
        article["job_document_render_retry_reason"] = "partial_render"
        article["job_document_render_error"] = (
            f"{failures} official document(s) could not be rendered."
        )
    else:
        article["job_document_render_status"] = "rendered"
        package["job_document_render_status"] = "rendered"
        article.pop("job_document_render_retry_after", None)
        article.pop("job_document_render_retry_reason", None)
        article.pop("job_document_render_error", None)

    log_event(
        "job_document_pages_ready",
        article_id=article.get("id"),
        pages=len(pages),
        documents=len({row.get("document_url") for row in pages}),
        status=article.get("job_document_render_status"),
        failures=failures,
    )
    return pages


def _clear_optional_job_cover(article, package):
    cover_url = str(
        article.get("job_article_cover_url")
        or package.get("job_article_cover_url")
        or ""
    ).strip()
    if cover_url:
        article.pop("job_article_cover_path", None)
        article.pop("job_article_cover_url", None)
        package.pop("job_article_cover_url", None)
    if (
        article.get("main_image_source_type") == "generated_job_template"
        or str(article.get("main_image") or "").strip() == cover_url
    ):
        article["main_image"] = ""
        article["article_images"] = []
        article["extra_article_images"] = []
        article.pop("main_image_source_type", None)
        article.pop("main_image_extraction_method", None)
        package["main_image"] = ""
        package["article_images"] = []
        package["extra_article_images"] = []
        package.pop("cover_alt", None)
        package.pop("cover_width", None)
        package.pop("cover_height", None)


def _prepare_job_article_cover(article):
    if not JOBS_MODE:
        return ""

    package = article.get("ai_input_package")
    if not isinstance(package, dict):
        package = {}
        article["ai_input_package"] = package

    existing_cover = str(
        article.get("job_article_cover_url")
        or package.get("job_article_cover_url")
        or ""
    ).strip()
    if existing_cover and article.get("article_logo_used"):
        article["job_article_cover_status"] = "ready"
        return existing_cover

    job_title = str(
        article.get("seo_title")
        or article.get("title")
        or package.get("title")
        or article.get("job_title")
        or package.get("job_title")
        or article.get("fetched_title")
        or ""
    ).strip()
    employer = str(
        article.get("job_company")
        or package.get("job_company")
        or article.get("source_name")
        or ""
    ).strip()

    logo_info = verified_company_logo(article)
    if not (
        logo_info.get("company_logo_verified")
        and str(logo_info.get("company_logo_url") or "").strip()
    ):
        try:
            logo_info = refresh_company_logo(article)
        except Exception as error:
            logo_info = {}
            article["logo_resolution_error"] = str(error)[:1000]

    logo_verified = bool(logo_info.get("company_logo_verified"))
    logo_url = str(logo_info.get("company_logo_url") or "").strip()

    if not (logo_verified and logo_url):
        _clear_optional_job_cover(article, package)
        article["logo_resolution_status"] = "unavailable_optional"
        article["job_article_cover_status"] = "skipped_missing_verified_logo"
        article["article_logo_used"] = False
        article["visual_readiness_status"] = "content_ready_visual_optional"
        package["logo_resolution_status"] = "unavailable_optional"
        package["job_article_cover_status"] = "skipped_missing_verified_logo"
        package["article_logo_used"] = False
        article.pop("publish_block_reason", None)
        package.pop("publish_block_reason", None)
        log_event(
            "job_article_cover_skipped_missing_logo",
            article_id=article.get("id"),
            company=employer,
        )
        return ""

    article["logo_resolution_status"] = "verified"
    package["logo_resolution_status"] = "verified"

    cover_key = _job_cover_key(article)
    output_path = JOB_ARTICLE_COVER_DIR / f"{cover_key}.jpg"
    try:
        result = generate_job_article_cover(
            job_title,
            logo_url,
            output_path,
            employer_name=employer,
        )
    except Exception as error:
        result = {"ok": False, "logo_loaded": False, "error": str(error)}

    if not result.get("ok") or not result.get("logo_loaded"):
        _clear_optional_job_cover(article, package)
        article["article_logo_used"] = False
        article["job_article_cover_status"] = "render_retry_optional"
        article["logo_resolution_status"] = "verified_render_failed_optional"
        article["logo_visual_retry_pending"] = True
        article["logo_visual_retry_after"] = _document_render_retry_at(hours=12)
        article["logo_visual_error"] = str(
            result.get("error") or "verified logo was not rendered"
        )[:1000]
        package["article_logo_used"] = False
        package["job_article_cover_status"] = "render_retry_optional"
        package["logo_resolution_status"] = "verified_render_failed_optional"
        article.pop("publish_block_reason", None)
        package.pop("publish_block_reason", None)
        log_event(
            "job_article_cover_render_deferred_optional",
            article_id=article.get("id"),
            company=employer,
            retry_after=article["logo_visual_retry_after"],
            error=article["logo_visual_error"],
        )
        return ""

    article["article_logo_used"] = True
    package["article_logo_used"] = True
    try:
        _persist_generated_job_cover(output_path)
    except Exception as error:
        _clear_optional_job_cover(article, package)
        article["article_logo_used"] = False
        article["job_article_cover_status"] = "asset_persist_retry_optional"
        article["logo_visual_retry_pending"] = True
        article["logo_visual_retry_after"] = _document_render_retry_at(hours=12)
        article["logo_visual_error"] = str(error)[:1000]
        package["article_logo_used"] = False
        package["job_article_cover_status"] = "asset_persist_retry_optional"
        log_event(
            "job_article_cover_asset_persist_deferred_optional",
            article_id=article.get("id"),
            retry_after=article["logo_visual_retry_after"],
            error=article["logo_visual_error"],
        )
        return ""
    public_url = f"{JOB_ARTICLE_RAW_BASE}/{quote(output_path.as_posix(), safe='/')}"
    location = str(
        article.get("job_location")
        or package.get("job_location")
        or ""
    ).strip()
    seo_title = str(article.get("seo_title") or "").strip()
    if seo_title:
        cover_alt = seo_title
    elif job_title and employer and location:
        cover_alt = f"وظيفة {job_title} لدى {employer} في {location}"
    elif job_title and employer:
        cover_alt = f"وظيفة {job_title} لدى {employer}"
    elif job_title:
        cover_alt = f"وظيفة {job_title}"
    else:
        cover_alt = f"فرصة عمل لدى {employer}" if employer else "فرصة عمل"

    article["job_article_cover_path"] = output_path.as_posix()
    article["job_article_cover_url"] = public_url
    article["job_article_cover_status"] = "ready"
    article["visual_readiness_status"] = "ready"
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
    article.pop("logo_visual_retry_pending", None)
    article.pop("logo_visual_retry_after", None)
    article.pop("logo_visual_error", None)
    article.pop("publish_block_reason", None)

    package["main_image"] = public_url
    package["article_images"] = list(article["article_images"])
    package["extra_article_images"] = []
    package["job_article_cover_url"] = public_url
    package["job_article_cover_status"] = "ready"
    package["cover_alt"] = cover_alt
    package["cover_width"] = result.get("width") or ""
    package["cover_height"] = result.get("height") or ""
    package.pop("publish_block_reason", None)

    log_event(
        "job_article_cover_ready",
        article_id=article.get("id"),
        path=output_path.as_posix(),
        url=public_url,
        logo_loaded="yes",
    )
    return public_url


def _sanitize_article_final_html(article):
    source_domain = _source_domain_for_article(article)
    if JOBS_MODE:
        _prepare_job_article_cover(article)
        _prepare_job_document_page_images(article)
    cleaned = format_phase3_article_html(
        article.get("final_html", ""),
        article.get("ai_input_package") or article,
    )

    if JOBS_MODE:
        # Jobs articles keep the AI/editorial body intact. Do not inject random
        # keyword links, a Jobs-hub anchor, or pRelate/"قد يهمك" blocks.
        cleaned = re.sub(r"<script\b[^>]*>.*?</script>", "", cleaned, flags=re.I | re.S).strip()
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
    """Restore the SEO title and append one backend-generated JobPosting block."""
    if not JOBS_MODE or _effective_publish_mode(mode) != "live":
        return post

    post_url = str((post or {}).get("url") or "").strip()
    errors = jobposting_validation_errors(article)
    body = _build_post_body(article)
    if post_url and not errors:
        body["content"] = append_jobposting(body.get("content", ""), article, post_url)
        operation = "restore SEO title and append JobPosting structured data"
    else:
        operation = "restore SEO title without JobPosting structured data"

    request = service.posts().update(blogId=BLOG_ID, postId=post["id"], body=body)
    updated = _execute_blogger_request(request, operation, safe_to_retry=True)
    updated = _ensure_returned_post_url(service, updated)

    if not post_url or errors:
        article["jobposting_schema_status"] = "skipped"
        article["jobposting_schema_errors"] = list(errors or ["missing live post URL"])
        log_event(
            "jobposting_schema_skipped",
            article_id=article.get("id"),
            errors="; ".join(article["jobposting_schema_errors"]),
        )
        return updated

    article["jobposting_schema_status"] = "applied"
    article.pop("jobposting_schema_errors", None)
    log_event(
        "jobposting_schema_applied",
        article_id=article.get("id"),
        post_id=post.get("id"),
        url=post_url,
    )
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
        indexing = notify_job_url(article.get("blogger_post_url", ""), article=article)
        article["google_indexing_status"] = indexing.get("status", "disabled")
        if indexing.get("error"):
            article["google_indexing_error"] = indexing["error"]
        else:
            article.pop("google_indexing_error", None)
    else:
        article["status"] = "draft_created"
        article["draft_created_at"] = now
        article["blogger_draft_id"] = post.get("id", "")
        article["blogger_draft_url"] = post.get("url", "")
        article["publish_status"] = "draft_created"

    article.pop("publish_error", None)
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
    log_event(
        "blogger_publish_result",
        status="failed",
        article_id=article.get("id"),
        error=error,
        source=article.get("source_name"),
    )


def _defer_job_logo(queue, article, error, result_shape):
    now = datetime.now(timezone.utc)
    retry_at = now + timedelta(hours=4)
    article["status"] = "selected"
    article.setdefault("logo_first_wait_at", now.isoformat())
    article["logo_retry_count"] = int(article.get("logo_retry_count") or 0) + 1
    article["publish_status"] = "waiting_for_logo"
    article["publish_error"] = str(error)
    article["candidate_failure_stage"] = "company-logo"
    article["candidate_retry_after"] = retry_at.isoformat()
    article["logo_retry_after"] = retry_at.isoformat()
    save_article_queue(queue)
    log_event(
        "job_logo_deferred",
        article_id=article.get("id"),
        company=article.get("job_company"),
        retry_after=article["logo_retry_after"],
        reason=article.get("publish_block_reason", ""),
    )
    result = dict(result_shape)
    result.update({
        "article": article,
        "error": str(error),
        "deferred": True,
        "retry_after": article["logo_retry_after"],
    })
    return result


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
    try:
        _sanitize_article_final_html(article)
    except JobLogoPendingError as error:
        return _defer_job_logo(
            queue,
            article,
            error,
            {"checked": 1, "created": False},
        )
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
    try:
        _sanitize_article_final_html(article)
    except JobLogoPendingError as error:
        return _defer_job_logo(
            queue,
            article,
            error,
            {
                "checked": 1,
                "duplicate_count": 0,
                "updated_existing": False,
                "created_new": False,
                "slug_warning": slug_warning,
            },
        )
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
    try:
        _sanitize_article_final_html(article)
    except JobLogoPendingError as error:
        return _defer_job_logo(
            queue,
            article,
            error,
            {
                "checked": 1,
                "duplicate_count": 0,
                "updated_existing": False,
                "created_new": False,
                "publishing_mode": publish_mode,
            },
        )
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
        saved_post = _get_saved_post_by_id(service, article, mode=publish_mode)
        if saved_post and (publish_mode == "live" or saved_post.get("status") != "LIVE"):
            generating_permalink = publish_mode == "live" and saved_post.get("status") != "LIVE"
            body = _build_post_body(article, permalink_seed=generating_permalink)
            request = service.posts().update(blogId=BLOG_ID, postId=saved_post["id"], body=body)
            post = _execute_blogger_request(request, f"update saved {publish_mode}", safe_to_retry=True)
            post = _ensure_returned_post_url(service, post)
            post = _publish_if_live(service, post, publish_mode)
            _ensure_post_url_for_mode(post, publish_mode)
            if generating_permalink:
                _reject_numeric_new_job_permalink(service, post, article)
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
            return result

        matches = _find_matching_blogger_posts(service, article)
        duplicate_count = len(matches)
        if matches:
            post_to_update = _choose_post_to_update(matches, article, mode=publish_mode)
            if not post_to_update:
                raise RuntimeError("Matching Blogger post found, but no post is safe to update for this mode.")
            generating_permalink = publish_mode == "live" and post_to_update.get("_matched_status") != "LIVE"
            body = _build_post_body(article, permalink_seed=generating_permalink)
            request = service.posts().update(blogId=BLOG_ID, postId=post_to_update["id"], body=body)
            post = _execute_blogger_request(request, f"update matching {publish_mode}", safe_to_retry=True)
            post = _ensure_returned_post_url(service, post)
            post = _publish_if_live(service, post, publish_mode)
            _ensure_post_url_for_mode(post, publish_mode)
            if generating_permalink:
                _reject_numeric_new_job_permalink(service, post, article)
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
            return result

        body = _build_post_body(
            article,
            permalink_seed=(JOBS_MODE and publish_mode == "live"),
        )
        request = service.posts().insert(blogId=BLOG_ID, body=body, isDraft=(publish_mode != "live"))
        post = _execute_blogger_request(request, f"insert {publish_mode}", safe_to_retry=False)
        post = _ensure_returned_post_url(service, post)
        _ensure_post_url_for_mode(post, publish_mode)
        if JOBS_MODE and publish_mode == "live":
            _reject_numeric_new_job_permalink(service, post, article)
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
    return result

def _retry_time_due(value, now=None):
    text = str(value or "").strip()
    if not text:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc) <= now
    except ValueError:
        return True


def retry_pending_job_document_renders(max_articles=1):
    """
    Retry Jobs PDF page rendering and sync only the same saved Blogger post.

    This path never calls AI and never inserts a new Blogger post.
    """
    if not JOBS_MODE:
        return {
            "checked": 0,
            "rendered": 0,
            "synced": 0,
            "still_pending": 0,
        }

    queue = load_article_queue()
    articles = queue.get("articles", [])
    now = datetime.now(timezone.utc)
    stats = {
        "checked": 0,
        "rendered": 0,
        "synced": 0,
        "still_pending": 0,
    }
    changed = False

    candidates = []
    for article in articles:
        if article.get("archived"):
            continue
        if article.get("job_document_render_status") != "document_render_retry":
            continue
        if article.get("publish_status") not in {"published", "draft_created"}:
            continue
        if not _retry_time_due(article.get("job_document_render_retry_after"), now=now):
            continue
        saved_id = str(
            article.get("blogger_post_id")
            or article.get("blogger_draft_id")
            or ""
        ).strip()
        if not saved_id:
            continue
        candidates.append(article)

    for article in candidates[:max(0, int(max_articles or 0))]:
        stats["checked"] += 1
        package = article.get("ai_input_package")
        if not isinstance(package, dict):
            package = {}
            article["ai_input_package"] = package

        try:
            pages = _prepare_job_document_page_images(article, force_retry=True)
            changed = True
            if not pages:
                stats["still_pending"] += 1
                continue

            stats["rendered"] += 1
            _sanitize_article_final_html(article)

            creds = get_credentials()
            if not creds:
                raise RuntimeError(
                    "Blogger credentials are not available for document render sync."
                )
            service = create_blogger_service(creds)
            if not service or is_local_publisher(service):
                raise RuntimeError(
                    "Blogger service is not available for document render sync."
                )

            _ensure_jobs_target_blog(service)
            mode = (
                "live"
                if article.get("publish_status") == "published"
                else "draft"
            )
            post = _get_saved_post_by_id(service, article, mode=mode)
            if not post or not post.get("id"):
                raise RuntimeError(
                    "Saved Blogger post was not found for document render sync."
                )

            body = _build_post_body(article)
            request = service.posts().update(
                blogId=BLOG_ID,
                postId=post["id"],
                body=body,
            )
            updated = _execute_blogger_request(
                request,
                "sync rendered job document pages",
                safe_to_retry=True,
            )
            updated = _ensure_returned_post_url(service, updated)

            if mode == "live":
                _apply_jobposting_schema(service, updated, article, "live")

            article["job_document_render_synced_at"] = _now_iso()
            if article.get("job_document_render_status") == "rendered":
                article.pop("job_document_render_retry_after", None)
                article.pop("job_document_render_retry_reason", None)
                article.pop("job_document_render_error", None)
            else:
                stats["still_pending"] += 1

            stats["synced"] += 1
            changed = True
            log_event(
                "job_document_render_retry_synced",
                article_id=article.get("id"),
                post_id=post.get("id"),
                pages=len(pages),
                status=article.get("job_document_render_status"),
            )
        except Exception as error:
            _mark_document_render_retry(
                article,
                package,
                error,
                reason="blogger_sync_failed",
            )
            stats["still_pending"] += 1
            changed = True

    if changed:
        save_article_queue(queue)
    return stats

