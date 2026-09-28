from __future__ import annotations

import json
import re
from datetime import date
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


MONTH_NAME_TO_NUMBER = {
    "janvier": 1, "january": 1, "يناير": 1,
    "février": 2, "fevrier": 2, "february": 2, "فبراير": 2,
    "mars": 3, "march": 3, "مارس": 3,
    "avril": 4, "april": 4, "أبريل": 4, "ابريل": 4,
    "mai": 5, "may": 5, "ماي": 5, "مايو": 5,
    "juin": 6, "june": 6, "يونيو": 6,
    "juillet": 7, "july": 7, "يوليوز": 7, "يوليو": 7,
    "août": 8, "aout": 8, "august": 8, "غشت": 8, "أغسطس": 8, "اغسطس": 8,
    "septembre": 9, "september": 9, "شتنبر": 9, "سبتمبر": 9,
    "octobre": 10, "october": 10, "أكتوبر": 10, "اكتوبر": 10,
    "novembre": 11, "november": 11, "نونبر": 11, "نوفمبر": 11,
    "décembre": 12, "decembre": 12, "december": 12, "دجنبر": 12, "ديسمبر": 12,
}


DEADLINE_LABEL_PATTERN = (
    r"(?:آخر\s+أجل(?:\s+للترشيح)?|اخر\s+اجل(?:\s+للترشيح)?|"
    r"date\s+limite(?:\s+de\s+candidature)?|deadline|last\s+date|"
    r"cl[oô]ture(?:\s+des\s+candidatures)?|jusqu(?:'|’)?au|avant\s+le)"
)


def _iso_date(year, month, day):
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except (TypeError, ValueError):
        return ""


def _deadline_details_from_text(text):
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return "", ""

    numeric_patterns = (
        rf"(?i){DEADLINE_LABEL_PATTERN}[^\d]{{0,40}}(\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{4}})",
        rf"(?i){DEADLINE_LABEL_PATTERN}[^\d]{{0,40}}(\d{{4}}-\d{{2}}-\d{{2}})",
    )
    for pattern in numeric_patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        raw = match.group(1)
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            return raw, raw
        parts = re.split(r"[/-]", raw)
        normalized = _iso_date(parts[2], parts[1], parts[0]) if len(parts) == 3 else ""
        return normalized or raw, raw

    word_pattern = (
        rf"(?i){DEADLINE_LABEL_PATTERN}[^\d]{{0,45}}"
        r"(\d{1,2})\s+([A-Za-zÀ-ÿ\u0600-\u06FF]+)\s+(\d{4})"
        r"(?:\s*(?:à|a|الساعة|على\s+الساعة)\s*(\d{1,2}:\d{2}))?"
    )
    match = re.search(word_pattern, value)
    if match:
        day_value, month_name, year_value, clock = match.groups()
        month_number = MONTH_NAME_TO_NUMBER.get(month_name.casefold())
        normalized = _iso_date(year_value, month_number, day_value) if month_number else ""
        raw = " ".join(x for x in (day_value, month_name, year_value, clock or "") if x)
        return normalized or raw, raw

    return "", ""


def _deadline_from_text(text):
    normalized, _display = _deadline_details_from_text(text)
    return normalized


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
    "liste", "list", "résultat", "resultat", "results", "convoqué",
    "convoques", "convoqués", "admis", "shortlist", "المدعوين",
    "اللائحة", "اللوائح", "النتائج", "النتيجة", "تحميل",
)

GENERIC_LINK_LABELS = {
    "pdf", "download", "télécharger", "telecharger", "تحميل", "هنا",
    "الرابط", "اضغط هنا", "voir", "consulter",
}


def _public_http_url(url):
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _link_context(anchor, label=""):
    for parent_name in ("tr", "li", "p"):
        parent = anchor.find_parent(parent_name)
        if not parent:
            continue
        context = _text(parent.get_text(" ", strip=True))
        if not context:
            continue
        if label and context == label:
            continue
        return context[:320]
    return ""


def _extract_job_action_links(soup, page_url):
    """Find official apply/document/result links exposed by the verified job page."""
    rows = []
    seen = set()

    def add(href, label="", kind="", context=""):
        absolute = urljoin(page_url, str(href or "").strip())
        if not _public_http_url(absolute):
            return
        key = absolute.split("#", 1)[0].rstrip("/")
        if key in seen:
            return
        seen.add(key)
        clean_label = _text(label)
        clean_context = _text(context)
        if clean_label.casefold() in GENERIC_LINK_LABELS and clean_context:
            clean_label = clean_context
        rows.append(
            {
                "url": absolute,
                "label": clean_label or ("التقديم الرسمي" if kind == "apply" else "ملف رسمي"),
                "kind": kind,
                "context": clean_context,
            }
        )

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        label = _text(anchor.get_text(" ", strip=True))
        context = _link_context(anchor, label=label)
        signature = f"{label} {context} {href}".casefold()
        if any(hint in signature for hint in APPLY_LINK_HINTS):
            add(href, label, "apply", context=context)
            continue
        if href.casefold().split("?", 1)[0].endswith(".pdf") or any(
            hint in signature for hint in DOCUMENT_LINK_HINTS
        ):
            add(href, label, "document", context=context)

    for form in soup.find_all("form", action=True):
        action = str(form.get("action") or "").strip()
        signature = f"{form.get('id', '')} {form.get('class', '')} {action}".casefold()
        if any(hint in signature for hint in APPLY_LINK_HINTS):
            add(action, "التقديم الرسمي", "apply")

    # Public recruitment campaigns can expose many specialization/result PDFs.
    # Keep enough exact official links to build a complete table instead of silently
    # dropping rows after the eighth document.
    return rows[:30]


def _notice_type(title, body):
    haystack = f"{title} {body}".casefold()
    if re.search(r"(النتائج\s+النهائية|نتائج\s+نهائية|résultats?\s+définitifs?|final\s+results?)", haystack):
        return "final_results"
    if re.search(r"(لوائح?\s+المدعوين|لائحة\s+المدعوين|convoqu[eé]s?|shortlist|admis.*(?:écrit|oral)|مدعوين.*(?:كتابي|شفوي))", haystack):
        return "candidate_list"
    if re.search(r"(النتائج|النتيجة|résultats?|results?)", haystack):
        return "results"
    return "vacancy"


def _notice_status(title, body):
    haystack = f"{title} {body}".casefold()
    if re.search(r"(مؤقتة|مؤقت|أولية|provisoire|provisional|préliminaire|preliminaire)", haystack):
        return "provisional"
    if re.search(r"(نهائية|نهائي|définitive|definitive|finale|final)", haystack):
        return "final"
    return ""


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
    structured_deadline = _text(node.get("validThrough"))
    text_deadline, text_deadline_display = _deadline_details_from_text(body)
    deadline = structured_deadline or text_deadline
    deadline_display = text_deadline_display or structured_deadline
    published_at = _text(node.get("datePosted")) or article.get("source_published_at", "")
    notice_type = _notice_type(job_title, body)
    notice_status = _notice_status(job_title, body)
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
        "job_deadline_display": deadline_display,
        "job_notice_type": notice_type,
        "job_notice_status": notice_status,
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
