from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, time, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEMORY_DIR = DATA_DIR / "job_memory"
STATE_PATH = DATA_DIR / "job_state.json"

MOROCCO_TIMEZONE = os.getenv("JOBS_TIMEZONE", "Africa/Casablanca").strip() or "Africa/Casablanca"
MIN_SELECTION_SCORE = int(os.getenv("JOBS_MIN_SELECTION_SCORE", "70"))
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
    1: (time(9, 0), time(19, 0)),
    2: (time(9, 0), time(19, 0)),
    3: (time(9, 0), time(20, 0)),
    4: (time(9, 30), time(19, 30)),
    5: (time(10, 0),),
    6: (time(10, 0), time(19, 0)),
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


def is_job_specific_url(url):
    normalized = canonicalize_job_url(url)
    if not normalized:
        return False
    parsed = urlparse(normalized)
    segments = [x.casefold() for x in parsed.path.split("/") if x]
    if parsed.query:
        return True
    if not segments:
        return False
    last = segments[-1]
    if last in GENERIC_JOB_PATHS and len(segments) <= 2:
        return False
    if re.search(r"\d{3,}|[a-f0-9]{8,}", last):
        return True
    return len(segments) >= 2 and last not in GENERIC_JOB_PATHS


def external_reference(article):
    raw = article.get("raw") or {}
    for key in REFERENCE_KEYS:
        value = article.get(key)
        if value not in (None, ""):
            return str(value).strip()
        value = raw.get(key) if isinstance(raw, dict) else None
        if value not in (None, ""):
            return str(value).strip()
    return str(article.get("job_external_reference") or "").strip()


def _core_key(article):
    return "|".join([
        normalize_text(article.get("job_company") or article.get("company")),
        normalize_text(article.get("job_title") or article.get("fetched_title") or article.get("title")),
        normalize_text(article.get("job_location") or article.get("location")),
    ])


def identity_key(article):
    reference = external_reference(article)
    if reference:
        base = f"ref|{normalize_text(article.get('source_name'))}|{normalize_text(reference)}"
    else:
        apply_url = canonicalize_job_url(article.get("job_application_url") or article.get("application_url"))
        canonical = canonicalize_job_url(article.get("canonical_url") or article.get("url") or article.get("source_url"))
        if canonical and is_job_specific_url(canonical):
            base = f"url|{canonical}"
        elif apply_url and is_job_specific_url(apply_url):
            base = f"apply|{apply_url}"
        else:
            base = f"core|{_core_key(article)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def semantic_key(article):
    return hashlib.sha256(_core_key(article).encode("utf-8")).hexdigest()[:20]


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
    points = {}
    official = bool(article.get("official_source") or article.get("job_official_source"))
    points["official_source"] = 25 if official else 0

    published = _parse_date(article.get("job_published_at") or article.get("source_published_at"))
    fresh = bool(published and 0 <= (now - published).total_seconds() / 3600 <= 24)
    points["fresh_under_24h"] = 15 if fresh else 0

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
    valid_apply = _public_http(apply_url)
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
        reasons.append("missing application URL")

    deadline = _parse_date(article.get("job_deadline"))
    expired = bool(deadline and deadline < now)
    if expired:
        reasons.append("deadline passed")

    passed = score >= MIN_SELECTION_SCORE and not reasons
    status = "publish" if passed else ("queue" if score >= QUEUE_SCORE and not expired else "reject")
    return {
        "score": score,
        "status": status,
        "passed": passed,
        "points": points,
        "reasons": reasons,
        "threshold": MIN_SELECTION_SCORE,
        "queue_threshold": QUEUE_SCORE,
    }


def classify_urgency(article, now=None):
    now = now or datetime.now(timezone.utc)
    deadline = _parse_date(article.get("job_deadline"))
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


def _latinize(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


def desired_slug(article, campaign_id=""):
    company = _latinize(article.get("job_company") or article.get("company"))
    title = _latinize(article.get("job_title") or article.get("title"))
    base_words = [x for x in f"{company}-{title}".split("-") if x][:6]
    base = "-".join(base_words).strip("-") or "job"
    token_seed = campaign_id or identity_key(article)
    token = hashlib.sha256(token_seed.encode("utf-8")).hexdigest()[:7]
    return f"{base}-{token}"[:80]


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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _day_key(now=None):
    return _local(now).date().isoformat()


def daily_publish_cap(now=None):
    local = _local(now)
    month_max = MONTHLY_VOLUME_RANGE.get(local.month, (1, 2))[1]
    weekday_cap = WEEKDAY_BLOGGER_CAP.get(local.weekday(), month_max)
    return min(month_max, weekday_cap)


def can_publish_new_job(now=None):
    state = load_job_state()
    return int(state.get("daily_publish_count", {}).get(_day_key(now), 0)) < daily_publish_cap(now)


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
    pairs = {
        "number_of_positions": article.get("job_number_of_positions"),
        "deadline": article.get("job_deadline"),
        "salary": article.get("job_salary"),
        "contract_type": article.get("job_contract_type"),
        "location": article.get("job_location"),
        "application_url": canonicalize_job_url(article.get("job_application_url")),
    }
    for field, new_value in pairs.items():
        old_value = record.get(field)
        if field == "application_url":
            old_value = canonicalize_job_url(old_value)
        if str(new_value or "").strip() != str(old_value or "").strip():
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


def classify_identity(article):
    exact = identity_key(article)
    records = []
    exact_record = get_by_identity(exact)
    if exact_record:
        records.append(exact_record)
    records.extend(x for x in get_semantic_candidates(semantic_key(article)) if x not in records)
    if not records:
        return {"action": "new", "reason": "no existing campaign", "existing": {}}

    record = records[0]
    if _campaign_rollover(article, record):
        return {"action": "new_campaign", "reason": "campaign rollover", "existing": record}

    new_ref, old_ref = external_reference(article), str(record.get("external_reference") or "")
    if new_ref and old_ref and normalize_text(new_ref) == normalize_text(old_ref):
        changed = _material_change(article, record)
        return {"action": "update" if changed else "duplicate", "reason": "same external reference", "existing": record}

    new_apply = canonicalize_job_url(article.get("job_application_url"))
    old_apply = canonicalize_job_url(record.get("application_url"))
    if new_apply and old_apply and new_apply == old_apply:
        changed = _material_change(article, record)
        return {"action": "update" if changed else "duplicate", "reason": "same application URL", "existing": record}

    if semantic_key(article) != record.get("semantic_key"):
        return {"action": "new", "reason": "different company/title/location", "existing": {}}
    if new_ref and old_ref and normalize_text(new_ref) != normalize_text(old_ref):
        return {"action": "new_campaign", "reason": "different external reference", "existing": record}
    if new_apply and old_apply and not _same_url_family(new_apply, old_apply):
        return {"action": "new_campaign", "reason": "different application URL", "existing": record}

    new_posted = _parse_date(article.get("job_published_at") or article.get("source_published_at"))
    old_posted = _parse_date(record.get("published_at"))
    if new_posted and old_posted and abs((new_posted - old_posted).days) >= 30:
        return {"action": "new_campaign", "reason": "same role reposted at least 30 days later", "existing": record}

    return {"action": "hold", "reason": "ambiguous same role without strong identifier", "existing": record}


def _new_campaign_id():
    return hashlib.sha256(os.urandom(24)).hexdigest()[:20]


def prepare_job_candidate(article, now=None):
    quality = score_job(article, now=now)
    article["job_score"] = quality["score"]
    article["score"] = quality["score"]
    article["job_quality_status"] = quality["status"]
    article["job_quality_reasons"] = quality["reasons"]
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
        quality, decision = prepare_job_candidate(article, now=now)
        if decision["action"] in {"duplicate", "hold"}:
            continue
        if not quality["passed"]:
            continue

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
        ranked.append((quality["score"], priority, article.get("source_published_at") or "", article))

    ranked.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    return ranked[0][3] if ranked else None


def record_job_publish(article, now=None):
    action = str(article.get("job_identity_action") or "")
    if action not in {"new", "new_campaign", "update"}:
        return {}
    now = now or datetime.now(timezone.utc)
    campaign_id = article.get("job_campaign_id") or _new_campaign_id()
    ikey, skey = identity_key(article), semantic_key(article)
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
        "source_url": canonicalize_job_url(article.get("url") or article.get("source_url")),
        "blogger_post_id": article.get("blogger_post_id", ""),
        "blogger_url": article.get("blogger_post_url", ""),
        "desired_slug": article.get("desired_slug", ""),
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

    if action in {"new", "new_campaign"}:
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


def facebook_slot_status(posted_times=None, now=None, urgent=False, window_minutes=25):
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
                p.date() == local_now.date() and abs((p - target).total_seconds()) <= 45 * 60
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
    return {
        "local_time": local.isoformat(),
        "daily_cap": daily_publish_cap(now),
        "published_today": int(state.get("daily_publish_count", {}).get(day, 0)),
        "urgent_overrides_today": int(state.get("daily_urgent_override_count", {}).get(day, 0)),
        "monthly_range": MONTHLY_VOLUME_RANGE.get(local.month, (1, 2)),
        "facebook_slots": [x.strftime("%H:%M") for x in FACEBOOK_SLOTS.get(local.weekday(), ())],
    }
