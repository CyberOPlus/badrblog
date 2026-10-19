"""Read public agency/ATS vacancy facts without inventing posting dates."""

import html
import json
import re
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup


ANAPEC_HOSTS = {"anapec.org", "www.anapec.org", "anapec.ma", "www.anapec.ma"}
OFPPT_HOST = "recrutement.ofppt.ma"


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


def ofppt_offer_id(url):
    """Return the numeric OFPPT offer id for an official detail URL."""
    parsed = urlparse(str(url or ""))
    if (parsed.hostname or "").casefold() != OFPPT_HOST:
        return ""
    match = re.fullmatch(r"/offre/(\d+)/?", parsed.path)
    return match.group(1) if match else ""


def _ofppt_date_iso(value):
    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", str(value or ""))
    if not match:
        return ""
    day, month, year = match.groups()
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except (TypeError, ValueError):
        return ""


def _ofppt_clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _ofppt_title(value):
    text = _ofppt_clean(value)
    return re.sub(r"\s*\(\s*expir[ée]e?\s*\)\s*$", "", text, flags=re.I).strip()


def ofppt_listing_row(card, source_url, now=None):
    """Read one OFPPT listing card, rejecting expired or malformed notices."""
    if getattr(card, "name", None) is None:
        return None
    anchor = card.find("a", href=re.compile(r"/offre/\d+", re.I))
    if anchor is None:
        return None
    url = urljoin(source_url, str(anchor.get("href") or ""))
    if not ofppt_offer_id(url):
        return None
    title = _ofppt_title(anchor.get_text(" ", strip=True))
    if len(title) < 5:
        return None

    card_text = _ofppt_clean(card.get_text(" ", strip=True))
    reference_match = re.search(
        r"R[ée]f[ée]rence\s*:\s*(.+?)(?=\s+Date\s+d[’']?\s*expiration|\s*$)",
        card_text,
        re.I,
    )
    reference = _ofppt_clean(reference_match.group(1)) if reference_match else ""
    deadline_match = re.search(
        r"(?:Date\s+d[’']?\s*expiration|Dernier\s+D[ée]lai)\s*:\s*([0-9][0-9./-]{6,})",
        card_text,
        re.I,
    )
    deadline_display = deadline_match.group(1).strip() if deadline_match else ""
    deadline = _ofppt_date_iso(deadline_display)
    current = now or datetime.now(timezone.utc)
    today = current.astimezone(timezone.utc).date() if current.tzinfo else current.date()
    expired = bool(re.search(r"expir[ée]e?", card_text, re.I))
    if deadline:
        try:
            expired = expired or date.fromisoformat(deadline) < today
        except ValueError:
            pass
    if expired:
        return None

    row = {"title": title, "url": url}
    if reference:
        row["job_external_reference"] = reference
        row["ats_reference"] = reference
    if deadline:
        row["job_deadline"] = deadline
        row["job_deadline_display"] = deadline_display
    row["job_notice_status"] = "active"
    return row


def ofppt_detail_evidence(document, article, url):
    """Extract the verified facts OFPPT exposes on its public detail page."""
    offer_id = ofppt_offer_id(url)
    if not offer_id:
        raise ValueError("Not an official OFPPT vacancy detail URL")
    soup = BeautifulSoup(document or "", "html.parser")
    container = soup.select_one("#offer-container") or soup
    heading = container.find("h1")
    heading_text = _ofppt_clean(heading.get_text(" ", strip=True) if heading else "")
    title = _ofppt_title(heading_text)

    info = {}
    table = container.select_one("#offer-infos") or container.find("table")
    if table is not None:
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = _ofppt_clean(cells[0].get_text(" ", strip=True)).casefold().rstrip(" :：")
            value = _ofppt_clean(cells[1].get_text(" ", strip=True))
            if label and value:
                info[label] = value

    reference = (
        info.get("référence")
        or info.get("reference")
        or info.get("المرجع")
        or ""
    )
    poste = info.get("poste") or info.get("المنصب") or title
    deadline_display = (
        info.get("dernier délai")
        or info.get("dernier delai")
        or info.get("date d'expiration")
        or info.get("date d’expiration")
        or ""
    )
    deadline = _ofppt_date_iso(deadline_display)
    current = datetime.now(timezone.utc)
    expired = bool(re.search(r"expir[ée]e?", heading_text, re.I))
    if deadline:
        try:
            expired = expired or date.fromisoformat(deadline) < current.date()
        except ValueError:
            pass

    documents = []
    seen = set()
    for anchor in container.find_all("a", href=True):
        href = urljoin(url, str(anchor.get("href") or "").strip())
        parsed = urlparse(href)
        if parsed.hostname not in {OFPPT_HOST, "www.ofppt.ma", "ofppt.ma"}:
            continue
        if not parsed.path.casefold().endswith(".pdf"):
            continue
        key = href.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        documents.append(
            {
                "url": href,
                "label": _ofppt_clean(anchor.get_text(" ", strip=True)) or "Avis de concours",
                "kind": "document",
            }
        )

    application_url = (
        f"https://{OFPPT_HOST}/offre/{offer_id}/postuler" if not expired else ""
    )
    summary_parts = [title or poste]
    if reference:
        summary_parts.append(f"Référence: {reference}")
    if poste and poste != title:
        summary_parts.append(f"Poste: {poste}")
    if deadline_display:
        summary_parts.append(f"Dernier délai: {deadline_display}")
    summary_parts.append("Statut: " + ("Expirée" if expired else "Active"))

    fields = {
        "job_title": title or poste,
        "job_company": "OFPPT",
        "job_country": "MA",
        "job_deadline": deadline,
        "job_deadline_display": deadline_display,
        "job_external_reference": reference,
        "job_notice_status": "expired" if expired else "active",
        "job_notice_type": "vacancy",
        "job_application_url": application_url,
        "job_application_link_kind": "direct_apply" if application_url else "",
        "job_application_is_specific": bool(application_url),
        "job_application_requires_registration": bool(application_url),
        "job_application_source": "official_detail_page",
        "job_document_links": documents,
        "job_official_source": True,
        "official_source": True,
    }
    return {
        "offer_id": offer_id,
        "title": title or poste,
        "reference": reference,
        "deadline": deadline,
        "deadline_display": deadline_display,
        "expired": expired,
        "documents": documents,
        "fields": {key: value for key, value in fields.items() if value not in (None, "", [])},
        "summary": "\n".join(summary_parts),
    }


def ofppt_detail_html(document, article, url, evidence=None, document_texts=None):
    """Build a clean article from OFPPT's table and official notice PDF text."""
    evidence = evidence or ofppt_detail_evidence(document, article, url)
    fields = evidence.get("fields") or {}
    parts = [f'<article class="job-detail"><h1>{html.escape(evidence.get("title") or "")}</h1>']
    for label, value in (
        ("المرجع", evidence.get("reference")),
        ("المنصب", fields.get("job_title")),
        ("آخر أجل", evidence.get("deadline_display")),
        ("الحالة", "منتهية" if evidence.get("expired") else "مفتوحة"),
    ):
        if value:
            parts.append(f"<p><strong>{html.escape(label)}:</strong> {html.escape(str(value))}</p>")

    documents = evidence.get("documents") or []
    if documents:
        parts.append("<h2>الوثائق الرسمية</h2><ul>")
        for item in documents:
            parts.append(
                f'<li><a href="{html.escape(str(item.get("url") or ""))}">'
                f'{html.escape(str(item.get("label") or "الإعلان الرسمي"))}</a></li>'
            )
        parts.append("</ul>")

    page_texts = document_texts
    if page_texts is None:
        page_texts = article.get("job_document_texts") or []
    chunks = []
    for page in page_texts:
        if isinstance(page, dict):
            value = str(page.get("text") or "").strip()
            if value:
                chunks.append(value)
    if chunks:
        parts.append("<h2>نص الإعلان الرسمي</h2>")
        for block in re.split(r"\n\s*\n|\r\n\s*\r\n", "\n\n".join(chunks)):
            clean = _ofppt_clean(block)
            if clean:
                parts.append(f"<p>{html.escape(clean)}</p>")

    parts.append("</article>")
    return "\n".join(parts)


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
