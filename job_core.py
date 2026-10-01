from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

from config import (
    JOBS_ACTIVE_END_HOUR,
    JOBS_ACTIVE_START_HOUR,
    JOBS_ADAPTIVE_PUBLISHING,
    JOBS_MAX_PUBLISH_AGE_HOURS,
    JOBS_MIN_PUBLISH_INTERVAL_MINUTES,
)
from jobs_adaptive_controller import current_policy

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEMORY_DIR = DATA_DIR / "job_memory"
STATE_PATH = DATA_DIR / "job_state.json"

MOROCCO_TIMEZONE = os.getenv("JOBS_TIMEZONE", "Africa/Casablanca").strip() or "Africa/Casablanca"
MIN_SELECTION_SCORE = int(os.getenv("JOBS_MIN_SELECTION_SCORE", "65"))
QUEUE_SCORE = int(os.getenv("JOBS_QUEUE_SCORE", "50"))
URGENT_EXTRA_DAILY_LIMIT = int(os.getenv("JOBS_URGENT_EXTRA_DAILY_LIMIT", "1"))

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "trk", "ref", "source",
}
REFERENCE_KEYS = (
    "job_id", "jobid", "id", "requisition_id", "requisitionId",
    "reference", "reference_id", "req_id", "vacancy_id", "posting_id",
)
GENERIC_JOB_PATHS = {
    "", "jobs", "job", "careers", "career", "recruitment", "recrutement",
    "vacancies", "vacancy", "opportunities", "opportunity", "apply",
}
GOOD_ELIGIBILITY = {"morocco", "remote_morocco", "abroad_open", "visa_confirmed"}
TOP_SOURCE_PRIORITIES = {"s+", "s", "a+"}


# Editorial priority only. These patterns never make an invalid or stale job
# publishable; they only move the user's preferred technical/student roles ahead
# of other equally fresh verified opportunities.
_CYBER_FOCUS_PATTERNS = (
    r"\bcyber(?:security|securite|sécurité)?\b",
    r"\bcybers[eé]curit[eé]\b",
    r"\binfosec\b",
    r"\bpentest(?:er|ing)?\b",
    r"\bdevsecops\b",
    r"\bsoc\s+(?:analyst|analyste|engineer|ing[eé]nieur)\b",
    r"\bsiem\b",
    r"\biam\b",
    r"s[eé]curit[eé]\s+(?:informatique|des\s+syst[eè]mes|r[eé]seau)",
    r"information\s+security",
    r"أمن\s+سيبراني",
    r"الأمن\s+السيبراني",
    r"سيبراني",
    r"أمن\s+(?:المعلومات|الأنظمة|الشبكات)",
)
_TECH_FOCUS_PATTERNS = (
    r"\binformatique\b",
    r"\b(?:it|ict)\b",
    r"\bd[eé]veloppeur\b",
    r"\bdeveloper\b",
    r"\bsoftware\b",
    r"\blogiciel\b",
    r"\b(?:front\s*end|frontend|back\s*end|backend|full\s*stack|fullstack)\b",
    r"\b(?:web|mobile)\s+(?:developer|d[eé]veloppeur|engineer|ing[eé]nieur)\b",
    r"\bdevops\b",
    r"\bcloud\b",
    r"\br[eé]seau(?:x)?\b",
    r"\bnetwork(?:ing)?\b",
    r"\bsyst[eè]mes?\b",
    r"\bsysadmin\b",
    r"\bdata\s+(?:engineer|scientist|analyst|analyste)\b",
    r"\b(?:database|dba)\b",
    r"\bmachine\s+learning\b",
    r"\bintelligence\s+artificielle\b",
    r"\bartificial\s+intelligence\b",
    r"\b(?:ai|ia)\s+(?:engineer|developer|ing[eé]nieur|d[eé]veloppeur)\b",
    r"\b(?:sap|erp|salesforce|servicenow)\b",
    r"تقنية\s+المعلومات",
    r"تكنولوجيا\s+المعلومات",
    r"معلوماتية",
    r"مطور(?:ة|\s+برمجيات)?",
    r"برمج(?:ة|يات)",
    r"مهندس\s+برمجيات",
    r"شبكات",
    r"أنظمة",
    r"سحابة",
    r"حوسبة\s+سحابية",
    r"بيانات",
    r"ذكاء\s+اصطناعي",
)
_STUDENT_FOCUS_PATTERNS = (
    r"\bstage\b",
    r"\bstagiaire\b",
    r"\binternship\b",
    r"\bintern\b",
    r"\balternance\b",
    r"\bapprentissage\b",
    r"\bpfe\b",
    r"تدريب",
    r"متدرب",
    r"متدربة",
    r"تداريب",
)


def job_focus_priority(article):
    """Rank cyber/IT/developer and internship opportunities ahead of general jobs."""
    text = " ".join(
        str(article.get(key) or "")
        for key in (
            "job_title",
            "fetched_title",
            "title",
            "job_contract_type",
            "job_description",
        )
    )
    folded = unicodedata.normalize("NFKC", text).casefold()
    cyber = any(re.search(pattern, folded, flags=re.I) for pattern in _CYBER_FOCUS_PATTERNS)
    tech = cyber or any(re.search(pattern, folded, flags=re.I) for pattern in _TECH_FOCUS_PATTERNS)
    student = bool(article.get("job_entry_level")) or any(
        re.search(pattern, folded, flags=re.I) for pattern in _STUDENT_FOCUS_PATTERNS
    )
    if cyber or (tech and student):
        return 4
    if tech:
        return 3
    if student:
        return 2
    return 0



# A generic application portal is never enough on its own. For public recruitment
# competitions it can be accepted only when the specific official notice itself
# exposes that exact channel as an application action.
PUBLIC_APPLICATION_HOSTS = {
    "emploi-public.ma",
}
PUBLIC_APPLICATION_HOST_SUFFIXES = (
    ".gov.ma",
    ".ac.ma",
)

# Latest agreed Morocco publishing experiment. Blogger publishes verified jobs
# immediately until the daily cap. Facebook uses these local slots.
WEEKDAY_BLOGGER_CAP = {
    0: 2,  # Monday
    1: 3,  # Tuesday
    2: 3,  # Wednesday
    3: 3,  # Thursday
    4: 2,  # Friday
    5: 1,  # Saturday
    6: 2,  # Sunday
}
MONTHLY_VOLUME_RANGE = {
    1: (2, 3), 2: (2, 3),
    3: (2, 2), 4: (2, 2),
    5: (2, 3), 6: (2, 3),
    7: (1, 2), 8: (1, 2),
    9: (2, 3), 10: (2, 3),
    11: (2, 2),
    12: (1, 2),
}
FACEBOOK_SLOTS = {
    0: (time(12, 30), time(19, 30)),
    1: (time(12, 30), time(19, 0)),
    2: (time(12, 30), time(19, 0)),
    3: (time(12, 30), time(20, 0)),
    4: (time(10, 30), time(19, 30)),
    5: (time(11, 0),),
    6: (time(19, 0),),
}
# Research-informed starting slots, not measured peaks for this Page.
# See docs/publishing-schedule.md. Local timezone rules apply all year.


def _local(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ZoneInfo(MOROCCO_TIMEZONE))


def normalize_text(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[\u064b-\u065f\u0670]", "", text)
    text = re.sub(r"[^\w\u0600-\u06ff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonicalize_job_url(url):
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
    return urlunparse(("https", host, path, "", urlencode(sorted(filtered)), ""))


_JOB_REFERENCE_QUERY_KEYS = {
    "id", "job", "jobid", "job_id", "job-id", "requisitionid",
    "requisition_id", "reqid", "req_id", "vacancyid", "vacancy_id",
    "postingid", "posting_id", "positionid", "position_id", "reference",
    "ljobid",
}


def _url_job_reference(url):
    normalized = canonicalize_job_url(url)
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    query = {
        key.casefold(): str(value or "").strip()
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    }
    for key in _JOB_REFERENCE_QUERY_KEYS:
        value = query.get(key, "")
        if value:
            return normalize_text(value)
    uuid_match = re.search(
        r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        parsed.path,
    )
    if uuid_match:
        return normalize_text(uuid_match.group(1))
    for pattern in (
        r"/requisition/(\d{2,})(?:/|$)",
        r"/jobs?/(\d{3,})(?:/|$)",
        r"/job/(\d{3,})(?:/|$)",
        r"/offre/(\d{2,})(?:/|$)",
    ):
        match = re.search(pattern, parsed.path, flags=re.I)
        if match:
            return normalize_text(match.group(1))
    return ""


def is_job_specific_url(url):
    normalized = canonicalize_job_url(url)
    if not normalized:
        return False
    parsed = urlparse(normalized)
    segments = [x.casefold() for x in parsed.path.split("/") if x]
    query = {
        key.casefold(): str(value or "").strip()
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    }

    has_specific_query = any(
        key in _JOB_REFERENCE_QUERY_KEYS and len(value) >= 3
        for key, value in query.items()
    )

    if (
        "pageuppeople.com" in parsed.netloc.casefold()
        and re.search(r"/applicationform/default\.asp$", parsed.path, flags=re.I)
    ):
        return has_specific_query

    if not segments:
        return has_specific_query

    last = segments[-1]
    generic_tail = last in GENERIC_JOB_PATHS or last in {
        "search", "job-search", "jobs-search", "offres", "offres-emploi",
        "emplois", "openings", "positions", "all-jobs", "all-jobs-search",
        "candidature", "postuler", "application", "applications", "register",
        "registration", "inscription", "list", "listing", "liste",
        "annonce", "annonces",
    }
    if generic_tail:
        if has_specific_query:
            return True
        parent = segments[-2] if len(segments) >= 2 else ""
        if re.search(r"\d{3,}|[a-f0-9]{8,}", parent):
            return True
        # A descriptive job slug immediately before /apply or /application is
        # also specific enough when it is not another generic careers segment.
        if len(segments) >= 3 and parent and parent not in GENERIC_JOB_PATHS:
            return True
        return False

    if re.search(r"\d{3,}|[a-f0-9]{8,}", last):
        return True
    if len(segments) >= 2 and last not in GENERIC_JOB_PATHS:
        return True
    return has_specific_query


def _document_external_reference(article):
    pages = article.get("job_document_texts") or []
    if not isinstance(pages, list):
        return ""
    patterns = (
        r"(?i)(?:référence|reference|réf\.?|ref\.?)\s*(?:du\s+concours|de\s+l['’]annonce|de\s+l['’]offre)?\s*[:#№-]?\s*([A-Z0-9][A-Z0-9._/-]{2,40})",
        r"(?:المرجع|رقم\s+(?:المباراة|الإعلان|الاعلان))\s*[:#№-]?\s*([A-Za-z0-9][A-Za-z0-9._/-]{2,40})",
    )
    for page in pages:
        if not isinstance(page, dict):
            continue
        text = str(page.get("text") or "")
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            value = str(match.group(1) or "").strip(" .,:;#-")
            if not value:
                continue
            if value.isdigit() and len(value) < 5:
                continue
            return value
    return ""


def external_reference(article):
    raw = article.get("raw") or {}
    for key in REFERENCE_KEYS:
        # article["id"] is the queue's internal hash, not an employer/source
        # vacancy identifier. raw["id"] may still be a real source reference.
        if key != "id":
            value = article.get(key)
            if value not in (None, ""):
                return str(value).strip()
        value = raw.get(key) if isinstance(raw, dict) else None
        if value not in (None, ""):
            return str(value).strip()
    explicit = str(article.get("job_external_reference") or "").strip()
    if explicit:
        return explicit
    ats_reference = str(article.get("ats_reference") or "").strip()
    if ats_reference:
        return ats_reference
    return _document_external_reference(article)


def is_foreign_job_detail_url(article, url):
    candidate = canonicalize_job_url(url)
    source = canonicalize_job_url(
        article.get("job_detail_url")
        or article.get("canonical_url")
        or article.get("url")
        or article.get("source_url")
    )
    if not candidate or not source:
        return False
    pc, ps = urlparse(candidate), urlparse(source)
    if pc.netloc != ps.netloc:
        return False
    candidate_ref = _url_job_reference(candidate)
    source_ref = _url_job_reference(source)
    return bool(candidate_ref and source_ref and candidate_ref != source_ref)


def _url_host(url):
    try:
        host = urlparse(canonicalize_job_url(url)).netloc.casefold()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_public_application_host(host):
    host = str(host or "").casefold().strip(".")
    if not host:
        return False
    return (
        host in PUBLIC_APPLICATION_HOSTS
        or any(host.endswith(suffix) for suffix in PUBLIC_APPLICATION_HOST_SUFFIXES)
    )


def _application_action_exposes_url(article, candidate):
    candidate = canonicalize_job_url(candidate)
    if not candidate:
        return False
    for row in (article.get("job_action_links") or []):
        if not isinstance(row, dict) or str(row.get("kind") or "").strip().lower() != "apply":
            continue
        row_url = canonicalize_job_url(row.get("url"))
        if row_url and row_url == candidate:
            return True
    return False


def is_verified_official_application_channel(article, url):
    """
    Allow a non-job-specific application portal only for a verified public
    recruitment competition whose specific official notice exposes that exact
    portal as the application action.
    """
    candidate = canonicalize_job_url(url)
    if not candidate or not _public_http(candidate) or is_job_specific_url(candidate):
        return False

    notice_type = str(article.get("job_notice_type") or "").strip().lower()
    if notice_type != "competition":
        return False
    if not bool(article.get("official_source") or article.get("job_official_source")):
        return False

    detail_url = canonicalize_job_url(
        article.get("job_detail_url")
        or article.get("canonical_url")
        or article.get("url")
        or article.get("source_url")
    )
    if not detail_url or not is_job_specific_url(detail_url):
        return False
    if not _application_action_exposes_url(article, candidate):
        return False

    candidate_host = _url_host(candidate)
    detail_host = _url_host(detail_url)
    if not candidate_host or not detail_host:
        return False

    # Same-host application channels are allowed because the specific official
    # competition page explicitly points to them. Cross-domain channels must
    # themselves be a recognized Moroccan public/academic host.
    return candidate_host == detail_host or _is_public_application_host(candidate_host)


def is_application_url_bound_to_job(article, url):
    candidate = canonicalize_job_url(url)
    if not candidate or not _public_http(candidate):
        return False
    if is_job_specific_url(candidate):
        return not is_foreign_job_detail_url(article, candidate)
    return is_verified_official_application_channel(article, candidate)


def _identity_strong_application_url(article, url):
    url = canonicalize_job_url(url)
    if not url or not is_job_specific_url(url):
        return ""
    if str(article.get("job_application_link_kind") or "").strip().lower() == "official_application_channel":
        return ""
    return url


def _core_key(article):
    return "|".join([
        normalize_text(article.get("job_company") or article.get("company")),
        normalize_text(article.get("job_title") or article.get("fetched_title") or article.get("title")),
        normalize_text(article.get("job_location") or article.get("location")),
    ])


def identity_key(article):
    reference = external_reference(article)
    if reference:
        employer = normalize_text(article.get("job_company") or article.get("company"))
        scope = employer or normalize_text(article.get("source_name"))
        base = f"ref|{scope}|{normalize_text(reference)}"
    else:
        apply_url = _identity_strong_application_url(
            article,
            article.get("job_application_url") or article.get("application_url"),
        )
        canonical = canonicalize_job_url(article.get("canonical_url") or article.get("url") or article.get("source_url"))
        if canonical and is_job_specific_url(canonical):
            base = f"url|{canonical}"
        elif apply_url:
            base = f"apply|{apply_url}"
        else:
            base = f"core|{_core_key(article)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def semantic_key(article):
    return hashlib.sha256(_core_key(article).encode("utf-8")).hexdigest()[:20]


IDENTITY_EVIDENCE_HINTS = (
    "تخصص", "التخصص", "التخصصات", "شعبة", "الشعبة", "درجة", "الدرجة",
    "منصب", "المناصب", "دورة", "الدورة", "فوج", "الفوج", "مباراة", "المباراة",
    "specialite", "spécialité", "specialites", "spécialités", "filiere", "filière",
    "grade", "poste", "postes", "session", "concours", "reference", "référence",
)


def _identity_evidence_fact_rows(article):
    table_facts = set()
    for table in (article.get("source_tables") or []):
        if not isinstance(table, dict):
            continue
        caption = normalize_text(table.get("caption"))
        if caption:
            table_facts.add(caption)
        for row in (table.get("rows") or []):
            if not isinstance(row, (list, tuple)):
                continue
            text = normalize_text(" | ".join(str(cell or "") for cell in row))
            if len(text) < 4:
                continue
            if any(hint in text for hint in IDENTITY_EVIDENCE_HINTS) or re.search(r"\b\d{1,5}\b", text):
                table_facts.add(text)

    document_facts = set()
    for page in (article.get("job_document_texts") or []):
        if not isinstance(page, dict):
            continue
        for raw_line in str(page.get("text") or "").splitlines():
            text = normalize_text(raw_line)
            if len(text) < 4:
                continue
            if any(hint in text for hint in IDENTITY_EVIDENCE_HINTS):
                document_facts.add(text)

    return (
        sorted(table_facts)[:120],
        sorted(document_facts)[:180],
    )


def identity_evidence_snapshot(article):
    table_facts, document_facts = _identity_evidence_fact_rows(article)
    try:
        positions = max(0, int(article.get("job_number_of_positions") or 0))
    except (TypeError, ValueError):
        positions = 0

    reference = normalize_text(external_reference(article))
    deadline = str(article.get("job_deadline") or "").strip()
    document_urls = sorted({
        canonicalize_job_url(item.get("url"))
        for item in (article.get("job_document_links") or [])
        if isinstance(item, dict) and canonicalize_job_url(item.get("url"))
    })

    # Cross-source campaign comparison deliberately excludes the source/employer
    # reference. Different systems can assign different IDs to the same campaign.
    comparison_payload = {
        "deadline": deadline,
        "positions": positions,
        "table_facts": table_facts,
        "document_facts": document_facts,
    }
    categories = []
    strength = 0
    comparison_strength = 0
    if reference:
        categories.append("reference")
        strength += 4
    if deadline:
        categories.append("deadline")
        strength += 1
        comparison_strength += 1
    if positions > 0:
        categories.append("positions")
        strength += 1
        comparison_strength += 1
    if table_facts:
        categories.append("tables")
        strength += 2
        comparison_strength += 2
    if document_facts:
        categories.append("pdf_text")
        strength += 3
        comparison_strength += 3
    if document_urls:
        categories.append("documents")
        strength += 1

    signature = ""
    if any((
        deadline,
        positions > 0,
        table_facts,
        document_facts,
    )):
        signature = hashlib.sha256(
            json.dumps(comparison_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]

    return {
        "signature": signature,
        "strength": strength,
        "comparison_strength": comparison_strength,
        "categories": categories,
        "reference": reference,
        "deadline": deadline,
        "positions": positions,
        "table_facts": table_facts,
        "document_facts": document_facts,
        "document_urls": document_urls,
    }


IDENTITY_EVIDENCE_STATE_FIELDS = (
    "identity_evidence_detail_checked",
    "identity_evidence_tables_checked",
    "identity_evidence_documents_checked",
    "identity_evidence_stage_status",
    "identity_evidence_stage_checked_at",
    "identity_evidence_signature",
    "identity_evidence_strength",
    "identity_evidence_comparison_strength",
    "identity_evidence_categories",
    "identity_evidence_reference",
    "identity_evidence_deadline",
    "identity_evidence_positions",
)


def invalidate_identity_evidence(article, reason=""):
    if not isinstance(article, dict):
        return False
    changed = False
    for field in IDENTITY_EVIDENCE_STATE_FIELDS:
        if field in article:
            article.pop(field, None)
            changed = True
    if changed or reason:
        article["identity_evidence_invalidated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        article["identity_evidence_invalidation_reason"] = str(reason or "verified facts changed")[:180]
    if article.get("status") == "identity_pending":
        article["identity_pending_evidence_status"] = "awaiting_more_evidence"
    return changed


def identity_evidence_stage_complete(article):
    return str(article.get("identity_evidence_stage_status") or "").strip().lower() == "complete"


def finalize_identity_evidence_stage(article, now=None):
    now = now or datetime.now(timezone.utc)
    detail_ready = bool(
        article.get("content_fetch_status") == "success"
        and (
            article.get("job_detail_url")
            or article.get("canonical_url")
            or article.get("url")
        )
    )
    tables_checked = (
        "source_tables" in article
        or "source_tables_count" in article
    )
    document_urls = [
        canonicalize_job_url(item.get("url"))
        for item in (article.get("job_document_links") or [])
        if isinstance(item, dict) and canonicalize_job_url(item.get("url"))
    ]
    expected_document_fingerprint = "|".join(sorted(set(document_urls)))
    document_failures = int(article.get("job_document_text_download_failures") or 0)
    documents_checked = (
        not document_urls
        or (
            str(article.get("identity_evidence_document_fingerprint") or "")
            == expected_document_fingerprint
            and document_failures == 0
        )
    )

    snapshot = identity_evidence_snapshot(article)
    complete = bool(detail_ready and tables_checked and documents_checked)
    article["identity_evidence_detail_checked"] = detail_ready
    article["identity_evidence_tables_checked"] = tables_checked
    article["identity_evidence_documents_checked"] = documents_checked
    article["identity_evidence_stage_status"] = "complete" if complete else "incomplete"
    article["identity_evidence_stage_checked_at"] = now.isoformat(timespec="seconds")
    article["identity_evidence_signature"] = snapshot["signature"]
    article["identity_evidence_strength"] = snapshot["strength"]
    article["identity_evidence_comparison_strength"] = snapshot["comparison_strength"]
    article["identity_evidence_categories"] = snapshot["categories"]
    article["identity_evidence_reference"] = snapshot["reference"]
    article["identity_evidence_deadline"] = snapshot["deadline"]
    article["identity_evidence_positions"] = snapshot["positions"]
    return snapshot


def _identity_evidence_relation(article, record):
    if not identity_evidence_stage_complete(article):
        return "pending"

    article_signature = str(article.get("identity_evidence_signature") or "")
    record_signature = str(record.get("identity_evidence_signature") or "")
    article_strength = int(article.get("identity_evidence_comparison_strength") or 0)
    record_strength = int(
        record.get("identity_evidence_comparison_strength")
        or record.get("identity_evidence_strength")
        or 0
    )

    if article_signature and record_signature:
        if article_signature == record_signature and min(article_strength, record_strength) >= 4:
            return "same"
        if article_signature != record_signature and min(article_strength, record_strength) >= 4:
            return "different"

    article_docs = set(identity_evidence_snapshot(article).get("document_urls") or [])
    record_docs = {
        canonicalize_job_url(value)
        for value in str(record.get("document_urls") or "").split("|")
        if canonicalize_job_url(value)
    }
    if article_docs and record_docs and article_docs.intersection(record_docs):
        return "same"

    return "unknown"


def _final_duplicate_decision(article, reason, record):
    if not identity_evidence_stage_complete(article):
        return {
            "action": "hold",
            "reason": f"potential duplicate awaiting evidence stage: {reason}",
            "existing": record,
        }
    return {"action": "duplicate", "reason": reason, "existing": record}


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
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(text[:10], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _public_http(url):
    try:
        parsed = urlparse(str(url or "").strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def job_deadline_time(article):
    raw = str(article.get("job_deadline") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            day = datetime.fromisoformat(raw).date()
            return datetime.combine(day, time.max, tzinfo=ZoneInfo(MOROCCO_TIMEZONE)).astimezone(timezone.utc)
        except ValueError:
            return None
    return _parse_date(raw)


def job_labels(article):
    labels = ["jobs"]
    eligibility = str(article.get("job_eligibility") or article.get("eligibility") or "").strip().lower()
    remote = bool(article.get("job_remote") or article.get("remote"))
    visa = bool(article.get("job_visa_sponsorship") or article.get("visa_sponsorship"))
    country = str(article.get("job_country") or article.get("country") or "").upper()

    if country == "MA" or eligibility == "morocco":
        labels.append("jobs-morocco")
    elif remote or eligibility == "remote_morocco":
        labels.append("remote-jobs")
    else:
        labels.append("jobs-abroad")
    if visa or eligibility == "visa_confirmed":
        labels.append("visa-sponsorship")
    return list(dict.fromkeys(labels))


def score_job(article, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    points = {}
    official = bool(article.get("official_source") or article.get("job_official_source"))
    points["official_source"] = 25 if official else 0

    published = _parse_date(article.get("job_published_at") or article.get("source_published_at"))
    publication_age_hours = (
        (now - published).total_seconds() / 3600
        if published
        else None
    )
    fresh = bool(
        publication_age_hours is not None
        and 0 <= publication_age_hours <= JOBS_MAX_PUBLISH_AGE_HOURS
    )
    points[f"fresh_under_{JOBS_MAX_PUBLISH_AGE_HOURS}h"] = 15 if fresh else 0
    points["preferred_tech_or_student"] = 10 if job_focus_priority(article) > 0 else 0

    priority = str(article.get("source_priority") or "").strip().lower()
    points["trusted_priority_source"] = 10 if priority in TOP_SOURCE_PRIORITIES else 0

    try:
        positions = max(0, int(article.get("job_number_of_positions") or article.get("number_of_positions") or 0))
    except (TypeError, ValueError):
        positions = 0
    points["large_hiring"] = 10 if positions >= 10 else 0
    points["clear_deadline"] = 10 if str(article.get("job_deadline") or "").strip() else 0
    points["clear_location"] = 5 if str(article.get("job_location") or "").strip() else 0
    points["clear_diploma"] = 5 if str(article.get("job_diploma") or "").strip() else 0

    apply_url = article.get("job_application_url") or article.get("application_url") or article.get("url")
    valid_apply = bool(
        _public_http(apply_url)
        and is_application_url_bound_to_job(article, apply_url)
    )
    points["clear_application"] = 10 if valid_apply else 0
    points["salary_listed"] = 5 if str(article.get("job_salary") or "").strip() else 0
    points["entry_level_or_student"] = 5 if bool(article.get("job_entry_level")) else 0

    score = max(0, min(sum(points.values()), 100))
    eligibility = str(article.get("job_eligibility") or "").strip().lower()
    source_url = article.get("canonical_url") or article.get("url") or article.get("source_url")
    reasons = []
    if eligibility not in GOOD_ELIGIBILITY:
        reasons.append("eligibility must be verified")
    if not _public_http(source_url):
        reasons.append("invalid source URL")
    if not valid_apply:
        reasons.append("missing verified application resource")

    if published is None:
        reasons.append("publication time is not verified")
    elif publication_age_hours is not None and publication_age_hours < 0:
        reasons.append("publication time is in the future")
    elif publication_age_hours is not None and publication_age_hours > JOBS_MAX_PUBLISH_AGE_HOURS:
        reasons.append(f"job is older than {JOBS_MAX_PUBLISH_AGE_HOURS} hours")

    normalized_title = normalize_text(article.get("job_title") or article.get("title"))
    if normalized_title in {
        "jobs", "job", "careers", "career", "recruitment", "recrutement",
        "vacancies", "opportunities", "emploi", "offres d emploi",
    }:
        reasons.append("generic careers/listing page is not a job posting")

    notice_type = str(article.get("job_notice_type") or "vacancy").strip().lower()
    deadline = job_deadline_time(article)
    expired = bool(notice_type in {"vacancy", "competition"} and deadline and deadline < now)
    if expired:
        reasons.append("deadline passed")

    # Ranking score is intentionally NOT a publication gate. A legitimate,
    # verified vacancy can score low simply because salary, diploma, location,
    # recency or large-hiring signals are absent. Those signals only decide
    # priority between otherwise publishable jobs.
    hard_gate_passed = not reasons
    permanent_hard_failure = bool(
        expired
        or published is None
        or publication_age_hours is None
        or publication_age_hours < 0
        or publication_age_hours > JOBS_MAX_PUBLISH_AGE_HOURS
        or not _public_http(source_url)
        or normalized_title in {
            "jobs", "job", "careers", "career", "recruitment", "recrutement",
            "vacancies", "opportunities", "emploi", "offres d emploi",
        }
    )
    passed = hard_gate_passed
    status = (
        "publish"
        if hard_gate_passed
        else ("reject" if permanent_hard_failure else "queue")
    )
    return {
        "score": score,
        "ranking_score": score,
        "status": status,
        "passed": passed,
        "hard_gate_passed": hard_gate_passed,
        "publication_age_hours": round(publication_age_hours, 2) if publication_age_hours is not None else None,
        "max_publish_age_hours": JOBS_MAX_PUBLISH_AGE_HOURS,
        "focus_priority": job_focus_priority(article),
        "points": points,
        "reasons": reasons,
        # Kept for compatibility/diagnostics only. This threshold no longer
        # decides publishability; it is a historical ranking reference.
        "threshold": MIN_SELECTION_SCORE,
        "queue_threshold": QUEUE_SCORE,
        "threshold_applies_to": "ranking_only",
    }


def classify_urgency(article, now=None):
    now = now or datetime.now(timezone.utc)
    notice_type = str(article.get("job_notice_type") or "vacancy").strip().lower()
    if notice_type not in {"vacancy", "competition"}:
        return {
            "level": "normal",
            "publish_immediately": False,
            "allow_daily_override": False,
            "reason": f"employment notice update: {notice_type}",
        }
    deadline = job_deadline_time(article)
    days = (deadline - now).total_seconds() / 86400 if deadline else None
    try:
        positions = max(0, int(article.get("job_number_of_positions") or 0))
    except (TypeError, ValueError):
        positions = 0
    official = bool(article.get("official_source") or article.get("job_official_source"))

    if days is not None and days < 0:
        return {"level": "expired", "publish_immediately": False, "allow_daily_override": False, "reason": "deadline passed"}
    if official and days is not None and days <= 2:
        return {"level": "critical", "publish_immediately": True, "allow_daily_override": True, "reason": "official opportunity closes within 48 hours"}
    if official and positions >= 100 and (days is None or days <= 7):
        return {"level": "high", "publish_immediately": True, "allow_daily_override": True, "reason": "large official hiring campaign"}
    if official and positions >= 20:
        return {"level": "elevated", "publish_immediately": False, "allow_daily_override": False, "reason": "notable official hiring campaign"}
    return {"level": "normal", "publish_immediately": False, "allow_daily_override": False, "reason": ""}


_ARABIC_SLUG_MAP = {
    "ا": "a", "أ": "a", "إ": "a", "آ": "a", "ء": "a", "ؤ": "w", "ئ": "y",
    "ب": "b", "ت": "t", "ث": "th", "ج": "j", "ح": "h", "خ": "kh",
    "د": "d", "ذ": "dh", "ر": "r", "ز": "z", "س": "s", "ش": "sh",
    "ص": "s", "ض": "d", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh",
    "ف": "f", "ق": "q", "ك": "k", "ل": "l", "م": "m", "ن": "n",
    "ه": "h", "ة": "a", "و": "w", "ي": "y", "ى": "a",
}


def _latinize(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(_ARABIC_SLUG_MAP.get(ch, ch) for ch in text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    # Job permalink stems are intentionally alphabetic: Blogger's date folders
    # contain digits, but the post filename itself must never contain them.
    text = re.sub(r"[^a-z]+", "-", text).strip("-")
    return text


def _alpha_token(seed, length=8):
    digest = hashlib.sha256(str(seed or "job").encode("utf-8")).digest()
    return "".join(chr(ord("a") + (byte % 26)) for byte in digest[:length])


def desired_slug(article, campaign_id=""):
    company = _latinize(article.get("job_company") or article.get("company"))
    title = _latinize(article.get("job_title") or article.get("title"))
    base_words = [x for x in f"{company}-{title}".split("-") if x][:8]
    base = "-".join(base_words).strip("-") or "job"

    reference_seed = str(
        article.get("job_external_reference")
        or article.get("ats_reference")
        or ""
    ).strip()
    if not reference_seed:
        for key in ("job_application_url", "job_detail_url", "url", "canonical_url"):
            value = str(article.get(key) or "").strip()
            if value:
                reference_seed = value
                break

    token_seed = reference_seed or campaign_id or identity_key(article)
    token = _alpha_token(token_seed)
    return f"{base}-{token}"[:90].strip("-")


def _state_default():
    return {"daily_publish_count": {}, "daily_urgent_override_count": {}, "last_publish_at": ""}


def load_job_state():
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        base = _state_default()
        if isinstance(data, dict):
            base.update(data)
        return base
    except Exception:
        return _state_default()


def save_job_state(state):
    # Daily counters are useful for pacing, but must not grow forever.
    for key in ("daily_publish_count", "daily_urgent_override_count"):
        rows = state.get(key)
        if isinstance(rows, dict) and len(rows) > 180:
            for day in sorted(rows)[:-180]:
                rows.pop(day, None)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _day_key(now=None):
    return _local(now).date().isoformat()


def daily_publish_cap(now=None):
    if JOBS_ADAPTIVE_PUBLISHING:
        return int(current_policy(now=now).get("daily_cap") or 3)
    local = _local(now)
    month_max = MONTHLY_VOLUME_RANGE.get(local.month, (1, 2))[1]
    weekday_cap = WEEKDAY_BLOGGER_CAP.get(local.weekday(), month_max)
    return min(month_max, weekday_cap)


def publishable_backlog_count(now=None):
    path = BASE_DIR / "jobs_article_queue.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, TypeError):
        return 0
    count = 0
    for article in payload.get("articles", []):
        if article.get("archived") or article.get("status") not in {"ready", "selected"}:
            continue
        if article.get("content_fetch_status") != "success":
            continue
        if score_job(dict(article), now=now).get("passed"):
            count += 1
    return count


def adaptive_publish_interval_minutes(publishable_backlog=None):
    backlog = publishable_backlog_count() if publishable_backlog is None else max(0, int(publishable_backlog or 0))
    target = 5 if backlog >= 16 else 7 if backlog >= 6 else 10
    return max(JOBS_MIN_PUBLISH_INTERVAL_MINUTES, target)


def job_publish_window_status(now=None, publishable_backlog=None):
    local = _local(now)
    state = load_job_state()
    day = local.date().isoformat()
    published_today = int(state.get("daily_publish_count", {}).get(day, 0))
    cap = daily_publish_cap(now)
    # Publish the next verified queued job as soon as the anti-spam interval
    # has elapsed. The adaptive daily cap still limits total volume.
    publishable_backlog = (
        publishable_backlog_count(now=now)
        if publishable_backlog is None
        else max(0, int(publishable_backlog or 0))
    )
    spread_interval = adaptive_publish_interval_minutes(publishable_backlog)
    reasons = []
    next_allowed = local

    if published_today >= cap:
        reasons.append("adaptive daily Blogger cap reached")
        tomorrow = local.date().fromordinal(local.date().toordinal() + 1)
        next_allowed = datetime.combine(
            tomorrow,
            time(JOBS_ACTIVE_START_HOUR, 0),
            tzinfo=local.tzinfo,
        )

    if not (JOBS_ACTIVE_START_HOUR <= local.hour < JOBS_ACTIVE_END_HOUR):
        reasons.append("outside Blogger active publishing window")
        if local.hour >= JOBS_ACTIVE_END_HOUR:
            tomorrow = local.date().fromordinal(local.date().toordinal() + 1)
            window_next = datetime.combine(
                tomorrow,
                time(JOBS_ACTIVE_START_HOUR, 0),
                tzinfo=local.tzinfo,
            )
        else:
            window_next = datetime.combine(
                local.date(),
                time(JOBS_ACTIVE_START_HOUR, 0),
                tzinfo=local.tzinfo,
            )
        if window_next > next_allowed:
            next_allowed = window_next

    last_publish = _parse_date(state.get("last_publish_at"))
    if last_publish:
        last_local = last_publish.astimezone(local.tzinfo)
        interval_next = last_local + __import__("datetime").timedelta(minutes=spread_interval)
        if interval_next > local:
            reasons.append("adaptive Blogger spacing has not elapsed")
            if interval_next > next_allowed:
                next_allowed = interval_next

    return {
        "allowed_now": not reasons,
        "reasons": reasons,
        "next_allowed_time": next_allowed,
        "published_today": published_today,
        "daily_cap": cap,
        "min_interval_minutes": spread_interval,
        "publishable_backlog": publishable_backlog,
        "policy": current_policy(now=now),
    }


def can_publish_new_job(now=None):
    return bool(job_publish_window_status(now=now)["allowed_now"])


def can_use_urgent_override(now=None):
    state = load_job_state()
    return int(state.get("daily_urgent_override_count", {}).get(_day_key(now), 0)) < URGENT_EXTRA_DAILY_LIMIT


def _shard(key):
    return (str(key or "00")[:2] or "00").lower()


def _memory_path(kind, key):
    return MEMORY_DIR / kind / _shard(key) / f"{key}.json"


def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_by_identity(key):
    pointer = _load_json(_memory_path("identity", key))
    campaign_id = pointer.get("campaign_id") if isinstance(pointer, dict) else ""
    if not campaign_id:
        return {}
    return _load_json(_memory_path("campaigns", campaign_id))


def get_semantic_candidates(key):
    index = _load_json(_memory_path("semantic", key))
    ids = index.get("campaign_ids", []) if isinstance(index, dict) else []
    return [
        row for row in (_load_json(_memory_path("campaigns", campaign_id)) for campaign_id in ids)
        if row
    ]


def maintain_job_memory(now=None, retention_days=730):
    """Prune campaign memory that is far beyond the campaign rollover horizon."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now.astimezone(timezone.utc) - timedelta(
        days=max(365, int(retention_days or 730))
    )
    stats = {
        "campaigns_checked": 0,
        "campaigns_pruned": 0,
        "identity_pointers_pruned": 0,
        "semantic_refs_pruned": 0,
    }

    campaigns_dir = MEMORY_DIR / "campaigns"
    if not campaigns_dir.exists():
        return stats

    for path in list(campaigns_dir.rglob("*.json")):
        record = _load_json(path)
        if not isinstance(record, dict):
            continue
        stats["campaigns_checked"] += 1
        anchor = _parse_date(record.get("updated_at") or record.get("published_at"))
        if not anchor:
            continue
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        if anchor.astimezone(timezone.utc) >= cutoff:
            continue

        campaign_id = str(record.get("campaign_id") or path.stem)
        identity = str(record.get("identity_key") or "")
        semantic = str(record.get("semantic_key") or "")

        path.unlink(missing_ok=True)
        stats["campaigns_pruned"] += 1

        if identity:
            identity_path = _memory_path("identity", identity)
            pointer = _load_json(identity_path)
            if (
                isinstance(pointer, dict)
                and str(pointer.get("campaign_id") or "") == campaign_id
            ):
                identity_path.unlink(missing_ok=True)
                stats["identity_pointers_pruned"] += 1

        if semantic:
            semantic_path = _memory_path("semantic", semantic)
            index = _load_json(semantic_path)
            ids = list(index.get("campaign_ids", [])) if isinstance(index, dict) else []
            kept = [value for value in ids if str(value) != campaign_id]
            if kept != ids:
                stats["semantic_refs_pruned"] += len(ids) - len(kept)
                if kept:
                    _save_json(semantic_path, {"campaign_ids": kept[-20:]})
                else:
                    semantic_path.unlink(missing_ok=True)

    for kind in ("campaigns", "identity", "semantic"):
        root = MEMORY_DIR / kind
        if not root.exists():
            continue
        for directory in sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass

    return stats


def _same_url_family(a, b):
    a, b = canonicalize_job_url(a), canonicalize_job_url(b)
    if not a or not b:
        return False
    pa, pb = urlparse(a), urlparse(b)
    if pa.netloc != pb.netloc:
        return False
    if a == b:
        return True
    aa = [x for x in pa.path.split("/") if x]
    bb = [x for x in pb.path.split("/") if x]
    return bool(aa and bb and aa[-1] == bb[-1])


def _material_change(article, record):
    document_urls = [
        canonicalize_job_url(row.get("url"))
        for row in (article.get("job_document_links") or [])
        if isinstance(row, dict) and row.get("url")
    ]
    pairs = {
        "number_of_positions": article.get("job_number_of_positions"),
        "deadline": article.get("job_deadline"),
        "salary": article.get("job_salary"),
        "contract_type": article.get("job_contract_type"),
        "location": article.get("job_location"),
        "application_url": canonicalize_job_url(article.get("job_application_url")),
        "notice_type": article.get("job_notice_type") or "vacancy",
        "notice_status": article.get("job_notice_status") or "",
        "document_urls": "|".join(sorted(x for x in document_urls if x)),
    }
    for field, new_value in pairs.items():
        old_value = record.get(field)
        if field == "application_url":
            old_value = canonicalize_job_url(old_value)
        if str(new_value or "").strip() != str(old_value or "").strip():
            return True

    if identity_evidence_stage_complete(article):
        article_signature = str(article.get("identity_evidence_signature") or "")
        record_signature = str(record.get("identity_evidence_signature") or "")
        article_strength = int(article.get("identity_evidence_comparison_strength") or 0)
        record_strength = int(
            record.get("identity_evidence_comparison_strength")
            or record.get("identity_evidence_strength")
            or 0
        )
        if (
            article_signature
            and record_signature
            and article_signature != record_signature
            and min(article_strength, record_strength) >= 4
        ):
            return True
    return False


def _campaign_rollover(article, record):
    new_posted = _parse_date(article.get("job_published_at") or article.get("source_published_at"))
    old_deadline = _parse_date(record.get("deadline"))
    old_posted = _parse_date(record.get("published_at"))
    if new_posted and old_deadline and (new_posted - old_deadline).days >= 90:
        return True
    if new_posted and old_posted and (new_posted - old_posted).days >= 330:
        return True
    return False


def _same_campaign_evidence(article, record):
    if semantic_key(article) != str(record.get("semantic_key") or ""):
        return False
    if not identity_evidence_stage_complete(article):
        return False

    relation = _identity_evidence_relation(article, record)
    if relation == "same":
        return True
    if relation == "different":
        return False

    # Legacy campaign records may not yet have an evidence signature. In that
    # case only a strong vacancy-specific application relation plus matching
    # campaign facts can confirm a duplicate. Deadline/positions alone are not
    # enough to bury a potentially distinct specialization or recruitment round.
    evidence = 0
    new_deadline = str(article.get("job_deadline") or "").strip()
    old_deadline = str(record.get("deadline") or "").strip()
    if new_deadline and old_deadline and new_deadline == old_deadline:
        evidence += 1
    try:
        new_positions = int(article.get("job_number_of_positions") or 0)
        old_positions = int(record.get("number_of_positions") or 0)
    except (TypeError, ValueError):
        new_positions = old_positions = 0
    if new_positions > 0 and old_positions > 0 and new_positions == old_positions:
        evidence += 1
    new_posted = _parse_date(article.get("job_published_at") or article.get("source_published_at"))
    old_posted = _parse_date(record.get("published_at"))
    if new_posted and old_posted and abs((new_posted - old_posted).days) <= 3:
        evidence += 1
    new_apply = _identity_strong_application_url(article, article.get("job_application_url"))
    old_apply = canonicalize_job_url(record.get("application_url"))
    if new_apply and old_apply and is_job_specific_url(old_apply) and _same_url_family(new_apply, old_apply):
        evidence += 4
    return evidence >= 6


def _classify_identity_against_record(article, record):
    if _campaign_rollover(article, record):
        return {"action": "new_campaign", "reason": "campaign rollover", "existing": record}

    new_ref, old_ref = external_reference(article), str(record.get("external_reference") or "")
    if new_ref and old_ref and normalize_text(new_ref) == normalize_text(old_ref):
        if not identity_evidence_stage_complete(article):
            return {
                "action": "hold",
                "reason": "same external reference awaiting evidence stage",
                "existing": record,
            }
        changed = _material_change(article, record)
        return (
            {"action": "update", "reason": "same external reference with verified material change", "existing": record}
            if changed
            else _final_duplicate_decision(article, "same external reference", record)
        )

    new_apply = _identity_strong_application_url(article, article.get("job_application_url"))
    old_apply = canonicalize_job_url(record.get("application_url"))
    if new_apply and old_apply and is_job_specific_url(old_apply) and new_apply == old_apply:
        if not identity_evidence_stage_complete(article):
            return {
                "action": "hold",
                "reason": "same application URL awaiting evidence stage",
                "existing": record,
            }
        changed = _material_change(article, record)
        return (
            {"action": "update", "reason": "same application URL with verified material change", "existing": record}
            if changed
            else _final_duplicate_decision(article, "same application URL", record)
        )

    if semantic_key(article) != record.get("semantic_key"):
        return {"action": "new", "reason": "different company/title/location", "existing": {}}
    if new_ref and old_ref and normalize_text(new_ref) != normalize_text(old_ref):
        if _same_campaign_evidence(article, record):
            return _final_duplicate_decision(article, "same campaign confirmed across sources", record)
        return {"action": "new_campaign", "reason": "different external reference", "existing": record}
    if new_apply and old_apply and is_job_specific_url(old_apply) and not _same_url_family(new_apply, old_apply):
        if _same_campaign_evidence(article, record):
            return _final_duplicate_decision(article, "same campaign facts across different application URLs", record)
        return {"action": "new_campaign", "reason": "different application URL", "existing": record}

    new_posted = _parse_date(article.get("job_published_at") or article.get("source_published_at"))
    old_posted = _parse_date(record.get("published_at"))
    if new_posted and old_posted and abs((new_posted - old_posted).days) >= 30:
        return {"action": "new_campaign", "reason": "same role reposted at least 30 days later", "existing": record}

    relation = _identity_evidence_relation(article, record)
    if relation == "same":
        return _final_duplicate_decision(article, "verified evidence matches existing campaign", record)
    if relation == "different":
        return {"action": "new_campaign", "reason": "verified evidence differs from existing campaign", "existing": record}
    if identity_evidence_stage_complete(article):
        return {
            "action": "new_campaign",
            "reason": "evidence stage complete; duplicate not confirmed",
            "existing": record,
        }

    return {"action": "hold", "reason": "ambiguous same role awaiting evidence stage", "existing": record}


def classify_identity(article):
    exact = identity_key(article)
    exact_record = get_by_identity(exact)
    if exact_record:
        return _classify_identity_against_record(article, exact_record)

    records = list(get_semantic_candidates(semantic_key(article)))
    if not records:
        return {"action": "new", "reason": "no existing campaign", "existing": {}}

    # Semantic memory can contain several historical campaigns for the same
    # company/title/location. Compare all of them instead of trusting index order.
    decisions = [
        _classify_identity_against_record(article, record)
        for record in reversed(records)
        if isinstance(record, dict) and record
    ]
    if not decisions:
        return {"action": "new", "reason": "no usable existing campaign", "existing": {}}

    # Updating the campaign identified by strong verified evidence is preferable
    # to inserting another post. A confirmed duplicate is next. If any candidate
    # still needs evidence, do not declare a new campaign yet.
    for preferred_action in ("update", "duplicate", "hold"):
        for decision in decisions:
            if decision.get("action") == preferred_action:
                return decision

    for decision in decisions:
        if decision.get("action") == "new_campaign":
            return decision
    return decisions[0]


def _new_campaign_id():
    return hashlib.sha256(os.urandom(24)).hexdigest()[:20]


def _mark_identity_pending(article, decision, now=None):
    now = now or datetime.now(timezone.utc)
    article["status"] = "identity_pending"
    article["job_identity_action"] = "hold"
    article["job_identity_reason"] = str(decision.get("reason") or "ambiguous identity")
    article["identity_pending_since"] = (
        article.get("identity_pending_since")
        or now.isoformat(timespec="seconds")
    )
    article["identity_pending_last_checked_at"] = now.isoformat(timespec="seconds")
    article["identity_pending_evidence_status"] = "awaiting_more_evidence"
    article.pop("skip_reason", None)
    return article


def prepare_job_candidate(article, now=None):
    quality = score_job(article, now=now)
    article["job_score"] = quality["score"]
    article["job_rank_score"] = quality.get("ranking_score", quality["score"])
    article["score"] = quality["score"]
    article["job_quality_status"] = quality["status"]
    article["job_quality_reasons"] = quality["reasons"]
    article["job_hard_gate_passed"] = bool(quality.get("hard_gate_passed", quality["passed"]))
    article["job_hard_gate_reasons"] = list(quality["reasons"])
    article["labels"] = job_labels(article)
    urgency = classify_urgency(article, now=now)
    article["job_urgency"] = urgency

    decision = classify_identity(article)
    article["job_identity_action"] = decision["action"]
    article["job_identity_reason"] = decision["reason"]
    existing = decision.get("existing") or {}

    if decision["action"] == "update":
        article["blogger_post_id"] = existing.get("blogger_post_id", "")
        article["blogger_post_url"] = existing.get("blogger_url", "")
        campaign_id = existing.get("campaign_id", "")
    else:
        campaign_id = ""
    if not campaign_id and decision["action"] in {"new", "new_campaign"}:
        campaign_id = _new_campaign_id()
    article["job_campaign_id"] = campaign_id
    article["seo_slug"] = desired_slug(article, campaign_id=campaign_id)
    article["desired_slug"] = article["seo_slug"]
    return quality, decision


def select_best_job_from_queue(queue, now=None):
    now = now or datetime.now(timezone.utc)
    ranked = []
    for article in queue.get("articles", []):
        if article.get("archived") or article.get("status") not in {"ready", "selected"}:
            continue
        if article.get("content_fetch_status") != "success":
            continue
        retry_after = _parse_date(article.get("candidate_retry_after") or article.get("enrichment_retry_after"))
        if retry_after and retry_after > now:
            continue
        quality, decision = prepare_job_candidate(article, now=now)
        if decision["action"] == "duplicate":
            article["status"] = "skipped"
            article["skip_reason"] = f"job duplicate confirmed: {decision['reason']}"
            article["job_identity_final"] = True
            article["job_duplicate_confirmed_at"] = now.isoformat(timespec="seconds")
            continue
        if decision["action"] == "hold":
            _mark_identity_pending(article, decision, now=now)
            continue
        if not quality["passed"]:
            if quality["status"] == "reject":
                article["status"] = "skipped"
                article["skip_reason"] = "; ".join(quality["reasons"]) or "hard gate rejected job"
            continue

        article.pop("identity_pending_last_checked_at", None)
        article.pop("identity_pending_evidence_status", None)
        article.pop("job_duplicate_confirmed_at", None)
        article["job_identity_final"] = False
        is_update = decision["action"] == "update"
        urgency = article.get("job_urgency") or {}
        normal_slot = is_update or can_publish_new_job(now=now)
        urgent_override = (
            not is_update
            and not normal_slot
            and urgency.get("allow_daily_override")
            and can_use_urgent_override(now=now)
        )
        if not normal_slot and not urgent_override:
            continue
        article["job_urgent_override"] = bool(urgent_override)
        article["job_publish_immediately"] = bool(urgency.get("publish_immediately"))
        priority = 2 if urgency.get("level") in {"critical", "high"} else 1 if urgency.get("level") == "elevated" else 0

        # Closing-soon notices stay first. Inside the verified <= freshness
        # window, preferred cyber/IT/developer/internship roles come before
        # general jobs; freshness and quality then break remaining ties.
        published = _parse_date(
            article.get("job_published_at")
            or article.get("source_published_at")
        )
        discovered = _parse_date(article.get("discovered_at"))
        published_epoch = published.timestamp() if published else 0.0
        discovered_epoch = discovered.timestamp() if discovered else 0.0
        focus_priority = job_focus_priority(article)
        ranked.append(
            (
                priority,
                focus_priority,
                published_epoch,
                discovered_epoch,
                quality["score"],
                article,
            )
        )

    ranked.sort(
        key=lambda row: (row[0], row[1], row[2], row[3], row[4]),
        reverse=True,
    )
    return ranked[0][5] if ranked else None


def record_job_publish(article, now=None):
    action = str(article.get("job_identity_action") or "")
    if action not in {"new", "new_campaign", "update"}:
        return {}
    now = now or datetime.now(timezone.utc)
    campaign_id = article.get("job_campaign_id") or _new_campaign_id()
    ikey, skey = identity_key(article), semantic_key(article)
    previous = get_by_identity(ikey) or {}
    already_recorded = bool(
        article.get("blogger_post_id")
        and previous.get("blogger_post_id") == article.get("blogger_post_id")
    )
    record = {
        "campaign_id": campaign_id,
        "identity_key": ikey,
        "semantic_key": skey,
        "external_reference": external_reference(article),
        "company": article.get("job_company", ""),
        "title": article.get("job_title") or article.get("title", ""),
        "location": article.get("job_location", ""),
        "published_at": article.get("job_published_at") or article.get("source_published_at", ""),
        "deadline": article.get("job_deadline", ""),
        "number_of_positions": article.get("job_number_of_positions", 0),
        "salary": article.get("job_salary", ""),
        "contract_type": article.get("job_contract_type", ""),
        "application_url": canonicalize_job_url(article.get("job_application_url")),
        "notice_type": article.get("job_notice_type") or "vacancy",
        "notice_status": article.get("job_notice_status") or "",
        "document_urls": "|".join(sorted(
            canonicalize_job_url(row.get("url"))
            for row in (article.get("job_document_links") or [])
            if isinstance(row, dict) and row.get("url")
        )),
        "identity_evidence_stage_status": article.get("identity_evidence_stage_status", ""),
        "identity_evidence_signature": article.get("identity_evidence_signature", ""),
        "identity_evidence_strength": int(article.get("identity_evidence_strength") or 0),
        "identity_evidence_comparison_strength": int(
            article.get("identity_evidence_comparison_strength") or 0
        ),
        "identity_evidence_categories": list(article.get("identity_evidence_categories") or []),
        "identity_evidence_reference": article.get("identity_evidence_reference", ""),
        "identity_evidence_deadline": article.get("identity_evidence_deadline", ""),
        "identity_evidence_positions": article.get("identity_evidence_positions", 0),
        "source_url": canonicalize_job_url(article.get("url") or article.get("source_url")),
        "source_name": article.get("source_name", ""),
        "source_priority": article.get("source_priority", ""),
        "blogger_post_id": article.get("blogger_post_id", ""),
        "blogger_url": article.get("blogger_post_url", ""),
        "desired_slug": article.get("desired_slug", ""),
        "facebook_status": (
            article.get("facebook_status")
            or previous.get("facebook_status")
            or ""
        ),
        "facebook_post_id": (
            article.get("facebook_post_id")
            or previous.get("facebook_post_id")
            or ""
        ),
        "facebook_posted_at": (
            article.get("facebook_posted_at")
            or previous.get("facebook_posted_at")
            or ""
        ),
        "facebook_comment_id": (
            article.get("facebook_comment_id")
            or previous.get("facebook_comment_id")
            or ""
        ),
        "facebook_queued_at": (
            article.get("facebook_queued_at")
            or previous.get("facebook_queued_at")
            or ""
        ),
        "facebook_retry_after_epoch": (
            article.get("facebook_retry_after_epoch")
            or previous.get("facebook_retry_after_epoch")
            or 0
        ),
        "facebook_comment_retry_after_epoch": (
            article.get("facebook_comment_retry_after_epoch")
            or previous.get("facebook_comment_retry_after_epoch")
            or 0
        ),
        "facebook_delivery_uncertain_at": (
            article.get("facebook_delivery_uncertain_at")
            or previous.get("facebook_delivery_uncertain_at")
            or ""
        ),
        "facebook_image_status": (
            article.get("facebook_image_status")
            or previous.get("facebook_image_status")
            or ""
        ),
        "facebook_error": str(
            article.get("facebook_error")
            or previous.get("facebook_error")
            or ""
        )[:300],
        "status": "active",
        "updated_at": now.isoformat(),
    }
    _save_json(_memory_path("campaigns", campaign_id), record)
    _save_json(_memory_path("identity", ikey), {"campaign_id": campaign_id})
    sem_path = _memory_path("semantic", skey)
    sem = _load_json(sem_path)
    ids = list(sem.get("campaign_ids", [])) if isinstance(sem, dict) else []
    if campaign_id not in ids:
        ids.append(campaign_id)
    _save_json(sem_path, {"campaign_ids": ids[-20:]})

    if action in {"new", "new_campaign"} and not already_recorded:
        state = load_job_state()
        day = _day_key(now)
        counts = state.setdefault("daily_publish_count", {})
        counts[day] = int(counts.get(day, 0)) + 1
        if article.get("job_urgent_override"):
            urgent = state.setdefault("daily_urgent_override_count", {})
            urgent[day] = int(urgent.get(day, 0)) + 1
        state["last_publish_at"] = now.isoformat()
        save_job_state(state)
    return record


def list_active_job_campaign_records(limit=500):
    """Return durable published campaign records newest-first for queue recovery."""
    campaigns_dir = MEMORY_DIR / "campaigns"
    if not campaigns_dir.exists():
        return []

    records = []
    for path in campaigns_dir.rglob("*.json"):
        record = _load_json(path)
        if not isinstance(record, dict):
            continue
        if str(record.get("status") or "active").strip().lower() != "active":
            continue
        if not str(record.get("blogger_url") or "").strip():
            continue
        records.append(record)

    records.sort(
        key=lambda row: str(row.get("updated_at") or row.get("published_at") or ""),
        reverse=True,
    )
    return records[: max(1, int(limit or 1))]


def record_job_social_state(article, now=None):
    """Persist Facebook delivery state independently from the volatile article queue."""
    if not isinstance(article, dict):
        return {}

    campaign_id = str(article.get("job_campaign_id") or "").strip()
    if not campaign_id:
        pointer = get_by_identity(identity_key(article))
        campaign_id = str((pointer or {}).get("campaign_id") or "").strip()
    if not campaign_id:
        return {}

    path = _memory_path("campaigns", campaign_id)
    record = _load_json(path)
    if not isinstance(record, dict) or not record:
        return {}

    now = now or datetime.now(timezone.utc)
    record.update({
        "facebook_status": str(article.get("facebook_status") or ""),
        "facebook_post_id": str(article.get("facebook_post_id") or ""),
        "facebook_posted_at": str(article.get("facebook_posted_at") or ""),
        "facebook_comment_id": str(article.get("facebook_comment_id") or ""),
        "facebook_queued_at": str(article.get("facebook_queued_at") or ""),
        "facebook_retry_after_epoch": article.get("facebook_retry_after_epoch") or 0,
        "facebook_comment_retry_after_epoch": article.get("facebook_comment_retry_after_epoch") or 0,
        "facebook_delivery_uncertain_at": str(article.get("facebook_delivery_uncertain_at") or ""),
        "facebook_image_status": str(article.get("facebook_image_status") or ""),
        "facebook_error": str(article.get("facebook_error") or "")[:300],
        "updated_at": now.isoformat(),
    })
    _save_json(path, record)
    return record


def facebook_slot_status(posted_times=None, now=None, urgent=False, window_minutes=50):
    local_now = _local(now)
    if urgent:
        return {"allowed_now": True, "mode": "immediate", "slot": "", "next_slot": local_now.isoformat()}

    posted_times = posted_times or []
    local_posts = []
    for value in posted_times:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = _parse_date(value)
        if not dt:
            continue
        local_posts.append(dt.astimezone(ZoneInfo(MOROCCO_TIMEZONE)))

    slots = FACEBOOK_SLOTS.get(local_now.weekday(), ())
    for slot in slots:
        target = datetime.combine(local_now.date(), slot, tzinfo=local_now.tzinfo)
        delta = (local_now - target).total_seconds() / 60
        if 0 <= delta <= window_minutes:
            already_used = any(
                p.date() == local_now.date()
                and -45 * 60 <= (p - target).total_seconds() <= window_minutes * 60
                for p in local_posts
            )
            if not already_used:
                return {"allowed_now": True, "mode": "scheduled", "slot": target.isoformat(), "next_slot": target.isoformat()}

    future = []
    for add_days in range(0, 8):
        day = local_now.date().fromordinal(local_now.date().toordinal() + add_days)
        weekday = (local_now.weekday() + add_days) % 7
        for slot in FACEBOOK_SLOTS.get(weekday, ()):
            target = datetime.combine(day, slot, tzinfo=local_now.tzinfo)
            if target > local_now:
                future.append(target)
    next_slot = min(future).isoformat() if future else ""
    return {"allowed_now": False, "mode": "scheduled", "slot": "", "next_slot": next_slot}


def job_status_snapshot(now=None):
    local = _local(now)
    state = load_job_state()
    day = local.date().isoformat()
    window = job_publish_window_status(now=now)
    return {
        "local_time": local.isoformat(),
        "daily_cap": window["daily_cap"],
        "published_today": int(state.get("daily_publish_count", {}).get(day, 0)),
        "urgent_overrides_today": int(state.get("daily_urgent_override_count", {}).get(day, 0)),
        "monthly_range": MONTHLY_VOLUME_RANGE.get(local.month, (1, 2)),
        "facebook_slots": [x.strftime("%H:%M") for x in FACEBOOK_SLOTS.get(local.weekday(), ())],
        "allowed_now": window["allowed_now"],
        "reasons": window["reasons"],
        "next_allowed_time": window["next_allowed_time"],
        "min_interval_minutes": window["min_interval_minutes"],
        "adaptive_policy": window["policy"],
    }
