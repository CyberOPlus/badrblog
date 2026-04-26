# ============================================================
# notifier.py - Optional Telegram Notifications
# ============================================================

import html
from datetime import datetime

import requests

from article_queue import load_article_queue, save_article_queue
from config import (
    FACEBOOK_PAGE_ACCESS_TOKEN,
    TELEGRAM_ALERTS_ENABLED,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)


TELEGRAM_API_BASE = "https://api.telegram.org"


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
                f"المرحلة: {_short(stage)}",
                f"السبب: {_sanitize_reason(result.get('error') or article.get('publish_error'))}",
            ]
        )
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
    success = bool(result.get("posted"))
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
    reason = _sanitize_reason(error or facebook_error or result.get("reason") or draft.get("error") or "")
    if error:
        status = "failed"
    elif facebook_error:
        status = "failed"
    elif result.get("completed"):
        status = "success"
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

    lines = [
        "📌 تقرير تشغيل البوت",
        f"الحالة: {status}",
        f"Blogger: {blogger_status}",
        f"Facebook: {facebook_status}",
        f"العنوان: {_article_title(article)}",
        f"الرابط: {_blogger_url(article)}",
        f"السبب إن وجد: {reason}",
    ]
    source_warnings_count = int(result.get("source_warnings_count") or 0)
    enrichment_failed_count = int(result.get("enrichment_failed_count") or 0)
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

    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.ok:
            return {"sent": True, "skipped": False, "reason": ""}
        if response.status_code in {401, 403}:
            return {
                "sent": False,
                "skipped": False,
                "reason": (
                    f"Telegram API returned HTTP {response.status_code}: "
                    "Bot token invalid/revoked or bot is not allowed to message this chat."
                ),
            }
        return {
            "sent": False,
            "skipped": False,
            "reason": f"Telegram API returned HTTP {response.status_code}.",
        }
    except requests.RequestException as error:
        return {"sent": False, "skipped": False, "reason": error.__class__.__name__}


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
