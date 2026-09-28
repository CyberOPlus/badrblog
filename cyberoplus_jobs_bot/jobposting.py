from __future__ import annotations

import json
import re
from html import unescape

from bs4 import BeautifulSoup


EMPLOYMENT_TYPE_MAP = {
    "full time": "FULL_TIME",
    "full-time": "FULL_TIME",
    "temps plein": "FULL_TIME",
    "cdi": "FULL_TIME",
    "part time": "PART_TIME",
    "part-time": "PART_TIME",
    "temps partiel": "PART_TIME",
    "contract": "CONTRACTOR",
    "contractor": "CONTRACTOR",
    "freelance": "CONTRACTOR",
    "intern": "INTERN",
    "internship": "INTERN",
    "stage": "INTERN",
    "temporary": "TEMPORARY",
    "temporaire": "TEMPORARY",
}


def _text(html):
    return BeautifulSoup(str(html or ""), "html.parser").get_text(" ", strip=True)


def _date_only(value):
    text = str(value or "").strip()
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else ""


def _employment_type(value):
    lowered = str(value or "").strip().casefold()
    for needle, schema_value in EMPLOYMENT_TYPE_MAP.items():
        if needle in lowered:
            return schema_value
    return ""


def build_jobposting(candidate, article, article_url):
    """Build schema.org JobPosting JSON-LD only for a single verified job."""
    if str(candidate.listing_kind or "single_job") != "single_job":
        return {}

    data = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": candidate.title,
        "description": article.html,
    }

    if candidate.company:
        data["hiringOrganization"] = {
            "@type": "Organization",
            "name": candidate.company,
        }

    if article_url and str(article_url).startswith(("http://", "https://")):
        data["url"] = article_url

    date_posted = _date_only(candidate.published_at)
    if date_posted:
        data["datePosted"] = date_posted

    valid_through = _date_only(candidate.deadline)
    if valid_through:
        data["validThrough"] = valid_through

    employment_type = _employment_type(candidate.contract_type)
    if employment_type:
        data["employmentType"] = employment_type

    workplace = str(candidate.workplace_type or "").strip().lower()
    if workplace == "remote" and candidate.eligibility == "remote_morocco":
        data["jobLocationType"] = "TELECOMMUTE"
        data["applicantLocationRequirements"] = {
            "@type": "Country",
            "name": "Morocco",
        }
    else:
        locations = list(candidate.locations or [])
        if candidate.location and candidate.location not in locations:
            locations.insert(0, candidate.location)
        places = [
            {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": place,
                    "addressCountry": candidate.country or "MA",
                },
            }
            for place in locations if str(place).strip()
        ]
        if len(places) == 1:
            data["jobLocation"] = places[0]
        elif places:
            data["jobLocation"] = places

    return data


def json_ld_script(candidate, article, article_url):
    payload = build_jobposting(candidate, article, article_url)
    if not payload:
        return ""
    return (
        "<script type='application/ld+json'>"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "</script>"
    )
