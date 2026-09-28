from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


MOROCCO_CITIES = (
    "Casablanca", "Rabat", "Salé", "Sale", "Marrakech", "Marrakesh", "Fès", "Fes",
    "Tanger", "Tangier", "Agadir", "Meknès", "Meknes", "Oujda", "Kénitra", "Kenitra",
    "Tétouan", "Tetouan", "El Jadida", "Safi", "Mohammedia", "Khouribga", "Benguerir",
    "Laâyoune", "Laayoune", "Dakhla",
)


def _text(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return re.sub(r"\s+", " ", BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)).strip()


def _jsonld_nodes(value):
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _jsonld_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _jsonld_nodes(item)


def _job_node(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except Exception:
            continue
        for node in _jsonld_nodes(data):
            types = node.get("@type")
            if isinstance(types, str):
                types = [types]
            if any(str(x).casefold() == "jobposting" for x in (types or [])):
                return node
    return {}


def _organization(node):
    org = node.get("hiringOrganization") or {}
    if isinstance(org, list):
        org = org[0] if org else {}
    return org if isinstance(org, dict) else {}


def _logo_url(org):
    logo = org.get("logo")
    if isinstance(logo, str):
        return logo
    if isinstance(logo, dict):
        return str(logo.get("url") or logo.get("contentUrl") or "")
    return ""


def _location(node):
    value = node.get("jobLocation")
    rows = value if isinstance(value, list) else [value] if value else []
    locations = []
    country = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        address = row.get("address") or {}
        if not isinstance(address, dict):
            continue
        locality = _text(address.get("addressLocality"))
        region = _text(address.get("addressRegion"))
        country_value = address.get("addressCountry")
        if isinstance(country_value, dict):
            country_value = country_value.get("name") or country_value.get("@id")
        country = country or _text(country_value)
        label = ", ".join(x for x in (locality, region) if x)
        if label and label not in locations:
            locations.append(label)
    return " / ".join(locations), country


def _salary(node):
    value = node.get("baseSalary")
    if not isinstance(value, dict):
        return _text(value)
    currency = _text(value.get("currency"))
    val = value.get("value")
    if isinstance(val, dict):
        amount = val.get("value") or val.get("minValue")
        max_value = val.get("maxValue")
        unit = _text(val.get("unitText"))
        if amount not in (None, "") and max_value not in (None, "") and str(max_value) != str(amount):
            amount_text = f"{amount}-{max_value}"
        else:
            amount_text = str(amount or "")
        return " ".join(x for x in (amount_text, currency, unit) if x)
    return " ".join(x for x in (_text(val), currency) if x)


def _number_of_positions(text):
    patterns = (
        r"(?i)\b(\d{1,5})\s+(?:postes?|positions?|recrutements?|vacancies|jobs?)\b",
        r"(?i)\b(?:recrute|recrutement de|hiring)\s+(\d{1,5})\b",
        r"(\d{1,5})\s+(?:منصب|مناصب|فرصة عمل)",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            try:
                return int(match.group(1))
            except Exception:
                pass
    return 0


def _deadline_from_text(text):
    patterns = (
        r"(?i)(?:date limite|deadline|last date|cl[oô]ture)[^\d]{0,30}(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        r"(?:آخر أجل|اخر اجل)[^\d]{0,30}(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        r"(?i)(?:date limite|deadline|cl[oô]ture)[^\d]{0,30}(\d{4}-\d{2}-\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            return match.group(1)
    return ""


def _city_from_text(text):
    folded = (text or "").casefold()
    for city in MOROCCO_CITIES:
        city_folded = city.casefold()
        pattern = rf"(?<!\\w){re.escape(city_folded)}(?!\\w)"
        if re.search(pattern, folded):
            return city
    return ""


def _source_company(source_name):
    value = re.sub(r"(?i)\b(careers?|jobs?|recrutement|recruitment|vacancies)\b", " ", str(source_name or ""))
    return re.sub(r"\s+", " ", value).strip(" -–—")


APPLY_LINK_HINTS = (
    "apply", "apply now", "postuler", "postulez", "candidater", "candidature",
    "déposer ma candidature", "deposer ma candidature", "submit application",
    "submit your application", "inscription", "register",
)
DOCUMENT_LINK_HINTS = (
    "pdf", "avis", "conditions", "condition", "règlement", "reglement",
    "dossier", "fiche", "communiqué", "communique", "télécharger",
    "telecharger", "download", "job description", "descriptif",
)


def _public_http_url(url):
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _extract_job_action_links(soup, page_url):
    """Find official apply/document links exposed by the verified job page."""
    rows = []
    seen = set()

    def add(href, label="", kind=""):
        absolute = urljoin(page_url, str(href or "").strip())
        if not _public_http_url(absolute):
            return
        key = absolute.split("#", 1)[0].rstrip("/")
        if key in seen:
            return
        seen.add(key)
        rows.append(
            {
                "url": absolute,
                "label": _text(label) or ("التقديم الرسمي" if kind == "apply" else "ملف رسمي"),
                "kind": kind,
            }
        )

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        label = _text(anchor.get_text(" ", strip=True))
        signature = f"{label} {href}".casefold()
        if any(hint in signature for hint in APPLY_LINK_HINTS):
            add(href, label, "apply")
            continue
        if href.casefold().split("?", 1)[0].endswith(".pdf") or any(
            hint in signature for hint in DOCUMENT_LINK_HINTS
        ):
            add(href, label, "document")

    for form in soup.find_all("form", action=True):
        action = str(form.get("action") or "").strip()
        signature = f"{form.get('id', '')} {form.get('class', '')} {action}".casefold()
        if any(hint in signature for hint in APPLY_LINK_HINTS):
            add(action, "التقديم الرسمي", "apply")

    return rows[:8]


def extract_job_fields(soup, article, page_url, full_text=""):
    node = _job_node(soup)
    org = _organization(node)
    location, country = _location(node)
    body = full_text or _text(node.get("description"))
    source_country = str(article.get("source_country") or "").upper()
    source_eligibility = str(article.get("source_eligibility") or "").strip().lower()

    identifier = node.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value") or identifier.get("name")
    elif isinstance(identifier, list):
        identifier = identifier[0] if identifier else ""

    job_title = _text(node.get("title")) or article.get("fetched_title") or article.get("title", "")
    company = _text(org.get("name")) or article.get("job_company") or _source_company(article.get("source_name"))
    structured_country = str(country or "").strip()
    is_morocco_source = (
        source_country == "MA"
        or structured_country.casefold() in {"ma", "morocco", "maroc"}
    )
    # Text-city fallback is Morocco-specific. Never scan a known foreign vacancy
    # for Moroccan city names because ordinary words can contain strings such as "fes".
    if not location and is_morocco_source:
        location = _city_from_text(body)
    country_code = "MA" if is_morocco_source else source_country or structured_country
    eligibility = source_eligibility
    if not eligibility and country_code == "MA":
        eligibility = "morocco"

    employment = node.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(_text(x) for x in employment if _text(x))

    action_links = _extract_job_action_links(soup, page_url)
    direct_apply = next((row for row in action_links if row.get("kind") == "apply"), None)
    documents = [row for row in action_links if row.get("kind") == "document"]
    structured_url = _text(node.get("url"))
    application_url = (
        (direct_apply or {}).get("url")
        or article.get("application_url")
        or structured_url
        or page_url
    )
    application_kind = "direct_apply" if direct_apply else "official_job_page"
    deadline = _text(node.get("validThrough")) or _deadline_from_text(body)
    published_at = _text(node.get("datePosted")) or article.get("source_published_at", "")
    remote = str(node.get("jobLocationType") or "").upper() == "TELECOMMUTE" or bool(article.get("source_remote"))
    visa = bool(re.search(r"(?i)visa\s+sponsor|sponsorship|parrainage\s+visa", body or ""))

    lower = (body or "").casefold()
    entry_level = any(x in lower for x in (
        "débutant", "debutant", "sans expérience", "sans experience", "entry level",
        "junior", "stage", "stagiaire", "fresh graduate",
    ))

    fields = {
        "job_title": job_title,
        "job_company": company,
        "job_location": location,
        "job_country": country_code,
        "job_contract_type": _text(employment),
        "job_salary": _salary(node),
        "job_deadline": deadline,
        "job_published_at": published_at,
        "job_application_url": application_url,
        "job_application_link_kind": application_kind,
        "job_detail_url": page_url,
        "job_action_links": action_links,
        "job_document_links": documents,
        "job_number_of_positions": _number_of_positions(f"{job_title} {body}"),
        "job_diploma": str(article.get("job_diploma") or ""),
        "job_experience": str(article.get("job_experience") or ""),
        "job_entry_level": entry_level,
        "job_remote": remote,
        "job_visa_sponsorship": visa,
        "job_eligibility": eligibility or "unknown",
        "job_official_source": bool(article.get("official_source")),
        "job_external_reference": _text(identifier),
        "company_logo_url": _logo_url(org),
        "job_description": body,
    }
    return {key: value for key, value in fields.items() if value not in (None, "")}
