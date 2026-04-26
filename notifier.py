# ============================================================
# notifier.py - Optional Telegram Notifications
# ============================================================

import html

import requests

from config import TELEGRAM_ALERTS_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


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
