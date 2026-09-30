from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

_EMPLOYMENT_TYPE_MAP = (
    (("full time", "full-time", "temps plein", "دوام كامل"), "FULL_TIME"),
    (("part time", "part-time", "temps partiel", "دوام جزئي"), "PART_TIME"),
    (("internship", "intern", "stage", "stagiaire", "تدريب"), "INTERN"),
    (("temporary", "temporaire", "intérim", "interim", "cdd", "مؤقت"), "TEMPORARY"),
    (("contractor", "freelance", "consultant indépendant", "independent contractor"), "CONTRACTOR"),
    (("volunteer", "bénévole", "benevole", "تطوع"), "VOLUNTEER"),
    (("per diem", "journalier", "يومي"), "PER_DIEM"),
)

_CURRENCY_HINTS = (
    (("mad", "dhs", "dh", "درهم", "د.م"), "MAD"),
    (("eur", "€", "euro"), "EUR"),
    (("usd", "$", "dollar"), "USD"),
    (("gbp", "£", "pound"), "GBP"),
)

_UNIT_HINTS = (
    (("per year", "yearly", "annual", "annuel", "par an", "سنوي", "سنويا"), "YEAR"),
    (("per month", "monthly", "mensuel", "par mois", "شهري", "شهريا"), "MONTH"),
    (("per week", "weekly", "hebdomadaire", "par semaine", "أسبوعي", "اسبوعي"), "WEEK"),
    (("per day", "daily", "journalier", "par jour", "يومي"), "DAY"),
    (("per hour", "hourly", "horaire", "par heure", "بالساعة", "ساعة"), "HOUR"),
)


def _plain(value):
    return re.sub(
        r"\s+",
        " ",
        BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True),
    ).strip()


def _public_http(url):
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _date_value(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    normalized = text.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(normalized)
        return text
    except ValueError:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
        return match.group(1) if match else ""


def _employment_type(value):
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    known = {
        "FULL_TIME",
        "PART_TIME",
        "CONTRACTOR",
        "TEMPORARY",
        "INTERN",
        "VOLUNTEER",
        "PER_DIEM",
        "OTHER",
    }
    upper = text.upper().replace("-", "_").replace(" ", "_")
    if upper in known:
        return upper
    for hints, schema_value in _EMPLOYMENT_TYPE_MAP:
        if any(hint in text for hint in hints):
            return schema_value
    return ""


def _currency(value, explicit=""):
    explicit = str(explicit or "").strip().upper()
    if re.fullmatch(r"[A-Z]{3}", explicit):
        return explicit
    text = str(value or "").casefold()
    for hints, currency in _CURRENCY_HINTS:
        if any(hint.casefold() in text for hint in hints):
            return currency
    return ""


def _salary_numbers(value):
    text = str(value or "").translate(_ARABIC_DIGITS)
    raw_values = re.findall(r"(?<![\w])\d[\d\s.,]*(?![\w])", text)
    numbers = []
    for raw in raw_values:
        token = re.sub(r"\s+", "", raw)
        if not token:
            continue
        if token.count(",") == 1 and token.count(".") == 0:
            left, right = token.split(",")
            token = left + ("." + right if len(right) <= 2 else right)
        elif token.count(".") == 1 and token.count(",") == 0:
            left, right = token.split(".")
            token = left + ("." + right if len(right) <= 2 else right)
        else:
            token = token.replace(",", "").replace(".", "")
        try:
            number = float(token)
        except ValueError:
            continue
        if number <= 0:
            continue
        numbers.append(int(number) if number.is_integer() else number)
    return numbers[:2]


def _salary_schema(article):
    salary = str(article.get("job_salary") or "").strip()
    if not salary:
        return None
    currency = _currency(salary, article.get("job_salary_currency"))
    numbers = _salary_numbers(salary)
    if not currency or not numbers:
        return None

    lower = salary.casefold()
    unit_text = ""
    for hints, unit in _UNIT_HINTS:
        if any(hint.casefold() in lower for hint in hints):
            unit_text = unit
            break

    value = {"@type": "QuantitativeValue"}
    if len(numbers) >= 2 and numbers[0] != numbers[1]:
        value["minValue"] = min(numbers[0], numbers[1])
        value["maxValue"] = max(numbers[0], numbers[1])
    else:
        value["value"] = numbers[0]
    if unit_text:
        value["unitText"] = unit_text

    return {
        "@type": "MonetaryAmount",
        "currency": currency,
        "value": value,
    }


def jobposting_validation_errors(article):
    notice_type = str(article.get("job_notice_type") or "vacancy").strip().lower()
    if notice_type != "vacancy":
        return ["notice is not an active vacancy"]

    errors = []
    title = str(article.get("job_title") or article.get("seo_title") or article.get("title") or "").strip()
    company = str(article.get("job_company") or "").strip()
    description = _plain(article.get("job_description") or article.get("final_html") or "")
    date_posted = _date_value(article.get("job_published_at") or article.get("source_published_at"))
    location = str(article.get("job_location") or "").strip()
    country = str(article.get("job_country") or "").strip()
    remote = bool(article.get("job_remote"))

    if not title:
        errors.append("missing job title")
    if not company:
        errors.append("missing hiring organization")
    if not description:
        errors.append("missing job description")
    if not date_posted:
        errors.append("missing datePosted")
    if remote:
        if not country:
            errors.append("remote job missing applicant country")
    elif not (location and country):
        errors.append("missing job location/country")
    return errors


def build_jobposting(article, article_url):
    data = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": article.get("job_title") or article.get("seo_title") or article.get("title", ""),
        "description": _plain(article.get("job_description") or article.get("final_html", "")),
        "datePosted": _date_value(article.get("job_published_at") or article.get("source_published_at")),
        "url": article_url,
        "hiringOrganization": {
            "@type": "Organization",
            "name": article.get("job_company") or "",
        },
    }

    deadline = _date_value(article.get("job_deadline"))
    if deadline:
        data["validThrough"] = deadline

    employment_type = _employment_type(article.get("job_contract_type"))
    if employment_type:
        data["employmentType"] = employment_type

    logo = str(article.get("company_logo_url") or "").strip()
    if article.get("company_logo_verified") and _public_http(logo):
        data["hiringOrganization"]["logo"] = logo

    company_url = str(
        article.get("company_official_url")
        or article.get("job_company_url")
        or article.get("company_website")
        or ""
    ).strip()
    if _public_http(company_url):
        data["hiringOrganization"]["sameAs"] = company_url

    reference = str(
        article.get("job_external_reference")
        or article.get("ats_reference")
        or ""
    ).strip()
    if reference:
        data["identifier"] = {
            "@type": "PropertyValue",
            "name": article.get("job_company") or "",
            "value": reference,
        }

    location = str(article.get("job_location") or "").strip()
    country = str(article.get("job_country") or "").strip()
    if article.get("job_remote"):
        data["jobLocationType"] = "TELECOMMUTE"
        if country:
            data["applicantLocationRequirements"] = {
                "@type": "Country",
                "name": country,
            }
    elif location or country:
        data["jobLocation"] = {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": location,
                "addressCountry": country,
            },
        }

    salary_schema = _salary_schema(article)
    if salary_schema:
        data["baseSalary"] = salary_schema

    application_url = str(article.get("job_application_url") or "").strip()
    if (
        application_url
        and _public_http(application_url)
        and str(article.get("job_application_link_kind") or "").strip().lower() == "direct_apply"
    ):
        data["directApply"] = True

    return _clean(data)


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_clean(v) for v in value if v not in (None, "", [], {})]
    return value


def append_jobposting(html, article, article_url):
    html = re.sub(
        r'<script[^>]*id=["\'][^"\']*jobposting["\'][^>]*>.*?</script>',
        "",
        str(html or ""),
        flags=re.I | re.S,
    )
    if jobposting_validation_errors(article):
        return html.rstrip()
    payload = json.dumps(
        build_jobposting(article, article_url),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return html.rstrip() + f'\n<script type="application/ld+json" id="jobs-jobposting">{payload}</script>'
