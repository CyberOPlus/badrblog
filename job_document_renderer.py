from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import pymupdf as fitz
except ImportError:  # Backward compatibility with older PyMuPDF installs.
    import fitz
import requests
from PIL import Image

from production_logging import log_event
from job_core import is_application_url_bound_to_job


PDF_HINTS = (
    "pdf", "avis", "conditions", "condition", "reglement", "règlement",
    "dossier", "fiche", "communique", "communiqué", "descriptif",
    "announcement", "notice", "decision", "description", "download",
    "إعلان", "الاعلان", "الإعلان", "شروط", "الشروط", "قرار", "مقرر",
    "بطاقة", "بطاقة الوظيفة", "بطاقة المنصب", "ملف", "وثيقة", "الوثيقة", "تحميل",
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
    if max_documents is None:
        return [item for _priority, _index, item in candidates]
    limit = max(1, int(max_documents or 1))
    return [item for _priority, _index, item in candidates[:limit]]


def _clean_pdf_page_text(value):
    lines = []
    for raw_line in str(value or "").splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def _ocr_pdf_page_text(page, *, article_id="", language_hint=""):
    """OCR scanned official PDF pages when Tesseract is available on the runner."""
    if not shutil.which("tesseract"):
        return "", "tesseract_unavailable"

    languages = []
    try:
        import subprocess
        probe = subprocess.run(
            ["tesseract", "--list-langs"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        languages = [
            line.strip()
            for line in (probe.stdout or "").splitlines()
            if line.strip() and "List of available languages" not in line
        ]
    except Exception:
        languages = []

    preferred = []
    hint = str(language_hint or "").casefold()
    # Moroccan official notices are commonly Arabic, French, or bilingual.
    # Prefer Arabic+French whenever the notice itself is Arabic or the source
    # context is Morocco; keep English only as a fallback for foreign ATS PDFs.
    if (
        "arab" in hint
        or "/ar/" in hint
        or "morocco" in hint
        or "maroc" in hint
        or hint == "ma"
    ):
        preferred.extend(["ara", "fra", "eng"])
    else:
        preferred.extend(["fra", "eng", "ara"])
    selected = [lang for lang in preferred if lang in languages]
    if not selected and "eng" in languages:
        selected = ["eng"]
    if not selected:
        return "", "ocr_language_unavailable"

    language = "+".join(selected[:2])
    try:
        textpage = page.get_textpage_ocr(
            language=language,
            dpi=150,
            full=True,
        )
        try:
            text = page.get_text("text", textpage=textpage, sort=True)
        except TypeError:
            text = page.get_text("text", textpage=textpage)
        text = _clean_pdf_page_text(text)
        if text:
            log_event(
                "job_document_page_ocr_used",
                article_id=article_id,
                language=language,
                chars=len(text),
            )
        return text, ""
    except Exception as error:
        log_event(
            "job_document_page_ocr_failed",
            article_id=article_id,
            language=language,
            error=str(error),
        )
        return "", str(error)


def extract_job_document_texts(
    article,
    *,
    max_documents=6,
    max_total_pages=48,
    max_chars_per_page=8000,
    max_total_chars=80000,
    max_ocr_pages=None,
):
    """Extract official PDF text as pre-AI evidence, using OCR for scanned pages.

    OCR follows the same bounded page budget as text extraction by default so a
    scanned condition on a later processed page is not invisible to the article AI.
    """
    existing = article.get("job_document_texts")
    previous_failures = int(article.get("job_document_text_download_failures") or 0)
    previous_read_complete = article.get("job_document_text_read_complete") is True
    if isinstance(existing, list) and previous_failures == 0 and previous_read_complete:
        return existing

    eligible = _eligible_documents(article, max_documents=max_documents)
    if not eligible:
        article["job_document_texts"] = []
        article["job_document_text_pages"] = 0
        article["job_document_text_chars"] = 0
        article["job_document_text_attempted_documents"] = 0
        article["job_document_text_download_failures"] = 0
        article["job_document_ocr_attempts"] = 0
        article["job_document_ocr_pages"] = 0
        article["job_document_ocr_failures"] = 0
        article["job_document_ocr_unavailable"] = False
        article["job_document_unread_pages"] = 0
        article["job_document_text_read_complete"] = True
        return []

    extracted = []
    total_pages = 0
    total_chars = 0
    truncated = False
    attempted_documents = 0
    download_failures = 0
    ocr_attempts = 0
    ocr_pages = 0
    ocr_failures = 0
    ocr_unavailable = False
    unread_pages = 0
    ocr_page_budget = (
        max(0, int(max_total_pages or 0))
        if max_ocr_pages is None
        else max(0, int(max_ocr_pages or 0))
    )

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

            # Some scanned notices expose only a page number/header as selectable
            # text. Treat very sparse text as scanned evidence and prefer OCR when
            # it recovers a materially fuller page.
            ocr_attempted = False
            ocr_error = ""
            if (
                len(page_text) < 80
                and not ocr_unavailable
                and ocr_attempts < ocr_page_budget
            ):
                original_page_text = page_text
                ocr_attempted = True
                ocr_attempts += 1
                ocr_text, ocr_error = _ocr_pdf_page_text(
                    page,
                    article_id=article.get("id"),
                    language_hint=" ".join(
                        str(value or "")
                        for value in (
                            article.get("source_country"),
                            article.get("job_country"),
                            article.get("url"),
                            article.get("source_url"),
                        )
                    ),
                )
                if ocr_text and len(ocr_text) > len(original_page_text):
                    page_text = ocr_text
                    ocr_pages += 1
                elif ocr_error in {"tesseract_unavailable", "ocr_language_unavailable"}:
                    ocr_unavailable = True
                elif ocr_error:
                    ocr_failures += 1

            if not page_text:
                # A successful OCR pass can legitimately find a blank/decorative
                # page. Only pages that could not actually be read stay retryable.
                if not ocr_attempted or ocr_error:
                    unread_pages += 1
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
    article["job_document_ocr_attempts"] = ocr_attempts
    article["job_document_ocr_pages"] = ocr_pages
    article["job_document_ocr_failures"] = ocr_failures
    article["job_document_ocr_unavailable"] = bool(ocr_unavailable)
    article["job_document_unread_pages"] = unread_pages
    # This marks a clean pass through the configured pre-AI PDF evidence budget.
    article["job_document_text_read_complete"] = bool(
        download_failures == 0
        and ocr_failures == 0
        and not ocr_unavailable
        and unread_pages == 0
    )
    return extracted



DOCUMENT_APPLICATION_HINTS = (
    "e-recrutement", "recrutement", "candidature", "candidater",
    "postuler", "inscription", "registration", "register", "apply", "application",
    "ترشيح", "الترشيح", "إيداع", "ايداع", "التسجيل",
)
DOCUMENT_ASCII_URL_RE = re.compile(
    r"(?i)(?:https?://|www\.)[A-Za-z0-9.-]+(?::\d+)?"
    r"(?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?"
)

def _document_application_url_candidates(article):
    """Find application channels explicitly printed inside verified PDF text."""
    document_keys = {
        _canonical_key(item.get("url"))
        for item in (article.get("job_document_links") or [])
        if isinstance(item, dict) and _canonical_key(item.get("url"))
    }
    detail_key = _canonical_key(
        article.get("job_detail_url")
        or article.get("canonical_url")
        or article.get("url")
    )
    candidates = {}

    for page in (article.get("job_document_texts") or []):
        if not isinstance(page, dict):
            continue
        text = str(page.get("text") or "")
        for match in DOCUMENT_ASCII_URL_RE.finditer(text):
            raw = str(match.group(0) or "").rstrip(".,;:)")
            if raw.casefold().startswith("www."):
                raw = "https://" + raw
            key = _canonical_key(raw)
            if not key or key == detail_key or key in document_keys:
                continue

            start = max(0, match.start() - 180)
            end = min(len(text), match.end() + 180)
            context = re.sub(r"\s+", " ", text[start:end]).strip()
            signature = f"{raw} {context}".casefold()
            if not any(hint.casefold() in signature for hint in DOCUMENT_APPLICATION_HINTS):
                continue

            score = 0
            url_folded = raw.casefold()
            if any(hint in url_folded for hint in (
                "e-recrutement", "recrutement", "candidature",
                "postuler", "inscription", "apply", "application",
            )):
                score += 8
            if any(hint.casefold() in context.casefold() for hint in DOCUMENT_APPLICATION_HINTS):
                score += 5
            host = urlparse(raw).netloc.casefold()
            if host.endswith(".gov.ma") or host.endswith(".ac.ma"):
                score += 4
            if "emploi-public.ma" in host:
                score += 2

            previous = candidates.get(key)
            row = {
                "url": raw,
                "context": context[:360],
                "score": score,
                "page_number": page.get("page_number"),
            }
            if previous is None or score > previous["score"]:
                candidates[key] = row

    return sorted(candidates.values(), key=lambda row: row["score"], reverse=True)


def promote_job_document_application_channel(article):
    """Promote an official application channel printed in the verified PDF.

    This runs only for official public competitions and never replaces an
    already-verified direct/application-channel URL. The PDF itself supplies the
    binding evidence by exposing the exact URL for this specific competition.
    """
    if str(article.get("job_notice_type") or "").strip().lower() != "competition":
        return ""
    if not bool(article.get("official_source") or article.get("job_official_source")):
        return ""

    current_url = str(article.get("job_application_url") or "").strip()
    current_kind = str(article.get("job_application_link_kind") or "").strip().lower()
    if current_url and current_kind in {"direct_apply", "official_application_channel"}:
        if is_application_url_bound_to_job(article, current_url):
            return current_url

    for candidate in _document_application_url_candidates(article):
        url = str(candidate.get("url") or "").strip()
        if not url:
            continue

        action_links = [
            dict(row)
            for row in (article.get("job_action_links") or [])
            if isinstance(row, dict)
        ]
        key = _canonical_key(url)
        if not any(
            str(row.get("kind") or "").strip().lower() == "apply"
            and _canonical_key(row.get("url")) == key
            for row in action_links
        ):
            action_links.append({
                "url": url,
                "label": "منصة الترشيح الرسمية",
                "kind": "apply",
                "context": "official_pdf",
            })

        probe = dict(article)
        probe["job_action_links"] = action_links
        probe["job_application_link_kind"] = "official_application_channel"
        if not is_application_url_bound_to_job(probe, url):
            continue

        article["job_action_links"] = action_links
        article["job_application_url"] = url
        article["job_application_link_kind"] = "official_application_channel"
        article["job_application_is_specific"] = False
        article["job_application_is_official_channel"] = True
        article["job_application_source"] = "official_pdf"
        log_event(
            "job_pdf_application_channel_promoted",
            article_id=article.get("id"),
            url=url,
            page=candidate.get("page_number"),
        )
        return url

    return ""



POSITION_COUNT_RE = re.compile(
    r"(?:\(\s*0*(\d{1,3})\s*\)|\)\s*0*(\d{1,3})\s*\()"
)
POSITION_RECRUITMENT_HINTS = (
    "توظيف", "مباراة", "المباراة", "منصب", "مناصب",
    "recrut", "concours", "poste", "postes",
)
POSITION_TITLE_STOPWORDS = {
    "مباراة", "مباريات", "التوظيف", "توظيف", "من", "الدرجة", "سلم",
    "وظيفة", "وظائف", "منصب", "مناصب",
    "concours", "recrutement", "emploi", "offre", "poste", "postes",
    "grade", "pour", "avec", "dans",
}
POSITION_GRADE_MARKERS = (
    "الاولى", "الثانية", "الثالثة", "الرابعة",
    "premier", "premiere", "deuxieme", "troisieme", "quatrieme",
    "1er", "1ere", "2eme", "3eme", "4eme",
)


def _normalize_position_text(value):
    text = str(value or "").casefold()
    text = re.sub(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]", "", text)
    text = text.translate(str.maketrans({
        "أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ؤ": "و", "ئ": "ي",
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "à": "a", "â": "a", "ä": "a",
        "î": "i", "ï": "i", "ô": "o", "ö": "o",
        "ù": "u", "û": "u", "ü": "u", "ç": "c",
    }))
    return re.sub(r"\s+", " ", text).strip()


def _position_scope_from_title(article):
    title = _normalize_position_text(article.get("job_title") or article.get("title"))
    if not title:
        return (), ()

    grades = tuple(marker for marker in POSITION_GRADE_MARKERS if marker in title)
    words = re.findall(r"[a-z\u0600-\u06ff]{4,}", title)
    role_tokens = []
    for word in words:
        if word in POSITION_TITLE_STOPWORDS or word in grades:
            continue
        if word not in role_tokens:
            role_tokens.append(word)
    return grades, tuple(role_tokens[:4])

def promote_job_document_position_count(article):
    """Promote a role-specific position count from official PDF/OCR evidence.

    Shared public decisions often contain several grades. A count is promoted only
    when one PDF line contains a parenthesized count, a recruitment hint, the
    current role token, and (when the title has one) the current grade marker.
    Conflicting matches are left unset rather than guessed.
    """
    try:
        current = int(article.get("job_number_of_positions") or 0)
    except (TypeError, ValueError):
        current = 0
    if current > 0:
        return current
    if not bool(article.get("official_source") or article.get("job_official_source")):
        return 0

    grades, role_tokens = _position_scope_from_title(article)
    if not role_tokens:
        return 0

    matches = []
    for page in (article.get("job_document_texts") or []):
        if not isinstance(page, dict):
            continue
        for raw_line in str(page.get("text") or "").splitlines():
            line = _normalize_position_text(raw_line)
            if not line:
                continue
            if grades and not any(grade in line for grade in grades):
                continue
            if not any(token in line for token in role_tokens):
                continue
            if not any(hint in line for hint in POSITION_RECRUITMENT_HINTS):
                continue
            for match in POSITION_COUNT_RE.finditer(line):
                raw_count = match.group(1) or match.group(2)
                try:
                    count = int(raw_count)
                except (TypeError, ValueError):
                    continue
                if 0 < count <= 500:
                    matches.append({
                        "count": count,
                        "page_number": page.get("page_number"),
                        "line": re.sub(r"\s+", " ", str(raw_line or "")).strip()[:360],
                    })

    counts = sorted({row["count"] for row in matches})
    if len(counts) != 1:
        return 0

    count = counts[0]
    article["job_number_of_positions"] = count
    article["job_number_of_positions_source"] = "official_pdf"
    article["job_position_count_evidence"] = matches[:6]
    log_event(
        "job_pdf_position_count_promoted",
        article_id=article.get("id"),
        count=count,
        evidence_rows=len(matches),
    )
    return count


def _safe_segment(value):
    cleaned = re.sub(r"[^a-z0-9-]+", "-", str(value or "").casefold())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned[:80] or "job"


def render_job_document_pages(
    article,
    *,
    output_root="assets/generated/job-documents",
    raw_base="https://raw.githubusercontent.com/CyberOPlus/badrblog/main",
    max_documents=6,
    max_total_pages=48,
):
    """Render all official PDF pages, checkpointing bounded batches across runs.

    The original official URLs remain available as action links. Rendering is a
    reader-facing representation of the same verified document. The limits
    bound new work per pass, never the final number of documents or pages.
    """
    eligible = _eligible_documents(article, max_documents=None)
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

    previous = article.get("job_document_page_images") or []
    cached = {}
    for row in previous:
        if not isinstance(row, dict) or not row.get("path") or not row.get("url"):
            continue
        try:
            page_number = int(row.get("page_number") or 0)
            page_count = int(row.get("page_count") or 0)
        except (TypeError, ValueError):
            continue
        path = Path(row["path"])
        if 1 <= page_number <= page_count and path.is_file():
            cached.setdefault(_canonical_key(row.get("document_url")), {})[page_number] = row

    rendered = []
    new_pages = 0
    attempted_documents = 0
    render_failures = 0
    failed_urls = []
    pending_urls = []
    page_budget = max(1, int(max_total_pages or 1))
    document_budget = max(1, int(max_documents or 1))
    for document_index, item in enumerate(eligible, start=1):
        url = str(item.get("url") or "").strip()
        label = _clean_text(item.get("label") or item.get("context") or f"الوثيقة الرسمية {document_index}")
        existing = cached.get(_canonical_key(url), {})
        known_count = max((int(row["page_count"]) for row in existing.values()), default=0)
        if known_count and len(existing) == known_count and all(
            int(row["page_count"]) == known_count for row in existing.values()
        ):
            rendered.extend(existing[index] for index in sorted(existing))
            continue
        if new_pages >= page_budget or attempted_documents >= document_budget:
            rendered.extend(existing[index] for index in sorted(existing))
            pending_urls.append(url)
            continue

        attempted_documents += 1
        document = None
        pages = dict(existing)
        try:
            payload = _download_pdf(url)
            document = fitz.open(stream=payload, filetype="pdf")

            digest = hashlib.sha256(_canonical_key(url).encode("utf-8")).hexdigest()[:10]
            document_page_count = document.page_count
            if document_page_count < 1:
                raise ValueError("official PDF contains no pages")
            pages = {
                number: dict(row, page_count=document_page_count)
                for number, row in pages.items() if number <= document_page_count
            }
            for page_index in range(document_page_count):
                if page_index + 1 in pages:
                    continue
                if new_pages >= page_budget:
                    pending_urls.append(url)
                    break
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
                pages[page_index + 1] = {
                    "document_url": url,
                    "document_label": label,
                    "page_number": page_index + 1,
                    "page_count": document_page_count,
                    "url": public_url,
                    "path": path.as_posix(),
                    "alt": f"{label} — الصفحة {page_index + 1}",
                }
                new_pages += 1
        except Exception as error:
            render_failures += 1
            failed_urls.append(url)
            log_event(
                "job_document_render_skipped",
                article_id=article.get("id"),
                url=url,
                error=str(error),
            )
        finally:
            if document is not None:
                try:
                    document.close()
                except Exception:
                    pass
        rendered.extend(pages[index] for index in sorted(pages))

    article["job_document_page_images"] = rendered
    article["job_document_pages_truncated"] = bool(pending_urls)
    article["job_document_render_pending_urls"] = pending_urls
    article["job_document_render_new_pages"] = new_pages
    article["job_document_rendered_pages"] = len(rendered)
    article["job_document_render_attempted_documents"] = attempted_documents
    article["job_document_render_failures"] = render_failures
    article["job_document_render_failed_urls"] = failed_urls
    return rendered
