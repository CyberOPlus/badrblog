"""Bounded service checks and delivery progress, separate from cycle liveness."""

import argparse
import json
import os
import signal
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import BASE_DIR, FACEBOOK_AUTO_POST, FACEBOOK_GRAPH_API_URL
from config import FACEBOOK_PAGE_ID, FACEBOOK_PAGE_ACCESS_TOKEN
from article_queue import load_article_queue
from job_core import job_publication_freshness, load_job_state
from state_io import atomic_write_json

HEALTH_PATH = BASE_DIR / "data" / "delivery_health.json"
SOURCE_HEALTH_PATH = BASE_DIR / "data" / "source_health.json"
TARGET_MINUTES = 60
BLOCKING_STATES = {"facebook_external_problem", "waiting_provider",
                   "facebook_stalled", "facebook_delivery_uncertain",
                   "facebook_comment_pending", "candidate_stalled"}
PROVIDER_SECRETS = {
    "gemini": ["GEMINI_API_KEY"], "groq": ["GROQ_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"], "mistral": ["MISTRAL_API_KEY"],
    "cloudflare": ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"],
    "openai": ["OPENAI_API_KEY"],
}


@contextmanager
def _probe_deadline(seconds=15):
    """Include SDK retries in the Linux runner's per-provider time limit."""
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return
    handler = signal.getsignal(signal.SIGALRM)
    timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def timeout(_signum, _frame):
        raise requests.Timeout("AI availability probe timed out")

    try:
        signal.signal(signal.SIGALRM, timeout)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, handler)
        if timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL,
                            max(0.001, timer[0] - (time.monotonic() - started)), timer[1])


def _read_health():
    try:
        value = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _date(value):
    try:
        dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _age(value, now):
    dt = _date(value)
    return max(0.0, (now - dt).total_seconds() / 60) if dt else None


def probe_sources(now=None):
    """Surface active source-level blockers without making the whole publisher fail."""
    now = now or datetime.now(timezone.utc)
    try:
        payload = json.loads(SOURCE_HEALTH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = {}
    rows = []
    for _, record in (payload.get("sources") or {}).items():
        if not isinstance(record, dict):
            continue
        retry_at = _date(record.get("cooldown_until"))
        if not retry_at or retry_at <= now:
            continue
        name = str(record.get("source_name") or "").strip()
        error = str(record.get("last_error") or "").strip()
        lowered = f"{name} {error}".casefold()
        category = (
            "network_timeout"
            if "timeout" in lowered
            else "http_block"
            if any(token in lowered for token in ("http 401", "http 403", "forbidden", "unauthorized"))
            else "source_error"
        )
        rows.append({
            "source_name": name,
            "failure_count": int(record.get("failure_count") or 0),
            "category": category,
            "retry_after": record.get("cooldown_until", ""),
        })
    rows.sort(key=lambda row: (-row.get("failure_count", 0), row.get("source_name", "")))
    anapec_blocked = any(
        "anapec" in str(row.get("source_name") or "").casefold()
        and row.get("category") == "network_timeout"
        and int(row.get("failure_count") or 0) >= 3
        for row in rows
    )
    if anapec_blocked:
        return {
            "status": "degraded",
            "active_issues": rows,
            "action": (
                "ANAPEC is unreachable from GitHub-hosted runners; automatic probing "
                "continues after source cooldown while other official sources keep running."
            ),
        }
    if rows:
        return {
            "status": "warning",
            "active_issues": rows,
            "action": "One or more sources are cooling down after transient fetch failures.",
        }
    return {
        "status": "ok",
        "active_issues": [],
        "action": "No active source cooldowns are currently blocking discovery.",
    }


def probe_facebook():
    """Check the configured Page/token using GET only; this creates no post."""
    if not FACEBOOK_AUTO_POST or not FACEBOOK_PAGE_ID or not FACEBOOK_PAGE_ACCESS_TOKEN:
        return {"status": "config", "action": "Check FACEBOOK_PAGE_ID and FACEBOOK_PAGE_ACCESS_TOKEN secrets."}
    try:
        response = requests.get(
            f"{FACEBOOK_GRAPH_API_URL.rstrip('/')}/{FACEBOOK_PAGE_ID}",
            params={"fields": "id", "access_token": FACEBOOK_PAGE_ACCESS_TOKEN},
            timeout=15,
        )
        data = response.json()
        error = data.get("error") or {}
        code = error.get("code") if isinstance(error, dict) else None
        if code in {190, 102}:
            return {"status": "auth", "code": code,
                    "action": "Renew FACEBOOK_PAGE_ACCESS_TOKEN."}
        if response.status_code in {401, 403} or code in {10, 200}:
            return {"status": "auth", "code": code,
                    "action": "Check Page token permissions and the configured Page ID."}
        if response.status_code == 429 or code in {4, 17, 32, 613}:
            return {"status": "quota", "code": code,
                    "action": "Facebook rate limit; automatic retries continue."}
        if response.status_code >= 400 or error:
            return {"status": "outage", "http_status": response.status_code,
                    "action": "Facebook API request failed; automatic retries continue."}
        if str(data.get("id") or "") != FACEBOOK_PAGE_ID:
            return {"status": "unconfirmed", "action": "Page lookup returned no matching acknowledged ID."}
        return {"status": "ok", "check": "page_token_read",
                "action": "Page/token lookup succeeded; publish permission is confirmed only by an actual delivery."}
    except requests.Timeout:
        return {"status": "timeout", "action": "Facebook lookup timed out; automatic retries continue."}
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return {"status": "outage", "action": "Facebook lookup could not be verified; automatic retries continue."}


def probe_ai():
    """Make one tiny generation per configured provider, without touching a job."""
    import article_ai_processor as ai

    try:
        candidates = ai._provider_candidates()
    except Exception:
        return {"status": "config", "providers": {},
                "action": "Configure at least one working AI provider secret."}
    providers = {}
    for candidate in candidates:
        name = candidate.get("provider")
        if name in providers:
            continue
        context = ai.AIExecutionContext(article_id="delivery-health", total_budget_seconds=15,
                                        current_stage="health_probe")
        try:
            with _probe_deadline():
                raw, _ = ai._generate_with_candidate(
                    candidate, 'Return only this JSON: {"status":"ok"}.', context=context,
                )
            providers[name] = {"status": "ok" if str(raw or "").strip() else "empty"}
        except Exception as error:
            # Do not copy provider responses, keys or account identifiers into
            # persisted diagnostics. Category + secret name is actionable.
            providers[name] = {"status": ai._provider_error_category(error)}
        providers[name]["secret_names"] = PROVIDER_SECRETS.get(name, [])
    working = any(row["status"] == "ok" for row in providers.values())
    return {"status": "ok" if working else "unavailable", "providers": providers,
            "action": "At least one provider generated a response." if working else
                      "All AI generation probes failed; renew an invalid key or restore quota/service availability."}


def _runtime_ai_circuit_status():
    """Read the shared AI circuit without spending another provider request."""
    try:
        import article_ai_processor as ai
        return ai.ai_circuit_status() or {}
    except Exception:
        return {}


def delivery_status(now=None, services=None, ai_circuit=None):
    now = now or datetime.now(timezone.utc)
    explicit_services = services is not None
    services = services if explicit_services else _read_health().get("services", {})
    if ai_circuit is None and not explicit_services:
        ai_circuit = _runtime_ai_circuit_status()
    ai_circuit = ai_circuit or {}
    if ai_circuit.get("global_open"):
        services = dict(services or {})
        ai_service = dict(services.get("ai") or {})
        ai_service.update({
            "status": "cooldown",
            "retry_after": ai_circuit.get("global_retry_after", ""),
            "category": ai_circuit.get("global_category", ""),
            "action": (
                "All configured AI providers are temporarily unavailable or cooling down; "
                "automatic generation resumes after the circuit retry window."
            ),
        })
        services["ai"] = ai_service
    elif (services.get("ai") or {}).get("status") == "cooldown":
        # The previous health snapshot may contain the overlay from a circuit
        # that has since recovered. Do not keep reporting waiting_provider after
        # the shared circuit is closed. Recover the base probe status from its
        # provider rows without making another API request.
        services = dict(services or {})
        ai_service = dict(services.get("ai") or {})
        provider_rows = ai_service.get("providers") or {}
        if any(
            isinstance(row, dict) and row.get("status") == "ok"
            for row in provider_rows.values()
        ):
            ai_service["status"] = "ok"
            ai_service["action"] = "At least one provider is available; automatic AI generation can continue."
        else:
            ai_service["status"] = "unavailable"
            ai_service["action"] = "No AI provider is currently confirmed available."
        ai_service.pop("retry_after", None)
        ai_service.pop("category", None)
        services["ai"] = ai_service
    articles = load_article_queue().get("articles", [])
    posts = [row for row in articles if row.get("facebook_post_id")]
    latest = max(posts, key=lambda row: _date(row.get("facebook_posted_at")) or
                 datetime.min.replace(tzinfo=timezone.utc), default={})
    facebook_age = _age(latest.get("facebook_posted_at"), now)
    pending = [row for row in articles if not row.get("facebook_post_id") and
               row.get("facebook_status") in {"facebook_pending", "failed", "delivery_uncertain"}]
    uncertain = [row for row in articles if row.get("facebook_status") == "delivery_uncertain"]
    comments = [row for row in posts if not row.get("facebook_comment_id") and
                (row.get("facebook_link_mode") in {"comment", "first_comment"} or
                 row.get("facebook_status") in {"posted_comment_failed", "posted_comment_uncertain"})]
    fresh = [row for row in articles if not row.get("archived") and
             row.get("status") not in {"published", "skipped"} and
             job_publication_freshness(row, now=now).get("fresh")]
    fresh_ready = [row for row in fresh if row.get("status") == "ready"]
    # A queue status alone does not make a job publishable. Mirror the
    # publisher/watchdog hard gates so incomplete eligibility evidence cannot
    # produce a false one-hour delivery blocker.
    verified_ready = [
        row for row in fresh_ready
        if row.get("content_fetch_status") == "success"
        and row.get("job_quality_status") == "publish"
        and not (row.get("job_quality_reasons") or [])
        and row.get("job_hard_gate_passed") is True
    ]
    pending_ages = [_age(row.get("facebook_queued_at") or row.get("published_at"), now) for row in pending]
    oldest_pending = max((age for age in pending_ages if age is not None), default=0)
    candidate_ages = [_age(row.get("discovered_at"), now) for row in verified_ready]
    oldest_candidate = max((age for age in candidate_ages if age is not None), default=0)
    target_met = facebook_age is not None and facebook_age <= TARGET_MINUTES
    if services.get("facebook", {}).get("status") not in {None, "ok"}:
        state, action = "facebook_external_problem", services["facebook"].get("action", "Check Facebook access.")
    elif services.get("ai", {}).get("status") in {"unavailable", "config", "cooldown"}:
        state, action = "waiting_provider", services["ai"].get("action", "Restore AI access.")
    elif uncertain:
        state, action = "facebook_delivery_uncertain", "Reconcile uncertain Facebook delivery before retrying that item."
    elif pending and oldest_pending >= TARGET_MINUTES:
        state, action = "facebook_stalled", "A Blogger article has waited at least one hour for Facebook; inspect its delivery error."
    elif comments:
        state, action = "facebook_comment_pending", "Retry missing first comments containing application/article links."
    elif oldest_candidate >= TARGET_MINUTES:
        state, action = "candidate_stalled", "A verified ready job has waited at least one hour before Facebook; inspect the publisher's blocking reason."
    elif (services.get("sources") or {}).get("status") == "degraded":
        state, action = "source_degraded", services["sources"].get(
            "action",
            "A critical source is externally unreachable; other sources continue.",
        )
    elif not target_met and not verified_ready and not pending:
        if fresh_ready:
            state, action = (
                "no_verified_candidate",
                "Hourly target missed: fresh ready queue items exist, but none has completed all publication verification gates.",
            )
        else:
            state, action = (
                "no_fresh_candidate",
                "Hourly target missed: no fresh candidate in the queue; audit discovery and source coverage.",
            )
    else:
        state, action = "healthy", "Independent queues continue; each verified fresh job follows Blogger to Facebook."
    return {"updated_at": now.isoformat(), "state": state, "action": action,
            "hourly_target_met": target_met, "target_minutes": TARGET_MINUTES,
            "last_facebook_posted_at": latest.get("facebook_posted_at", ""),
            "last_facebook_post_id": latest.get("facebook_post_id", ""),
            "minutes_since_facebook": round(facebook_age, 1) if facebook_age is not None else None,
            "last_blogger_publish_at": load_job_state().get("last_publish_at", ""),
            "fresh_candidate_count": len(fresh),
            "verified_ready_candidate_count": len(verified_ready),
            "unverified_ready_candidate_count": len(fresh_ready) - len(verified_ready),
            "facebook_pending_count": len(pending),
            "oldest_facebook_pending_minutes": round(oldest_pending, 1),
            "oldest_ready_candidate_minutes": round(oldest_candidate, 1),
            "pending_first_comments": len(comments), "services": services}


def run_monitor(force=False):
    now = datetime.now(timezone.utc)
    previous = _read_health()
    services = previous.get("services") or {}
    checked_age = _age(previous.get("services_checked_at"), now)
    interval = 60 if (services.get("facebook", {}).get("status") == "ok" and
                      services.get("ai", {}).get("status") == "ok") else 15
    if force or checked_age is None or checked_age >= interval:
        services = {"facebook": probe_facebook(), "ai": probe_ai()}
        checked_at = datetime.now(timezone.utc).isoformat()
    else:
        checked_at = previous.get("services_checked_at", "")
    services = dict(services or {})
    services["sources"] = probe_sources(now=now)
    report = delivery_status(
        services=services,
        ai_circuit=_runtime_ai_circuit_status(),
    )
    report["services_checked_at"] = checked_at
    atomic_write_json(HEALTH_PATH, report)
    return report


def emit_report(report):
    print(json.dumps(report, ensure_ascii=False, indent=2))
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n### Jobs Facebook delivery\n\n")
            handle.write(f"- State: `{report.get('state')}`\n")
            handle.write(f"- Hourly target met: `{report.get('hourly_target_met')}`\n")
            handle.write(f"- Latest Facebook delivery: `{report.get('last_facebook_posted_at')}`\n")
            handle.write(f"- Fresh candidates: {report.get('fresh_candidate_count')}; Facebook pending: {report.get('facebook_pending_count')}\n")
            handle.write(f"- Action: {report.get('action')}\n")
            for name, row in report.get("services", {}).get("ai", {}).get("providers", {}).items():
                handle.write(f"- AI `{name}`: `{row.get('status')}`\n")
            handle.write(f"- Facebook token/read check: `{report.get('services', {}).get('facebook', {}).get('status')}`\n")
            handle.write(f"- Sources: `{report.get('services', {}).get('sources', {}).get('status', 'unknown')}`\n")
    if report.get("state") in BLOCKING_STATES:
        print("::error::" + report.get("action", "Delivery blocker needs attention."))
    elif report.get("state") == "source_degraded":
        print("::warning::" + report.get("action", "A critical source is degraded."))
    elif not report.get("hourly_target_met"):
        print("::warning::" + report.get("action", "Hourly Facebook target missed."))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--cached", action="store_true")
    args = parser.parse_args()
    report = _read_health() if args.cached else run_monitor(force=args.force)
    emit_report(report)
    if args.cached and report.get("state") in BLOCKING_STATES:
        raise SystemExit(1)
