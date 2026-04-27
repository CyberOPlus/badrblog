# ============================================================
# quality_gate.py - Final Blogger pre-publish quality gate
# ============================================================

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from duplicate_utils import canonicalize_url, content_hash_from_html, title_hash
from production_logging import html_to_text, html_word_count
from config import (
    ALLOW_UNKNOWN_DATE_IN_FAST_MODE,
    FAST_NEWS_MODE,
    MIN_ARTICLE_WORDS,
    RECENT_NEWS_MAX_AGE_HOURS,
    RECENT_NEWS_ONLY,
    TARGET_ARTICLE_WORDS,
)


MIN_BLOGGER_ARTICLE_WORDS = 800
TARGET_BLOGGER_ARTICLE_WORDS = "800-1200"
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
    minimum_words = MIN_ARTICLE_WORDS if fast_mode else MIN_BLOGGER_ARTICLE_WORDS
    if word_count < minimum_words:
        return QualityGateResult(
            False,
            f"article too short ({word_count} words; minimum {minimum_words})",
            word_count,
        )

    body_text = html_to_text(html_content)
    if fast_mode:
        if not str(article.get("url") or article.get("source_url") or "").strip():
            return QualityGateResult(False, "missing source URL", word_count)
        if RECENT_NEWS_ONLY:
            published_at = article.get("source_published_at") or article.get("original_published_at")
            if not published_at and not ALLOW_UNKNOWN_DATE_IN_FAST_MODE:
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
                if age_hours > RECENT_NEWS_MAX_AGE_HOURS:
                    return QualityGateResult(
                        False,
                        f"article older than {RECENT_NEWS_MAX_AGE_HOURS} hours",
                        word_count,
                    )
        if check_duplicate:
            duplicate_reason = duplicate_publish_reason(article, existing_articles or [])
            if duplicate_reason:
                return QualityGateResult(False, duplicate_reason, word_count)
        warnings = []
        if word_count < 300:
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
        if current_content_hash and other.get("final_content_hash") == current_content_hash:
            return "another queue record with the same content hash is already published/drafted"
    return ""
