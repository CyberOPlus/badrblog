# ============================================================
# quality_gate.py - Final Blogger pre-publish quality gate
# ============================================================

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from duplicate_utils import (
    canonicalize_url,
    content_hash_from_html,
    similar_topic_signature,
    title_hash,
    topic_signature,
)
from production_logging import html_to_text, html_word_count
from job_core import is_application_url_bound_to_job, is_job_specific_url
from verified_fact_manifest import build_verified_fact_manifest, validate_output_against_manifest
from config import (
    JOBS_MODE,
)


MIN_BLOGGER_ARTICLE_WORDS = 700
TARGET_BLOGGER_ARTICLE_WORDS = "700-1000"
FRESHNESS_HARD_MAX_HOURS = 24
REQUIRED_READER_SECTION = "\u0645\u0627\u0630\u0627 \u064a\u0639\u0646\u064a \u0647\u0630\u0627 \u0644\u0643"
REQUIRED_READER_SECTION_WITH_QUESTION = REQUIRED_READER_SECTION + "\u061f"

CYBER_HINTS = (
    "cyber",
    "security",
    "malware",
    "phishing",
    "ransomware",
    "breach",
    "vulnerability",
    "exploit",
    "cve",
    "\u0627\u0644\u0623\u0645\u0646 \u0627\u0644\u0633\u064a\u0628\u0631\u0627\u0646\u064a",
    "\u0628\u0631\u0645\u062c\u064a\u0627\u062a \u062e\u0628\u064a\u062b\u0629",
    "\u062b\u063a\u0631\u0629",
    "\u0627\u062e\u062a\u0631\u0627\u0642",
)

PROTECTION_SECTION_HINTS = (
    "\u0643\u064a\u0641 \u062a\u062d\u0645\u064a",
    "\u0646\u0635\u0627\u0626\u062d \u0644\u0644\u062d\u0645\u0627\u064a\u0629",
    "\u0627\u0644\u062d\u0645\u0627\u064a\u0629",
    "\u062e\u0637\u0648\u0627\u062a \u0627\u0644\u0648\u0642\u0627\u064a\u0629",
    "\u0627\u0644\u0646\u0635\u0627\u0626\u062d \u0627\u0644\u0639\u0645\u0644\u064a\u0629",
)

CONCLUSION_HINTS = (
    "\u0627\u0644\u062e\u0644\u0627\u0635\u0629",
    "\u062e\u0644\u0627\u0635\u0629",
    "\u0641\u064a \u0627\u0644\u0646\u0647\u0627\u064a\u0629",
    "\u0627\u0644\u0627\u0633\u062a\u0646\u062a\u0627\u062c",
)

TECHNICAL_LATIN_TOKENS = {
    "ai", "api", "cve", "sql", "xss", "rce", "llm", "vpn", "ios", "android",
    "windows", "linux", "github", "docker", "chatgpt", "gemini", "openai",
    "malware", "ransomware", "phishing", "cloud", "google", "microsoft",
}


@dataclass
class QualityGateResult:
    passed: bool
    reason: str = ""
    word_count: int = 0
    warnings: tuple = ()

    def to_dict(self):
        return {
            "passed": self.passed,
            "reason": self.reason,
            "word_count": self.word_count,
            "warnings": list(self.warnings),
        }


def is_cybersecurity_article(article):
    text = " ".join(
        str(article.get(field, ""))
        for field in (
            "title",
            "fetched_title",
            "seo_title",
            "suggested_category",
            "category_hint",
            "content_preview",
            "final_html",
        )
    ).casefold()
    return any(hint in text for hint in CYBER_HINTS)


def _has_reader_section(html_content, body_text):
    return (
        REQUIRED_READER_SECTION in html_content
        or REQUIRED_READER_SECTION_WITH_QUESTION in html_content
        or REQUIRED_READER_SECTION in body_text
        or REQUIRED_READER_SECTION_WITH_QUESTION in body_text
    )


def _has_heading_like_section(html_content, body_text, hints):
    heading_text = " ".join(re.findall(r"<h[23][^>]*>(.*?)</h[23]>", html_content, re.I | re.S))
    heading_text = html_to_text(heading_text) if heading_text else ""
    haystack = f"{heading_text} {body_text}".casefold()
    return any(hint.casefold() in haystack for hint in hints)


def _has_intro_before_first_heading(html_content):
    before_heading = re.split(r"<h[23]\b", html_content, maxsplit=1, flags=re.I)[0]
    return html_word_count(before_heading) >= 80


def _has_visible_json_or_markdown(text):
    if "```" in text:
        return True
    if re.search(r'(^|\s)"(?:title|description|slug|html_content|facebook_post_text)"\s*:', text):
        return True
    if re.search(r"\{[^{}]{0,80}\"(?:title|description|slug|html_content)\"\s*:", text, re.S):
        return True
    return False


def _has_repeated_text_blocks(body_text):
    normalized = re.sub(r"\s+", " ", body_text or "").strip()
    if not normalized:
        return False
    chunks = re.split(r"[.!؟؛]\s+|\n+", normalized)
    seen = set()
    for chunk in chunks:
        chunk = chunk.strip()
        if len(chunk) < 80:
            continue
        fingerprint = re.sub(r"\W+", "", chunk.casefold())[:180]
        if fingerprint in seen:
            return True
        seen.add(fingerprint)
    return False


_JOB_FACT_STOPWORDS = {
    "في", "من", "الى", "إلى", "على", "عن", "مع", "لدى", "عند", "حسب",
    "هذا", "هذه", "ذلك", "تلك", "الذي", "التي", "الذين", "كما", "تم", "يتم",
    "هو", "هي", "وهو", "وهي", "ثم", "او", "أو", "قد", "فقط", "كل", "ضمن",
    "بالنسبة", "يمكن", "يجب", "وفق", "وفقا", "خلال", "بعد", "قبل", "بين",
}

_JOB_FACT_SEMANTIC_REPLACEMENTS = (
    (r"اخر\s+اجل(?:\s+لايداع\s+ملفات\s+الترشيح|\s+للترشيح)?", " deadline "),
    (r"موعد\s+انتهاء\s+الترشيح", " deadline "),
    (r"تاريخ\s+انتهاء\s+الترشيح", " deadline "),
    (r"اخر\s+موعد\s+للتقديم", " deadline "),
    (r"تاريخ\s+اجراء\s+(?:المباراة|الاختبار)", " examdate "),
    (r"موعد\s+(?:المباراة|الاختبار)", " examdate "),
    (r"تاريخ\s+(?:المباراة|الاختبار)", " examdate "),
    (r"تاريخ\s+النشر", " publishdate "),
    (r"عدد\s+المناصب", " positions "),
    (r"نوع\s+العقد", " contract "),
    (r"مكان\s+العمل|مقر\s+العمل", " location "),
    (r"سنوات?\s+الخبرة|الخبرة\s+المطلوبة", " experience "),
    (r"الشهادة\s+المطلوبة|الدبلوم\s+المطلوب", " diploma "),
    (r"النتائج\s+النهائية", " finalresults "),
)

_JOB_DATE_MONTHS = (
    "يناير", "فبراير", "مارس", "ابريل", "أبريل", "ماي", "مايو", "يونيو",
    "يوليوز", "يوليو", "غشت", "اغسطس", "أغسطس", "شتنبر", "سبتمبر",
    "اكتوبر", "أكتوبر", "نونبر", "نوفمبر", "دجنبر", "ديسمبر",
)


def _normalize_job_fact_text(text):
    value = html_to_text(str(text or ""))
    value = value.casefold()
    value = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", value)
    value = value.translate(str.maketrans({
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ى": "ي",
    }))
    for pattern, replacement in _JOB_FACT_SEMANTIC_REPLACEMENTS:
        value = re.sub(pattern, replacement, value, flags=re.I)
    value = re.sub(r"[^\w\u0600-\u06ff./-]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _job_fact_tokens(text):
    normalized = _normalize_job_fact_text(text)
    tokens = []
    for token in normalized.split():
        if token in _JOB_FACT_STOPWORDS:
            continue
        if len(token) == 1 and not token.isdigit():
            continue
        tokens.append(token)
    return set(tokens)


def _job_date_tokens(text):
    normalized = _normalize_job_fact_text(text)
    dates = set(re.findall(
        r"\b(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2})\b",
        normalized,
    ))
    month_pattern = "|".join(re.escape(_normalize_job_fact_text(month)) for month in _JOB_DATE_MONTHS)
    if month_pattern:
        for match in re.findall(
            rf"\b\d{{1,2}}\s+(?:{month_pattern})(?:\s+\d{{4}})?\b",
            normalized,
            flags=re.I,
        ):
            dates.add(re.sub(r"\s+", " ", match).strip())
    return dates


def _job_fact_atoms(text):
    normalized = _normalize_job_fact_text(text)
    atoms = set()

    for number in re.findall(r"\b(\d{1,4})\s*(?:منصب|مناصب|منصبا)\b", normalized):
        atoms.add(f"positions:{int(number)}")
    for number in re.findall(r"\bpositions\s*:?\s*(\d{1,4})\b", normalized):
        atoms.add(f"positions:{int(number)}")

    dates = _job_date_tokens(normalized)
    if "deadline" in normalized:
        atoms.update(f"deadline:{date}" for date in dates)
    if "examdate" in normalized:
        atoms.update(f"examdate:{date}" for date in dates)
    if "publishdate" in normalized:
        atoms.update(f"publishdate:{date}" for date in dates)

    for amount in re.findall(
        r"\b(\d[\d\s.,]{1,12})\s*(?:درهم|dh|mad)\b",
        normalized,
        flags=re.I,
    ):
        compact = re.sub(r"\s+", "", amount)
        atoms.add(f"salary:{compact}")

    for years in re.findall(
        r"\b(\d{1,2})\s*(?:سنوات|سنواتا|سنة|عاما|عام)\s+(?:من\s+)?الخبرة\b",
        normalized,
    ):
        atoms.add(f"experience_years:{int(years)}")

    for age in re.findall(
        r"\b(\d{1,2})\s*(?:سنة|عاما|عام)\b",
        normalized,
    ):
        if any(hint in normalized for hint in ("السن", "العمر", "age")):
            atoms.add(f"age:{int(age)}")

    return atoms


def _job_duplicate_structured_rows_reason(html_content):
    soup = BeautifulSoup(html_content or "", "html.parser")
    seen = set()
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [
                _normalize_job_fact_text(cell.get_text(" ", strip=True))
                for cell in row.find_all(["th", "td"])
            ]
            cells = [cell for cell in cells if cell]
            if len(cells) < 2:
                continue
            fingerprint = " | ".join(cells)
            if len(fingerprint) < 8:
                continue
            if fingerprint in seen:
                return "duplicate structured row found in Jobs article"
            seen.add(fingerprint)
    return ""


def _job_unverified_external_link_reason(html_content, verification_context):
    package = verification_context or {}
    allowed = set()

    def add_url(value):
        url = str(value or "").strip()
        if url:
            allowed.add(canonicalize_url(url) or url)

    add_url(package.get("job_application_url"))
    add_url(package.get("job_detail_url"))

    for item in package.get("job_document_links") or []:
        if isinstance(item, dict):
            add_url(item.get("url"))

    for item in package.get("job_action_links") or []:
        if isinstance(item, dict):
            add_url(item.get("url"))

    for href in re.findall(r"<a\b[^>]*\bhref=['\"]([^'\"]+)['\"]", html_content, flags=re.I):
        if not re.match(r"^https?://", href, flags=re.I):
            continue
        key = canonicalize_url(href) or href
        if key not in allowed:
            return "Jobs article contains an external URL not present in verified application/document evidence"
    return ""


def _job_fact_categories(text):
    normalized = _normalize_job_fact_text(text)
    categories = set()
    category_hints = {
        "deadline": ("deadline",),
        "examdate": ("examdate",),
        "publishdate": ("publishdate",),
        "positions": ("positions", "منصب", "مناصب"),
        "contract": ("contract", "عقد"),
        "location": ("location", "المكان", "المدينة"),
        "experience": ("experience", "الخبرة"),
        "diploma": ("diploma", "دبلوم", "شهادة", "الشهادة"),
        "salary": ("الراتب", "الاجر", "درهم", " mad ", " dh "),
        "age": ("السن", "العمر"),
        "test_duration": ("المدة", "ساعات", "ساعة", "دقيقة", "دقائق"),
        "coefficient": ("المعامل",),
        "status": ("finalresults", "النتائج", "المدعوين", "اللائحة", "اللوائح"),
    }
    padded = f" {normalized} "
    for category, hints in category_hints.items():
        if any(hint in padded for hint in hints):
            categories.add(category)
    return categories


def _job_content_blocks(html_content):
    soup = BeautifulSoup(html_content or "", "html.parser")
    blocks = []
    current_heading = "intro"
    heading_seen = False

    for node in soup.find_all(["h2", "h3", "p", "li", "tr"]):
        if node.name in {"h2", "h3"}:
            heading_seen = True
            current_heading = _normalize_job_fact_text(node.get_text(" ", strip=True)) or "section"
            blocks.append({
                "kind": "heading",
                "region": f"heading:{current_heading}",
                "text": node.get_text(" ", strip=True),
            })
            continue

        text = node.get_text(" ", strip=True)
        if not text:
            continue
        if node.name == "tr":
            region = f"table:{current_heading}"
            kind = "table"
        else:
            region = "intro" if not heading_seen else f"section:{current_heading}"
            kind = node.name

        blocks.append({"kind": kind, "region": region, "text": text})

    return soup, blocks


def _job_semantic_repetition_reason(html_content, seo_title):
    """Return only deterministic structural repetition blockers."""
    soup, blocks = _job_content_blocks(html_content)

    if soup.find("h1") is not None:
        return "Jobs article body must not contain an h1; Blogger already renders the page title"

    title_tokens = _job_fact_tokens(seo_title)
    if len(title_tokens) >= 4:
        for block in blocks:
            if block["kind"] != "heading":
                continue
            heading_tokens = _job_fact_tokens(block["text"])
            if len(heading_tokens) < 4:
                continue
            shared = title_tokens & heading_tokens
            containment = len(shared) / max(1, min(len(title_tokens), len(heading_tokens)))
            if containment >= 0.90:
                return "Jobs article body semantically repeats the Blogger/SEO title as a heading"

    # Intro/title overlap is an editorial warning, not a publication blocker.
    # A factual first sentence can naturally share employer/role/location tokens
    # with the SEO title; rejecting the whole verified job for that overlap
    # wastes fresh vacancies. Duplicate H1/heading blocks above remain blockers.
    return ""


def _job_semantic_repetition_warnings(html_content, seo_title=""):
    """Heuristic repetition signals are advisory and must never reject a real job."""
    _soup, blocks = _job_content_blocks(html_content)
    warnings = []

    title_tokens = _job_fact_tokens(seo_title)
    intro_blocks_for_title = [
        block for block in blocks
        if block["region"] == "intro" and block["kind"] == "p"
    ]
    if intro_blocks_for_title and len(title_tokens) >= 4:
        intro = intro_blocks_for_title[0]["text"]
        sentences = [part.strip() for part in re.split(r"[.!؟]+", intro) if part.strip()]
        intro_tokens = _job_fact_tokens(sentences[0] if sentences else intro)
        if len(intro_tokens) >= 4:
            shared = title_tokens & intro_tokens
            containment = len(shared) / max(1, min(len(title_tokens), len(intro_tokens)))
            if containment >= 0.90:
                warnings.append("Jobs introduction substantially overlaps the Blogger/SEO title")

    intro_blocks = [block for block in blocks if block["region"] == "intro" and block["kind"] == "p"]
    if intro_blocks:
        sentences = [
            part.strip()
            for part in re.split(r"[.!؟]+", intro_blocks[0]["text"])
            if part.strip()
        ]
        if len(sentences) > 2:
            warnings.append("Jobs introduction is longer than the preferred two-sentence shape")

    table_atoms = set()
    for block in blocks:
        if block["kind"] == "table":
            table_atoms.update(_job_fact_atoms(block["text"]))

    if table_atoms:
        for block in blocks:
            if block["kind"] in {"table", "heading"}:
                continue
            overlap = _job_fact_atoms(block["text"]) & table_atoms
            if overlap:
                warnings.append(
                    f"Jobs prose may repeat a structured table fact ({sorted(overlap)[0]})"
                )
                break

    comparable = [
        block for block in blocks
        if block["kind"] != "heading" and len(_job_fact_tokens(block["text"])) >= 4
    ]
    for index, left in enumerate(comparable):
        left_tokens = _job_fact_tokens(left["text"])
        left_atoms = _job_fact_atoms(left["text"])
        for right in comparable[index + 1:]:
            if left["region"] == right["region"]:
                continue
            right_tokens = _job_fact_tokens(right["text"])
            shared = left_tokens & right_tokens
            shared_atoms = left_atoms & _job_fact_atoms(right["text"])
            shared_categories = _job_fact_categories(left["text"]) & _job_fact_categories(right["text"])
            if len(shared) < 3 and not shared_atoms:
                continue
            containment = len(shared) / max(1, min(len(left_tokens), len(right_tokens)))
            jaccard = len(shared) / max(1, len(left_tokens | right_tokens))
            if len(shared) >= 4 and containment >= 0.84 and jaccard >= 0.55:
                warnings.append("Jobs content may repeat the same fact across different sections")
                return warnings
            if shared_atoms and containment >= 0.65:
                warnings.append("Jobs content may paraphrase a structured fact in multiple sections")
                return warnings
            if shared_categories and len(shared) >= 3 and containment >= 0.55:
                warnings.append("Jobs content may repeat a fact category across table/intro/sections")
                return warnings

    return warnings


def _has_random_language_mixing(body_text):
    arabic_tokens = re.findall(r"[\u0600-\u06FF]{2,}", body_text or "")
    latin_tokens = re.findall(r"\b[A-Za-z][A-Za-z0-9+._-]{1,}\b", body_text or "")
    if len(arabic_tokens) < 30 or len(latin_tokens) < 18:
        return False
    nontechnical = [
        token for token in latin_tokens
        if token.casefold().strip("._-") not in TECHNICAL_LATIN_TOKENS
        and not re.match(r"^(CVE-\d{4}-\d+|v?\d+(?:\.\d+)+)$", token, re.I)
    ]
    return (
        len(nontechnical) / max(1, len(latin_tokens)) > 0.65
        and len(nontechnical) > 60
        and len(nontechnical) > len(arabic_tokens) * 0.6
    )


JOB_TITLE_ACTION_HINTS = (
    "توظيف", "توظف", "يوظف", "وظيفة", "وظائف", "فرص عمل", "فرصة عمل",
    "مباراة", "مباريات", "عقود العمل", "تشغيل",
)
JOB_TITLE_LIST_HINTS = (
    "لوائح المدعوين", "لائحة المدعوين", "المقبولين", "المدعوين",
)
JOB_TITLE_RESULT_HINTS = (
    "النتائج", "نتائج", "الناجحين", "النتيجة",
)


def _job_title_style_reason(seo_title, notice_type="vacancy"):
    title = re.sub(r"\s+", " ", str(seo_title or "")).strip()
    if len(title) < 28:
        return "job SEO title is too short and vague"
    if len(title) > 150:
        return "job SEO title is excessively long"
    meaningful_tokens = _job_fact_tokens(title)
    if len(meaningful_tokens) < 4:
        return "job SEO title is not specific enough to understand the notice"
    if re.search(r"(?:الإعلان\s*\d+|اخر\s+اجل.*تاريخ\s+اجراء|آخر\s+أجل.*تاريخ\s+إجراء)", title, flags=re.I):
        return "job SEO title contains raw source-chain text instead of a clear editorial headline"
    folded = title.casefold()
    notice_type = str(notice_type or "vacancy").strip().lower()

    if notice_type == "candidate_list":
        if not any(hint in title for hint in JOB_TITLE_LIST_HINTS):
            return "candidate-list title does not clearly say it is a list/invitation update"
    elif notice_type in {"results", "final_results"}:
        if not any(hint in title for hint in JOB_TITLE_RESULT_HINTS):
            return "results title does not clearly say it contains results"
    elif notice_type in {"vacancy", "competition"}:
        if not any(hint.casefold() in folded for hint in JOB_TITLE_ACTION_HINTS):
            return "active job title lacks a clear employment/competition action"
    elif notice_type == "update":
        pass

    if re.search(r"\bSi[eè]ge\b", title, flags=re.I):
        return "job SEO title contains raw source-page layout text"
    if re.search(r"\w(?:Si[eè]ge|Marina),?\s", title, flags=re.I):
        return "job SEO title contains concatenated source-page text"
    return ""


def _expected_image_missing(article, html_content):
    package = article.get("ai_input_package") or {}
    has_expected_image = bool(
        article.get("main_image")
        or article.get("image")
        or package.get("main_image")
        or article.get("article_images")
        or package.get("article_images")
    )
    if not has_expected_image:
        return False
    return not re.search(r"<img\b", html_content, flags=re.I)


def validate_ai_article_output(data, package=None):
    article = {
        "final_html": str(data.get("html_content", "")).strip(),
        "seo_title": str(data.get("title", "")).strip(),
        "seo_description": str(data.get("description", "")).strip(),
        "suggested_category": (package or {}).get("suggested_category", ""),
        "category_hint": (package or {}).get("category_hint", ""),
        "title": (package or {}).get("title", ""),
        "url": (package or {}).get("url", ""),
        "source_url": (package or {}).get("source_url", ""),
        "source_published_at": (package or {}).get("source_published_at", ""),
        "published_at_source": (package or {}).get("published_at_source", ""),
        "content_preview": (package or {}).get("content_preview", ""),
        "main_image": (package or {}).get("main_image", ""),
        "article_images": (package or {}).get("article_images", []),
        "ai_input_package": package or {},
    }
    return validate_before_publish(article, check_duplicate=False)


def validate_before_publish(article, existing_articles=None, check_duplicate=True):
    html_content = str(article.get("final_html") or article.get("blogger_article_html") or "").strip()
    seo_title = str(article.get("seo_title") or "").strip()
    seo_description = str(article.get("seo_description") or "").strip()

    if not html_content:
        return QualityGateResult(False, "missing final_html")
    if not seo_title:
        return QualityGateResult(False, "missing seo_title")
    if not seo_description:
        return QualityGateResult(False, "missing seo_description")

    word_count = html_word_count(html_content)

    body_text = html_to_text(html_content)
    if _has_visible_json_or_markdown(html_content) or _has_visible_json_or_markdown(body_text):
        return QualityGateResult(False, "visible JSON/markdown found in article output", word_count)
    if _has_repeated_text_blocks(body_text):
        return QualityGateResult(False, "repeated text blocks found in article output", word_count)
    if _has_random_language_mixing(body_text):
        return QualityGateResult(False, "random language mixing found in article output", word_count)
    if _expected_image_missing(article, html_content):
        return QualityGateResult(False, "expected article image is missing from final HTML", word_count)
    semantic_repeat_reason = _job_semantic_repetition_reason(html_content, seo_title)
    if semantic_repeat_reason:
        return QualityGateResult(False, semantic_repeat_reason, word_count)
    semantic_warnings = _job_semantic_repetition_warnings(html_content, seo_title)

    if re.search(r"class\s*=\s*['\"][^'\"]*\bpRelate\b", html_content, flags=re.I):
        return QualityGateResult(False, "related-post pRelate block is forbidden in Jobs articles", word_count)
    if any(
        phrase in body_text
        for phrase in (
            "قد يهمك أيضًا",
            "قد يهمك أيضا",
            "مقالات ذات صلة",
            "مواضيع ذات صلة",
        )
    ):
        return QualityGateResult(False, "related-post text is forbidden in Jobs articles", word_count)
    if re.search(
        r"<a\b[^>]*\bhref=['\"][^'\"]*/search/label/[^'\"]*['\"]",
        html_content,
        flags=re.I,
    ):
        return QualityGateResult(False, "automatic label/category internal link is forbidden in Jobs articles", word_count)

    if re.search(r"<script\b", html_content, flags=re.I):
        return QualityGateResult(False, "script tag found in Jobs article body", word_count)

    if len(seo_description) < 80 or len(seo_description) > 180:
        return QualityGateResult(
            False,
            "job meta description should stay between 80 and 180 characters",
            word_count,
        )
    if re.search(r"(?:\bنبحث\s+عن\b|\bعملائنا\b|\bفريقنا\b|انضم\s+(?:إلينا|لفريقنا))", seo_description):
        return QualityGateResult(
            False,
            "job meta description uses employer first-person/promotional voice",
            word_count,
        )

    internal_metadata_text = f"{seo_title} {seo_description} {body_text}"
    if re.search(
        r"(?:تاريخ\s+النشر|تاريخ\s+نشر\s+(?:الإعلان|الوظيفة)|"
        r"date\s+de\s+publication|publication\s+date|published\s+on|"
        r"المرجع|الرقم\s+المرجعي|رمز\s+المباراة|"
        r"r[eé]f(?:[ée]rence)?\.?\s*[:：#-])",
        internal_metadata_text,
        flags=re.I,
    ):
        return QualityGateResult(
            False,
            "internal job publication/reference metadata leaked into reader-facing content",
            word_count,
        )

    for reference_value in (
        article.get("job_external_reference"),
        article.get("ats_reference"),
        (article.get("ai_input_package") or {}).get("job_external_reference"),
        (article.get("ai_input_package") or {}).get("ats_reference"),
    ):
        reference_value = str(reference_value or "").strip()
        if len(reference_value) >= 3 and reference_value.casefold() in internal_metadata_text.casefold():
            return QualityGateResult(
                False,
                "internal job reference value leaked into reader-facing content",
                word_count,
            )

    promotional_job_phrases = (
        "الشركة الرائدة",
        "شركة رائدة",
        "الشركة المرموقة",
        "فرصة مميزة",
        "فرصة رائعة",
        "أحدث معايير",
        "حماية قصوى",
        "مهام حيوية",
        "تحديات مثيرة",
        "يهم هذا الإعلان فرصة",
        "يتم الاعتماد في هذا الإعلان على البيانات",
        "تعد هذه الفرصة مناسبة",
        "تُعد هذه الفرصة مناسبة",
        "فرصة تستحق الاطلاع",
        "فرصة توظيف جديدة",
        "لمزيد من التفاصيل يرجى",
        "للمزيد من التفاصيل يرجى",
    )
    if any(phrase in body_text or phrase in seo_description for phrase in promotional_job_phrases):
        return QualityGateResult(
            False,
            "generic promotional wording found in Jobs content",
            word_count,
        )

    if not str(article.get("url") or article.get("source_url") or "").strip():
        return QualityGateResult(False, "missing job source URL", word_count)

    package = article.get("ai_input_package") or {}
    verification_context = dict(package)
    verification_context.update({
        key: value
        for key, value in article.items()
        if key != "ai_input_package" and value not in (None, "", [], {})
    })

    duplicate_row_reason = _job_duplicate_structured_rows_reason(html_content)
    if duplicate_row_reason:
        return QualityGateResult(False, duplicate_row_reason, word_count)

    manifest = (
        package.get("verified_fact_manifest")
        if isinstance(package.get("verified_fact_manifest"), dict)
        else {}
    )
    if not manifest:
        manifest = build_verified_fact_manifest(verification_context)

    manifest_blocking, manifest_warnings = validate_output_against_manifest(
        manifest,
        seo_title,
        html_content,
    )
    job_warnings = list(semantic_warnings) + list(manifest_warnings)
    if manifest_blocking:
        return QualityGateResult(
            False,
            manifest_blocking[0],
            word_count,
            tuple(job_warnings),
        )

    unverified_link_reason = _job_unverified_external_link_reason(html_content, verification_context)
    if unverified_link_reason:
        return QualityGateResult(False, unverified_link_reason, word_count, tuple(job_warnings))

    notice_type = str(
        article.get("job_notice_type")
        or package.get("job_notice_type")
        or "vacancy"
    ).strip().lower()
    application_url = str(
        article.get("job_application_url")
        or package.get("job_application_url")
        or ""
    ).strip()
    application_kind = str(
        article.get("job_application_link_kind")
        or package.get("job_application_link_kind")
        or ""
    ).strip().lower()
    application_context = dict(verification_context)
    application_context["job_notice_type"] = notice_type
    application_context["job_application_link_kind"] = application_kind

    if application_url and not is_application_url_bound_to_job(application_context, application_url):
        if not is_job_specific_url(application_url):
            return QualityGateResult(
                False,
                "generic application portal is not a verified official channel for this competition",
                word_count,
            )
        return QualityGateResult(False, "job application URL belongs to a different vacancy", word_count)
    if (
        application_url
        and not is_job_specific_url(application_url)
        and application_kind != "official_application_channel"
    ):
        return QualityGateResult(
            False,
            "generic application portal must be classified as official_application_channel",
            word_count,
        )
    if application_kind == "official_application_channel":
        visible_application_text = html_to_text(html_content)
        if any(
            phrase in visible_application_text
            for phrase in (
                "التقديم المباشر",
                "رابط التقديم المباشر",
                "رابط الوظيفة المباشر",
            )
        ):
            return QualityGateResult(
                False,
                "official application channel is mislabeled as a direct vacancy link",
                word_count,
            )

    title_style_reason = _job_title_style_reason(seo_title, notice_type=notice_type)
    if title_style_reason:
        return QualityGateResult(False, title_style_reason, word_count, tuple(job_warnings))

    job_links = re.findall(
        r"<a\b[^>]*\bhref=['\"]([^'\"]+)['\"]",
        html_content,
        flags=re.I,
    )
    job_link_keys = [canonicalize_url(url) or url for url in job_links]
    if len(job_link_keys) != len(set(job_link_keys)):
        return QualityGateResult(False, "duplicate job link found in final HTML", word_count)

    cover_url = str(
        article.get("job_article_cover_url")
        or package.get("job_article_cover_url")
        or ""
    ).strip()
    image_sources = re.findall(
        r"<img\b[^>]*\bsrc=['\"]([^'\"]+)['\"]",
        html_content,
        flags=re.I,
    )
    expected_document_images = [
        str(item.get("url") or "").strip()
        for item in (
            article.get("job_document_page_images")
            or package.get("job_document_page_images")
            or []
        )
        if isinstance(item, dict) and str(item.get("url") or "").strip()
    ]
    if cover_url:
        if not image_sources or image_sources[0] != cover_url:
            return QualityGateResult(False, "job article must start with the generated cover image", word_count)
        if image_sources[1:] != expected_document_images:
            return QualityGateResult(
                False,
                "rendered official PDF pages are missing, reordered, duplicated, or unverified",
                word_count,
            )
    elif expected_document_images and image_sources != expected_document_images:
        return QualityGateResult(
            False,
            "rendered official PDF pages are missing, reordered, duplicated, or unverified",
            word_count,
        )

    # Jobs pass/fail is based on verified completeness and accuracy,
    # not word count or a mandatory heading shape.
    return QualityGateResult(True, "", word_count, tuple(job_warnings))


def duplicate_publish_reason(article, existing_articles):
    article_id = article.get("id")
    canonical_url = article.get("canonical_url") or canonicalize_url(article.get("url") or article.get("original_url"))
    current_title_hash = article.get("title_hash") or title_hash(article.get("title") or article.get("fetched_title") or article.get("seo_title"))
    current_topic_signature = article.get("topic_signature") or topic_signature(article.get("title") or article.get("fetched_title") or article.get("seo_title"))
    current_content_hash = article.get("final_content_hash") or content_hash_from_html(article.get("final_html", ""))

    for other in existing_articles:
        if other is article:
            continue
        if article_id and other.get("id") == article_id:
            continue
        already_published = other.get("publish_status") in {"published", "draft_created"} or other.get("status") in {"published", "draft_created"}
        if not already_published:
            continue
        other_canonical = other.get("canonical_url") or canonicalize_url(other.get("url") or other.get("original_url"))
        if canonical_url and other_canonical and canonical_url == other_canonical:
            return "another queue record with the same canonical URL is already published/drafted"
        if current_title_hash and other.get("title_hash") == current_title_hash:
            return "another queue record with the same title hash is already published/drafted"
        other_title = other.get("title") or other.get("fetched_title") or other.get("seo_title") or ""
        other_topic_signature = other.get("topic_signature") or topic_signature(other_title)
        if current_topic_signature and other_topic_signature and current_topic_signature == other_topic_signature:
            return "another queue record with the same topic is already published/drafted"
        if similar_topic_signature(
            article.get("title") or article.get("fetched_title") or article.get("seo_title"),
            other_title,
        ):
            return "another queue record with a similar topic is already published/drafted"
        if current_content_hash and other.get("final_content_hash") == current_content_hash:
            return "another queue record with the same content hash is already published/drafted"
    return ""
