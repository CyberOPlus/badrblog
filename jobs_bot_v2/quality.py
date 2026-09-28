from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from .config import MIN_SELECTION_SCORE


GOOD_ELIGIBILITY = {"morocco", "remote_morocco", "abroad_open", "visa_confirmed"}


def _public_http(url):
    try:
        parsed = urlparse(str(url or "").strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def suitability(candidate):
    value = str(candidate.eligibility or "unknown").strip().lower()
    if value in GOOD_ELIGIBILITY:
        return True, ""
    if value in {"local_only", "closed", "expired", "not_for_morocco"}:
        return False, f"eligibility={value}"
    return False, "eligibility must be verified before publishing"


def score_candidate(candidate):
    score = 0
    reasons = []

    if candidate.official_source:
        score += 28
        reasons.append("official source")
    if candidate.application_url and _public_http(candidate.application_url):
        score += 16
        reasons.append("valid application URL")
    if candidate.company:
        score += 8
    if candidate.location:
        score += 8
    if candidate.deadline:
        score += 8
    if candidate.contract_type:
        score += 5
    if candidate.description and len(candidate.description) >= 400:
        score += 10
    if candidate.eligibility in GOOD_ELIGIBILITY:
        score += 12
    if candidate.visa_sponsorship or candidate.relocation:
        score += 5
    if candidate.main_image:
        score += 2

    ok, reason = suitability(candidate)
    if not ok:
        score -= 50
        reasons.append(reason)

    if not _public_http(candidate.canonical_url or candidate.source_url):
        score -= 30
        reasons.append("invalid source URL")

    if candidate.requires_attribution:
        reasons.append("attribution required")

    return {
        "score": max(0, min(score, 100)),
        "passed": score >= MIN_SELECTION_SCORE and ok,
        "reasons": reasons,
    }


def choose_best(candidates):
    ranked = []
    for candidate in candidates:
        quality = score_candidate(candidate)
        ranked.append((quality["score"], quality, candidate))
    ranked.sort(key=lambda row: row[0], reverse=True)
    for _, quality, candidate in ranked:
        if quality["passed"]:
            return candidate, quality, ranked
    return None, None, ranked
