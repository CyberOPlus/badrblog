from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import fitz
import requests
from PIL import Image

from production_logging import log_event


PDF_HINTS = (
    "pdf", "avis", "conditions", "condition", "reglement", "règlement",
    "dossier", "fiche", "communique", "communiqué", "descriptif",
    "announcement", "notice", "decision", "description", "download",
    "إعلان", "الاعلان", "الإعلان", "شروط", "الشروط", "قرار", "مقرر",
    "ملف", "وثيقة", "الوثيقة", "تحميل",
)
RESULT_HINTS = (
    "result", "résultat", "resultat", "liste", "list", "shortlist",
    "convoque", "convoqué", "admis", "النتائج", "النتيجة", "اللائحة",
    "اللوائح", "المدعوين",
)
TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid",
}


def _clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _canonical_key(url):
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in TRACKING_KEYS
    ]
    return urlunparse(
        parsed._replace(
            scheme=parsed.scheme.casefold(),
            netloc=parsed.netloc.casefold(),
            query=urlencode(query, doseq=True),
            fragment="",
        )
    ).rstrip("/")


def _document_should_render(item):
    if not isinstance(item, dict):
        return False
    url = str(item.get("url") or "").strip()
    if not _canonical_key(url):
        return False
    signature = " ".join(
        [
            str(item.get("label") or ""),
            str(item.get("context") or ""),
            url,
        ]
    ).casefold()
    path = urlparse(url).path.casefold()
    if path.endswith(".pdf"):
        # Lists/results are useful as links, but rendering them can create dozens
        # of low-value pages. Render condition/notice PDFs and generic PDFs only.
        if any(hint in signature for hint in RESULT_HINTS) and not any(
            hint in signature for hint in PDF_HINTS if hint not in {"pdf", "تحميل"}
        ):
            return False
        return True
    return any(hint in signature for hint in PDF_HINTS)


def _download_pdf(url, timeout=20, max_bytes=25 * 1024 * 1024):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; CyberoPlusJobs/1.0; "
            "+https://www.cyberoplus.com/)"
        ),
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.5",
    }
    response = requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
    response.raise_for_status()
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=256 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("official PDF exceeds render size limit")
        chunks.append(chunk)
    payload = b"".join(chunks)
    content_type = str(response.headers.get("Content-Type") or "").casefold()
    if not payload.startswith(b"%PDF") and "pdf" not in content_type:
        raise ValueError("official document is not a PDF")
    return payload


def _safe_segment(value):
    cleaned = re.sub(r"[^a-z0-9-]+", "-", str(value or "").casefold())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned[:80] or "job"


def render_job_document_pages(
    article,
    *,
    output_root="assets/generated/job-documents",
    raw_base="https://raw.githubusercontent.com/CyberOPlus/badrblog/main",
    max_documents=3,
    max_total_pages=24,
):
    """Render verified vacancy-condition PDFs into sequential JPEG pages.

    The original official URLs remain the primary source links. Rendering is a
    convenience layer for readers and never replaces the official document.
    """
    if str(article.get("job_notice_type") or "vacancy").strip().lower() != "vacancy":
        return []

    documents = article.get("job_document_links") or []
    eligible = []
    seen = set()
    for item in documents:
        if not _document_should_render(item):
            continue
        key = _canonical_key(item.get("url"))
        if not key or key in seen:
            continue
        seen.add(key)
        eligible.append(item)
        if len(eligible) >= max_documents:
            break

    if not eligible:
        return []

    job_key = _safe_segment(
        article.get("seo_slug")
        or article.get("desired_slug")
        or article.get("job_campaign_id")
        or article.get("job_title")
    )
    root = Path(output_root) / job_key
    root.mkdir(parents=True, exist_ok=True)

    rendered = []
    total_pages = 0
    for document_index, item in enumerate(eligible, start=1):
        if total_pages >= max_total_pages:
            break
        url = str(item.get("url") or "").strip()
        label = _clean_text(item.get("label") or item.get("context") or f"الوثيقة الرسمية {document_index}")
        try:
            payload = _download_pdf(url)
            document = fitz.open(stream=payload, filetype="pdf")
        except Exception as error:
            log_event(
                "job_document_render_skipped",
                article_id=article.get("id"),
                url=url,
                error=str(error),
            )
            continue

        digest = hashlib.sha256(_canonical_key(url).encode("utf-8")).hexdigest()[:10]
        document_page_count = document.page_count
        available = max_total_pages - total_pages
        page_limit = min(document_page_count, available)
        for page_index in range(page_limit):
            page = document.load_page(page_index)
            # A moderate scale keeps Arabic/French conditions readable on phones
            # without turning every article into a multi-megabyte payload.
            pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
            mode = "RGB" if pix.n < 4 else "RGBA"
            image = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            if image.mode != "RGB":
                image = image.convert("RGB")
            filename = f"doc-{document_index:02d}-{digest}-page-{page_index + 1:02d}.jpg"
            path = root / filename
            image.save(path, format="JPEG", quality=84, optimize=True, progressive=True)
            public_url = raw_base.rstrip("/") + "/" + path.as_posix()
            rendered.append(
                {
                    "document_url": url,
                    "document_label": label,
                    "page_number": page_index + 1,
                    "page_count": document_page_count,
                    "url": public_url,
                    "path": path.as_posix(),
                    "alt": f"{label} — الصفحة {page_index + 1}",
                }
            )
            total_pages += 1
        document.close()

        if page_limit < document_page_count:
            article["job_document_pages_truncated"] = True
            break

    article["job_document_page_images"] = rendered
    article["job_document_rendered_pages"] = len(rendered)
    return rendered
