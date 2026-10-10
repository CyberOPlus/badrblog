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
    JOBS_FACEBOOK_FOLLOW_ARTICLE,
    JOBS_MIN_PUBLISH_INTERVAL_MINUTES,
)
from jobs_adaptive_controller import current_policy
from state_io import atomic_write_json

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


# Technical and student roles receive only a light tie-break preference.
# No sector is excluded: freshness, eligibility, source diversity and verified facts win.
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
    """Optional sector tie-break, never a mandatory technical focus."""
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
    value = str(url or "").strip()
    if value.lower().startswith("mailto:"):
        address = value[7:].split("?", 1)[0].strip().lower()
        return bool(
            address
            and str(article.get("job_application_email") or "").strip().lower() == address
            and article.get("job_application_email_verified") is True
            and re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", address)
            and bool(article.get("official_source") or article.get("job_official_source"))
        )
    candidate = canonicalize_job_url(url)
    if not candidate or not _public_http(candidate):
        return False
    if is_job_specific_url(candidate):
        return not is_foreign_job_detail_url(article, candidate)
    return is_verified_official_application_channel(article, candidate)



# A verified vacancy-specific employer action may require an account later.
# Generic sign-in / register paths are never a valid landing page.
_DIRECT_APPLY_URL_RE = re.compile(
    r"(?i)(?:/(?:apply|postuler|candidater|candidature/submit|applications?/new|"
    r"applicationform)(?:/|[.?]|$)|[?&](?:apply|postuler|lJobID|jobId)=)"
)
_ACCOUNT_GATE_URL_RE = re.compile(
    r"(?i)/(?:login|sign[-_]?in|sign[-_]?up|register|registration|"
    r"inscription|connexion|auth(?:entication)?|create[-_]?account|"
    r"mon[-_]?compte|candidate[-_]?account)(?:/|[.?]|$)"
)
_ACCOUNT_ONLY_HOSTS = (
    "moncallcenter.ma",
    "rekrute.com",
)


def _retired_job_source_names():
    """Source removal must also prevent legacy ready jobs publishing."""
    try:
        payload = json.loads((BASE_DIR / "sources.json").read_text(encoding="utf-8"))
        return {
            str(name).strip().casefold()
            for name in (payload.get("policy", {}).get("retired_source_names") or [])
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return set()


def job_direct_application_policy(article):
    """Reject listings, login routes, ambiguous action links and other jobs."""
    source_name = str(article.get("source_name") or "").strip().casefold()
    if source_name and source_name in _retired_job_source_names():
        return "retired source is not approved for publishing"
    url = str(article.get("job_application_url") or article.get("application_url") or "").strip()
    # A mailto application is allowed only when the specific official notice or
    # PDF explicitly instructs applicants to send candidatures to that address.
    if str(url or "").lower().startswith("mailto:"):
        address = str(url)[7:].split("?", 1)[0].strip().lower()
        verified = str(article.get("job_application_email") or "").strip().lower()
        if (
            verified == address
            and bool(article.get("job_application_email_verified"))
            and re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", address)
            and bool(article.get("official_source") or article.get("job_official_source"))
        ):
            return ""
        return "email application is not verified from this exact official notice"
    if not url or not _public_http(url):
        return "missing direct job-specific application URL"
    candidate = canonicalize_job_url(url)
    parsed = urlparse(candidate)
    host = parsed.netloc.casefold().removeprefix("www.")
    account_platform = (
        host in _ACCOUNT_ONLY_HOSTS
        or any(host.endswith("." + h) for h in _ACCOUNT_ONLY_HOSTS)
        or article.get("job_application_requires_registration") is True
    )
    if _ACCOUNT_GATE_URL_RE.search(parsed.path) or re.search(
        r"(?i)(?:^|[?&])(?:redirect|returnUrl|next)=[^&]*(?:login|register|signup)", parsed.query
    ):
        return "application URL leads to generic registration or sign-in"
    kind = str(article.get("job_application_link_kind") or "").strip().lower()
    # An official PDF may explicitly designate one centralized portal for THIS
    # competition (e.g. recrutement.enssup.gov.ma). It is the official action,
    # even when the portal address does not contain an individual vacancy ID.
    # Require the linked action to be on the specific notice and preserve the
    # distinction between a direct form and an official application platform.
    if kind == "official_application_channel":
        if is_verified_official_application_channel(article, url):
            if str(article.get("job_application_source") or "").strip().lower() == "official_pdf":
                exact = str(url).rstrip("/").casefold()
                pdf_urls = " ".join(
                    str(p.get("text") or "").casefold()
                    for p in (article.get("job_document_texts") or [])
                    if isinstance(p, dict)
                )
                if exact not in pdf_urls:
                    return "official PDF does not prove this application portal"
            return ""
        return "generic application portal is not verified in this competition"
    if kind in {"official_job_page", "listing", "generic"}:
        return "application URL is not the direct form/action"
    if not is_job_specific_url(url) or not is_application_url_bound_to_job(article, url):
        return "application URL is a generic portal or belongs to another vacancy"
    detail = canonicalize_job_url(
        article.get("job_detail_url") or article.get("canonical_url") or article.get("url")
    )
    if detail and detail == candidate and not _DIRECT_APPLY_URL_RE.search(url):
        return "application URL points to the job description instead of the apply action"
    explicit_apply = _application_action_exposes_url(article, url)
    direct_kind = kind == "direct_apply"
    direct_url = bool(_DIRECT_APPLY_URL_RE.search(url))
    if not (explicit_apply or direct_kind or direct_url):
        return "a verified job-specific apply action is missing"
    if account_platform and not _application_action_exposes_url(article, url):
        return "account-required platform needs a verified apply action on this vacancy"
    # A candidate may need an account later. The article should disclose that.
    return ""


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


OPPORTUNITY_FRESHNESS_HOURS = {
    "job": JOBS_MAX_PUBLISH_AGE_HOURS,
    "internship": 168,
    "scholarship": 720,
    "training": 720,
    "apprenticeship": 168,
}


def job_opportunity_kind(article):
    """Classify editorial scope only; type alone proves no eligibility."""
    raw = str(
        (article or {}).get("opportunity_kind")
        or (article or {}).get("job_opportunity_kind")
        or "job"
    ).strip().casefold()
    aliases = {
        "jobs": "job", "vacancy": "job", "competition": "job",
        "internships": "internship", "stage": "internship",
        "scholarships": "scholarship", "bourse": "scholarship",
        "vocational_training": "training", "formation": "training",
        "apprenticeships": "apprenticeship",
    }
    value = aliases.get(raw, raw)
    return value if value in OPPORTUNITY_FRESHNESS_HOURS else "job"


def job_freshness_limit_hours(article):
    return OPPORTUNITY_FRESHNESS_HOURS[job_opportunity_kind(article)]


def verified_rolling_application(article):
    """Do not interpret a missing deadline as rolling without official proof."""
    return (
        job_opportunity_kind(article) in {"internship", "training", "apprenticeship"}
        and bool((article or {}).get("official_source") or (article or {}).get("job_official_source"))
        and (article or {}).get("job_application_rolling_verified") is True
        and bool(str((article or {}).get("job_application_rolling_evidence") or "").strip())
    )


def job_publication_freshness(article, now=None, max_age_hours=None):
    """
    Validate publication freshness without inventing a posting time.

    Official sources sometimes expose only a calendar date (YYYY-MM-DD).
    For those records, the exact hour is unknowable: the official date is fresh
    only while that same date is still current in the Jobs timezone. Real
    timestamps keep the strict hour-based window.
    """
    raw = str(
        (article or {}).get("job_published_at")
        or (article or {}).get("source_published_at")
        or ""
    ).strip()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    try:
        max_hours = float(
            job_freshness_limit_hours(article)
            if max_age_hours is None
            else max_age_hours
        )
    except (TypeError, ValueError):
        max_hours = float(job_freshness_limit_hours(article))

    if not raw:
        return {
            "verified": False,
            "fresh": False,
            "future": False,
            "date_only": False,
            "age_hours": None,
            "raw": raw,
        }

    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw))
    official = bool(
        (article or {}).get("official_source")
        or (article or {}).get("job_official_source")
    )
    if date_only and official:
        try:
            publication_day = datetime.fromisoformat(raw).date()
        except ValueError:
            publication_day = None
        if publication_day is not None:
            current_day = current.astimezone(
                ZoneInfo(MOROCCO_TIMEZONE)
            ).date()
            if publication_day > current_day:
                return {
                    "verified": True,
                    "fresh": False,
                    "future": True,
                    "date_only": True,
                    "age_hours": None,
                    "raw": raw,
                }
            return {
                "verified": True,
                "fresh": 0 <= (current_day - publication_day).days * 24 < max_hours,
                "future": False,
                "date_only": True,
                "age_hours": None,
                "raw": raw,
            }

    published = _parse_date(raw)
    if published is None:
        return {
            "verified": False,
            "fresh": False,
            "future": False,
            "date_only": date_only,
            "age_hours": None,
            "raw": raw,
        }
    age_hours = (current - published).total_seconds() / 3600
    return {
        "verified": True,
        "fresh": 0 <= age_hours <= max_hours,
        "future": age_hours < 0,
        "date_only": date_only,
        "age_hours": age_hours,
        "raw": raw,
    }


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



# Publication policy: require an open deadline and verified qualification of
# Bac+3 / Licence / Bachelor (or equivalent) or lower. Higher-only posts fail.
_EDUCATION_LABEL = re.compile(
    r"(?:dipl[oô]me|niveau\s+d['’]?[eé]tudes|niveau\s+scolaire|"
    r"formation\s+(?:requise|demand[eé]e)|profil\s+(?:recherch[eé]|demand[eé])|"
    r"qualification|education\s+(?:required|level)|"
    r"(?:ال)?شهادة|(?:ال)?مؤهل|المستوى\s+الدراسي|المستوى\s+التعليمي|(?:ال)?دبلوم|"
    r"شروط\s+(?:الترشح|التوظيف)|الاجازة|الإجازة)",
    re.IGNORECASE,
)
_EDUCATION_HIGH = re.compile(
    r"\bbac\s*\+\s*(?:[4-9]|[1-9]\d+)\b|"
    r"\b(?:master|mastere|master's|m[12]\s+degree|ingenieur|"
    r"doctorat|doctorate|phd|mba|bac\s*plus\s*(?:quatre|cinq))\b|"
    r"(?:(?:ال)?ماستر|الماجستير|الدكتوراه|مهندس\s+دولة|باك\s*\+\s*[٤٥٦٧٨٩])",
    re.IGNORECASE,
)
_EDUCATION_BACHELOR = re.compile(
    r"\bbac\s*\+\s*3\b|\bbac\s*plus\s*trois\b|"
    r"\b(?:licence(?!\s+de\s+conduire\b)|bachelor(?:'s)?|"
    r"licenciatura|licence\s+professionnelle)\b|"
    r"(?:الاجازة|الإجازة|باك\s*\+\s*٣)",
    re.IGNORECASE,
)
_EDUCATION_LOW = re.compile(
    r"\bbac\s*\+\s*[012]\b|\bbac\b(?!\s*\+)|"
    r"\b(?:baccalaureat|bts|dut|deug|deust|dts|cap|bep|"
    r"technicien\s+specialise|sans\s+diplome|niveau\s+secondaire)\b|"
    r"(?:بكالوريا|البكالوريا|مستوى\s+(?:باك|الباك)|تقني\s+متخصص|"
    r"الثانوي|التأهيل\s+المهني|التاهيل\s+المهني|بدون\s+شهادة|دون\s+شهادة|"
    r"باك\s*\+\s*[٠١٢])",
    re.IGNORECASE,
)


def _education_plain(text):
    value = unicodedata.normalize("NFKD", str(text or "")).casefold()
    return "".join(c for c in value if not unicodedata.combining(c))


def job_qualification_evidence(article):
    """Conservative source-grounded diploma evidence. Never infer from job title."""
    direct = [
        article.get(field)
        for field in (
            "job_diploma", "job_required_diploma", "job_education",
            "job_required_education", "job_qualification",
        )
        if str(article.get(field) or "").strip()
    ]
    evidence = [str(value).strip()[:350] for value in direct]
    # Only inspected job detail paragraphs with education labels; a generic
    # Bac+2 mention in unrelated duties or a listing cannot establish eligibility.
    for field in ("full_article_text", "job_description"):
        body = str(article.get(field) or "")
        for line in re.split(r"[\n\r]+|(?<=[.!?])\s+", body):
            if not _EDUCATION_LABEL.search(line):
                continue
            match = _EDUCATION_LABEL.search(line)
            snippet = line[max(0, match.start() - 35):match.start() + 320]
            if snippet and snippet not in evidence:
                evidence.append(snippet)
            if len(evidence) >= 15:
                break
    # Also inspect text from the specific official competition PDF. Mixed
    # qualifications are rejected by job_qualification_policy, not cherry-picked.
    for page in (article.get("job_document_texts") or []):
        if not isinstance(page, dict):
            continue
        for line in str(page.get("text") or "").splitlines():
            if not _EDUCATION_LABEL.search(line):
                continue
            snippet = line.strip()[:350]
            if snippet and snippet not in evidence:
                evidence.append(snippet)
    return evidence


def job_qualification_policy(article):
    """Return (state, evidence); require proven Bachelor/Licence or lower."""
    evidence = job_qualification_evidence(article)
    normalized = [_education_plain(value) for value in evidence]
    # An explicit higher-than-Bac+3 requirement blocks mixed multi-position
    # calls too; do not falsely advertise a Bac+5-only position to a Bac+2 reader.
    for value, original in zip(normalized, evidence):
        if _EDUCATION_HIGH.search(value):
            return "above_limit", original
    for value, original in zip(normalized, evidence):
        if _EDUCATION_BACHELOR.search(value):
            return "bac3", original
        if _EDUCATION_LOW.search(value):
            return "below_bac3", original
    return "unverified", ""


def job_publication_policy(article, now=None):
    """Independent fail-closed editorial gates for selection and Blogger writes."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    reasons = []
    raw_deadline = str(article.get("job_deadline") or "").strip()
    deadline = job_deadline_time(article) if raw_deadline else None
    if deadline is None:
        if not verified_rolling_application(article):
            reasons.append("application closing deadline is missing or invalid")
    elif deadline <= current:
        reasons.append("application registration deadline passed")
    education, evidence = job_qualification_policy(article)
    if education == "above_limit":
        reasons.append("required diploma is above Bac+3")
    elif education not in {"below_bac3", "bac3"}:
        reasons.append("required diploma Bac+3 or below is not verified")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "deadline": raw_deadline if deadline else "",
        "education": education,
        "education_evidence": evidence,
        "permanent_reject": bool(
            (deadline is not None and deadline <= current)
            or education == "above_limit"
        ),
    }


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

    freshness = job_publication_freshness(article, now=now)
    publication_age_hours = freshness["age_hours"]
    fresh = bool(freshness["fresh"])
    maximum_hours = job_freshness_limit_hours(article)
    points[f"fresh_under_{maximum_hours}h"] = 15 if fresh else 0
    # Exact timestamps <=12h are preferred, while verified jobs remain
    # publishable up to the 24h safety ceiling. Date-only records never invent
    # an hour just to gain this priority bonus.
    points["fresh_under_12h_priority"] = (
        10
        if fresh and publication_age_hours is not None and publication_age_hours <= 12
        else 0
    )
    points["preferred_tech_or_student"] = 3 if job_focus_priority(article) > 0 else 0

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

    apply_url = article.get("job_application_url") or article.get("application_url") or ""
    valid_apply = bool(is_application_url_bound_to_job(article, apply_url))
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

    if not freshness["verified"]:
        reasons.append("publication time is not verified")
    elif freshness["future"]:
        reasons.append("publication time is in the future")
    elif not freshness["fresh"]:
        reasons.append(f"opportunity is older than {maximum_hours} hours")

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
    editorial_policy = job_publication_policy(article, now=now)
    reasons.extend(editorial_policy["reasons"])
    direct_apply_reason = job_direct_application_policy(article)
    if direct_apply_reason:
        reasons.append(direct_apply_reason)

    # Ranking score is intentionally NOT a publication gate. A legitimate,
    # verified vacancy can score low simply because salary, diploma, location,
    # recency or large-hiring signals are absent. Those signals only decide
    # priority between otherwise publishable jobs.
    hard_gate_passed = not reasons
    permanent_hard_failure = bool(
        expired
        or editorial_policy["permanent_reject"]
        or (freshness["verified"] and (freshness["future"] or not freshness["fresh"]))
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
        "publication_date_only": bool(freshness["date_only"]),
        "max_publish_age_hours": maximum_hours,
        "focus_priority": job_focus_priority(article),
        "publication_policy": editorial_policy,
        "direct_application_policy_reason": direct_apply_reason,
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


def desired_slug(article, campaign_id=""):
    """Return a stable readable permalink stem without opaque hash suffixes.

    Campaign identity/deduplication is handled separately. The public slug keeps
    only employer + role words, so changing deadline, seat count, reference IDs,
    or internal campaign IDs never creates a random-looking filename.
    """
    company = _latinize(article.get("job_company") or article.get("company"))
    title = _latinize(article.get("job_title") or article.get("title"))
    base_words = [x for x in f"{company}-{title}".split("-") if x][:8]
    return ("-".join(base_words).strip("-") or "job")[:90].strip("-")


def _state_default():
    return {
        "daily_publish_count": {},
        "daily_urgent_override_count": {},
        "last_publish_at": "",
        "source_last_publish_at": {},
    }


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

    # Source fairness only needs recent publication history. Keep this bounded
    # so a long-running bot never grows state forever when sources are renamed
    # or retired.
    source_rows = state.get("source_last_publish_at")
    if isinstance(source_rows, dict) and len(source_rows) > 200:
        ordered = sorted(
            source_rows.items(),
            key=lambda item: str(item[1] or ""),
            reverse=True,
        )
        state["source_last_publish_at"] = dict(ordered[:200])

    atomic_write_json(STATE_PATH, state)


def _day_key(now=None):
    return _local(now).date().isoformat()


def daily_publish_cap(now=None):
    if JOBS_ADAPTIVE_PUBLISHING:
        local = _local(now)
        cap = int(current_policy(now=now).get("daily_cap") or 8)
        # Midday deployment migration: posts already made under the earlier
        # daily ceiling must not deadlock the first pilot day. The limited
        # one-day grace disappears automatically tomorrow without intervention.
        grace_day = os.getenv("JOBS_PILOT_ROLLOUT_GRACE_DAY", "").strip()
        if grace_day and local.date().isoformat() == grace_day:
            try:
                grace_limit = int(os.getenv("JOBS_PILOT_ROLLOUT_GRACE_CAP", "0"))
            except ValueError:
                grace_limit = 0
            cap = max(cap, min(12, max(0, grace_limit)))
        # Quiet weekends get a lower soft editorial ceiling. It is still
        # a maximum, not a publication goal or permission to skip other gates.
        if local.weekday() >= 5:
            return min(cap, 8)
        return cap
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
    # JOBS_MIN_PUBLISH_INTERVAL_MINUTES is the anti-spam spacing contract.
    # Do not silently stretch a configured 5-minute minimum to 7/10 minutes
    # when backlog is small: that made fresh Jobs wait for no safety benefit.
    # Backlog still affects ranking/capacity elsewhere, not this minimum.
    _ = publishable_backlog
    return max(1, int(JOBS_MIN_PUBLISH_INTERVAL_MINUTES or 1))


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
    atomic_write_json(path, data)


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
    if "publication time is not verified" not in quality["reasons"]:
        article.pop("publication_evidence_refresh_pending", None)
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


def _source_rotation_key(article):
    """Rotate by actual originating website, not its different category names."""
    article = article or {}
    source_url = str(article.get("source_url") or "").strip()
    try:
        host = (urlparse(source_url).hostname or "").casefold().removeprefix("www.")
    except Exception:
        host = ""
    if host:
        return host
    return re.sub(r"\s+", " ", str(article.get("source_name") or "").strip().casefold())


def _source_publish_history_from_memory(limit=500):
    """Bootstrap source fairness from durable campaign memory after upgrades."""
    history = {}
    try:
        records = list_active_job_campaign_records(limit=limit)
    except Exception:
        return history

    for record in records:
        key = _source_rotation_key(record)
        if not key:
            continue
        anchor = _parse_date(record.get("updated_at") or record.get("published_at"))
        if not anchor:
            continue
        current = _parse_date(history.get(key))
        if current is None or anchor > current:
            history[key] = anchor.isoformat()
    return history


def _source_publish_history():
    state = load_job_state()
    rows = state.get("source_last_publish_at")
    history = dict(rows) if isinstance(rows, dict) else {}
    return history or _source_publish_history_from_memory()


def _source_last_publish_epoch(article, history):
    key = _source_rotation_key(article)
    if not key:
        return 0.0
    # Recognize legacy history persisted under display names during migration.
    legacy_key = re.sub(r"\s+", " ", str((article or {}).get("source_name") or "").strip().casefold())
    dates = [_parse_date((history or {}).get(source_key)) for source_key in {key, legacy_key} if source_key]
    timestamps = [value.timestamp() for value in dates if value]
    return max(timestamps, default=0.0)


def _defer_quality_candidate(article, quality, now=None):
    """Back off incomplete evidence without turning it into a permanent skip."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    reasons = sorted({
        str(reason or "").strip()
        for reason in (quality or {}).get("reasons", [])
        if str(reason or "").strip()
    })
    reason_text = "; ".join(reasons) or "verified quality evidence incomplete"
    if "publication time is not verified" in reasons:
        article["publication_evidence_refresh_pending"] = True
    fingerprint = hashlib.sha256(reason_text.encode("utf-8")).hexdigest()[:16]

    previous_fingerprint = str(article.get("job_quality_wait_fingerprint") or "")
    previous_count = int(article.get("job_quality_wait_count") or 0)
    count = previous_count + 1 if previous_fingerprint == fingerprint else 1
    backoff_minutes = min(120, 10 * (2 ** min(count - 1, 4)))
    retry_after = now.astimezone(timezone.utc) + timedelta(minutes=backoff_minutes)

    article["job_quality_wait_fingerprint"] = fingerprint
    article["job_quality_wait_count"] = count
    article["candidate_failure_stage"] = "quality-evidence"
    article["candidate_failure_reason"] = reason_text[:500]
    article["candidate_failure_fingerprint"] = fingerprint
    article["candidate_failure_repeat_count"] = count
    article["candidate_failure_backoff_minutes"] = backoff_minutes
    article["candidate_failed_at"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    article["candidate_retry_after"] = retry_after.isoformat(timespec="seconds")
    return retry_after


def select_best_job_from_queue(queue, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    source_history = _source_publish_history()
    ranked = []
    for article in queue.get("articles", []):
        if article.get("archived") or article.get("status") not in {"ready", "selected"}:
            continue
        if article.get("content_fetch_status") != "success":
            continue
        retry_deadlines = [
            _parse_date(article.get(field))
            for field in ("ai_retry_after", "candidate_retry_after", "enrichment_retry_after")
        ]
        if any(retry_after and retry_after > now for retry_after in retry_deadlines):
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
            else:
                # Missing eligibility/application evidence is temporary. Do not
                # let the same unverifiable row consume every minute-long cycle.
                _defer_quality_candidate(article, quality, now=now)
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

        # Urgent/closing-soon notices remain first. Otherwise rotate fairly
        # across sources: a source that has never published (or published least
        # recently) gets the next turn, while focus/freshness/quality still rank
        # jobs inside that fair source choice. Newer general jobs outrank
        # older technical jobs from the same source.
        published = _parse_date(
            article.get("job_published_at")
            or article.get("source_published_at")
        )
        discovered = _parse_date(article.get("discovered_at"))
        published_epoch = published.timestamp() if published else 0.0
        discovered_epoch = discovered.timestamp() if discovered else 0.0
        focus_priority = job_focus_priority(article)
        source_last_epoch = _source_last_publish_epoch(article, source_history)
        source_fairness = -source_last_epoch
        ranked.append(
            (
                priority,
                source_fairness,
                published_epoch,
                focus_priority,
                discovered_epoch,
                quality["score"],
                article,
            )
        )

    ranked.sort(
        key=lambda row: (row[0], row[1], row[2], row[3], row[4], row[5]),
        reverse=True,
    )
    return ranked[0][6] if ranked else None


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
        "company_logo_url": (
            article.get("company_logo_url")
            if article.get("company_logo_verified") and article.get("company_logo_url")
            else previous.get("company_logo_url", "")
        ),
        "company_logo_verified": bool(
            (
                article.get("company_logo_verified")
                and article.get("company_logo_url")
            )
            or previous.get("company_logo_verified")
        ),
        "company_logo_confidence": int(
            article.get("company_logo_confidence")
            if article.get("company_logo_verified") and article.get("company_logo_url")
            else previous.get("company_logo_confidence") or 0
        ),
        "company_logo_source": (
            article.get("company_logo_source")
            if article.get("company_logo_verified") and article.get("company_logo_url")
            else previous.get("company_logo_source", "")
        ),
        "company_official_domain": (
            article.get("company_official_domain")
            if article.get("company_logo_verified") and article.get("company_logo_url")
            else previous.get("company_official_domain", "")
        ),
        "company_logo_checksum": (
            article.get("company_logo_checksum")
            if article.get("company_logo_verified") and article.get("company_logo_url")
            else previous.get("company_logo_checksum", "")
        ),
        "location": article.get("job_location", ""),
        "published_at": article.get("job_published_at") or article.get("source_published_at", ""),
        "deadline": article.get("job_deadline", ""),
        "number_of_positions": article.get("job_number_of_positions", 0),
        "salary": article.get("job_salary", ""),
        "contract_type": article.get("job_contract_type", ""),
        "application_url": canonicalize_job_url(article.get("job_application_url")),
        "application_link_kind": str(article.get("job_application_link_kind") or ""),
        "application_is_specific": bool(article.get("job_application_is_specific")),
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

        source_history = state.setdefault("source_last_publish_at", {})
        if not source_history:
            source_history.update(_source_publish_history_from_memory())
        source_key = _source_rotation_key(article)
        if source_key:
            source_history[source_key] = now.astimezone(timezone.utc).isoformat(timespec="seconds")

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
    # Preserve the verified application semantics needed by the Facebook
    # template policy across queue loss/recovery. Never replace good campaign
    # facts with blank volatile values.
    semantic_updates = {
        "application_url": canonicalize_job_url(article.get("job_application_url")),
        "application_link_kind": str(article.get("job_application_link_kind") or "").strip(),
        "notice_type": str(article.get("job_notice_type") or "").strip(),
        "notice_status": str(article.get("job_notice_status") or "").strip(),
    }
    for key, value in semantic_updates.items():
        if value not in (None, "", [], {}):
            record[key] = value
    if article.get("job_application_is_specific") is not None:
        record["application_is_specific"] = bool(article.get("job_application_is_specific"))

    if article.get("company_logo_verified") and str(article.get("company_logo_url") or "").strip():
        record.update({
            "company_logo_url": str(article.get("company_logo_url") or "").strip(),
            "company_logo_verified": True,
            "company_logo_confidence": int(article.get("company_logo_confidence") or 0),
            "company_logo_source": str(article.get("company_logo_source") or ""),
            "company_official_domain": str(article.get("company_official_domain") or ""),
            "company_logo_checksum": str(article.get("company_logo_checksum") or ""),
        })

    record.update({
        "facebook_status": str(article.get("facebook_status") or ""),
        "facebook_post_id": str(article.get("facebook_post_id") or ""),
        "facebook_posted_at": str(article.get("facebook_posted_at") or ""),
        "facebook_comment_id": str(article.get("facebook_comment_id") or ""),
        "facebook_queued_at": str(article.get("facebook_queued_at") or ""),
        "facebook_retry_after_epoch": article.get("facebook_retry_after_epoch") or 0,
        "facebook_ai_deferred_at": str(article.get("facebook_ai_deferred_at") or ""),
        "facebook_ai_deferred_reason": str(article.get("facebook_ai_deferred_reason") or "")[:500],
        "facebook_comment_retry_after_epoch": article.get("facebook_comment_retry_after_epoch") or 0,
        "facebook_delivery_uncertain_at": str(article.get("facebook_delivery_uncertain_at") or ""),
        "facebook_attempt_caption": str(article.get("facebook_attempt_caption") or ""),
        "facebook_attempt_fingerprint": str(article.get("facebook_attempt_fingerprint") or ""),
        "facebook_attempt_started_at": str(article.get("facebook_attempt_started_at") or ""),
        "facebook_delivery_reconcile_checks": int(article.get("facebook_delivery_reconcile_checks") or 0),
        "facebook_delivery_reconcile_last_checked_at": str(article.get("facebook_delivery_reconcile_last_checked_at") or ""),
        "facebook_comment_uncertain_at": str(article.get("facebook_comment_uncertain_at") or ""),
        "facebook_comment_reconcile_checks": int(article.get("facebook_comment_reconcile_checks") or 0),
        "facebook_comment_reconcile_last_checked_at": str(article.get("facebook_comment_reconcile_last_checked_at") or ""),
        "facebook_image_status": str(article.get("facebook_image_status") or ""),
        "facebook_error": str(article.get("facebook_error") or "")[:300],
        "updated_at": now.isoformat(),
    })
    _save_json(path, record)
    return record


# Pilot windows spread Facebook posts across Morocco audience's morning, noon
# and evening sessions. Slots are NOT required for Blogger: vacancies must be
# published when verified, without waiting for a social-engagement hour.
JOBS_WEEKLY_FACEBOOK_SCHEDULE_ENABLED = (
    os.getenv("JOBS_WEEKLY_FACEBOOK_SCHEDULE_ENABLED", "false").strip().casefold()
    in {"1", "true", "yes", "on"}
)
WEEKLY_FACEBOOK_SLOTS = {
    0: ((9, 0), (12, 30), (19, 0)),                  # Monday
    1: ((9, 0), (13, 0), (18, 30), (20, 30)),       # Tuesday
    2: ((9, 0), (13, 0), (18, 30), (20, 30)),       # Wednesday
    3: ((9, 0), (13, 0), (18, 30), (20, 30)),       # Thursday
    4: ((9, 30), (16, 0), (19, 0)),                 # Friday
    5: ((10, 0), (18, 0)),                           # Saturday
    6: ((9, 30), (18, 0)),                           # Sunday
}


def facebook_slot_status(posted_times=None, now=None, urgent=False, window_minutes=50):
    local_now = _local(now)
    if not JOBS_WEEKLY_FACEBOOK_SCHEDULE_ENABLED:
        return {"allowed_now": True, "mode": "immediate", "slot": "", "next_slot": local_now.isoformat()}
    if urgent:
        # Closing-soon competitions cannot wait for an engagement experiment.
        # Hard daily cap and the safety interval remain enforced separately.
        return {"allowed_now": True, "mode": "urgent", "slot": "deadline-priority", "next_slot": local_now.isoformat()}
    span = max(10, min(60, int(window_minutes or 50)))
    slots = WEEKLY_FACEBOOK_SLOTS.get(local_now.weekday(), ())
    for hour, minute in slots:
        start = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start <= local_now < start + timedelta(minutes=span):
            return {
                "allowed_now": True,
                "mode": "weekly",
                "slot": f"{hour:02d}:{minute:02d}",
                "next_slot": start.isoformat(),
            }
    for offset in range(8):
        day = local_now.date() + timedelta(days=offset)
        day_slots = WEEKLY_FACEBOOK_SLOTS.get(day.weekday(), ())
        for hour, minute in day_slots:
            candidate = datetime.combine(day, time(hour, minute), tzinfo=local_now.tzinfo)
            if candidate > local_now:
                return {
                    "allowed_now": False,
                    "mode": "weekly",
                    "slot": "",
                    "next_slot": candidate.isoformat(),
                }
    return {"allowed_now": False, "mode": "weekly", "slot": "", "next_slot": ""}


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
        "facebook_policy": "follow_article",
        "allowed_now": window["allowed_now"],
        "reasons": window["reasons"],
        "next_allowed_time": window["next_allowed_time"],
        "min_interval_minutes": window["min_interval_minutes"],
        "adaptive_policy": window["policy"],
    }
