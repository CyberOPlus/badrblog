from __future__ import annotations

import json
import re
from bs4 import BeautifulSoup


NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,:/-]\d+)*%?(?![A-Za-z0-9])")
URL_RE = re.compile(r"https?://[^\s<>'\"]+")


def _numbers(value):
    return set(NUMBER_RE.findall(str(value or "")))


def _source_blob(candidate):
    return json.dumps(candidate.to_dict(), ensure_ascii=False, sort_keys=True)


def validate_generated_facts(candidate, data):
    """Reject obvious AI hallucinations without trying to understand the prose.

    Every visible numeric token in title/meta/card/body must already exist
    somewhere in the normalized source package. This catches invented salaries,
    years of experience, vacancy counts and dates.
    """
    source = _source_blob(candidate)
    allowed_numbers = _numbers(source)

    combined = " ".join([
        str(data.get("title") or ""),
        str(data.get("seo_title") or ""),
        str(data.get("meta_description") or ""),
        BeautifulSoup(str(data.get("html") or ""), "html.parser").get_text(" ", strip=True),
        str(data.get("card_title") or ""),
    ])

    generated_numbers = _numbers(combined)
    invented = sorted(generated_numbers - allowed_numbers)
    if invented:
        raise ValueError("AI introduced unsupported numeric facts: " + ", ".join(invented))

    application_url = str(candidate.application_url or "").strip()
    if application_url:
        html = str(data.get("html") or "")
        if application_url not in html:
            raise ValueError("official application URL missing from generated article")
        if html.count(application_url) != 1:
            raise ValueError("official application URL must appear exactly once")

    company = str(candidate.company or "").strip()
    visible = combined.casefold()
    if company and company.casefold() not in visible:
        raise ValueError("company name disappeared from generated output")

    return True
