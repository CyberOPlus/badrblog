# ============================================================
# article_draft_publisher.py - Phase 7 Blogger Draft Publishing
# ============================================================

from datetime import datetime

from googleapiclient.errors import HttpError

from article_queue import load_article_queue, save_article_queue
from blogger_client import create_blogger_service, get_credentials, is_local_publisher
from config import BLOG_ID, PUBLISH_MODE
from notifier import notify_blogger_result


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _effective_publish_mode(mode=None):
    requested_mode = mode or PUBLISH_MODE
    return "live" if PUBLISH_MODE == "live" and requested_mode == "live" else "draft"


def _eligible_for_publish(article):
    return (
        article.get("status") in {"selected", "draft_created", "published"}
        and article.get("processing_status") == "ready_for_ai"
        and article.get("ai_status") == "completed"
        and bool(article.get("final_html"))
    )


def _build_post_body(article):
    body = {
        "kind": "blogger#post",
        "title": article.get("seo_title") or article.get("title", ""),
        "content": article.get("final_html", ""),
        "labels": [article.get("suggested_category", "")],
    }

    if article.get("seo_description"):
        body["customMetaData"] = article["seo_description"]

    return body


def _list_posts_by_status(service, status):
    try:
        response = (
            service.posts()
            .list(blogId=BLOG_ID, status=status, fetchBodies=False, maxResults=50)
            .execute()
        )
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
        post = service.posts().get(blogId=BLOG_ID, postId=saved_id).execute()
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

    refreshed = service.posts().get(blogId=BLOG_ID, postId=post["id"]).execute()
    post.update(refreshed or {})
    return post


def _publish_if_live(service, post, mode):
    if _effective_publish_mode(mode) != "live":
        return post
    if post.get("status") == "LIVE" and post.get("url"):
        return post
    published = service.posts().publish(blogId=BLOG_ID, postId=post["id"]).execute()
    return _ensure_returned_post_url(service, published)


def _apply_success(article, post, mode):
    publish_mode = _effective_publish_mode(mode)
    now = _now_iso()
    article["blogger_post_id"] = post.get("id", "")
    article["blogger_post_url"] = post.get("url", "")

    if publish_mode == "live":
        article["status"] = "published"
        article["published_at"] = now
        article["publish_status"] = "published"
    else:
        article["status"] = "draft_created"
        article["draft_created_at"] = now
        article["blogger_draft_id"] = post.get("id", "")
        article["blogger_draft_url"] = post.get("url", "")
        article["publish_status"] = "draft_created"

    article.pop("publish_error", None)
    article.pop("telegram_blogger_notified", None)
    article.pop("telegram_blogger_event_key", None)


def _apply_failure(article, error):
    article["publish_status"] = "failed"
    article["publish_error"] = str(error)
    article.pop("telegram_blogger_notified", None)
    article.pop("telegram_blogger_event_key", None)


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

        post = (
            service.posts()
            .insert(blogId=BLOG_ID, body=_build_post_body(article), isDraft=True)
            .execute()
        )
        post = _ensure_returned_post_url(service, post)
        _apply_success(article, post, "draft")
        save_article_queue(queue)
        result = {
            "checked": 1,
            "created": True,
            "article": article,
            "error": "",
        }
        notify_blogger_result(queue, article, result, stage="create draft")
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
    notify_blogger_result(queue, article, result, stage="create draft")
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
        creds = get_credentials()
        if not creds:
            raise RuntimeError("Blogger credentials are not available.")

        service = create_blogger_service(creds)
        if not service or is_local_publisher(service):
            raise RuntimeError("Blogger service is not available; refusing local fallback for drafts.")

        body = _build_post_body(article)
        saved_draft = _get_saved_post_by_id(service, article, mode="draft")
        if saved_draft and saved_draft.get("status") != "LIVE":
            post = (
                service.posts()
                .update(blogId=BLOG_ID, postId=saved_draft["id"], body=body)
                .execute()
            )
            post = _ensure_returned_post_url(service, post)
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
            notify_blogger_result(queue, article, result, stage="update draft")
            return result

        matches = _find_matching_blogger_posts(service, article)
        duplicate_count = len(matches)

        if matches:
            post_to_update = _choose_post_to_update(matches, article, mode="draft")
            if not post_to_update:
                raise RuntimeError("Matching live post found, but no matching draft is safe to update.")
            post = (
                service.posts()
                .update(blogId=BLOG_ID, postId=post_to_update["id"], body=body)
                .execute()
            )
            post = _ensure_returned_post_url(service, post)
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
            notify_blogger_result(queue, article, result, stage="update draft")
            return result

        post = (
            service.posts()
            .insert(blogId=BLOG_ID, body=body, isDraft=True)
            .execute()
        )
        post = _ensure_returned_post_url(service, post)
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
        notify_blogger_result(queue, article, result, stage="create draft")
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
    notify_blogger_result(queue, article, result, stage="update draft")
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
        creds = get_credentials()
        if not creds:
            raise RuntimeError("Blogger credentials are not available.")

        service = create_blogger_service(creds)
        if not service or is_local_publisher(service):
            raise RuntimeError("Blogger service is not available; refusing local fallback for Blogger publishing.")

        body = _build_post_body(article)
        saved_post = _get_saved_post_by_id(service, article, mode=publish_mode)
        if saved_post and (publish_mode == "live" or saved_post.get("status") != "LIVE"):
            post = (
                service.posts()
                .update(blogId=BLOG_ID, postId=saved_post["id"], body=body)
                .execute()
            )
            post = _ensure_returned_post_url(service, post)
            post = _publish_if_live(service, post, publish_mode)
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
            notify_blogger_result(queue, article, result, stage=f"update {publish_mode}")
            return result

        matches = _find_matching_blogger_posts(service, article)
        duplicate_count = len(matches)
        if matches:
            post_to_update = _choose_post_to_update(matches, article, mode=publish_mode)
            if not post_to_update:
                raise RuntimeError("Matching Blogger post found, but no post is safe to update for this mode.")
            post = (
                service.posts()
                .update(blogId=BLOG_ID, postId=post_to_update["id"], body=body)
                .execute()
            )
            post = _ensure_returned_post_url(service, post)
            post = _publish_if_live(service, post, publish_mode)
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
            notify_blogger_result(queue, article, result, stage=f"update {publish_mode}")
            return result

        post = (
            service.posts()
            .insert(blogId=BLOG_ID, body=body, isDraft=(publish_mode != "live"))
            .execute()
        )
        post = _ensure_returned_post_url(service, post)
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
        notify_blogger_result(queue, article, result, stage=f"create {publish_mode}")
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
    notify_blogger_result(queue, article, result, stage=f"publish {publish_mode}")
    return result
