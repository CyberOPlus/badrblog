from __future__ import annotations

import json

import requests

from .config import GOOGLE_INDEXING_ENABLED, GOOGLE_SERVICE_ACCOUNT_JSON


SCOPE = "https://www.googleapis.com/auth/indexing"
ENDPOINT = "https://indexing.googleapis.com/v3/urlNotifications:publish"


def notify_google(url, action="URL_UPDATED"):
    """Notify Google's Indexing API for a live single-job URL.

    Disabled by default. Authentication is loaded lazily so preview runs do not
    require google-auth.
    """
    if not GOOGLE_INDEXING_ENABLED:
        return {"ok": False, "skipped": True, "reason": "indexing disabled"}

    if action not in {"URL_UPDATED", "URL_DELETED"}:
        raise ValueError("action must be URL_UPDATED or URL_DELETED")
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is missing")

    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
    except ImportError as exc:
        raise RuntimeError("google-auth package is required for Indexing API") from exc

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=[SCOPE],
    )
    credentials.refresh(Request())

    response = requests.post(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        },
        json={"url": url, "type": action},
        timeout=30,
    )
    response.raise_for_status()
    return {"ok": True, "response": response.json()}
