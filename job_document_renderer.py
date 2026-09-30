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


IDENTITY_DOCUMENT_HINTS = (
    "avis", "announcement", "notice", "decision", "conditions", "condition",
    "concours", "descriptif", "description", "fiche de poste",
    "إعلان", "الاعلان", "الإعلان", "قرار", "مقرر", "شروط", "المباراة",
)


def _document_identity_priority(item, article):
    signature = " ".join(
        [
            str((item or {}).get("label") or ""),
            str((item or {}).get("context") or ""),
            str((item or {}).get("url") or ""),
        ]
    ).casefold()
    notice_type = str((article or {}).get("job_notice_type") or "").strip().lower()
    result_notice = notice_type in {"candidate_list", "results", "final_results"}

    score = 0
    if any(hint in signature for hint in IDENTITY_DOCUMENT_HINTS):
        score += 8
    if any(hint in signature for hint in RESULT_HINTS):
        score += 12 if result_notice else -5
    if str((item or {}).get("url") or "").casefold().split("?", 1)[0].endswith(".pdf"):
        score += 1
    return score


def _eligible_documents(article, max_documents=3):
    documents = article.get("job_document_links") or []
    candidates = []
    seen = set()
    for index, item in enumerate(documents):
        if not _document_should_render(item):
            continue
        key = _canonical_key(item.get("url"))
        if not key or key in seen:
            continue
        seen.add(key)
        candidates.append(
            (_document_identity_priority(item, article), -index, item)
        )

    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    limit = max(1, int(max_documents or 1))
    return [item for _priority, _index, item in candidates[:limit]]


def _clean_pdf_page_text(value):
    lines = []
    for raw_line in str(value or "").splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def extract_job_document_texts(
    article,
    *,
    max_documents=3,
    max_total_pages=24,
    max_chars_per_page=8000,
    max_total_chars=40000,
):
    """Extract selectable PDF text as pre-AI evidence; scanned pages remain image-only evidence."""
    existing = article.get("job_document_texts")
    previous_failures = int(article.get("job_document_text_download_failures") or 0)
    if isinstance(existing, list) and existing and previous_failures == 0:
        return existing

    eligible = _eligible_documents(article, max_documents=max_documents)
    if not eligible:
        article["job_document_texts"] = []
        article["job_document_text_pages"] = 0
        article["job_document_text_chars"] = 0
        article["job_document_text_attempted_documents"] = 0
        article["job_document_text_download_failures"] = 0
        return []

    extracted = []
    total_pages = 0
    total_chars = 0
    truncated = False
    attempted_documents = 0
    download_failures = 0

    for document_index, item in enumerate(eligible, start=1):
        if total_pages >= max_total_pages or total_chars >= max_total_chars:
            truncated = True
            break

        url = str(item.get("url") or "").strip()
        label = _clean_text(
            item.get("label")
            or item.get("context")
            or f"الوثيقة الرسمية {document_index}"
        )
        attempted_documents += 1
        try:
            payload = _download_pdf(url)
            document = fitz.open(stream=payload, filetype="pdf")
        except Exception as error:
            download_failures += 1
            log_event(
                "job_document_text_skipped",
                article_id=article.get("id"),
                url=url,
                error=str(error),
            )
            continue

        document_page_count = document.page_count
        available_pages = max_total_pages - total_pages
        page_limit = min(document_page_count, available_pages)

        for page_index in range(page_limit):
            if total_chars >= max_total_chars:
                truncated = True
                break
            page = document.load_page(page_index)
            try:
                page_text = page.get_text("text", sort=True)
            except TypeError:
                page_text = page.get_text("text")
            page_text = _clean_pdf_page_text(page_text)
            total_pages += 1
            if not page_text:
                continue

            remaining = max_total_chars - total_chars
            page_text = page_text[: min(max_chars_per_page, remaining)].strip()
            if not page_text:
                continue
            if len(page_text) >= max_chars_per_page or len(page_text) >= remaining:
                truncated = True

            extracted.append(
                {
                    "document_url": url,
                    "document_label": label,
                    "page_number": page_index + 1,
                    "page_count": document_page_count,
                    "text": page_text,
                }
            )
            total_chars += len(page_text)

        document.close()
        if page_limit < document_page_count:
            truncated = True

    article["job_document_texts"] = extracted
    article["job_document_text_pages"] = len(extracted)
    article["job_document_text_chars"] = total_chars
    article["job_document_text_truncated"] = bool(truncated)
    article["job_document_text_attempted_documents"] = attempted_documents
    article["job_document_text_download_failures"] = download_failures
    return extracted


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
    """Render verified official job PDFs into sequential JPEG pages.

    The original official URLs remain available as action links. Rendering is a
    reader-facing representation of the same verified document.
    """
    eligible = _eligible_documents(article, max_documents=max_documents)
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
    attempted_documents = 0
    render_failures = 0
    failed_urls = []
    for document_index, item in enumerate(eligible, start=1):
        if total_pages >= max_total_pages:
            break
        url = str(item.get("url") or "").strip()
        label = _clean_text(item.get("label") or item.get("context") or f"الوثيقة الرسمية {document_index}")
        attempted_documents += 1
        document = None
        try:
            payload = _download_pdf(url)
            document = fitz.open(stream=payload, filetype="pdf")

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

            if page_limit < document_page_count:
                article["job_document_pages_truncated"] = True
                break
        except Exception as error:
            render_failures += 1
            failed_urls.append(url)
            log_event(
                "job_document_render_skipped",
                article_id=article.get("id"),
                url=url,
                error=str(error),
            )
            continue
        finally:
            if document is not None:
                try:
                    document.close()
                except Exception:
                    pass

    article["job_document_page_images"] = rendered
    article["job_document_rendered_pages"] = len(rendered)
    article["job_document_render_attempted_documents"] = attempted_documents
    article["job_document_render_failures"] = render_failures
    article["job_document_render_failed_urls"] = failed_urls
    return rendered
