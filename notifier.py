# ============================================================
# notifier.py - Optional Telegram Notifications
# ============================================================

import html
import time
from datetime import datetime

import requests

from article_queue import load_article_queue, save_article_queue
from config import (
    FACEBOOK_PAGE_ACCESS_TOKEN,
    FAST_NEWS_MODE,
    MAX_LIVE_POSTS_PER_DAY,
    RECENT_NEWS_MAX_AGE_HOURS,
    RECENT_NEWS_ONLY,
    SAFE_MODE,
    TARGET_LIVE_POSTS_PER_DAY,
    TELEGRAM_ALERTS_ENABLED,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from production_logging import html_word_count, log_event


TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_SEND_RETRIES = 2


def telegram_alert_status():
    return {
        "enabled": TELEGRAM_ALERTS_ENABLED,
        "bot_token_configured": bool(TELEGRAM_BOT_TOKEN),
        "chat_id_configured": bool(TELEGRAM_CHAT_ID),
        "ready": TELEGRAM_ALERTS_ENABLED and bool(TELEGRAM_BOT_TOKEN) and bool(TELEGRAM_CHAT_ID),
    }


def _escape_message(message):
    return html.escape(str(message or ""), quote=False)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _short(value, limit=300):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _sanitize_reason(value):
    text = str(value or "unknown")
    for secret in (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, FACEBOOK_PAGE_ACCESS_TOKEN):
        if secret:
            text = text.replace(secret, "[redacted]")
    return _short(text, 300)


def _article_title(article):
    article = article or {}
    return _short(article.get("seo_title") or article.get("title") or article.get("fetched_title") or "")


def _article_category(article):
    article = article or {}
    return _short(article.get("suggested_category") or article.get("category_hint") or "")


def _blogger_url(article):
    article = article or {}
    return article.get("blogger_post_url") or article.get("blogger_draft_url") or ""


def _source_name(article):
    article = article or {}
    return _short(article.get("source_name") or article.get("source_url") or "")


def _selected_source_name(result, article):
    source_name = _source_name(article)
    if source_name:
        return source_name
    fetch = (result or {}).get("fetch") or {}
    return _short(fetch.get("selected_source_name") or fetch.get("selected_source_url") or "")


def _article_word_count(article):
    article = article or {}
    try:
        stored = int(article.get("final_word_count") or article.get("word_count") or 0)
    except (TypeError, ValueError):
        stored = 0
    return stored or html_word_count(article.get("final_html") or article.get("content") or "")


def _article_warnings(article):
    article = article or {}
    warnings = [str(item) for item in (article.get("pre_publish_warnings") or []) if item]
    quality = article.get("pre_publish_quality") or {}
    if quality and not quality.get("passed", True):
        warnings.append(str(quality.get("reason") or "quality gate failed"))
    return _short("; ".join(warnings), 300)


def _execution_time(result):
    result = result or {}
    value = result.get("execution_seconds")
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.2f}s"
    except (TypeError, ValueError):
        return str(value)


def _blogger_mode(article, result=None):
    article = article or {}
    result = result or {}
    mode = result.get("publishing_mode") or article.get("publish_status") or ""
    return "Live" if mode == "published" or mode == "live" else "Draft"


def _main_image_url(article):
    article = article or {}
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


def _send_once_for_article(queue, article, flag_name, event_key_name, event_key, message):
    if not article:
        return {"sent": False, "skipped": True, "reason": "No article available for notification."}
    if article.get(flag_name) and article.get(event_key_name) == event_key:
        return {"sent": False, "skipped": True, "reason": "Duplicate notification suppressed."}

    result = send_telegram_message(message)
    if result.get("sent"):
        now = _now_iso()
        article[flag_name] = True
        article[event_key_name] = event_key
        article[flag_name.replace("_notified", "_notified_at")] = now
        article["telegram_last_notification_at"] = now
        save_article_queue(queue)
    return result


def _set_queue_notification_sent(queue, key, event_key):
    now = _now_iso()
    notifications = queue.setdefault("notifications", {})
    notifications[f"{key}_event_key"] = event_key
    notifications[f"{key}_notified_at"] = now
    notifications["last_notification_at"] = now
    save_article_queue(queue)


def notify_blogger_result(queue, article, result=None, stage="publish"):
    result = result or {}
    success = bool(result.get("created") or result.get("created_new") or result.get("updated_existing"))
    article_warnings = _article_warnings(article)
    event_key = "|".join(
        [
            "success" if success else "failed",
            str(stage),
            str(article.get("blogger_post_id") or article.get("blogger_draft_id") or ""),
            str(article.get("publish_status") or result.get("publishing_mode") or ""),
            _sanitize_reason(result.get("error") or article.get("publish_error") or ""),
        ]
    )
    if success:
        message = "\n".join(
            [
                "✅ تم النشر في Blogger بنجاح",
                f"العنوان: {_article_title(article)}",
                f"Source: {_source_name(article)}",
                f"Warnings: {article_warnings}" if article_warnings else "",
                f"Words: {_article_word_count(article)}",
                f"الوضع: {_blogger_mode(article, result)}",
                f"الرابط: {_blogger_url(article)}",
                f"التصنيف: {_article_category(article)}",
                f"الوقت: {_now_iso()}",
            ]
        )
    else:
        message = "\n".join(
            [
                "❌ فشل النشر في Blogger",
                f"العنوان: {_article_title(article)}",
                f"Source: {_source_name(article)}",
                f"Words: {_article_word_count(article)}",
                f"المرحلة: {_short(stage)}",
                f"السبب: {_sanitize_reason(result.get('error') or article.get('publish_error'))}",
            ]
        )
    message = "\n".join(line for line in message.splitlines() if line)
    return _send_once_for_article(
        queue,
        article,
        "telegram_blogger_notified",
        "telegram_blogger_event_key",
        event_key,
        message,
    )


def notify_facebook_result(queue, article, result=None):
    result = result or {}
    success = bool(result.get("posted")) and article.get("facebook_status") == "posted"
    blogger_url = _blogger_url(article)
    event_key = "|".join(
        [
            "success" if success else "failed",
            str(article.get("facebook_post_id") or ""),
            str(article.get("facebook_comment_id") or ""),
            _sanitize_reason(result.get("error") or article.get("facebook_error") or ""),
        ]
    )
    if success:
        message = "\n".join(
            [
                "✅ تم النشر في Facebook بنجاح",
                f"العنوان: {_article_title(article)}",
                f"Source: {_source_name(article)}",
                f"Words: {_article_word_count(article)}",
                f"Post ID: {article.get('facebook_post_id', '')}",
                f"Comment ID: {article.get('facebook_comment_id', '')}",
                f"رابط Blogger: {blogger_url}",
                f"الصورة: {'نعم' if _main_image_url(article) else 'لا'}",
            ]
        )
    else:
        blogger_published = bool(article and article.get("publish_status") == "published" and blogger_url)
        message = "\n".join(
            [
                "❌ فشل النشر في Facebook",
                f"العنوان: {_article_title(article)}",
                f"Source: {_source_name(article)}",
                f"Words: {_article_word_count(article)}",
                f"السبب: {_sanitize_reason(result.get('error') or article.get('facebook_error'))}",
                f"هل تم نشر Blogger؟ {'نعم' if blogger_published else 'لا'}",
                f"رابط Blogger: {blogger_url}",
            ]
        )
    return _send_once_for_article(
        queue,
        article,
        "telegram_facebook_notified",
        "telegram_facebook_event_key",
        event_key,
        message,
    )


def notify_auto_cycle_blocked(reason, next_expected=""):
    queue = load_article_queue()
    event_key = f"{_sanitize_reason(reason)}|{next_expected}"
    notifications = queue.setdefault("notifications", {})
    if notifications.get("telegram_auto_cycle_blocked_event_key") == event_key:
        return {"sent": False, "skipped": True, "reason": "Duplicate notification suppressed."}
    result = send_telegram_message(
        "\n".join(
            [
                "⏸️ توقف البوت مؤقتًا",
                f"السبب: {_sanitize_reason(reason)}",
                f"التشغيل القادم المتوقع: {next_expected}",
            ]
        )
    )
    if result.get("sent"):
        _set_queue_notification_sent(queue, "telegram_auto_cycle_blocked", event_key)
    return result


def notify_auto_cycle_summary(result=None, error=None, run_id=""):
    result = result or {}
    article = result.get("article") or {}
    draft = result.get("draft") or {}
    facebook = result.get("facebook") or {}
    facebook_error = facebook.get("error") if facebook and not facebook.get("posted") else ""
    reason = _sanitize_reason(error or result.get("reason") or draft.get("error") or "")
    warning_value = facebook_error or result.get("warning") or ""
    warning = _sanitize_reason(warning_value) if warning_value else ""
    if error:
        status = "failed"
    elif result.get("completed"):
        status = "success"
    elif result.get("skipped"):
        status = "skipped"
    else:
        status = "blocked" if result.get("schedule") or result.get("reason") else "failed"

    queue = load_article_queue()
    event_key = run_id or f"{status}|{article.get('id', '')}|{result.get('step_reached', '')}|{reason}"
    notifications = queue.setdefault("notifications", {})
    if notifications.get("telegram_final_summary_event_key") == event_key:
        return {"sent": False, "skipped": True, "reason": "Duplicate notification suppressed."}

    blogger_status = article.get("publish_status") or draft.get("publishing_mode") or result.get("draft_action") or "skipped"
    facebook_status = "skipped"
    if facebook:
        facebook_article = facebook.get("article") or article
        facebook_status = facebook_article.get("facebook_status") or ("posted" if facebook.get("posted") else "failed")
    fetch = result.get("fetch") or {}
    schedule = result.get("schedule") or {}
    selected_category = fetch.get("selected_category") or article.get("suggested_category") or ""
    selected_source = _selected_source_name(result, article)
    sources_checked = fetch.get("sources_checked", "")
    candidates_found = fetch.get("articles_found", "")
    posts_today = schedule.get("posts_today")
    if posts_today in (None, ""):
        posts_today = schedule.get("live_posts_created_today", "")
    daily_line = (
        f"Posts today: {posts_today}/{MAX_LIVE_POSTS_PER_DAY} (target {TARGET_LIVE_POSTS_PER_DAY})"
        if posts_today not in (None, "")
        else f"Daily limit: {MAX_LIVE_POSTS_PER_DAY} (target {TARGET_LIVE_POSTS_PER_DAY})"
    )
    run_duration = _execution_time(result) or "unknown"
    lightweight = "yes" if result.get("lightweight_run", True) else "no"
    next_run = result.get("next_run_expected_at") or "scheduled by GitHub Actions"

    if status == "success":
        clear_lines = [
            "✅ تم نشر مقال جديد",
            f"Title: {_article_title(article)}",
            f"Blogger URL: {_blogger_url(article)}",
            f"Facebook status: {facebook_status}",
            f"Category: {selected_category}",
            f"AI provider: {article.get('ai_provider_used', '')}",
            f"AI quality: {article.get('ai_quality_status', '')}",
            f"AI attempts: {article.get('ai_quality_attempts', '')}",
            f"Main image found: {'yes' if _main_image_url(article) else 'no'}",
            f"Image source type: {article.get('main_image_source_type', '')}",
            f"Removed source links: {article.get('removed_source_links_count', 0)}",
            f"Internal cache loaded: {article.get('internal_cache_loaded', 0)}",
            f"Expired internal links removed: {article.get('expired_internal_links_removed', 0)}",
            f"Internal links inserted: {article.get('internal_links_inserted_count', 0)}",
            f"Trusted external links inserted: {article.get('external_trusted_links_inserted_count', 0)}",
            f"Internal cache saved: {'yes' if article.get('internal_cache_saved') else 'no'}",
            f"Facebook image: {article.get('facebook_image_status', 'skipped')}",
            f"Sources checked: {sources_checked}",
            f"Candidates found: {candidates_found}",
            f"Source: {selected_source}",
            f"Age: {article.get('article_age_hours', '')}",
            f"Words: {_article_word_count(article)}",
            f"Run duration: {run_duration}",
            f"Lightweight run: {lightweight}",
            "Publish result: published",
            daily_line,
            f"Next run: {next_run}",
        ]
    elif status == "skipped":
        clear_lines = [
            "⚠️ لم يتم النشر في هذه الدورة",
            f"Reason: {reason or result.get('reason') or 'no publishable article'}",
            f"Category: {selected_category}",
            f"Source: {selected_source}",
            f"Sources checked: {sources_checked}",
            f"Candidates found: {candidates_found}",
            f"Best candidate: {_article_title(article)}",
            f"Run duration: {run_duration}",
            f"Lightweight run: {lightweight}",
            "Publish result: skipped",
            daily_line,
            "Bot alive: yes",
            f"Next run: {next_run}",
        ]
    else:
        clear_lines = [
            "❌ فشل التشغيل",
            f"Reason: {reason or 'unknown'}",
            f"Category: {selected_category}",
            f"Source: {selected_source}",
            f"Run duration: {run_duration}",
            f"Lightweight run: {lightweight}",
            daily_line,
            f"Safe error: {reason or 'unknown'}",
            "Bot alive: yes",
            f"Next run: {next_run}",
        ]

    lines = clear_lines
    if warning:
        lines.append(f"Warning: {warning}")
    runtime_state = result.get("runtime_state") or {}
    if runtime_state:
        lines.append(f"Runtime state saved: {'yes' if runtime_state.get('saved') else 'no'}")
        lines.append(f"Git push state: {runtime_state.get('git_push_state', 'skipped')}")
    source_warnings_count = int(result.get("source_warnings_count") or 0)
    enrichment_failed_count = int(result.get("enrichment_failed_count") or 0)
    article_warnings = _article_warnings(article)
    if article_warnings:
        lines.append(f"Warnings: {article_warnings}")
    if source_warnings_count:
        lines.append(f"⚠️ تحذيرات المصادر: {source_warnings_count}")
    if enrichment_failed_count:
        lines.append(f"⚠️ فشل إثراء المقالات: {enrichment_failed_count}")

    send_result = send_telegram_message("\n".join(lines))
    if send_result.get("sent"):
        now = _now_iso()
        notifications["telegram_final_summary_event_key"] = event_key
        notifications["telegram_final_summary_notified_at"] = now
        notifications["last_notification_at"] = now
        if article:
            for queue_article in queue.get("articles", []):
                if article.get("id") and queue_article.get("id") == article.get("id"):
                    queue_article["telegram_final_summary_notified_at"] = now
                    break
        save_article_queue(queue)
    return send_result


def get_notification_status():
    status = telegram_alert_status()
    queue = load_article_queue()
    articles = queue.get("articles", [])
    notifications = queue.get("notifications", {})
    notification_times = [
        notifications.get("last_notification_at"),
        notifications.get("telegram_final_summary_notified_at"),
        notifications.get("telegram_auto_cycle_blocked_notified_at"),
    ]
    for article in articles:
        notification_times.extend(
            [
                article.get("telegram_blogger_notified_at"),
                article.get("telegram_facebook_notified_at"),
                article.get("telegram_final_summary_notified_at"),
                article.get("telegram_last_notification_at"),
            ]
        )
    last_notification_time = max([value for value in notification_times if value], default="")
    published_not_notified = [
        article
        for article in articles
        if article.get("publish_status") == "published"
        and article.get("blogger_post_url")
        and not article.get("telegram_blogger_notified")
    ]
    facebook_not_notified = [
        article
        for article in articles
        if article.get("facebook_post_id") and not article.get("telegram_facebook_notified")
    ]
    return {
        "telegram_enabled": status["enabled"],
        "telegram_ready": status["ready"],
        "last_notification_time": last_notification_time,
        "published_articles_not_notified": len(published_not_notified),
        "facebook_posts_not_notified": len(facebook_not_notified),
    }


def send_telegram_message(message: str):
    """
    Send a Telegram alert only when alerts are explicitly enabled and configured.
    Secret values are never returned or printed by this function.
    """
    status = telegram_alert_status()
    if not status["ready"]:
        return {
            "sent": False,
            "skipped": True,
            "reason": "Telegram alerts are disabled or not fully configured.",
        }

    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": _escape_message(message),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    last_reason = ""
    for attempt in range(TELEGRAM_SEND_RETRIES + 1):
        try:
            log_event("telegram_notification_start", attempt=attempt + 1)
            response = requests.post(url, json=payload, timeout=15)
            if response.ok:
                log_event("telegram_notification_result", status="sent")
                return {"sent": True, "skipped": False, "reason": ""}
            if response.status_code in {401, 403}:
                reason = (
                    f"Telegram API returned HTTP {response.status_code}: "
                    "Bot token invalid/revoked or bot is not allowed to message this chat."
                )
                log_event("telegram_notification_result", status="failed", reason=reason)
                return {"sent": False, "skipped": False, "reason": reason}

            last_reason = f"Telegram API returned HTTP {response.status_code}."
            log_event(
                "telegram_notification_result",
                status="failed",
                http_status=response.status_code,
            )
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
        except requests.RequestException as error:
            last_reason = error.__class__.__name__
            log_event("telegram_notification_result", status="failed", reason=last_reason)

        if attempt < TELEGRAM_SEND_RETRIES:
            time.sleep(1 + attempt)

    return {"sent": False, "skipped": False, "reason": last_reason or "Telegram send failed."}


def telegram_debug_probe():
    """
    Probe Telegram getMe and sendMessage with HTTP status codes only.
    Secret values and response bodies are never returned.
    """
    status = telegram_alert_status()
    result = {
        "configured": status,
        "get_me_status": None,
        "send_message_status": None,
        "working": False,
        "reason": "",
    }
    if not status["ready"]:
        result["reason"] = "Telegram alerts are disabled or not fully configured."
        return result

    try:
        get_me = requests.get(f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=15)
        result["get_me_status"] = get_me.status_code
    except requests.RequestException as error:
        result["reason"] = error.__class__.__name__
        return result

    if result["get_me_status"] in {401, 403}:
        result["reason"] = "Bot token invalid/revoked or bot is not allowed to message this chat."
        return result
    if result["get_me_status"] != 200:
        result["reason"] = f"Telegram getMe returned HTTP {result['get_me_status']}."
        return result

    try:
        send_message = requests.post(
            f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": _escape_message("Telegram debug test."),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        result["send_message_status"] = send_message.status_code
    except requests.RequestException as error:
        result["reason"] = error.__class__.__name__
        return result

    if result["send_message_status"] == 200:
        result["working"] = True
        return result
    if result["send_message_status"] == 403:
        result["reason"] = "Bot token invalid/revoked or bot is not allowed to message this chat."
        return result
    result["reason"] = f"Telegram sendMessage returned HTTP {result['send_message_status']}."
    return result
