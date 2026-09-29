# ============================================================
# quality_gate.py - Final Blogger pre-publish quality gate
# ============================================================

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from duplicate_utils import canonicalize_url, content_hash_from_html, similar_topic_signature, title_hash, topic_signature
from production_logging import html_to_text, html_word_count
from config import (
    ALLOW_UNKNOWN_DATE_IN_FAST_MODE,
    ALLOW_SHORT_ARTICLES,
    FAST_NEWS_MODE,
    MIN_ARTICLE_WORDS,
    RECENT_NEWS_ONLY,
    TARGET_ARTICLE_WORDS,
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
    folded = title.casefold()
    notice_type = str(notice_type or "vacancy").strip().lower()

    if notice_type == "candidate_list":
        if not any(hint in title for hint in JOB_TITLE_LIST_HINTS):
            return "candidate-list title does not clearly say it is a list/invitation update"
    elif notice_type in {"results", "final_results"}:
        if not any(hint in title for hint in JOB_TITLE_RESULT_HINTS):
            return "results title does not clearly say it contains results"
    else:
        if not any(hint.casefold() in folded for hint in JOB_TITLE_ACTION_HINTS):
            return "vacancy title lacks a clear employment/competition action"

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


def validate_before_publish(article, existing_articles=None, check_duplicate=True, fast_news_mode=None):
    fast_mode = FAST_NEWS_MODE if fast_news_mode is None else bool(fast_news_mode)
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
    minimum_words = 100 if JOBS_MODE else (MIN_ARTICLE_WORDS if fast_mode else MIN_BLOGGER_ARTICLE_WORDS)
    if word_count < minimum_words and not (
        (not JOBS_MODE) and fast_mode and ALLOW_SHORT_ARTICLES and word_count >= 80
    ):
        return QualityGateResult(
            False,
            f"article too short ({word_count} words; minimum {minimum_words})",
            word_count,
        )

    body_text = html_to_text(html_content)
    if _has_visible_json_or_markdown(html_content) or _has_visible_json_or_markdown(body_text):
        return QualityGateResult(False, "visible JSON/markdown found in article output", word_count)
    if _has_repeated_text_blocks(body_text):
        return QualityGateResult(False, "repeated text blocks found in article output", word_count)
    if _has_random_language_mixing(body_text):
        return QualityGateResult(False, "random language mixing found in article output", word_count)
    if _expected_image_missing(article, html_content):
        return QualityGateResult(False, "expected article image is missing from final HTML", word_count)
    if JOBS_MODE:
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

        if not str(article.get("url") or article.get("source_url") or "").strip():
            return QualityGateResult(False, "missing job source URL", word_count)

        package = article.get("ai_input_package") or {}
        application_url = str(
            article.get("job_application_url")
            or package.get("job_application_url")
            or ""
        ).strip()
        if application_url and application_url not in html_content:
            return QualityGateResult(False, "job application URL is missing from final HTML", word_count)

        deadline_value = str(
            article.get("job_deadline_display")
            or article.get("job_deadline")
            or package.get("job_deadline_display")
            or package.get("job_deadline")
            or ""
        ).strip()
        notice_type = str(
            article.get("job_notice_type")
            or package.get("job_notice_type")
            or "vacancy"
        ).strip().lower()
        title_style_reason = _job_title_style_reason(seo_title, notice_type=notice_type)
        if title_style_reason:
            return QualityGateResult(False, title_style_reason, word_count)

        if notice_type == "vacancy" and deadline_value:
            deadline_labels = (
                "آخر أجل للترشيح",
                "آخر أجل",
                "موعد انتهاء الترشيح",
                "تاريخ انتهاء الترشيح",
            )
            if not any(label in body_text for label in deadline_labels):
                return QualityGateResult(
                    False,
                    "verified job deadline is missing from final HTML",
                    word_count,
                )

        job_links = re.findall(
            r"<a\b[^>]*\bhref=['\"]([^'\"]+)['\"]",
            html_content,
            flags=re.I,
        )
        if len(job_links) != len(set(job_links)):
            return QualityGateResult(False, "duplicate job link found in final HTML", word_count)

        cover_url = str(
            article.get("job_article_cover_url")
            or package.get("job_article_cover_url")
            or ""
        ).strip()
        if cover_url:
            image_sources = re.findall(
                r"<img\b[^>]*\bsrc=['\"]([^'\"]+)['\"]",
                html_content,
                flags=re.I,
            )
            if len(image_sources) != 1:
                return QualityGateResult(
                    False,
                    f"job article must contain exactly one generated cover image; found {len(image_sources)}",
                    word_count,
                )
            if image_sources[0] != cover_url:
                return QualityGateResult(False, "job article image is not the generated cover", word_count)

        document_links = (
            article.get("job_document_links")
            or package.get("job_document_links")
            or []
        )
        for item in document_links:
            if not isinstance(item, dict):
                continue
            document_url = str(item.get("url") or "").strip()
            if document_url and document_url not in html_content:
                return QualityGateResult(
                    False,
                    "verified official job document URL is missing from final HTML",
                    word_count,
                )

        detail_url = str(
            article.get("job_detail_url")
            or package.get("job_detail_url")
            or ""
        ).strip()
        if detail_url and detail_url != application_url and detail_url not in html_content:
            return QualityGateResult(
                False,
                "official job detail URL is missing from final HTML",
                word_count,
            )

        max_job_words = 650 if len(document_links) >= 4 else 320
        if word_count > max_job_words:
            return QualityGateResult(
                False,
                f"job article too long ({word_count} words; maximum {max_job_words})",
                word_count,
            )
        warnings = []
        if not re.search(r"<h2\b", html_content, flags=re.I):
            warnings.append("job article has no h2 section")
        return QualityGateResult(True, "", word_count, tuple(warnings))

    if fast_mode:
        promotional, promo_reason = is_promotional_article(article)
        if promotional:
            return QualityGateResult(False, promo_reason, word_count)
        non_technical, non_technical_reason = is_non_technical_entertainment_article(article)
        if non_technical:
            return QualityGateResult(False, non_technical_reason, word_count)
        if not str(article.get("url") or article.get("source_url") or "").strip():
            return QualityGateResult(False, "missing source URL", word_count)
        if RECENT_NEWS_ONLY:
            published_at = article.get("source_published_at") or article.get("original_published_at")
            freshness_source = str(article.get("freshness_source") or "").strip()
            if not published_at and freshness_source == "fallback_no_date":
                published_at = ""
            elif not published_at and not ALLOW_UNKNOWN_DATE_IN_FAST_MODE:
                return QualityGateResult(False, "publish date missing in strict recent mode", word_count)
            if published_at:
                try:
                    text = str(published_at).replace("Z", "+00:00")
                    parsed = datetime.fromisoformat(text)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    age_hours = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600
                except ValueError:
                    return QualityGateResult(False, "invalid publish date in strict recent mode", word_count)
                if age_hours > FRESHNESS_HARD_MAX_HOURS:
                    return QualityGateResult(
                        False,
                        f"article older than {FRESHNESS_HARD_MAX_HOURS} hours",
                        word_count,
                    )
        if check_duplicate:
            duplicate_reason = duplicate_publish_reason(article, existing_articles or [])
            if duplicate_reason:
                return QualityGateResult(False, duplicate_reason, word_count)
        warnings = []
        bypass_message = article.get("content_filter_bypass_message")
        if bypass_message:
            warnings.append(str(bypass_message))
        if word_count < 120:
            warnings.append(f"fast news article below target range {TARGET_ARTICLE_WORDS}")
        if not _has_reader_section(html_content, body_text):
            warnings.append("reader-impact section omitted in fast mode")
        if not (article.get("main_image") or article.get("image") or (article.get("ai_input_package") or {}).get("main_image")):
            warnings.append("missing article image")
        return QualityGateResult(True, "", word_count, tuple(warnings))

    if not _has_reader_section(html_content, body_text):
        return QualityGateResult(False, f"missing required section: {REQUIRED_READER_SECTION_WITH_QUESTION}", word_count)
    if not _has_intro_before_first_heading(html_content):
        return QualityGateResult(False, "missing strong introduction before first heading", word_count)
    if len(re.findall(r"<h2\b", html_content, flags=re.I)) < 2:
        return QualityGateResult(False, "missing main explanatory sections", word_count)
    if is_cybersecurity_article(article) and not _has_heading_like_section(html_content, body_text, PROTECTION_SECTION_HINTS):
        return QualityGateResult(False, "missing cybersecurity protection/advice section", word_count)
    if not _has_heading_like_section(html_content, body_text, CONCLUSION_HINTS):
        return QualityGateResult(False, "missing strong conclusion section", word_count)

    if check_duplicate:
        duplicate_reason = duplicate_publish_reason(article, existing_articles or [])
        if duplicate_reason:
            return QualityGateResult(False, duplicate_reason, word_count)

    warnings = []
    if not (article.get("main_image") or article.get("image") or (article.get("ai_input_package") or {}).get("main_image")):
        warnings.append("missing article image")
    return QualityGateResult(True, "", word_count, tuple(warnings))


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
from content_filter import is_non_technical_entertainment_article, is_promotional_article
