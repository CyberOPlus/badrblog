from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from job_core import (
    is_application_url_bound_to_job,
    is_foreign_job_detail_url,
    is_job_specific_url,
)


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
    raw = str(value).strip()
    if re.fullmatch(r"(?:https?://|www\.)\S+", raw, flags=re.I):
        return raw
    return re.sub(r"\s+", " ", BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)).strip()


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


def unicef_job_content(soup, page_url):
    """Accept only the PageUp detail block matching this official vacancy URL."""
    parsed = urlparse(page_url)
    match = re.search(r"/job/(\d+)(?:/|$)", parsed.path)
    if parsed.hostname != "jobs.unicef.org" or not match:
        return None
    content = soup.select_one("#job-content")
    reference = content.select_one(".job-externalJobNo") if content else None
    if reference is None or reference.get_text(strip=True) != match.group(1):
        return None
    return content if content.select_one("h2") and content.select_one("#job-details") else None


def _unicef_job_node(soup, page_url):
    content = unicef_job_content(soup, page_url)
    if content is None:
        return {}

    def text_at(selector):
        tag = content.select_one(selector)
        return tag.get_text(" ", strip=True) if tag else ""

    def date_at(selector):
        tag = content.select_one(selector)
        return str(tag.get("datetime") or "").strip() if tag else ""

    station = ""
    label = content.find("b", string=re.compile(r"^\s*Duty Station\s*:\s*$", re.I))
    if label:
        parts = []
        for sibling in label.next_siblings:
            if getattr(sibling, "name", None) == "br":
                break
            parts.append(_text(str(sibling)))
        station = " ".join(part for part in parts if part)

    title = text_at("h2")
    description = text_at("#job-details")
    # An international title is positive evidence; national/local restrictions
    # take precedence over both that title and generic equal-opportunity copy.
    restricted = re.search(
        r"\b(?:national\s+consultant|nationals?\s+only|local\s+candidates?\s+only|"
        r"only\s+(?:open\s+)?(?:to|for)\s+.{0,70}?nationals?|"
        r"must\s+be\s+.{0,50}?national|valid\s+work\s+permit|"
        r"legal\s+right\s+to\s+work)\b",
        f"{title} {description}", re.I,
    )
    international = bool(re.search(r"\binternational\s+consultan(?:t|cy)\b", title, re.I))
    return {
        "title": title,
        "description": description,
        "identifier": text_at(".job-externalJobNo"),
        "hiringOrganization": {"name": "UNICEF"},
        "jobLocation": {"address": {
            "addressLocality": station,
            "addressCountry": text_at(".location"),
        }},
        "employmentType": text_at(".work-type"),
        "datePosted": date_at(".open-date time[datetime]"),
        "validThrough": date_at(".close-date time[datetime]"),
        "jobLocationType": "TELECOMMUTE" if re.search(r"\b(?:remote|home[ -]based)\b", title, re.I) else "",
        "_eligibility": "abroad_open" if international and not restricted else "unknown",
    }


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
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
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
    r"(?:آخر\s+أجل(?:\s+(?:للترشيح|لإيداع\s+الترشيحات))?|"
    r"اخر\s+اجل(?:\s+(?:للترشيح|لايداع\s+الترشيحات))?|"
    r"إلى\s+غاية|الى\s+غاية|وذلك\s+قبل|قبل\s*:?[\s]*|"
    r"date\s+limite(?:\s+(?:de\s+candidature|de\s+dépôt\s+des\s+candidatures|"
    r"de\s+depot\s+des\s+candidatures|d'inscription))?|"
    r"dernier\s+délai|dernier\s+delai|deadline|last\s+date|"
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
        rf"(?i){DEADLINE_LABEL_PATTERN}[^\d]{{0,40}}(\d{{4}}[/-]\d{{1,2}}[/-]\d{{1,2}})",
    )
    for pattern in numeric_patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        raw = match.group(1)
        parts = re.split(r"[/-]", raw)
        if len(parts) == 3 and len(parts[0]) == 4:
            normalized = _iso_date(parts[0], parts[1], parts[2])
        else:
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


def _labelled_date_details(text, label_pattern):
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return "", ""

    numeric = re.search(
        rf"(?i){label_pattern}[^\d]{{0,40}}(\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})",
        value,
    )
    if numeric:
        raw = numeric.group(1)
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            return raw, raw
        parts = re.split(r"[/-]", raw)
        normalized = _iso_date(parts[2], parts[1], parts[0]) if len(parts) == 3 else ""
        return normalized or raw, raw

    words = re.search(
        rf"(?i){label_pattern}[^\d]{{0,45}}"
        r"(\d{1,2})\s+([A-Za-zÀ-ÿ\u0600-\u06FF]+)\s+(\d{4})"
        r"(?:\s*(?:à|a|الساعة|على\s+الساعة)\s*(\d{1,2}:\d{2}))?",
        value,
    )
    if words:
        day_value, month_name, year_value, clock = words.groups()
        month_number = MONTH_NAME_TO_NUMBER.get(month_name.casefold())
        normalized = _iso_date(year_value, month_number, day_value) if month_number else ""
        raw = " ".join(x for x in (day_value, month_name, year_value, clock or "") if x)
        return normalized or raw, raw

    # Official ATS pages can label month-first dates, e.g. Capgemini's
    # "Posted on: Sep 7, 2026". Require the posting label; an unlabelled footer
    # year, expiry date or discovery time must never prove publication freshness.
    month_first = re.search(
        rf"(?i){label_pattern}\s*:?\s*"
        r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b",
        value,
    )
    if month_first:
        month_name, day_value, year_value = month_first.groups()
        month_number = MONTH_NAME_TO_NUMBER.get(month_name.casefold())
        normalized = _iso_date(year_value, month_number, day_value) if month_number else ""
        if normalized:
            return normalized, f"{month_name} {day_value}, {year_value}"
    return "", ""


def _exam_date_details_from_text(text):
    return _labelled_date_details(
        text,
        r"(?:تاريخ\s+(?:إجراء\s+)?المباراة|date\s+du\s+concours|exam\s+date|test\s+date)",
    )


def _publication_date_details_from_text(text):
    return _labelled_date_details(
        text,
        r"(?:تاريخ\s+النشر|date\s+de\s+publication|published\s+on|publication\s+date|posted\s+on|date\s+posted)",
    )


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


EMPLOYER_LABEL_PATTERNS = (
    r"Administration\s+qui\s+recrute",
    r"Administration\s+organisatrice",
    r"Organisme\s+recruteur",
    r"Employeur",
    r"الإدارة\s+التي\s+توظف",
    r"الإدارة\s+المنظمة",
    r"الإدارة\s+المشغلة",
    r"الجهة\s+المنظمة",
)

EMPLOYER_VALUE_STOP_PATTERNS = (
    r"Délai\s+de\s+dépôt",
    r"Date\s+du\s+concours",
    r"Date\s+de\s+publication",
    r"Téléchargement",
    r"Description",
    r"Site\s+de\s+dépôt",
    r"آخر\s+أجل",
    r"تاريخ\s+المباراة",
    r"تاريخ\s+النشر",
    r"تحميل",
)


def _company_from_page(soup, page_url=""):
    """Extract a visibly labelled employer/administration without guessing."""
    try:
        page_text = soup.get_text("\n", strip=True)
    except Exception:
        return ""
    if not page_text:
        return ""

    label_union = "|".join(f"(?:{pattern})" for pattern in EMPLOYER_LABEL_PATTERNS)
    stop_union = "|".join(f"(?:{pattern})" for pattern in EMPLOYER_VALUE_STOP_PATTERNS)
    pattern = (
        rf"(?im)^\s*(?:{label_union})\s*:?[ \t]*(?:\n[ \t]*)?"
        rf"([^\n]{{2,220}})"
    )
    for match in re.finditer(pattern, page_text):
        candidate = _text(match.group(1)).strip(" :-–—")
        if not candidate:
            continue
        candidate = re.split(
            rf"(?i)\s+(?={stop_union})",
            candidate,
            maxsplit=1,
        )[0].strip(" :-–—")
        if len(candidate) < 2 or len(candidate) > 180:
            continue
        if re.search(
            r"(?i)^(?:administration|organisme|employeur|وزارة|الإدارة|الجهة)$",
            candidate,
        ):
            continue
        return candidate
    return ""


APPLY_LINK_HINTS = (
    "apply", "apply now", "postuler", "postulez", "candidater", "candidature",
    "déposer ma candidature", "deposer ma candidature", "submit application",
    "submit your application", "inscription", "register",
    "التقديم", "الترشيح", "إيداع الترشيح", "ايداع الترشيح",
    "إيداع الملف", "ايداع الملف", "الإيداع الإلكتروني", "الايداع الالكتروني",
    "تسجيل الترشيح", "تقديم الطلب",
)
DOCUMENT_LINK_HINTS = (
    "pdf", "avis", "conditions", "condition", "règlement", "reglement",
    "dossier", "fiche", "communiqué", "communique", "télécharger",
    "telecharger", "download", "job description", "descriptif",
    "liste", "list", "résultat", "resultat", "results", "convoqué",
    "convoques", "convoqués", "admis", "shortlist", "المدعوين",
    "اللائحة", "اللوائح", "النتائج", "النتيجة", "تحميل",
    "تحميل الإعلان", "نص الإعلان", "قرار المباراة", "مقرر المباراة",
    "قرار فتح", "قرار فتح المباراة", "بطاقة الوظيفة", "بطاقة المنصب",
    "الاستدعاء", "استدعاء", "محضر", "الوثيقة الرسمية",
)

GENERIC_LINK_LABELS = {
    "pdf", "download", "télécharger", "telecharger", "تحميل", "هنا",
    "الرابط", "اضغط هنا", "voir", "consulter",
}

APPLICATION_CHANNEL_TEXT_RE = re.compile(
    r"(?i)(?:"
    r"site\s+de\s+d[eé]p[oô]t|"
    r"site\s+de\s+candidature|"
    r"plateforme\s+(?:de\s+)?(?:candidature|d[eé]p[oô]t)|"
    r"موقع\s+(?:الإيداع|الايداع|إيداع|ايداع|التقديم|الترشيح)(?:\s+الترشيحات)?|"
    r"منصة\s+(?:الإيداع|الايداع|إيداع|ايداع|التقديم|الترشيح)(?:\s+الترشيحات)?"
    r")\s*:?\s*((?:https?://|www\.)[^\s<>'\"،؛]+)"
)


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


def _extract_job_action_links(soup, page_url, full_text=""):
    """Find official apply/document/result links exposed by the verified job page."""
    rows = []
    seen = set()

    def add(href, label="", kind="", context=""):
        absolute = urljoin(page_url, str(href or "").strip())
        if not _public_http_url(absolute):
            return
        if is_foreign_job_detail_url({"canonical_url": page_url, "url": page_url}, absolute):
            return
        parsed = urlparse(absolute.split("#", 1)[0])
        normalized_path = unquote(parsed.path or "").rstrip("/")
        key = (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            normalized_path,
            parsed.query,
        )
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
        decoded_href = unquote(href)
        signature = f"{label} {context} {href} {decoded_href}".casefold()
        is_pdf = href.casefold().split("?", 1)[0].endswith(".pdf")
        if is_pdf:
            add(href, label, "document", context=context)
            continue
        if any(hint in signature for hint in APPLY_LINK_HINTS):
            add(href, label, "apply", context=context)
            continue
        if any(hint in signature for hint in DOCUMENT_LINK_HINTS):
            add(href, label, "document", context=context)

    for form in soup.find_all("form", action=True):
        action = str(form.get("action") or "").strip()
        signature = f"{form.get('id', '')} {form.get('class', '')} {action}".casefold()
        if any(hint in signature for hint in APPLY_LINK_HINTS):
            add(action, "التقديم الرسمي", "apply")

    # Modern ATS pages (for example Teamtailor) can lazy-load the application
    # form through a turbo-frame or another src-bearing element instead of a
    # normal anchor/form action.
    for node in soup.find_all(src=True):
        src = str(node.get("src") or "").strip()
        node_id = str(node.get("id") or "")
        classes = " ".join(str(x) for x in (node.get("class") or []))
        label = _text(node.get_text(" ", strip=True))
        signature = f"{node_id} {classes} {label} {src}".casefold()
        if (
            any(hint in signature for hint in APPLY_LINK_HINTS)
            or "application_form" in signature
            or re.search(r"/applications?/(?:new|apply)(?:[/?#]|$)", src, flags=re.I)
        ):
            add(src, label or "التقديم المباشر", "apply")

    # Some official public-recruitment pages expose the deposit platform as
    # contextual text ("Site de dépôt: ...") instead of a clickable anchor.
    # Capture only URLs immediately tied to an explicit application-channel label;
    # never promote arbitrary URLs found elsewhere in the page text.
    text_source = " ".join(
        part
        for part in (
            _text(full_text),
            _text(soup.get_text(" ", strip=True)),
        )
        if part
    ).strip()
    for match in APPLICATION_CHANNEL_TEXT_RE.finditer(text_source):
        channel_url = str(match.group(1) or "").strip().rstrip(".,;:،؛)]}>\"'")
        if channel_url.casefold().startswith("www."):
            channel_url = "https://" + channel_url
        add(
            channel_url,
            "منصة الترشيح الرسمية",
            "apply",
            context=match.group(0)[:320],
        )

    # Emploi-Public occasionally exposes its official download controls through
    # client-rendered markup that is not present as ordinary <a href> nodes in the
    # raw response consumed by the bot. Recover only deterministic official
    # document routes tied to the exact notice UUID; never guess a foreign notice.
    parsed_page = urlparse(page_url)
    host = parsed_page.netloc.casefold().removeprefix("www.")
    notice_uuid_match = re.search(
        r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        parsed_page.path,
    )
    if host == "emploi-public.ma" and notice_uuid_match:
        notice_uuid = notice_uuid_match.group(1)
        page_text = _text(soup.get_text(" ", strip=True))
        raw_markup = str(soup)
        evidence = f"{page_text} {raw_markup}".casefold()

        # First keep any exact paths that are already embedded anywhere in the
        # markup, even when they are not represented by a normal anchor node.
        path_patterns = (
            rf"(/(?:ar|fr)/[^\"'<>\s]*/arrete/{re.escape(notice_uuid)})",
            rf"(/(?:ar|fr)/[^\"'<>\s]*/fichiers_att/{re.escape(notice_uuid)}/\d+)",
        )
        for pattern in path_patterns:
            for match in re.finditer(pattern, raw_markup, flags=re.I):
                path = match.group(1).replace("&amp;", "&")
                label = (
                    "قرار فتح المباراة"
                    if "/arrete/" in path.casefold()
                    else "بطاقة الوظيفة"
                )
                add(path, label, "document", context=label)

        # Arabic detail pages have stable official download routes. Synthesize
        # them only when the exact visible label proves that the document exists.
        if "قرار فتح المباراة" in page_text or "قرار فتح" in page_text:
            add(
                f"/ar/تحميل/المباريات/arrete/{notice_uuid}",
                "قرار فتح المباراة",
                "document",
                context="تحميل الملفات",
            )

        if "بطاقة الوظيفة" in page_text:
            attachment_indexes = sorted({
                int(value)
                for value in re.findall(
                    rf"fichiers_att/{re.escape(notice_uuid)}/(\d+)",
                    raw_markup,
                    flags=re.I,
                )
            })
            if not attachment_indexes:
                attachment_indexes = [0]
            for index in attachment_indexes[:12]:
                add(
                    f"/ar/تحميل/المباريات/fichiers_att/{notice_uuid}/{index}",
                    "بطاقة الوظيفة" if index == 0 else f"ملف مرفق {index + 1}",
                    "document",
                    context="الملفات المرفقة",
                )

    # Public recruitment campaigns can expose many specialization/result PDFs.
    # Keep enough exact official links to build a complete table instead of silently
    # dropping rows after the eighth document.
    return rows[:30]


def ofppt_official_document_links(soup, page_url):
    """Return only the official PDF attached to the exact OFPPT offer page."""
    parsed_page = urlparse(str(page_url or "").strip())
    offer_match = re.fullmatch(r"/offre/(\d+)/?", parsed_page.path, flags=re.I)
    if parsed_page.scheme != "https" or parsed_page.hostname != "recrutement.ofppt.ma" or not offer_match:
        return []
    offer_id = offer_match.group(1)
    documents, seen = [], set()
    for anchor in soup.select("a[href]"):
        absolute = urljoin(page_url, str(anchor.get("href") or "").strip())
        parsed = urlparse(absolute)
        if parsed.scheme != "https" or parsed.hostname != "recrutement.ofppt.ma" or parsed.username or parsed.password:
            continue
        if not re.fullmatch(rf"/files/offer/{re.escape(offer_id)}/avis_concours/[^/]+\.pdf", parsed.path, flags=re.I):
            continue
        key = absolute.split("#", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        documents.append({"url": absolute, "label": _text(anchor.get_text(" ", strip=True)) or "Avis de concours officiel", "kind": "document", "context": "official_ofppt_detail"})
        if len(documents) >= 3:
            break
    return documents


def _notice_type(title, body):
    title_text = str(title or "").casefold()
    haystack = f"{title} {body}".casefold()

    # The current page title/stage outranks future boilerplate in the body.
    # Active competition notices commonly say that final results "will be
    # published" later; that must not turn today's open competition into a
    # final-results article.
    if re.search(r"(النتائج\s+النهائية|نتائج\s+نهائية|résultats?\s+définitifs?|final\s+results?)", title_text):
        return "final_results"
    if re.search(r"(لوائح?\s+المدعوين|لائحة\s+المدعوين|convoqu[eé]s?|shortlist|admis.*(?:écrit|oral)|مدعوين.*(?:كتابي|شفوي))", title_text):
        return "candidate_list"
    if re.search(r"(النتائج|النتيجة|résultats?|results?)", title_text):
        return "results"
    if re.search(
        r"(مباراة(?:\s+توظيف)?|مباريات(?:\s+توظيف)?|"
        r"concours(?:\s+de\s+recrutement)?|recrutement\s+par\s+concours)",
        title_text,
    ):
        return "competition"

    if re.search(r"(النتائج\s+النهائية|نتائج\s+نهائية|résultats?\s+définitifs?|final\s+results?)", haystack):
        return "final_results"
    if re.search(r"(لوائح?\s+المدعوين|لائحة\s+المدعوين|convoqu[eé]s?|shortlist|admis.*(?:écrit|oral)|مدعوين.*(?:كتابي|شفوي))", haystack):
        return "candidate_list"
    if re.search(r"(النتائج|النتيجة|résultats?|results?)", haystack):
        return "results"
    if re.search(
        r"(مباراة(?:\s+توظيف)?|مباريات(?:\s+توظيف)?|"
        r"concours(?:\s+de\s+recrutement)?|recrutement\s+par\s+concours)",
        haystack,
    ):
        return "competition"
    return "vacancy"


def _notice_status(title, body):
    haystack = f"{title} {body}".casefold()
    if re.search(r"(مؤقتة|مؤقت|أولية|provisoire|provisional|préliminaire|preliminaire)", haystack):
        return "provisional"
    if re.search(r"(نهائية|نهائي|définitive|definitive|finale|final)", haystack):
        return "final"
    return ""


def extract_job_fields(soup, article, page_url, full_text=""):
    unicef_node = _unicef_job_node(soup, page_url)
    node = _job_node(soup) or unicef_node
    org = _organization(node)
    location, country = _location(node)
    body = full_text or _text(node.get("description"))
    try:
        page_evidence_text = soup.get_text("\n", strip=True)
    except Exception:
        page_evidence_text = ""
    labelled_evidence = "\n".join(
        part for part in (body, page_evidence_text) if str(part or "").strip()
    )
    source_country = str(article.get("source_country") or "").upper()
    source_eligibility = str(article.get("source_eligibility") or "").strip().lower()

    identifier = node.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value") or identifier.get("name")
    elif isinstance(identifier, list):
        identifier = identifier[0] if identifier else ""

    job_title = _text(node.get("title")) or article.get("fetched_title") or article.get("title", "")
    company = (
        _text(org.get("name"))
        or _company_from_page(soup, page_url)
        or article.get("job_company")
        or _source_company(article.get("source_name"))
    )
    structured_country = str(country or "").strip()
    is_morocco_source = (
        source_country == "MA"
        or structured_country.casefold() in {"ma", "morocco", "maroc"}
    )
    # Text-city fallback is Morocco-specific. Never scan a known foreign vacancy
    # for Moroccan city names because ordinary words can contain strings such as "fes".
    if not location and is_morocco_source:
        location = _city_from_text(body)
    country_code = "MA" if is_morocco_source else (
        structured_country if source_country == "GLOBAL" else source_country or structured_country
    )
    eligibility = source_eligibility
    if unicef_node:
        eligibility = unicef_node["_eligibility"]
    if not eligibility and country_code == "MA":
        eligibility = "morocco"

    employment = node.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(_text(x) for x in employment if _text(x))

    parsed_page = urlparse(str(page_url or "").strip())
    is_ofppt_page = parsed_page.scheme == "https" and parsed_page.hostname == "recrutement.ofppt.ma" and bool(re.fullmatch(r"/offre/\d+/?", parsed_page.path, flags=re.I))
    ofppt_documents = ofppt_official_document_links(soup, page_url) if is_ofppt_page else []
    ofppt_source_verified = bool(article.get("official_source") or article.get("job_official_source")) and bool(ofppt_documents)
    notice_type = _notice_type(job_title, body)
    ats_provider = str(article.get("ats_provider") or "").strip().lower()
    if notice_type == "vacancy" and (ats_provider == "emploi_public" or ofppt_source_verified):
        notice_type = "competition"
    # A verified OFPPT offer PDF makes competition-specific evidence mandatory.
    notice_type_source = (
        "source"
        if bool(article.get("official_source") or article.get("job_official_source"))
        and (ats_provider == "emploi_public" or ofppt_source_verified)
        else "heuristic"
    )

    # Active vacancy/competition notices routinely mention the future publication
    # of provisional/final results in legal boilerplate. Those words describe a
    # later stage, not the current page status. Only result/list/update notices use
    # provisional/final status inference.
    notice_status = (
        ""
        if notice_type in {"vacancy", "competition"}
        else _notice_status(job_title, body)
    )

    action_links = _extract_job_action_links(soup, page_url, full_text=body)
    binding_article = dict(article)
    binding_article["canonical_url"] = page_url
    binding_article["url"] = page_url
    binding_article["job_detail_url"] = page_url
    binding_article["job_notice_type"] = notice_type
    binding_article["job_action_links"] = action_links
    direct_apply = next(
        (
            row
            for row in action_links
            if row.get("kind") == "apply"
            and is_application_url_bound_to_job(binding_article, row.get("url"))
        ),
        None,
    )
    documents = ofppt_documents if is_ofppt_page else [row for row in action_links if row.get("kind") == "document"]
    structured_url = _text(node.get("url"))

    application_candidates = [
        ("direct_apply", (direct_apply or {}).get("url")),
        ("official_job_page", article.get("application_url")),
        ("official_job_page", structured_url),
        ("official_job_page", page_url),
    ]
    application_kind = ""
    application_url = ""
    for candidate_kind, candidate_url in application_candidates:
        candidate_url = str(candidate_url or "").strip()
        if candidate_url and is_application_url_bound_to_job(binding_article, candidate_url):
            application_url = candidate_url
            application_kind = (
                candidate_kind
                if is_job_specific_url(candidate_url)
                else "official_application_channel"
            )
            break
    structured_deadline = _text(node.get("validThrough"))
    # Some official portals (notably Emploi-Public) keep labelled metadata
    # outside the content container selected as the article body. Read only
    # labelled date facts from the complete official page evidence so freshness
    # and deadlines are not lost, while keeping the clean body for article copy.
    text_deadline, text_deadline_display = _deadline_details_from_text(labelled_evidence)
    deadline = structured_deadline or text_deadline
    deadline_display = text_deadline_display or structured_deadline
    text_exam_date, text_exam_date_display = _exam_date_details_from_text(labelled_evidence)
    text_published_at, text_published_display = _publication_date_details_from_text(labelled_evidence)
    published_at = (
        _text(node.get("datePosted"))
        or article.get("source_published_at", "")
        or text_published_at
    )
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
        "job_exam_date": text_exam_date,
        "job_exam_date_display": text_exam_date_display,
        "job_notice_type": notice_type,
        "job_notice_type_source": notice_type_source,
        "job_notice_status": notice_status,
        "job_published_at": published_at,
        "job_published_at_display": text_published_display,
        "job_application_url": application_url,
        "job_application_link_kind": application_kind,
        "job_application_is_specific": bool(application_url and is_job_specific_url(application_url)),
        "job_application_is_official_channel": application_kind == "official_application_channel",
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
    if unicef_node:
        fields["unicef_detail_extraction_version"] = 1
    return {key: value for key, value in fields.items() if value not in (None, "")}
