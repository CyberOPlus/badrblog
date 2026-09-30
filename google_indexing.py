from __future__ import annotations

import json
from urllib.parse import urlparse

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from config import GOOGLE_SERVICE_ACCOUNT_JSON, JOBS_GOOGLE_INDEXING_ENABLED
from production_logging import log_event


_INDEXING_SCOPE = "https://www.googleapis.com/auth/indexing"
_INDEXING_ENDPOINT = "https://indexing.googleapis.com/v3/urlNotifications:publish"


def _public_http(url):
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _credentials():
    raw = str(GOOGLE_SERVICE_ACCOUNT_JSON or "").strip()
    if not raw:
        return None, "missing GOOGLE_SERVICE_ACCOUNT_JSON"
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as error:
        return None, f"invalid GOOGLE_SERVICE_ACCOUNT_JSON: {error}"
    try:
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=[_INDEXING_SCOPE],
        )
    except Exception as error:
        return None, f"could not load service account credentials: {error}"
    return creds, ""


def notify_job_url(url, article=None, notification_type="URL_UPDATED"):
    article = article or {}
    result = {
        "enabled": bool(JOBS_GOOGLE_INDEXING_ENABLED),
        "status": "disabled",
        "url": str(url or "").strip(),
        "type": notification_type,
        "error": "",
    }
    if not JOBS_GOOGLE_INDEXING_ENABLED:
        return result

    notice_type = str(article.get("job_notice_type") or "vacancy").strip().lower()
    if notice_type != "vacancy":
        result["status"] = "skipped_non_vacancy"
        return result

    if not _public_http(url):
        result["status"] = "skipped_invalid_url"
        result["error"] = "Google Indexing notification requires a public HTTP(S) URL"
        return result

    if notification_type not in {"URL_UPDATED", "URL_DELETED"}:
        result["status"] = "skipped_invalid_type"
        result["error"] = f"unsupported Indexing API notification type: {notification_type}"
        return result

    creds, error = _credentials()
    if not creds:
        result["status"] = "configuration_error"
        result["error"] = error
        log_event(
            "google_indexing_configuration_error",
            article_id=article.get("id"),
            url=url,
            error=error,
        )
        return result

    try:
        session = AuthorizedSession(creds)
        response = session.post(
            _INDEXING_ENDPOINT,
            json={"url": str(url).strip(), "type": notification_type},
            timeout=20,
        )
        if response.status_code < 200 or response.status_code >= 300:
            detail = response.text.strip()[:800]
            result["status"] = "failed"
            result["error"] = f"HTTP {response.status_code}: {detail}"
            log_event(
                "google_indexing_failed",
                article_id=article.get("id"),
                url=url,
                status=response.status_code,
                error=detail,
            )
            return result

        payload = response.json() if response.content else {}
        result["status"] = "submitted"
        result["response"] = payload
        log_event(
            "google_indexing_submitted",
            article_id=article.get("id"),
            url=url,
            notification_type=notification_type,
        )
        return result
    except Exception as error:
        result["status"] = "failed"
        result["error"] = str(error)
        log_event(
            "google_indexing_failed",
            article_id=article.get("id"),
            url=url,
            error=error,
        )
        return result
