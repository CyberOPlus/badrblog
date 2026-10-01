from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import HEADERS, JOBS_MEMORY_DIR
from production_logging import log_event


REGISTRY_PATH = JOBS_MEMORY_DIR / "company_logo_registry.json"
MIN_LOGO_CONFIDENCE = 90
REGISTRY_TTL_DAYS = 30
REQUEST_TIMEOUT_SECONDS = 8
MAX_IMAGE_BYTES = 3_000_000
MAX_CANDIDATES_TO_PROBE = 6

INDIRECT_HOST_HINTS = (
    "emploi-public.ma",
    "linkedin.com",
    "indeed.",
    "glassdoor.",
    "rekrute.",
    "emploi.ma",
    "anapec.",
    "myworkdayjobs.com",
    "workdayjobs.com",
    "workday.com",
    "icims.com",
    "greenhouse.io",
    "lever.co",
    "smartrecruiters.com",
    "teamtailor.com",
    "recruitee.com",
    "successfactors.",
    "oraclecloud.com",
)

SOCIAL_HOST_HINTS = (
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "tiktok.com",
)

BLOCKED_IMAGE_HINTS = (
    "avatar",
    "profile",
    "author",
    "banner",
    "advert",
    "tracking",
    "pixel",
    "sprite",
    "social",
    "share",
)

OFFICIAL_SITE_LABELS = (
    "site web",
    "website",
    "site officiel",
    "official site",
    "الموقع الإلكتروني",
    "الموقع الالكتروني",
    "الموقع الرسمي",
)

NAME_STOPWORDS = {
    "sa", "sarl", "sas", "inc", "llc", "ltd", "plc", "company", "groupe", "group",
    "societe", "société", "ministere", "ministère", "وزارة", "de", "du", "des", "la",
    "le", "les", "of", "the", "and", "et", "pour", "department", "département",
}


def _utc_now():
    return datetime.now(timezone.utc)


def _normalize_name(value):
    value = str(value or "").casefold()
    value = re.sub(r"[^\w\u0600-\u06ff]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _name_tokens(value):
    return {
        token
        for token in _normalize_name(value).split()
        if len(token) > 1 and token not in NAME_STOPWORDS
    }


def _name_similarity(company, text):
    company_norm = _normalize_name(company)
    text_norm = _normalize_name(text)
    if not company_norm or not text_norm:
        return 0.0
    if company_norm in text_norm or text_norm in company_norm:
        return 1.0
    company_tokens = _name_tokens(company)
    text_tokens = _name_tokens(text)
    if not company_tokens or not text_tokens:
        return 0.0
    overlap = len(company_tokens & text_tokens)
    return overlap / max(1, min(len(company_tokens), len(text_tokens)))


def _host(url):
    try:
        return (urlparse(str(url or "")).hostname or "").casefold().lstrip("www.")
    except Exception:
        return ""


def _is_private_literal_host(host):
    if not host:
        return True
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)


def _safe_public_url(url, base_url=""):
    absolute = urljoin(base_url, str(url or "").strip())
    try:
        parsed = urlparse(absolute)
    except Exception:
        return ""
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host or _is_private_literal_host(host):
        return ""
    if parsed.username or parsed.password:
        return ""
    return absolute


def _same_or_subdomain(host, domain):
    host = str(host or "").casefold().lstrip("www.")
    domain = str(domain or "").casefold().lstrip("www.")
    return bool(host and domain and (host == domain or host.endswith("." + domain)))


def _indirect_host(host):
    host = str(host or "").casefold()
    return any(hint in host for hint in INDIRECT_HOST_HINTS)


def _social_host(host):
    host = str(host or "").casefold()
    return any(hint in host for hint in SOCIAL_HOST_HINTS)


def _eligible_official_url(url):
    url = _safe_public_url(url)
    if not url:
        return ""
    host = _host(url)
    if _social_host(host) or _indirect_host(host):
        return ""
    if urlparse(url).path.casefold().endswith((".pdf", ".doc", ".docx", ".zip")):
        return ""
    return url


def _iter_jsonld(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop(0)
            if not isinstance(node, dict):
                continue
            yield node
            graph = node.get("@graph")
            if isinstance(graph, list):
                stack.extend(x for x in graph if isinstance(x, dict))


def _types(node):
    value = node.get("@type") if isinstance(node, dict) else None
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, list):
        return {str(x).casefold() for x in value}
    return set()


def _job_organization(soup):
    for node in _iter_jsonld(soup):
        if "jobposting" not in _types(node):
            continue
        org = node.get("hiringOrganization") or {}
        if isinstance(org, list):
            org = org[0] if org else {}
        if isinstance(org, dict):
            return org
    return {}


def _logo_value(value, base_url=""):
    if isinstance(value, str):
        return _safe_public_url(value, base_url)
    if isinstance(value, dict):
        return _safe_public_url(
            value.get("url") or value.get("contentUrl") or value.get("@id"),
            base_url,
        )
    return ""


def _organization_urls(org, base_url=""):
    rows = []
    for key in ("url", "@id", "sameAs"):
        value = org.get(key) if isinstance(org, dict) else None
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                item = item.get("url") or item.get("@id")
            url = _eligible_official_url(_safe_public_url(item, base_url))
            if url and url not in rows:
                rows.append(url)
    return rows


def _image_src(img, base_url):
    for attr in ("src", "data-src", "data-lazy-src", "data-original"):
        value = img.get(attr)
        if value:
            return _safe_public_url(value, base_url)
    return ""


def _context_text(node):
    text = " ".join(
        str(value or "")
        for value in (
            node.get("alt"),
            node.get("title"),
            node.get("aria-label"),
            node.get("id"),
            " ".join(node.get("class") or []),
        )
    )
    parent = node.parent
    if parent is not None:
        try:
            text += " " + parent.get_text(" ", strip=True)[:240]
        except Exception:
            pass
    return re.sub(r"\s+", " ", text).strip()


def _candidate(candidates, url, score, source, official_domain="", evidence=""):
    url = _safe_public_url(url)
    if not url:
        return
    signature = url.casefold()
    if any(hint in signature for hint in BLOCKED_IMAGE_HINTS) and "logo" not in signature:
        return
    row = {
        "url": url,
        "score": int(score),
        "source": source,
        "official_domain": str(official_domain or ""),
        "evidence": str(evidence or "")[:240],
    }
    current = candidates.get(url)
    if not current or row["score"] > current["score"]:
        candidates[url] = row


def _official_site_from_page(soup, company, page_url, article):
    page_host = _host(page_url)
    org = _job_organization(soup)
    ranked = []

    def add(url, score, reason):
        url = _eligible_official_url(url)
        if not url:
            return
        ranked.append((int(score), url, reason))

    for url in _organization_urls(org, page_url):
        add(url, 100, "jobposting_organization_url")

    # The configured source may point to the employer's own careers domain even
    # when the individual vacancy redirects into an ATS such as iCIMS/Workday.
    configured_source_url = _eligible_official_url(article.get("source_url"))
    if article.get("official_source") and configured_source_url:
        add(configured_source_url, 98, "configured_official_source")

    for node in _iter_jsonld(soup):
        if not ({"organization", "corporation", "governmentorganization"} & _types(node)):
            continue
        if _name_similarity(company, node.get("name")) < 0.55:
            continue
        for url in _organization_urls(node, page_url):
            add(url, 99, "organization_jsonld_url")

    for anchor in soup.find_all("a", href=True):
        url = _safe_public_url(anchor.get("href"), page_url)
        if not url:
            continue
        host = _host(url)
        if not host or host == page_host or _social_host(host) or _indirect_host(host):
            continue
        label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        context = label
        parent = anchor.parent
        if parent is not None:
            try:
                context += " " + parent.get_text(" ", strip=True)[:300]
            except Exception:
                pass
        lowered = _normalize_name(context)
        if any(_normalize_name(hint) in lowered for hint in OFFICIAL_SITE_LABELS):
            add(url, 96, "official_site_label")
        elif _name_similarity(company, context) >= 0.72:
            add(url, 93, "company_link_context")

    # A first-party careers page is itself enough evidence of the employer domain,
    # but ATS/job-board hosts are deliberately excluded.
    if article.get("official_source") and page_host and not _indirect_host(page_host):
        add(f"{urlparse(page_url).scheme}://{page_host}/", 94, "first_party_job_page")

    if not ranked:
        return "", ""
    ranked.sort(key=lambda row: (-row[0], len(row[1])))
    return ranked[0][1], ranked[0][2]


def _page_logo_candidates(soup, company, page_url, official_site_url=""):
    candidates = {}
    page_host = _host(page_url)
    official_domain = _host(official_site_url)
    org = _job_organization(soup)
    org_name = str(org.get("name") or company or "")

    job_logo = _logo_value(org.get("logo"), page_url)
    if job_logo:
        similarity = _name_similarity(company, org_name)
        _candidate(
            candidates,
            job_logo,
            99 if similarity >= 0.55 else 92,
            "jobposting_jsonld",
            official_domain,
            f"hiringOrganization={org_name}",
        )

    for node in _iter_jsonld(soup):
        if not ({"organization", "corporation", "governmentorganization"} & _types(node)):
            continue
        name = str(node.get("name") or "")
        similarity = _name_similarity(company, name)
        if similarity < 0.55:
            continue
        logo = _logo_value(node.get("logo"), page_url)
        if logo:
            _candidate(
                candidates,
                logo,
                100 if similarity >= 0.8 else 97,
                "organization_jsonld",
                official_domain,
                f"organization={name}",
            )

    official_page = bool(official_domain and _same_or_subdomain(page_host, official_domain))
    is_emploi_public = page_host.endswith("emploi-public.ma")

    for img in soup.find_all("img"):
        src = _image_src(img, page_url)
        if not src:
            continue
        context = _context_text(img)
        similarity = _name_similarity(company, context)
        signature = f"{src} {context}".casefold()
        logo_hint = "logo" in signature or "brand" in signature
        in_header = img.find_parent("header") is not None or "header" in signature

        if is_emploi_public and (
            "/images/administrations/" in src.casefold()
            or "/images/administration/" in src.casefold()
            or "/administrations/" in src.casefold()
        ):
            _candidate(
                candidates,
                src,
                100,
                "emploi_public_administration",
                official_domain,
                context,
            )
            continue

        if is_emploi_public and similarity >= 0.75:
            _candidate(
                candidates,
                src,
                99,
                "emploi_public_named_image",
                official_domain,
                context,
            )
            continue

        if official_page:
            if similarity >= 0.65 and logo_hint:
                score = 100
            elif similarity >= 0.72:
                score = 98
            elif logo_hint and in_header:
                score = 97
            elif logo_hint:
                score = 94
            else:
                score = 0
            if score:
                _candidate(
                    candidates,
                    src,
                    score,
                    "official_site_page_image",
                    official_domain,
                    context,
                )
            continue

        if similarity >= 0.80 and logo_hint:
            _candidate(candidates, src, 95, "named_page_logo", official_domain, context)
        elif similarity >= 0.92:
            _candidate(candidates, src, 93, "named_page_image", official_domain, context)

    return candidates


def _official_homepage_candidates(company, official_site_url):
    candidates = {}
    official_site_url = _eligible_official_url(official_site_url)
    if not official_site_url:
        return candidates

    parsed = urlparse(official_site_url)
    root = f"{parsed.scheme}://{parsed.netloc}/"
    try:
        response = requests.get(
            root,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        response.raise_for_status()
        if len(response.content) > 4_000_000:
            return candidates
        soup = BeautifulSoup(response.text, "html.parser")
        final_url = response.url or root
    except Exception as error:
        log_event(
            "company_logo_official_site_fetch_failed",
            domain=_host(root),
            error=error.__class__.__name__,
        )
        return candidates

    official_domain = _host(final_url) or _host(root)

    for node in _iter_jsonld(soup):
        if not ({"organization", "corporation", "governmentorganization"} & _types(node)):
            continue
        name = str(node.get("name") or "")
        similarity = _name_similarity(company, name)
        logo = _logo_value(node.get("logo"), final_url)
        if logo and (similarity >= 0.45 or not name):
            _candidate(
                candidates,
                logo,
                100 if similarity >= 0.65 else 97,
                "official_homepage_jsonld",
                official_domain,
                f"organization={name}",
            )

    for img in soup.find_all("img"):
        src = _image_src(img, final_url)
        if not src:
            continue
        context = _context_text(img)
        signature = f"{src} {context}".casefold()
        similarity = _name_similarity(company, context)
        logo_hint = "logo" in signature or "brand" in signature
        in_header = img.find_parent("header") is not None or "header" in signature
        if similarity >= 0.62 and logo_hint:
            score = 100
        elif similarity >= 0.75:
            score = 99
        elif logo_hint and in_header:
            score = 98
        elif logo_hint:
            score = 95
        else:
            score = 0
        if score:
            _candidate(
                candidates,
                src,
                score,
                "official_homepage_image",
                official_domain,
                context,
            )

    for link in soup.find_all("link", href=True):
        rel = {str(x).casefold() for x in (link.get("rel") or [])}
        if not rel.intersection({"icon", "shortcut icon", "apple-touch-icon"}):
            continue
        href = _safe_public_url(link.get("href"), final_url)
        if href:
            _candidate(
                candidates,
                href,
                88,
                "official_site_icon",
                official_domain,
                "favicon fallback",
            )

    return candidates


def _probe_image(url, referer=""):
    from PIL import Image

    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        response.raise_for_status()
    except Exception as error:
        return {"ok": False, "reason": f"http:{error.__class__.__name__}"}

    length_header = response.headers.get("content-length")
    try:
        if length_header and int(length_header) > MAX_IMAGE_BYTES:
            return {"ok": False, "reason": "too_large"}
    except ValueError:
        pass

    content = response.content
    if not content or len(content) > MAX_IMAGE_BYTES:
        return {"ok": False, "reason": "empty_or_too_large"}

    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].casefold()
    sample = content[:2048].lstrip().lower()
    is_svg = (
        "svg" in content_type
        or urlparse(response.url or url).path.casefold().endswith(".svg")
        or b"<svg" in sample
    )
    if is_svg:
        if b"<svg" not in sample and b"<svg" not in content[:12000].lower():
            return {"ok": False, "reason": "invalid_svg"}
        return {
            "ok": True,
            "kind": "svg",
            "width": 0,
            "height": 0,
            "content_type": content_type or "image/svg+xml",
            "checksum": hashlib.sha256(content).hexdigest(),
            "final_url": response.url or url,
        }

    try:
        image = Image.open(BytesIO(content))
        width, height = image.size
        image.verify()
    except Exception:
        return {"ok": False, "reason": "not_an_image"}

    if width < 32 or height < 24:
        return {"ok": False, "reason": "too_small"}
    if width > 12000 or height > 12000:
        return {"ok": False, "reason": "dimensions_too_large"}
    ratio = max(width / max(1, height), height / max(1, width))
    if ratio > 15:
        return {"ok": False, "reason": "extreme_aspect_ratio"}

    return {
        "ok": True,
        "kind": "raster",
        "width": width,
        "height": height,
        "content_type": content_type,
        "checksum": hashlib.sha256(content).hexdigest(),
        "final_url": response.url or url,
    }


def _load_registry():
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "records": {}}
    if not isinstance(data, dict):
        return {"version": 1, "records": {}}
    data.setdefault("version", 1)
    data.setdefault("records", {})
    if not isinstance(data["records"], dict):
        data["records"] = {}
    return data


def _save_registry(data):
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = REGISTRY_PATH.with_suffix(".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(REGISTRY_PATH)


def _record_fresh(record):
    value = str(record.get("verified_at") or "")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        verified = datetime.fromisoformat(value)
    except ValueError:
        return False
    if verified.tzinfo is None:
        verified = verified.replace(tzinfo=timezone.utc)
    return _utc_now() - verified.astimezone(timezone.utc) <= timedelta(days=REGISTRY_TTL_DAYS)


def _registry_lookup(registry, company, official_domain):
    company_norm = _normalize_name(company)
    official_domain = str(official_domain or "").casefold().lstrip("www.")
    for record in registry.get("records", {}).values():
        if int(record.get("confidence") or 0) < MIN_LOGO_CONFIDENCE:
            continue
        aliases = {_normalize_name(x) for x in (record.get("aliases") or [])}
        domain = str(record.get("official_domain") or "").casefold().lstrip("www.")
        if official_domain and domain and official_domain == domain:
            return record
        if company_norm and company_norm in aliases:
            return record
    return None


def verified_company_logo(article):
    """Return the verified employer logo used by every Jobs visual.

    The article/package metadata produced by resolve_company_logo is preferred.
    If that metadata is missing later in the pipeline, only a fresh verified
    registry record may restore it. No unverified or guessed logo is returned.
    """

    package = article.get("ai_input_package")
    if not isinstance(package, dict):
        package = {}

    direct_verified = bool(
        article.get("company_logo_verified")
        or package.get("company_logo_verified")
    )
    direct_url = str(
        article.get("company_logo_url")
        or package.get("company_logo_url")
        or ""
    ).strip()

    if direct_verified and direct_url:
        result = {
            "company_logo_url": direct_url,
            "company_logo_verified": True,
            "company_logo_confidence": int(
                article.get("company_logo_confidence")
                or package.get("company_logo_confidence")
                or 0
            ),
            "company_logo_source": str(
                article.get("company_logo_source")
                or package.get("company_logo_source")
                or "verified_article"
            ),
            "company_official_domain": str(
                article.get("company_official_domain")
                or package.get("company_official_domain")
                or ""
            ),
            "company_logo_checksum": str(
                article.get("company_logo_checksum")
                or package.get("company_logo_checksum")
                or ""
            ),
        }
    else:
        company = str(
            article.get("job_company")
            or package.get("job_company")
            or article.get("source_name")
            or ""
        ).strip()
        official_domain = str(
            article.get("company_official_domain")
            or package.get("company_official_domain")
            or ""
        ).strip()
        cached = _registry_lookup(_load_registry(), company, official_domain)
        if not cached or not _record_fresh(cached):
            return {
                "company_logo_url": "",
                "company_logo_verified": False,
                "company_logo_confidence": 0,
                "company_logo_source": "none",
                "company_official_domain": official_domain,
                "company_logo_checksum": "",
            }
        result = {
            "company_logo_url": str(cached.get("logo_url") or ""),
            "company_logo_verified": True,
            "company_logo_confidence": int(cached.get("confidence") or 0),
            "company_logo_source": "verified_registry",
            "company_official_domain": str(
                cached.get("official_domain") or official_domain
            ),
            "company_logo_checksum": str(cached.get("checksum") or ""),
        }
        log_event(
            "company_logo_registry_reused",
            company=company,
            confidence=result["company_logo_confidence"],
            official_domain=result["company_official_domain"],
        )

    article.update(result)
    existing_package = article.get("ai_input_package")
    if isinstance(existing_package, dict):
        existing_package.update(result)
    return result


def _registry_store(registry, company, selected, probe, official_domain):
    domain = str(official_domain or selected.get("official_domain") or "").casefold().lstrip("www.")
    company_norm = _normalize_name(company)
    key_basis = domain or company_norm or selected["url"]
    key = hashlib.sha1(key_basis.encode("utf-8")).hexdigest()[:20]
    current = registry["records"].get(key) or {}
    aliases = {
        str(x).strip()
        for x in (current.get("aliases") or [])
        if str(x).strip()
    }
    if company:
        aliases.add(str(company).strip())
    registry["records"][key] = {
        "company": str(company or current.get("company") or "").strip(),
        "aliases": sorted(aliases),
        "official_domain": domain,
        "logo_url": probe.get("final_url") or selected["url"],
        "source": selected.get("source") or "",
        "confidence": int(selected.get("score") or 0),
        "image_kind": probe.get("kind") or "",
        "width": int(probe.get("width") or 0),
        "height": int(probe.get("height") or 0),
        "checksum": probe.get("checksum") or "",
        "verified_at": _utc_now().isoformat().replace("+00:00", "Z"),
    }
    _save_registry(registry)
    return registry["records"][key]


def refresh_company_logo(article):
    """Re-fetch the official job page and run the same strict logo resolver.

    This is a late-pipeline recovery path for jobs that were enriched before a
    logo became discoverable. It never accepts an unverified image.
    """
    current = verified_company_logo(article)
    if current.get("company_logo_verified") and current.get("company_logo_url"):
        return current

    page_url = str(
        article.get("job_detail_url")
        or article.get("url")
        or article.get("source_url")
        or ""
    ).strip()
    safe_url = _safe_public_url(page_url)
    if not safe_url:
        return current
    try:
        response = requests.get(
            safe_url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        response.raise_for_status()
        if not response.text or len(response.content) > 4_000_000:
            return current
        soup = BeautifulSoup(response.text, "html.parser")
        resolved = resolve_company_logo(soup, article, response.url or safe_url)
        article.update(resolved)
        package = article.get("ai_input_package")
        if isinstance(package, dict):
            package.update(resolved)
        log_event(
            "company_logo_late_refresh",
            article_id=article.get("id"),
            company=article.get("job_company"),
            verified=bool(resolved.get("company_logo_verified")),
        )
        return resolved
    except Exception as error:
        log_event(
            "company_logo_late_refresh_failed",
            article_id=article.get("id"),
            error=error.__class__.__name__,
        )
        return current


def resolve_company_logo(soup, article, page_url):
    """Return only a verified employer logo.

    False positives are intentionally treated as worse than a missing logo.
    The resolver prefers first-party structured data, official public-sector
    artwork, and official employer websites. Unverified URLs are cleared.
    """

    company = str(
        article.get("job_company")
        or article.get("source_name")
        or ""
    ).strip()
    if not company:
        return {
            "company_logo_url": "",
            "company_logo_verified": False,
            "company_logo_confidence": 0,
            "company_logo_source": "none",
            "company_official_domain": "",
        }

    official_site_url, official_site_reason = _official_site_from_page(
        soup,
        company,
        page_url,
        article,
    )
    official_domain = _host(official_site_url)

    registry = _load_registry()
    cached = _registry_lookup(registry, company, official_domain)
    if cached and _record_fresh(cached):
        return {
            "company_logo_url": str(cached.get("logo_url") or ""),
            "company_logo_verified": True,
            "company_logo_confidence": int(cached.get("confidence") or 0),
            "company_logo_source": "verified_registry",
            "company_official_domain": str(cached.get("official_domain") or official_domain),
            "company_logo_checksum": str(cached.get("checksum") or ""),
        }

    candidates = _page_logo_candidates(
        soup,
        company,
        page_url,
        official_site_url=official_site_url,
    )
    # Avoid an unnecessary homepage request when the job page already provides
    # a very strong employer-specific logo (for example JobPosting JSON-LD or
    # emploi-public's administration artwork).
    strongest_page_score = max(
        (int(row.get("score") or 0) for row in candidates.values()),
        default=0,
    )
    if official_site_url and strongest_page_score < 99:
        for url, row in _official_homepage_candidates(company, official_site_url).items():
            current = candidates.get(url)
            if not current or row["score"] > current["score"]:
                candidates[url] = row

    ranked = sorted(
        candidates.values(),
        key=lambda row: (-int(row.get("score") or 0), len(row.get("url") or "")),
    )

    rejected = []
    for candidate in ranked[:MAX_CANDIDATES_TO_PROBE]:
        score = int(candidate.get("score") or 0)
        if score < MIN_LOGO_CONFIDENCE:
            continue
        probe = _probe_image(candidate["url"], referer=page_url)
        if not probe.get("ok"):
            rejected.append(
                {
                    "source": candidate.get("source"),
                    "score": score,
                    "reason": probe.get("reason"),
                }
            )
            continue

        record = _registry_store(
            registry,
            company,
            candidate,
            probe,
            official_domain,
        )
        log_event(
            "company_logo_verified",
            company=company,
            source=candidate.get("source"),
            confidence=score,
            official_domain=record.get("official_domain"),
            kind=probe.get("kind"),
            official_site_reason=official_site_reason,
        )
        return {
            "company_logo_url": str(record.get("logo_url") or ""),
            "company_logo_verified": True,
            "company_logo_confidence": score,
            "company_logo_source": str(candidate.get("source") or ""),
            "company_official_domain": str(record.get("official_domain") or official_domain),
            "company_logo_checksum": str(record.get("checksum") or ""),
        }

    log_event(
        "company_logo_unverified",
        company=company,
        official_domain=official_domain,
        candidate_count=len(ranked),
        rejected_count=len(rejected),
    )
    return {
        "company_logo_url": "",
        "company_logo_verified": False,
        "company_logo_confidence": 0,
        "company_logo_source": "none",
        "company_official_domain": official_domain,
    }
