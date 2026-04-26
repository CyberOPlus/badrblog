# ============================================================
# facebook_publisher.py - Phase 12 Facebook Page Auto-Posting
# ============================================================

import random
import re
from datetime import datetime, timedelta

import requests

from article_queue import load_article_queue, save_article_queue
from config import (
    FACEBOOK_AUTO_POST,
    FACEBOOK_GRAPH_API_URL,
    MAX_FACEBOOK_POSTS_PER_DAY,
    MIN_MINUTES_BETWEEN_FACEBOOK_POSTS,
    FACEBOOK_PAGE_ACCESS_TOKEN,
    FACEBOOK_PAGE_ID,
)
from notifier import notify_facebook_result


CAPTION_STYLES = (
    "breaking_alert",
    "question_hook",
    "insight_knowledge",
    "story_scenario",
    "warning_tip",
)

FORBIDDEN_CAPTION_PHRASES = (
    "مقال",
    "اقرأ المزيد",
    "في هذا المقال",
    "اضغط على الرابط",
    "افتح الرابط",
    "الرابط",
)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _log_notification_result(label, result):
    if result.get("sent") or result.get("skipped"):
        return
    print(f"Telegram {label} notification failed: {result.get('reason', 'unknown error')}")


def _parse_local_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _is_configured():
    return bool(FACEBOOK_AUTO_POST and FACEBOOK_PAGE_ID and FACEBOOK_PAGE_ACCESS_TOKEN)


def _has_blogger_live_publish(article):
    return (
        article.get("status") == "published"
        and article.get("publish_status") == "published"
        and bool(article.get("blogger_post_url"))
    )


def _eligible_for_facebook(article):
    return (
        _has_blogger_live_publish(article)
        and not article.get("facebook_post_id")
        and article.get("facebook_status") in {None, "", "failed"}
    )


def _find_latest_eligible_article(articles):
    eligible = [article for article in articles if _eligible_for_facebook(article)]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda article: (
            article.get("published_at", ""),
            article.get("selected_at", ""),
            article.get("discovered_at", ""),
        ),
    )


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
        and bool(article.get("blogger_draft_url") or article.get("blogger_post_url"))
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
        and article.get("facebook_status") in {None, "", "failed"}
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda article: (
            article.get("published_at", ""),
            article.get("draft_created_at", ""),
            article.get("selected_at", ""),
            article.get("discovered_at", ""),
        ),
    )


def _short_summary(article):
    description = str(article.get("seo_description") or article.get("meta_description") or "").strip()
    if description:
        return description

    preview = " ".join(str(article.get("content_preview") or "").split())
    if len(preview) > 260:
        return preview[:257].rstrip() + "..."
    return preview


def _last_caption_style(articles):
    posted = [
        article
        for article in articles
        if article.get("facebook_posted_at") and article.get("facebook_caption_pattern")
    ]
    if not posted:
        return ""
    latest = max(posted, key=lambda article: article.get("facebook_posted_at", ""))
    return latest.get("facebook_caption_pattern", "")


def _choose_caption_pattern(article, articles):
    last_style = _last_caption_style(articles)
    choices = [style for style in CAPTION_STYLES if style != last_style]
    return random.choice(choices or list(CAPTION_STYLES))


def _hashtags(article):
    text = " ".join(
        str(article.get(field, ""))
        for field in (
            "title",
            "fetched_title",
            "seo_title",
            "suggested_category",
            "content_preview",
        )
    ).casefold()
    tags = []

    def add(tag):
        if tag not in tags and len(tags) < 7:
            tags.append(tag)

    category = article.get("suggested_category", "")
    if "السيبراني" in category or any(word in text for word in ("security", "malware", "breach", "vulnerability", "cve")):
        add("#الأمن_السيبراني")
        add("#CyberSecurity")
    if "ذكاء" in category or any(word in text for word in ("ai", "artificial intelligence", "llm", "gemini", "openai")):
        add("#الذكاء_الاصطناعي")
        add("#AI")
    if "تقنية" in category or any(word in text for word in ("technology", "software", "platform", "training")):
        add("#أخبار_التقنية")
    if "تطبيقات" in category or any(word in text for word in ("app", "android", "ios", "windows")):
        add("#تطبيقات")

    if any(word in text for word in ("teams", "microsoft")):
        add("#MicrosoftTeams")
    if "api" in text:
        add("#API")
    if "malware" in text:
        add("#Malware")
    if any(word in text for word in ("privacy", "data", "training")):
        add("#Data")

    for default_tag in ("#تقنية", "#Tech", "#أخبار_التقنية", "#AI"):
        add(default_tag)

    return tags[:7]


def _clean_caption_line(line):
    cleaned = str(line or "").strip()
    for phrase in FORBIDDEN_CAPTION_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:،")
    return cleaned


def _short_title(article):
    title = article.get("seo_title") or article.get("fetched_title") or article.get("title", "")
    return _clean_caption_line(title)


def _human_summary(article):
    summary = _clean_caption_line(_short_summary(article))
    if len(summary) <= 180:
        return summary
    cut_at = max(summary.rfind("،", 0, 180), summary.rfind(".", 0, 180), summary.rfind(" ", 0, 180))
    if cut_at < 80:
        cut_at = 180
    return summary[:cut_at].rstrip() + "..."


def _build_caption(article, pattern):
    title = _short_title(article)
    summary = _human_summary(article)
    tags = " ".join(_hashtags(article))

    if pattern == "breaking_alert":
        parts = ["🚨 تم اكتشاف تطور مهم في المشهد التقني.", title]
        if summary:
            parts.append(summary)
    elif pattern == "question_hook":
        parts = [f"هل تعلم أن {title}؟"]
        if summary:
            parts.append(summary)
        parts.append("التفاصيل الصغيرة هنا قد تغيّر طريقة فهمنا للمشهد.")
    elif pattern == "insight_knowledge":
        parts = [f"🧠 {title}", "القيمة الحقيقية ليست في الخبر نفسه، بل في ما يكشفه عن الاتجاه القادم."]
        if summary:
            parts.append(summary)
    elif pattern == "story_scenario":
        parts = ["تخيّل نظامًا يعمل تحت ضغط هائل، ثم يتعطل جزء منه فجأة.", title]
        if summary:
            parts.append(summary)
    else:
        parts = ["⚠️ لا تتعامل مع هذا التطور كخبر عابر.", title]
        if summary:
            parts.append(summary)
        parts.append("راقب الأثر العملي قبل أن يصبح واقعا يوميا.")

    if tags:
        parts.append(tags)
    return "\n".join(_clean_caption_line(part) for part in parts if _clean_caption_line(part))[:1200]


def _main_image_url(article):
    if article.get("main_image"):
        return article["main_image"]
    package = article.get("ai_input_package") or {}
    if package.get("main_image"):
        return package["main_image"]
    for image in article.get("article_images") or package.get("article_images") or []:
        if isinstance(image, dict) and image.get("url"):
            return image["url"]
        if isinstance(image, str) and image:
            return image
    return ""


def _post_to_graph(path, payload):
    url = f"{FACEBOOK_GRAPH_API_URL.rstrip('/')}/{path.lstrip('/')}"
    response = requests.post(url, data=payload, timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(f"Facebook Graph API error {response.status_code}: {response.text[:500]}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Facebook Graph API returned an unexpected response.")
    return data


def _publish_facebook_post(article, caption_pattern):
    caption = _build_caption(article, caption_pattern)
    image_url = _main_image_url(article)
    base_payload = {
        "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
    }

    if image_url:
        payload = {
            **base_payload,
            "url": image_url,
            "caption": caption,
            "published": "true",
        }
        data = _post_to_graph(f"{FACEBOOK_PAGE_ID}/photos", payload)
        return data.get("post_id") or data.get("id") or "", "photo"

    payload = {
        **base_payload,
        "message": caption,
    }
    data = _post_to_graph(f"{FACEBOOK_PAGE_ID}/feed", payload)
    return data.get("id") or "", "feed"


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
    return f"🔗 اقرأ المنشور الكامل:\n{blogger_post_url}"


def _apply_failure(article, error):
    article["facebook_status"] = "failed"
    article["facebook_error"] = str(error)
    article.pop("telegram_facebook_notified", None)
    article.pop("telegram_facebook_event_key", None)


def _failure_result(queue, article, error, checked=1, extra=None):
    if article:
        _apply_failure(article, error)
        save_article_queue(queue)
        result = {
            "checked": checked,
            "posted": False,
            "article": article,
            "error": article.get("facebook_error", ""),
        }
        if extra:
            result.update(extra)
        _log_notification_result("Facebook", notify_facebook_result(queue, article, result))
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


def get_facebook_limits_status(now=None):
    now = now or datetime.now()
    queue = load_article_queue()
    posted_times = []

    for article in queue.get("articles", []):
        if article.get("facebook_status") not in {"posted", "posted_comment_failed"}:
            continue
        posted_at = _parse_local_datetime(article.get("facebook_posted_at"))
        if posted_at:
            posted_times.append(posted_at)

    today_posts = [posted_at for posted_at in posted_times if posted_at.date() == now.date()]
    last_post_time = max(posted_times) if posted_times else None
    minutes_since_last = (
        max(0, int((now - last_post_time).total_seconds() // 60))
        if last_post_time
        else None
    )

    daily_blocked = len(today_posts) >= MAX_FACEBOOK_POSTS_PER_DAY
    interval_next_allowed = now
    if last_post_time:
        interval_next_allowed = last_post_time + timedelta(minutes=MIN_MINUTES_BETWEEN_FACEBOOK_POSTS)
    interval_blocked = bool(last_post_time and interval_next_allowed > now)
    daily_next_allowed = (
        datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        if daily_blocked
        else now
    )
    allowed_now = not daily_blocked and not interval_blocked
    next_allowed_time = now if allowed_now else max(daily_next_allowed, interval_next_allowed)

    reasons = []
    if daily_blocked:
        reasons.append("daily Facebook post limit reached")
    if interval_blocked:
        reasons.append("minimum minutes between Facebook posts has not elapsed")

    return {
        "facebook_posts_today": len(today_posts),
        "max_facebook_posts_per_day": MAX_FACEBOOK_POSTS_PER_DAY,
        "last_facebook_post_time": last_post_time,
        "minutes_since_last_facebook_post": minutes_since_last,
        "min_minutes_between_facebook_posts": MIN_MINUTES_BETWEEN_FACEBOOK_POSTS,
        "allowed_now": allowed_now,
        "next_allowed_time": next_allowed_time,
        "reasons": reasons,
    }


def post_one_article_to_facebook(target_article_id=None, respect_limits=True):
    """
    Post exactly one live Blogger article to a Facebook Page.
    This is a no-op unless Facebook auto-posting is explicitly enabled.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])
    article = _target_article(articles, target_article_id=target_article_id)

    if not FACEBOOK_AUTO_POST:
        return _failure_result(queue, article, "FACEBOOK_AUTO_POST is disabled.", checked=0)

    if not FACEBOOK_PAGE_ID:
        return _failure_result(queue, article, "FACEBOOK_PAGE_ID is not configured.", checked=0)

    if not FACEBOOK_PAGE_ACCESS_TOKEN:
        return _failure_result(queue, article, "FACEBOOK_PAGE_ACCESS_TOKEN is not configured.", checked=0)

    if not article:
        return {
            "checked": 0,
            "posted": False,
            "article": None,
            "error": "No eligible published article without Facebook post found.",
        }

    if article.get("facebook_post_id"):
        return {
            "checked": 1,
            "posted": False,
            "article": article,
            "error": "Article already has facebook_post_id; refusing duplicate post.",
        }

    if not _has_blogger_live_publish(article):
        return _failure_result(
            queue,
            article,
            "Article is not a successful live Blogger publish with blogger_post_url.",
        )

    if respect_limits:
        limits = get_facebook_limits_status()
        if not limits["allowed_now"]:
            return _failure_result(
                queue,
                article,
                "; ".join(limits["reasons"]) or "Facebook posting limits blocked this run.",
                extra={"limits": limits},
            )

    try:
        caption_pattern = _choose_caption_pattern(article, articles)
        facebook_post_id, post_type = _publish_facebook_post(article, caption_pattern)
        if not facebook_post_id:
            raise RuntimeError("Facebook Graph API did not return a post id.")

        article["facebook_status"] = "posted"
        article["facebook_post_id"] = facebook_post_id
        article["facebook_posted_at"] = _now_iso()
        article["facebook_post_type"] = post_type
        article["facebook_caption_pattern"] = caption_pattern
        article.pop("facebook_error", None)
        article.pop("telegram_facebook_notified", None)
        article.pop("telegram_facebook_event_key", None)

        try:
            comment_id = _post_first_comment(facebook_post_id, article["blogger_post_url"])
            article["facebook_comment_id"] = comment_id
        except Exception as comment_error:
            article["facebook_status"] = "posted_comment_failed"
            article["facebook_error"] = f"First comment failed: {comment_error}"

        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": article.get("facebook_status") == "posted",
            "article": article,
            "error": article.get("facebook_error", ""),
        }
        _log_notification_result("Facebook", notify_facebook_result(queue, article, result))
        return result

    except Exception as error:
        _apply_failure(article, error)
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": False,
            "article": article,
            "error": article.get("facebook_error", ""),
        }
        _log_notification_result("Facebook", notify_facebook_result(queue, article, result))
        return result


def _facebook_backfill_candidates(articles):
    new_post_candidates = [
        article
        for article in articles
        if _has_blogger_live_publish(article)
        and not article.get("facebook_post_id")
        and article.get("facebook_status") in {None, "", "failed"}
    ]
    comment_retry_candidates = [
        article
        for article in articles
        if _has_blogger_live_publish(article)
        and article.get("facebook_post_id")
        and not article.get("facebook_comment_id")
        and article.get("facebook_status") == "posted_comment_failed"
    ]
    sort_key = lambda article: (
        article.get("published_at", ""),
        article.get("selected_at", ""),
        article.get("discovered_at", ""),
    )
    return sorted(new_post_candidates, key=sort_key), sorted(comment_retry_candidates, key=sort_key)


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
    if not article.get("blogger_post_url"):
        return _failure_result(queue, article, "Article has no real Blogger URL for comment retry.")

    try:
        comment_id = _post_first_comment(article["facebook_post_id"], article["blogger_post_url"])
        article["facebook_comment_id"] = comment_id
        article["facebook_status"] = "posted"
        article.pop("facebook_error", None)
        article.pop("telegram_facebook_notified", None)
        article.pop("telegram_facebook_event_key", None)
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": True,
            "article": article,
            "error": "",
            "comment_retry": True,
        }
    except Exception as error:
        article["facebook_status"] = "posted_comment_failed"
        article["facebook_error"] = f"First comment failed: {error}"
        article.pop("telegram_facebook_notified", None)
        article.pop("telegram_facebook_event_key", None)
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": False,
            "article": article,
            "error": article.get("facebook_error", ""),
            "comment_retry": True,
        }

    _log_notification_result("Facebook", notify_facebook_result(queue, article, result))
    return result


def backfill_facebook_posts():
    """
    Post every live Blogger article that is still missing a Facebook post.
    This intentionally bypasses spacing limits because each Blogger publish is
    expected to have a matching Facebook post as soon as possible.
    """
    queue = load_article_queue()
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
    }

    for article in new_post_candidates:
        target_article_id = article.get("id") or article.get("url")
        result = post_one_article_to_facebook(
            target_article_id=target_article_id,
            respect_limits=False,
        )
        result_article = result.get("article") or {}
        stats["results"].append(result)
        if result_article.get("facebook_post_id"):
            stats["created"] += 1
            stats["latest_facebook_post_id"] = result_article.get("facebook_post_id", "")
            stats["latest_facebook_comment_id"] = result_article.get("facebook_comment_id", "")
        if result_article.get("facebook_comment_id"):
            stats["comments_created"] += 1
        if not result.get("posted"):
            stats["failed"] += 1

    for article in comment_retry_candidates:
        target_article_id = article.get("id") or article.get("url")
        result = retry_facebook_first_comment(target_article_id=target_article_id)
        result_article = result.get("article") or {}
        stats["results"].append(result)
        if result_article.get("facebook_post_id"):
            stats["latest_facebook_post_id"] = result_article.get("facebook_post_id", "")
        if result_article.get("facebook_comment_id"):
            stats["comments_created"] += 1
            stats["latest_facebook_comment_id"] = result_article.get("facebook_comment_id", "")
        if not result.get("posted"):
            stats["failed"] += 1

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
        }

    caption_pattern = _choose_caption_pattern(article, articles)
    caption = _build_caption(article, caption_pattern)
    lines = [line for line in caption.splitlines() if line.strip()]
    hashtags = lines[-1] if lines and lines[-1].startswith("#") else ""
    post_text = "\n".join(lines[:-1]) if hashtags else caption
    blogger_url = article.get("blogger_post_url") or article.get("blogger_draft_url")

    return {
        "available": True,
        "article": article,
        "selected_style": caption_pattern,
        "post_text": post_text,
        "hashtags": hashtags,
        "first_comment_text": _first_comment_text(blogger_url),
        "image_url": _main_image_url(article),
        "error": "",
    }


def get_facebook_status():
    queue = load_article_queue()
    articles = queue.get("articles", [])
    published = [article for article in articles if _has_blogger_live_publish(article)]
    without_post = [
        article
        for article in published
        if not article.get("facebook_post_id")
        and article.get("facebook_status") in {None, "", "failed"}
    ]
    posted = [article for article in published if article.get("facebook_post_id")]

    return {
        "auto_post_enabled": FACEBOOK_AUTO_POST,
        "page_id_configured": bool(FACEBOOK_PAGE_ID),
        "token_configured": bool(FACEBOOK_PAGE_ACCESS_TOKEN),
        "published_without_facebook": len(without_post),
        "posted_to_facebook": len(posted),
        "latest_eligible": _find_latest_eligible_article(articles),
    }
