"""Bounded, read-only Facebook post feedback for weekly schedule decisions.

Never changes publishing decisions or touches Graph write endpoints. A failed
metric/API read cannot block Blogger, social delivery, or source discovery.
Interaction totals are NOT Facebook reach or application conversions.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from config import FACEBOOK_PAGE_ACCESS_TOKEN, JOBS_TIMEZONE
from job_core import list_active_job_campaign_records, _parse_date
from state_io import atomic_write_json
from production_logging import log_event

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "data" / "facebook_performance.json"
API_FIELDS = "created_time,reactions.limit(0).summary(true),comments.limit(0).summary(true),shares"
MAX_POST_READS_PER_PASS = 4
GLOBAL_RECHECK_HOURS = 6
POST_RECHECK_HOURS = 24
POST_MAX_AGE_DAYS = 21


def _utc(now=None):
    value = now or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _load(path=STATE_PATH):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _latest_posts(now, queue_articles=None, campaign_records=None):
    """Only confirmed Facebook IDs; dedupe queue and durable campaign history."""
    found = {}
    for record in campaign_records or []:
        if not isinstance(record, dict):
            continue
        post_id = str(record.get("facebook_post_id") or "").strip()
        posted = _parse_date(record.get("facebook_posted_at"))
        if post_id and posted:
            found[post_id] = {
                "post_id": post_id, "published_at": posted.astimezone(timezone.utc).isoformat(),
                "source": str(record.get("source_name") or "")[:90],
            }
    for article in queue_articles or []:
        if not isinstance(article, dict):
            continue
        post_id = str(article.get("facebook_post_id") or "").strip()
        posted = _parse_date(article.get("facebook_posted_at"))
        if post_id and posted:
            found[post_id] = {
                "post_id": post_id, "published_at": posted.astimezone(timezone.utc).isoformat(),
                "source": str(article.get("source_name") or "")[:90],
            }
    relevant = []
    for row in found.values():
        dt = _parse_date(row["published_at"])
        age = now - dt
        if timedelta(hours=1) <= age <= timedelta(days=POST_MAX_AGE_DAYS):
            relevant.append(row)
    return sorted(relevant, key=lambda r:r["published_at"], reverse=True)


def _count_from_summary(reply, name):
    node = reply.get(name)
    if not isinstance(node, dict):
        return None
    if name == "shares":
        count = node.get("count")
    else:
        count = (node.get("summary") or {}).get("total_count")
    if count is None or isinstance(count, bool):
        return None
    try:
        return max(0, int(count))
    except (ValueError, TypeError):
        return None


def _observation(row, payload, now):
    local = _parse_date(row["published_at"]).astimezone(ZoneInfo(JOBS_TIMEZONE))
    engagement = {
        "reactions": _count_from_summary(payload, "reactions"),
        "comments": _count_from_summary(payload, "comments"),
        "shares": _count_from_summary(payload, "shares"),
    }
    available = [value for value in engagement.values() if value is not None]
    return {
        **row,
        "checked_at": now.isoformat(),
        "local_weekday": local.weekday(),
        "local_hour": local.hour,
        "local_date": local.date().isoformat(),
        **engagement,
        "interactions_available": len(available),
        "interactions_observed": sum(available) if len(available) == 3 else None,
        "warning": "Counts are post interactions, not reach, link clicks, or applications.",
    }


def summarize_performance(posts, min_samples=3):
    """Meaningful timing comparison only after repeated observed posts per bucket."""
    buckets = {}
    for post in posts.values():
        if not isinstance(post, dict) or post.get("interactions_observed") is None:
            continue
        try:
            hour = int(post.get("local_hour"))
        except (ValueError, TypeError):
            continue
        if hour < 8:
            window = "overnight"
        elif hour < 12:
            window = "morning"
        elif hour < 17:
            window = "midday"
        else:
            window = "evening"
        group = buckets.setdefault(window, [])
        group.append(int(post["interactions_observed"]))
    return {
        name: {"samples": len(values), "average_interactions": round(sum(values) / len(values), 1)}
        for name, values in sorted(buckets.items())
        if len(values) >= min_samples
    }


def collect_facebook_performance(now=None, queue_articles=None, campaign_records=None,
                                 fetcher=None, state_path=STATE_PATH, token=None):
    now = _utc(now)
    token = FACEBOOK_PAGE_ACCESS_TOKEN if token is None else token
    if not str(token or "").strip():
        return {"checked": 0, "status": "no_facebook_token"}
    state = _load(state_path)
    previous = _parse_date(state.get("last_attempt_at"))
    if previous and now - previous < timedelta(hours=GLOBAL_RECHECK_HOURS):
        return {"checked": 0, "status": "cooldown", "next_attempt_at": (previous + timedelta(hours=GLOBAL_RECHECK_HOURS)).isoformat()}
    if queue_articles is None:
        from article_queue import load_article_queue
        queue_articles = load_article_queue().get("articles") or []
    if campaign_records is None:
        campaign_records = list_active_job_campaign_records(limit=500)
    if fetcher is None:
        from facebook_publisher import _get_from_graph
        fetcher = _get_from_graph

    posts = state.get("posts") if isinstance(state.get("posts"), dict) else {}
    candidates = []
    for row in _latest_posts(now, queue_articles, campaign_records):
        previous_post = posts.get(row["post_id"], {})
        checked = _parse_date(previous_post.get("checked_at")) if isinstance(previous_post, dict) else None
        if checked and now - checked < timedelta(hours=POST_RECHECK_HOURS):
            continue
        candidates.append(row)

    checked = success = 0
    errors = []
    for row in candidates[:MAX_POST_READS_PER_PASS]:
        post_id = row["post_id"]
        checked += 1
        try:
            response = fetcher(post_id, {
                "access_token": token, "fields": API_FIELDS,
            })
            if not isinstance(response, dict) or response.get("id", post_id) != post_id:
                raise ValueError("Unexpected Facebook response identity")
            result = _observation(row, response, now)
            if not result["interactions_available"]:
                raise ValueError("Facebook did not return supported reaction/comment/share counts")
            posts[post_id] = result
            success += 1
        except Exception as exc:
            # No secrets or raw Graph response data in durable diagnostics.
            previous_post = posts.get(post_id, {})
            if not isinstance(previous_post, dict):
                previous_post = {}
            posts[post_id] = {
                **previous_post,
                **row,
                "checked_at": now.isoformat(),
                "metric_status": "unavailable",
                "error_type": type(exc).__name__,
            }
            errors.append(type(exc).__name__)
            log_event("facebook_metrics_read_unavailable", reason=type(exc).__name__)

    # Bounded history independent of the social queue's archive lifecycle.
    cutoff = now - timedelta(days=POST_MAX_AGE_DAYS + 2)
    posts = {
        key: row for key, row in posts.items()
        if isinstance(row, dict)
        and (_parse_date(row.get("published_at")) or datetime(1970, 1, 1, tzinfo=timezone.utc)) >= cutoff
    }
    outcome = {
        "version": 1,
        "last_attempt_at": now.isoformat(),
        "last_success_at": now.isoformat() if success else state.get("last_success_at", ""),
        "posts": posts,
        "windows": summarize_performance(posts),
        "note": "Observed post reactions/comments/shares; no reach, clicks, or job applications inferred.",
    }
    atomic_write_json(state_path, outcome)
    return {"checked": checked, "succeeded": success, "unavailable": len(errors),
            "status": "ok" if success else ("no_recent_posts" if not checked else "metrics_unavailable"),
            "window_samples": outcome["windows"]}


def main():
    results = collect_facebook_performance()
    print(json.dumps({k: v for k, v in results.items() if k != "posts"}, ensure_ascii=False))
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as file:
            file.write("\n### Facebook performance feedback\n")
            file.write(f"- Read-only result: {results.get('status')}\n")
            file.write(f"- Post metrics checked: {results.get('checked', 0)}; verified samples: {results.get('succeeded', 0)}\n")
            file.write("- The experiment does not infer reach, clicks, or job applications from engagement totals.\n")
            for window, metrics in results.get("window_samples", {}).items():
                file.write(f"- {window}: {metrics['samples']} posts, mean {metrics['average_interactions']} observed interactions\n")


if __name__ == "__main__":
    main()
