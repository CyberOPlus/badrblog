from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from .config import MIN_SELECTION_SCORE


GOOD_ELIGIBILITY = {"morocco", "remote_morocco", "abroad_open", "visa_confirmed"}
TOP_SOURCE_PRIORITIES = {"s+", "s", "a+"}


def _public_http(url):
    try:
        parsed = urlparse(str(url or "").strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def _parse_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _is_fresh_24h(candidate, now=None):
    published = _parse_datetime(candidate.published_at)
    if not published:
        return False
    now = now or datetime.now(timezone.utc)
    age_hours = (now - published).total_seconds() / 3600
    return 0 <= age_hours <= 24


def suitability(candidate):
    value = str(candidate.eligibility or "unknown").strip().lower()
    if value in GOOD_ELIGIBILITY:
        return True, ""
    if value in {"local_only", "closed", "expired", "not_for_morocco"}:
        return False, f"eligibility={value}"
    return False, "eligibility must be verified before publishing"


def score_candidate(candidate, now=None):
    """100-point quality score. Eligibility is also a hard gate."""
    points = {}
    reasons = []

    points["official_source"] = 25 if candidate.official_source else 0
    points["fresh_under_24h"] = 15 if _is_fresh_24h(candidate, now=now) else 0

    source_priority = str(candidate.source_priority or "").strip().lower()
    points["trusted_priority_source"] = 10 if source_priority in TOP_SOURCE_PRIORITIES else 0

    try:
        positions = max(0, int(candidate.number_of_positions or 0))
    except (TypeError, ValueError):
        positions = 0
    points["large_hiring"] = 10 if positions >= 10 else 0

    points["clear_deadline"] = 10 if str(candidate.deadline or "").strip() else 0
    points["clear_location"] = 5 if str(candidate.location or "").strip() else 0
    points["clear_diploma"] = 5 if str(candidate.diploma or "").strip() else 0

    valid_apply = bool(candidate.application_url and _public_http(candidate.application_url))
    points["clear_application"] = 10 if valid_apply else 0
    points["salary_listed"] = 5 if str(candidate.salary or "").strip() else 0
    points["entry_level_or_student"] = 5 if bool(candidate.entry_level) else 0

    raw_score = sum(points.values())

    ok, reason = suitability(candidate)
    if not ok:
        reasons.append(reason)

    source_url_ok = _public_http(candidate.canonical_url or candidate.source_url)
    if not source_url_ok:
        reasons.append("invalid source URL")

    if not valid_apply:
        reasons.append("missing or invalid application URL")

    if candidate.requires_attribution:
        reasons.append("source requires attribution")

    passed = (
        raw_score >= MIN_SELECTION_SCORE
        and ok
        and source_url_ok
        and valid_apply
    )

    return {
        "score": max(0, min(raw_score, 100)),
        "passed": passed,
        "points": points,
        "reasons": reasons,
        "threshold": MIN_SELECTION_SCORE,
    }


def choose_best(candidates, now=None):
    ranked = []
    for candidate in candidates:
        quality = score_candidate(candidate, now=now)
        ranked.append((quality["score"], quality, candidate))
    ranked.sort(key=lambda row: row[0], reverse=True)
    for _, quality, candidate in ranked:
        if quality["passed"]:
            return candidate, quality, ranked
    return None, None, ranked
