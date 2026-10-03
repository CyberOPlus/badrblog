"""Read public agency/ATS vacancy facts without inventing posting dates."""

import html
import json
import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup


ANAPEC_HOSTS = {"anapec.org", "www.anapec.org", "anapec.ma", "www.anapec.ma"}


def anapec_offer_id(url):
    parsed = urlparse(url)
    if parsed.hostname not in ANAPEC_HOSTS:
        return ""
    match = re.fullmatch(r"/sigec-app-rv/(?:fr/|ar/)?entreprises/bloc_offre_home/(\d+)(?:/resultat_recherche)?/?", parsed.path)
    return match.group(1) if match else ""


def _labelled_publication_date(text, header=False):
    labels = r"date\s+(?:de\s+)?(?:publication|diffusion|l['’]offre)|publi[ée]+\s+le"
    if header:
        labels += r"|(?<!\w)date"
    match = re.search(rf"(?i)(?:{labels})\s*[:：]?\s*(\d{{1,2}}/\d{{1,2}}/\d{{4}})", text)
    if not match:
        return ""
    try:
        return datetime.strptime(match.group(1), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return ""


def parse_anapec_links(document, source_url, per_source_limit=None):
    soup = BeautifulSoup(document or "", "html.parser")
    rows, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        url = urljoin(source_url, anchor["href"])
        identity = anapec_offer_id(url)
        title = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
        if not identity or identity in seen or len(title) < 5:
            continue
        row = {"title": title, "url": url, "ats_provider": "anapec",
               "ats_reference": identity}
        for parent in list(anchor.parents)[:6]:
            ids = {anapec_offer_id(urljoin(source_url, a["href"]))
                   for a in parent.find_all("a", href=True)} - {""}
            if len(ids) > 1:
                break
            date = _labelled_publication_date(parent.get_text(" ", strip=True))
            if date:
                row.update(source_published_at=date, published_at_source="official_listing")
                break
        rows.append(row)
        seen.add(identity)
        if len(rows) >= int(per_source_limit or 500):
            break
    # A sign-in page or redesigned portal is not evidence of zero vacancies.
    if not rows and not re.search(r"(?i)aucune\s+offre|0\s+offres?\s+(?:trouv|disponible)", soup.get_text(" ", strip=True)):
        raise ValueError("ANAPEC listing contains no recognized official offer links")
    return rows


def anapec_detail_html(document, article, url):
    if not anapec_offer_id(url):
        raise ValueError("Not an official ANAPEC vacancy detail URL")
    soup = BeautifulSoup(document or "", "html.parser")
    text = soup.get_text("\n", strip=True)
    reference = re.search(r"(?i)r[ée]f[ée]rence\s+de\s+l['’]offre\s*[:：]?\s*([A-Z]{1,6}\d[A-Z0-9-]{4,}|\d{5,})", text)
    header = text[reference.start():reference.start()+600] if reference else ""
    header = re.split(r"(?i)description\s+(?:de\s+l['’]entreprise|de\s+poste)|profil\s+recherch", header)[0]
    date = _labelled_publication_date(header, header=True) or article.get("source_published_at", "")
    company = re.search(r"(?im)^\s*(?:entreprise|employeur)\s*[:：]\s*([^\n]+)", text)
    node = {"@context": "https://schema.org", "@type": "JobPosting",
            "title": article.get("title", ""), "url": url,
            "identifier": reference.group(1) if reference else anapec_offer_id(url)}
    if date:
        node["datePosted"] = date
    if company:
        node["hiringOrganization"] = {"@type": "Organization", "name": company.group(1).strip()}
    encoded = json.dumps(node, ensure_ascii=False).replace("<", "\\u003c")
    return f'<script type="application/ld+json">{encoded}</script>{document}'


def smartrecruiters_listing_rows(payload, source_url):
    parsed = urlparse(source_url)
    match = re.fullmatch(r"/v1/companies/([A-Za-z0-9_-]+)/postings/?", parsed.path)
    if parsed.hostname != "api.smartrecruiters.com" or not match:
        raise ValueError("Invalid public SmartRecruiters listing")
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
        raise ValueError("Invalid public SmartRecruiters listing response")
    country = parse_qs(parsed.query).get("country", [""])[0].casefold()
    base = f"https://api.smartrecruiters.com/v1/companies/{match.group(1)}/postings"
    rows = []
    for job in payload["content"]:
        identity = str(job.get("id") or "")
        if not identity.isdigit() or not str(job.get("name") or "").strip():
            continue
        location = job.get("location") or {}
        if country and str(location.get("country") or "").casefold() != country:
            continue
        rows.append({"title": job["name"], "url": f"{base}/{identity}",
                     "ats_provider": "smartrecruiters", "ats_reference": identity,
                     "source_published_at": job.get("releasedDate", ""),
                     "published_at_source": "official_ats", "job_location": location.get("city", ""),
                     "job_country": location.get("country", ""),
                     "job_company": (job.get("company") or {}).get("name", "")})
    return rows


def smartrecruiters_detail_html(document, article, url):
    payload = json.loads(document)
    expected = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    if str(payload.get("id") or "") != expected:
        raise ValueError("SmartRecruiters detail does not match the selected posting")
    sections = (payload.get("jobAd") or {}).get("sections") or {}
    body = "".join(f'<h2>{html.escape(str(section.get("title") or key))}</h2>{section.get("text") or ""}'
                   for key, section in sections.items() if isinstance(section, dict))
    location = payload.get("location") or {}
    node = {"@context": "https://schema.org", "@type": "JobPosting", "title": payload.get("name", ""),
            "datePosted": payload.get("releasedDate", ""),
            "identifier": payload.get("refNumber") or expected,
            "hiringOrganization": {"@type": "Organization", "name": (payload.get("company") or {}).get("name", "")},
            "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress",
                "addressLocality": location.get("city", ""), "addressCountry": location.get("country", "")}},
            "employmentType": (payload.get("typeOfEmployment") or {}).get("label", ""),
            "url": payload.get("postingUrl") or url}
    encoded = json.dumps(node, ensure_ascii=False).replace("<", "\\u003c")
    return (f'<html><head><title>{html.escape(payload.get("name", ""))}</title>'
            f'<script type="application/ld+json">{encoded}</script></head><body><article>'
            f'<h1>{html.escape(payload.get("name", ""))}</h1>{body}</article></body></html>')
