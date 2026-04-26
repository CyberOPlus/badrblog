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
        return {
            "sent": False,
            "skipped": False,
            "reason": f"Telegram API returned HTTP {response.status_code}.",
        }
    except requests.RequestException as error:
        return {"sent": False, "skipped": False, "reason": error.__class__.__name__}
