from __future__ import annotations

import json
import re
from bs4 import BeautifulSoup


def _plain(value):
    return re.sub(r"\s+", " ", BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)).strip()


def build_jobposting(article, article_url):
    data = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": article.get("job_title") or article.get("seo_title") or article.get("title", ""),
        "description": _plain(article.get("job_description") or article.get("final_html", "")),
        "datePosted": str(article.get("job_published_at") or article.get("source_published_at") or "")[:10],
        "validThrough": str(article.get("job_deadline") or "")[:10],
        "employmentType": article.get("job_contract_type") or "",
        "url": article_url,
        "hiringOrganization": {
            "@type": "Organization",
            "name": article.get("job_company") or article.get("source_name") or "Employer",
        },
    }
    logo = str(article.get("company_logo_url") or "").strip()
    if article.get("company_logo_verified") and logo:
        data["hiringOrganization"]["logo"] = logo

    location = str(article.get("job_location") or "").strip()
    country = str(article.get("job_country") or "").strip()
    if article.get("job_remote"):
        data["jobLocationType"] = "TELECOMMUTE"
    elif location or country:
        data["jobLocation"] = {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": location,
                "addressCountry": country or "MA",
            },
        }

    salary = str(article.get("job_salary") or "").strip()
    if salary:
        data["baseSalary"] = {
            "@type": "MonetaryAmount",
            "currency": "MAD" if "MAD" in salary.upper() or country == "MA" else "",
            "value": {"@type": "QuantitativeValue", "value": salary},
        }

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
    notice_type = str(article.get("job_notice_type") or "vacancy").strip().lower()
    if notice_type != "vacancy":
        # Candidate lists/results are editorial updates, not active job vacancies.
        # Keep the page's normal BlogPosting schema and avoid misleading JobPosting markup.
        return html.rstrip()
    payload = json.dumps(build_jobposting(article, article_url), ensure_ascii=False, separators=(",", ":"))
    return html.rstrip() + f'\n<script type="application/ld+json" id="jobs-jobposting">{payload}</script>'
