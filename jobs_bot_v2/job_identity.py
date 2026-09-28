from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "trk", "ref", "source",
}

REFERENCE_KEYS = (
    "job_id", "jobid", "id", "requisition_id", "requisitionId",
    "reference", "reference_id", "req_id", "vacancy_id", "posting_id",
)


def normalize_text(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[\u064b-\u065f\u0670]", "", text)
    text = re.sub(r"[^\w\u0600-\u06ff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonicalize_url(url):
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return raw

    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]

    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")

    filtered = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(filtered))
    return urlunparse(("https", host, path, "", query, ""))


def external_reference(candidate):
    raw = candidate.raw or {}
    for key in REFERENCE_KEYS:
        value = raw.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def core_key(candidate):
    parts = [
        normalize_text(candidate.company),
        normalize_text(candidate.title),
        normalize_text(candidate.location),
    ]
    return "|".join(parts)


def identity_key(candidate):
    """Stable identity for the same real-world posting.

    Strong signals win: source reference ID, then canonical application URL.
    The semantic company/title/location key is used only as a fallback.
    """
    reference = external_reference(candidate)
    if reference:
        base = f"ref|{normalize_text(candidate.source_name)}|{normalize_text(reference)}"
    else:
        apply_url = canonicalize_url(candidate.application_url)
        canonical = canonicalize_url(candidate.canonical_url or candidate.source_url)
        if apply_url:
            base = f"apply|{apply_url}"
        elif canonical:
            base = f"url|{canonical}"
        else:
            base = f"core|{core_key(candidate)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def semantic_key(candidate):
    """Company/title/location key used to detect possible reposts or campaigns."""
    return hashlib.sha256(core_key(candidate).encode("utf-8")).hexdigest()[:20]


def _parse_date(value):
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


def _same_url_family(a, b):
    a = canonicalize_url(a)
    b = canonicalize_url(b)
    if not a or not b:
        return False
    pa, pb = urlparse(a), urlparse(b)
    if pa.netloc != pb.netloc:
        return False
    # exact URL is strong. Same host + same last meaningful path segment is also useful.
    if a == b:
        return True
    aa = [x for x in pa.path.split("/") if x]
    bb = [x for x in pb.path.split("/") if x]
    return bool(aa and bb and aa[-1] == bb[-1])


@dataclass
class IdentityDecision:
    action: str
    reason: str
    existing_identity: str = ""
    material_update: bool = False

    def to_dict(self):
        return {
            "action": self.action,
            "reason": self.reason,
            "existing_identity": self.existing_identity,
            "material_update": self.material_update,
        }


def compare_to_existing(candidate, record):
    """Decide whether a candidate updates an existing posting or is a new campaign."""
    if not record:
        return IdentityDecision("new", "no existing record")

    new_ref = external_reference(candidate)
    old_ref = str(record.get("external_reference") or "").strip()
    if new_ref and old_ref and normalize_text(new_ref) == normalize_text(old_ref):
        return IdentityDecision(
            "update",
            "same external job/reference ID",
            record.get("identity_key", ""),
            material_update=_material_change(candidate, record),
        )

    new_apply = canonicalize_url(candidate.application_url)
    old_apply = canonicalize_url(record.get("application_url"))
    if new_apply and old_apply and new_apply == old_apply:
        return IdentityDecision(
            "update",
            "same canonical application URL",
            record.get("identity_key", ""),
            material_update=_material_change(candidate, record),
        )

    if semantic_key(candidate) != record.get("semantic_key"):
        return IdentityDecision("new", "different company/title/location")

    # Same company/title/location can still be a new campaign. Different strong
    # references or clearly separated dates make it a new posting.
    if new_ref and old_ref and normalize_text(new_ref) != normalize_text(old_ref):
        return IdentityDecision("new_campaign", "same role but different external reference")

    if new_apply and old_apply and not _same_url_family(new_apply, old_apply):
        return IdentityDecision("new_campaign", "same role but different application URL")

    new_posted = _parse_date(candidate.published_at)
    old_posted = _parse_date(record.get("published_at"))
    if new_posted and old_posted and abs((new_posted - old_posted).days) >= 30:
        return IdentityDecision("new_campaign", "same role reposted at least 30 days later")

    # Position count, salary, deadline or description changes alone update the same page.
    return IdentityDecision(
        "update",
        "same semantic job; mutable facts changed or source reposted",
        record.get("identity_key", ""),
        material_update=_material_change(candidate, record),
    )


def _material_change(candidate, record):
    checks = {
        "number_of_positions": candidate.number_of_positions,
        "deadline": candidate.deadline,
        "salary": candidate.salary,
        "contract_type": candidate.contract_type,
        "location": candidate.location,
        "application_url": canonicalize_url(candidate.application_url),
    }
    for field, new_value in checks.items():
        old_value = record.get(field)
        if field == "application_url":
            old_value = canonicalize_url(old_value)
        if str(new_value or "").strip() != str(old_value or "").strip():
            return True
    return False


def snapshot(candidate):
    return {
        "identity_key": identity_key(candidate),
        "semantic_key": semantic_key(candidate),
        "external_reference": external_reference(candidate),
        "company": candidate.company,
        "title": candidate.title,
        "location": candidate.location,
        "published_at": candidate.published_at,
        "deadline": candidate.deadline,
        "number_of_positions": candidate.number_of_positions,
        "salary": candidate.salary,
        "contract_type": candidate.contract_type,
        "application_url": canonicalize_url(candidate.application_url),
        "canonical_url": canonicalize_url(candidate.canonical_url or candidate.source_url),
    }
