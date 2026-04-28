# ============================================================
# article_ai_processor.py - Phase 6 AI Article Processing
# ============================================================

import json
import random
import re
import hashlib
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from bs4 import NavigableString

with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    try:
        import google.generativeai as genai
    except ImportError:
        genai = None

from article_queue import load_article_queue, save_article_queue
from duplicate_utils import content_hash_from_html
from config import (
    AI_PROVIDER,
    AI_PROVIDER_MEMORY_PATH,
    AI_MODEL_TIMEOUT_SECONDS,
    AI_TOTAL_TIME_BUDGET_SECONDS,
    ALLOW_SHORT_ARTICLES,
    FAST_OPENROUTER_MODELS,
    FAST_NEWS_MODE,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_TIMEOUT_SECONDS,
    MAX_AI_RETRIES,
    MIN_ARTICLE_WORDS,
    OPENAI_API_KEY,
    OPENAI_API_URL,
    OPENAI_MAX_TOKENS,
    OPENAI_MODEL,
    OPENAI_TIMEOUT_SECONDS,
    OPENROUTER_API_KEY,
    OPENROUTER_API_URL,
    OPENROUTER_APP_NAME,
    OPENROUTER_MAX_TOKENS,
    OPENROUTER_MODEL,
    OPENROUTER_MODELS,
    OPENROUTER_REFERER,
    OPENROUTER_TIMEOUT_SECONDS,
    TARGET_ARTICLE_WORDS,
)
from production_logging import elapsed_ms, html_word_count, log_event
from quality_gate import (
    MIN_BLOGGER_ARTICLE_WORDS,
    REQUIRED_READER_SECTION,
    REQUIRED_READER_SECTION_WITH_QUESTION,
    TARGET_BLOGGER_ARTICLE_WORDS,
    validate_ai_article_output,
)

MAX_AI_ATTEMPTS = max(1, MAX_AI_RETRIES)
MIN_PUBLISHABLE_WORDS = 120
AI_MODEL_COOLDOWN_SECONDS = 30 * 60
_AI_COOLDOWNS = {}
_AI_MEMORY_CACHE = None
FAST_OPENROUTER_MODEL_SET = set(FAST_OPENROUTER_MODELS)


class AIProviderFallbackNeeded(RuntimeError):
    """Raised when a provider error must switch to another AI provider first."""


class AIProviderRotationExhausted(RuntimeError):
    """Raised when all available AI providers fail before quality validation."""


class AITimeBudgetExceeded(RuntimeError):
    """Raised when the full AI generation budget is exhausted."""


@dataclass
class AIExecutionContext:
    article_id: str = ""
    started_at: float = field(default_factory=time.perf_counter)
    total_budget_seconds: int = AI_TOTAL_TIME_BUDGET_SECONDS
    gemini_failures: int = 0
    openrouter_failures_after_gemini: int = 0
    skipped_slow_models_count: int = 0
    fast_mode_enabled: bool = True

    @property
    def deadline(self):
        return self.started_at + self.total_budget_seconds

    def elapsed_seconds(self):
        return max(0.0, time.perf_counter() - self.started_at)

    def remaining_seconds(self):
        return max(0.0, self.deadline - time.perf_counter())


ALLOWED_LATIN_INLINE = {
    "ai",
    "api",
    "github",
    "cve",
    "nvd",
    "cisa",
    "microsoft",
    "google",
    "windows",
    "linux",
    "android",
    "ios",
    "chrome",
    "openai",
    "gemini",
    "openrouter",
    "malware",
    "ransomware",
    "zero-day",
    "vpn",
    "http",
    "https",
    "dns",
    "sql",
    "xss",
}


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _has_real_key(value, placeholder):
    return bool(value and value != placeholder)


def _key_id(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:10]


def _models(values, fallback):
    model_names = []
    seen = set()
    for value in values or [fallback]:
        value = str(value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        model_names.append(value)
    return model_names


def _candidate_id(candidate):
    return f"{candidate['provider']}:{candidate.get('model', '')}:{_key_id(candidate.get('api_key'))}"


def _empty_ai_memory():
    return {"avg_time": 0.0, "cooldowns": {}, "fastest_success_model": "", "stats": {}}


def _load_ai_memory():
    global _AI_MEMORY_CACHE
    if _AI_MEMORY_CACHE is not None:
        return _AI_MEMORY_CACHE
    try:
        if AI_PROVIDER_MEMORY_PATH.exists():
            with AI_PROVIDER_MEMORY_PATH.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("avg_time", 0.0)
                data.setdefault("cooldowns", {})
                data.setdefault("fastest_success_model", "")
                data.setdefault("stats", {})
                _AI_MEMORY_CACHE = data
                return _AI_MEMORY_CACHE
    except Exception as error:
        log_event("ai_memory_load_failed", error=error.__class__.__name__)
    _AI_MEMORY_CACHE = _empty_ai_memory()
    return _AI_MEMORY_CACHE


def _save_ai_memory(memory):
    try:
        AI_PROVIDER_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AI_PROVIDER_MEMORY_PATH.open("w", encoding="utf-8") as handle:
            json.dump(memory, handle, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception as error:
        log_event("ai_memory_save_failed", error=error.__class__.__name__)


def _safe_error_reason(error):
    text = re.sub(r"\s+", " ", str(error or error.__class__.__name__)).strip()
    text = re.sub(r"(key|token|secret|password)[=:]\s*\S+", r"\1=***", text, flags=re.IGNORECASE)
    text = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "AIza***", text)
    text = re.sub(r"sk-[0-9A-Za-z_\-]{12,}", "sk-***", text)
    return text[:160] or error.__class__.__name__


def _candidate_memory_key(candidate):
    return f"{candidate.get('provider')}:{candidate.get('model')}"


def _preferred_model_memory_key():
    return str(_load_ai_memory().get("fastest_success_model") or "").strip()


def _reorder_candidates_by_speed_memory(candidates):
    preferred = _preferred_model_memory_key()
    if not preferred:
        return list(candidates)
    preferred_candidates = [candidate for candidate in candidates if _candidate_memory_key(candidate) == preferred]
    others = [candidate for candidate in candidates if _candidate_memory_key(candidate) != preferred]
    return preferred_candidates + others


def _preferred_provider():
    preferred = _preferred_model_memory_key()
    return preferred.split(":", 1)[0] if ":" in preferred else ""


def _remaining_budget_seconds(context):
    return context.remaining_seconds() if context else float(AI_TOTAL_TIME_BUDGET_SECONDS)


def _provider_timeout_seconds(candidate, context=None):
    provider = candidate.get("provider")
    base_timeout = GEMINI_TIMEOUT_SECONDS if provider == "gemini" else AI_MODEL_TIMEOUT_SECONDS
    remaining = _remaining_budget_seconds(context)
    if remaining <= 0:
        raise AITimeBudgetExceeded("ai_time_budget_exceeded")
    return max(1, min(base_timeout, int(remaining) if remaining >= 1 else 1))


def _check_ai_time_budget(context, stage=""):
    if context and context.remaining_seconds() <= 0:
        log_event(
            "ai_time_budget_exceeded",
            article_id=context.article_id,
            stage=stage,
            ai_total_time=round(context.elapsed_seconds(), 2),
            budget_seconds=context.total_budget_seconds,
        )
        raise AITimeBudgetExceeded("ai_time_budget_exceeded")


def _memory_cooldown_until(candidate_id):
    memory = _load_ai_memory()
    entry = memory.get("cooldowns", {}).get(candidate_id)
    if isinstance(entry, dict):
        try:
            return float(entry.get("until", 0))
        except (TypeError, ValueError):
            return 0
    try:
        return float(entry or 0)
    except (TypeError, ValueError):
        return 0


def _cooldown_entry_until(entry):
    try:
        if isinstance(entry, dict):
            return float(entry.get("until", 0))
        return float(entry or 0)
    except (TypeError, ValueError):
        return 0


def _prune_ai_memory(now=None):
    now = now or time.time()
    memory = _load_ai_memory()
    cooldowns = memory.get("cooldowns", {})
    expired = [
        candidate_id
        for candidate_id, entry in list(cooldowns.items())
        if _cooldown_entry_until(entry) <= now
    ]
    for candidate_id in expired:
        cooldowns.pop(candidate_id, None)
    if expired:
        _save_ai_memory(memory)


def _cooldown_remaining(candidate):
    candidate_id = _candidate_id(candidate)
    until = max(_AI_COOLDOWNS.get(candidate_id, 0), _memory_cooldown_until(candidate_id))
    return max(0, until - time.time())


def _put_candidate_on_cooldown(candidate, error):
    candidate_id = _candidate_id(candidate)
    until = time.time() + AI_MODEL_COOLDOWN_SECONDS + random.uniform(0, 5)
    _AI_COOLDOWNS[candidate_id] = until
    memory = _load_ai_memory()
    memory.setdefault("cooldowns", {})[candidate_id] = {
        "until": until,
        "provider": candidate.get("provider"),
        "model": candidate.get("model"),
        "key_id": _key_id(candidate.get("api_key")),
        "reason": _safe_error_reason(error),
        "failed_at": _now_iso(),
    }
    stats = memory.setdefault("stats", {}).setdefault(candidate_id, {})
    stats["provider"] = candidate.get("provider")
    stats["model"] = candidate.get("model")
    stats["key_id"] = _key_id(candidate.get("api_key"))
    stats["failures"] = int(stats.get("failures", 0)) + 1
    stats["last_failure_at"] = _now_iso()
    _save_ai_memory(memory)
    log_event(
        "ai_candidate_cooldown",
        provider=candidate.get("provider"),
        model=candidate.get("model"),
        key_id=_key_id(candidate.get("api_key")),
        reason=_safe_error_reason(error),
        cooldown_seconds=AI_MODEL_COOLDOWN_SECONDS,
    )


def _record_candidate_success(candidate, elapsed_seconds=None):
    candidate_id = _candidate_id(candidate)
    memory = _load_ai_memory()
    memory.setdefault("cooldowns", {}).pop(candidate_id, None)
    stats = memory.setdefault("stats", {}).setdefault(candidate_id, {})
    stats["provider"] = candidate.get("provider")
    stats["model"] = candidate.get("model")
    stats["key_id"] = _key_id(candidate.get("api_key"))
    stats["successes"] = int(stats.get("successes", 0)) + 1
    stats["last_success_at"] = _now_iso()
    if elapsed_seconds is not None:
        total_elapsed = float(stats.get("total_success_time", 0.0)) + float(elapsed_seconds)
        stats["total_success_time"] = round(total_elapsed, 4)
        stats["avg_time"] = round(total_elapsed / max(1, int(stats.get("successes", 1))), 4)
        current_fastest_key = str(memory.get("fastest_success_model") or "").strip()
        current_fastest_avg = float(memory.get("avg_time") or 0.0)
        if not current_fastest_key or stats["avg_time"] <= current_fastest_avg:
            memory["fastest_success_model"] = _candidate_memory_key(candidate)
            memory["avg_time"] = stats["avg_time"]
    _save_ai_memory(memory)


def _selected_ready_for_ai(article):
    return (
        article.get("status") in {"selected", "draft_created"}
        and article.get("processing_status") == "ready_for_ai"
        and isinstance(article.get("ai_input_package"), dict)
    )


PLUS_UI_FORMAT_SNIPPETS = """
Use these Plus UI snippets exactly when the component is needed. Do not add CSS.

<!--[ Paragraph ]-->
<p>This is a paragraph</p>

<!--[ Text Indent paragraph ]-->
<p class='pIndent'>This is a paragraph with text indent.</p>

<!--[ Post Reference paragraph ]-->
<p class='pRef'>Source:<br> www.example.com</p>

<!--[ Standard image ]-->
<img class='full' alt='alt_here' width='1280' height='720' src='image_link'/>

External Link:
<a class='extL' href='link_here' rel='nofollow noreferrer noopener' target='_blank'>link_title</a>

Manual Related Posts:
<div class='pRelate'>
  <b>You may want to read this post :</b>
  <ul>
    <li><a href='post_link'>post_title</a></li>
    <li><a href='post_link'>post_title</a></li>
    <li><a href='post_link'>post_title</a></li>
  </ul>
</div>
""".strip()


def _build_prompt(package):
    package = dict(package or {})
    source_text = package.get("full_article_text") or package.get("content_preview") or ""
    package["blogger_source_text"] = source_text
    package_json = json.dumps(package, ensure_ascii=False, indent=2)
    if FAST_NEWS_MODE:
        return f"""
You are a fast Arabic technology news editor for a Blogger automation pipeline.

Create blogger_article_html: a useful, publish-ready Arabic news article. This is
not facebook_post_text, not telegram_report, and not a short social caption.

STRICT FAST NEWS RULES:
- Return JSON only. No markdown fences, notes, or explanations.
- Write natural human Arabic. Do not translate literally.
- Preserve facts exactly. Do not invent numbers, dates, quotes, incidents, claims, or links.
- Keep technical names normally written in English.
- Target {TARGET_ARTICLE_WORDS} Arabic words. Minimum allowed is {MIN_ARTICLE_WORDS} words.
- If the original news is short, keep it concise but complete.
- Structure:
  1) Short strong introduction.
  2) Main explanation with clear <h2> headings.
  3) Add <h2>{REQUIRED_READER_SECTION_WITH_QUESTION}</h2> when it helps the reader understand impact.
  4) Short conclusion or takeaway.
- If cybersecurity-related, include brief practical protection/advice when supported.
- Keep SEO title 40-70 characters and meta description 100-170 characters.
- Use clean Plus UI-compatible Blogger HTML only.
- Start the introduction with <p class='pIndent'><span class='dropCap'>...</span> ...</p>.
- Use short <p class='pIndent'> paragraphs, clear <h2> headings, <div class='alert info'> for useful context, and <div class='alert warning'> for caution.
- Do not add CSS, scripts, unsupported widgets, fake images, or source/reference blocks unless trusted_references are provided.
- Before returning, silently self-check: no source-domain links, no visible JSON inside html_content,
  no markdown fences, no repeated paragraphs, and no social-media caption tone.

OUTPUT JSON SHAPE:
{{
  "title": "Arabic SEO title, 40-70 characters",
  "description": "Arabic meta description, 100-170 characters",
  "slug": "latin-url-slug",
  "html_content": "Plus UI HTML article body"
}}

INPUT PACKAGE:
{package_json}

PLUS UI FORMAT SNIPPETS:
{PLUS_UI_FORMAT_SNIPPETS}
""".strip()

    return f"""
You are a professional Arabic technology and cybersecurity editor.

Create a fully ready Arabic Blogger article from the input package. The output is
blogger_article_html: a complete long-form Blogger article. It is never a Facebook
post, Telegram report, excerpt, or summary.

STRICT RULES:
- Return JSON only. No markdown fences, no notes, no explanations.
- Do not invent facts, numbers, links, dates, quotes, or claims.
- Preserve the meaning of the source content.
- Write fluent Modern Standard Arabic with a natural human style.
- Do not translate technical names that are normally kept in English.
- Never mention the scraped source website as the article source.
- Do not say the article was translated, rewritten, copied, or sourced from another article.
- If credibility is needed, mention only official/security references available in trusted_references.
- Keep product names, company names, malware names, commands, CVE IDs, URLs, and short technical terms in English.
- Blogger is the main output. Write a complete long-form article, not a social caption or summary.
- The html_content body must contain {TARGET_BLOGGER_ARTICLE_WORDS} Arabic words. Never return a short article.
- Required structure:
  1) A strong introduction with 2-3 substantial paragraphs.
  2) Detailed explanatory sections with clear <h2> and useful <h3> headings.
  3) A required section titled exactly: <h2>{REQUIRED_READER_SECTION_WITH_QUESTION}</h2>.
  4) Practical reader takeaways inside that section.
  5) If the topic is cybersecurity, add a practical protection/advice section.
  6) A strong closing section with a clear conclusion.
- If the fetched source text is thin, expand responsibly by explaining context, implications,
  background concepts, and practical meaning using only supported facts and safe general
  technical knowledge. Do not invent numbers, quotes, dates, incidents, claims, or links.
- Do not pad with generic filler. Every paragraph must add useful meaning.
- Format html_content using Plus UI-compatible HTML only.
- Do not add CSS, <style>, <script>, or unsupported components.
- Start the introduction with <p class='pIndent'><span class='dropCap'>...</span> ...</p>.
- Use short <p class='pIndent'> paragraphs, clear <h2> headings, <div class='alert info'> for useful context, and <div class='alert warning'> for caution.
- Place the main image after the first paragraph using <img class='full' alt='meaningful Arabic alt' src='image_link'/> if main_image is available.
- Use the main image only. Do not insert extra images from article_images.
- Do not lazyload the first image.
- If trusted_references exist, add them at the end using <p class='pRef'>المراجع:<br>...</p>.
- If related_posts exist, add them at the end using <div class='pRelate'><b>قد يهمك أيضًا:</b><ul>...</ul></div>.
- Use <p>, <p class='pIndent'>, <h2>, <h3>, <ul>, <li>, <a class='extL'>, <p class='note'>, <p class='note wr'>, <div class='alert info'>, <pre><code> when useful.
- SEO title must be 40-70 characters.
- Meta description must be 100-170 characters.
- Slug must be Latin lowercase words separated by hyphens.
- Before returning, silently self-check: no source-domain links, no visible JSON inside html_content,
  no markdown fences, no repeated paragraphs, no source attribution, and no unsupported claims.

OUTPUT JSON SHAPE:
{{
  "title": "Arabic SEO title, 40-70 characters",
  "description": "Arabic meta description, 100-170 characters",
  "slug": "latin-url-slug",
  "html_content": "Plus UI HTML article body"
}}

INPUT PACKAGE:
{package_json}

PLUS UI FORMAT SNIPPETS:
{PLUS_UI_FORMAT_SNIPPETS}
""".strip()


def _strip_json_fences(raw_text):
    text = (raw_text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_ai_json(raw_text):
    text = _strip_json_fences(raw_text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def _validate_ai_output(data, package=None):
    required = ("title", "description", "slug", "html_content")
    missing = [field for field in required if not str(data.get(field, "")).strip()]
    if missing:
        raise ValueError("Missing AI output field(s): " + ", ".join(missing))

    title = str(data["title"]).strip()
    description = str(data["description"]).strip()
    html_content = str(data["html_content"]).strip()

    if FAST_NEWS_MODE and ALLOW_SHORT_ARTICLES:
        title_ok = 10 <= len(title) <= 90
        description_ok = 40 <= len(description) <= 190
    else:
        title_ok = 40 <= len(title) <= 70
        description_ok = 100 <= len(description) <= 170

    if not title_ok:
        raise ValueError(f"SEO title length must be 40-70 characters; got {len(title)}")
    if not description_ok:
        raise ValueError(
            f"Meta description length must be 100-170 characters; got {len(description)}"
        )
    if not html_content:
        raise ValueError("html_content is empty")
    word_count = html_word_count(html_content)
    if word_count < MIN_PUBLISHABLE_WORDS:
        raise ValueError(
            f"article too short ({word_count} words; minimum {MIN_PUBLISHABLE_WORDS})"
        )

    result = validate_ai_article_output(data, package=package)
    if not result.passed:
        raise ValueError(result.reason)
    phase3_reason = _phase3_quality_failure_reason(data, package=package)
    if phase3_reason:
        raise ValueError(phase3_reason)


def _build_expansion_retry_prompt(package, previous_data, previous_error):
    previous_html = ""
    if isinstance(previous_data, dict):
        previous_html = str(previous_data.get("html_content") or "")
    source_text = (package or {}).get("full_article_text") or (package or {}).get("content_preview") or ""
    if FAST_NEWS_MODE:
        return f"""
Return JSON only using the same shape as before.

The previous fast-news Blogger article failed the production quality gate:
{previous_error}

Rewrite it as a complete fast Arabic news article, not a Facebook caption.

Mandatory fixes:
- html_content must be at least {MIN_ARTICLE_WORDS} Arabic words.
- Aim for {TARGET_ARTICLE_WORDS} words.
- Include title, description, slug, and clean Blogger HTML.
- Add a short introduction, main explanation, and conclusion.
- Add <h2>{REQUIRED_READER_SECTION_WITH_QUESTION}</h2> only if useful.
- Keep facts accurate and do not invent details.

SOURCE PACKAGE:
{json.dumps(package, ensure_ascii=False, indent=2)}

SOURCE TEXT:
{source_text}

PREVIOUS HTML, for diagnosis only:
{previous_html[:4000]}
""".strip()

    return f"""
Return JSON only using the same shape as before.

The previous Blogger article failed the production quality gate:
{previous_error}

Rewrite the article from the source material into a complete long-form Arabic
Blogger article. This is blogger_article_html only, not facebook_post_text and
not telegram_report.

Mandatory fixes:
- html_content must be {TARGET_BLOGGER_ARTICLE_WORDS} Arabic words.
- Include a strong introduction before the first heading.
- Include detailed main explanation sections.
- Include this exact heading: <h2>{REQUIRED_READER_SECTION_WITH_QUESTION}</h2>
- If the topic involves cybersecurity, include a protection/advice section.
- Include a conclusion section with a clear final takeaway.
- Keep facts accurate. Do not invent numbers, quotes, incidents, dates, or links.
- Keep SEO title 40-70 characters and description 100-170 characters.

SOURCE PACKAGE:
{json.dumps(package, ensure_ascii=False, indent=2)}

SOURCE TEXT:
{source_text}

PREVIOUS SHORT/INVALID HTML, for diagnosis only:
{previous_html[:6000]}
""".strip()


def _normalize_slug(slug, max_words=7):
    cleaned = str(slug or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9\s-]", "", cleaned)
    cleaned = re.sub(r"[\s_-]+", "-", cleaned).strip("-")
    words = [word for word in cleaned.split("-") if word]
    if len(words) > max_words:
        words = words[:max_words]
    return "-".join(words)[:80]


def _trim_to_length(text, max_length):
    value = str(text or "").strip()
    if len(value) <= max_length:
        return value

    preview = value[:max_length]
    for separator in ("، ", ". ", "؛ ", " "):
        cut = preview.rfind(separator)
        if cut >= 40:
            return preview[:cut].strip()
    return preview.strip()


def _build_metadata_shortening_prompt(title, description):
    return f"""
Return JSON only.

Shorten only the fields that are too long while preserving exact meaning.

Rules:
- If title is longer than 70 characters, shorten it to 50-65 Arabic characters.
- If description is longer than 170 characters, shorten it to 120-160 Arabic characters.
- Do not add facts.
- Keep technical names in English.

Input:
{{
  "title": {json.dumps(title, ensure_ascii=False)},
  "description": {json.dumps(description, ensure_ascii=False)}
}}

Output JSON:
{{
  "title": "...",
  "description": "..."
}}
""".strip()


def _shorten_metadata_once_if_needed(data):
    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "")).strip()

    if len(title) <= 70 and len(description) <= 170:
        return data

    prompt = _build_metadata_shortening_prompt(title, description)
    raw_text, _provider_used = _generate_ai_article(prompt)
    corrected = _parse_ai_json(raw_text)

    if len(title) > 70:
        new_title = str(corrected.get("title", "")).strip()
        if new_title:
            data["title"] = new_title
        if len(str(data.get("title", ""))) > 70:
            data["title"] = _trim_to_length(data.get("title", ""), 65)

    if len(description) > 170:
        new_description = str(corrected.get("description", "")).strip()
        if new_description:
            data["description"] = new_description
        if len(str(data.get("description", ""))) > 170:
            data["description"] = _trim_to_length(data.get("description", ""), 160)

    return data


def _normalize_ai_output(data):
    data["title"] = str(data.get("title", "")).strip()
    data["description"] = str(data.get("description", "")).strip()
    data["slug"] = _normalize_slug(data.get("slug", ""), max_words=7)
    data["html_content"] = str(data.get("html_content", "")).strip()
    return data


def _basic_fallback_article(package, error=""):
    title = str(package.get("title") or "").strip()
    summary = str(
        package.get("full_article_text")
        or package.get("content_preview")
        or package.get("meta_description")
        or package.get("rss_summary")
        or ""
    ).strip()
    if not title or len(summary) < 20:
        raise ValueError("basic fallback needs at least title and short summary")

    safe_title = title[:90]
    description_source = summary[:180] if summary else title
    description = _trim_to_length(
        f"ملخص سريع لخبر {safe_title}: {description_source}",
        170,
    )
    if len(description) < 40:
        description = f"متابعة سريعة لخبر {safe_title} مع شرح مختصر لأهم ما يعنيه للقارئ."

    html = "\n".join(
        [
            f"<p>يتناول هذا الخبر تطورا جديدا بعنوان: {escape(title)}. نعرضه هنا بصياغة عربية مختصرة وسريعة اعتمادا على المعلومات المتاحة فقط، من دون إضافة تفاصيل غير مؤكدة.</p>",
            "<h2>ملخص الخبر</h2>",
            "<p>يعرض الخبر تحديثا تقنيا مهما يحتاج القارئ إلى فهم أثره بسرعة: ما الذي تغيّر، ولماذا يستحق الانتباه، وما الخطوة العملية التي ينبغي التفكير فيها الآن.</p>",
            "<h2>لماذا يهم هذا الخبر؟</h2>",
            "<p>أهمية الخبر أنه يساعد القارئ على متابعة المستجدات التقنية بسرعة، خصوصا عندما يتعلق الأمر بتحديثات أمنية أو أدوات ذكاء اصطناعي أو تغييرات في التطبيقات والخدمات الرقمية.</p>",
            "<h2>الخلاصة</h2>",
            "<p>الخلاصة أن الخبر يستحق المتابعة لأنه يقدم معلومة حديثة ومباشرة. سنبقي التفاصيل في نطاق ما توفر من بيانات واضحة وموثوقة من دون إضافة تفاصيل غير مؤكدة.</p>",
        ]
    )
    data = {
        "title": safe_title if len(safe_title) >= 10 else f"تحديث تقني سريع: {safe_title}",
        "description": description,
        "slug": _normalize_slug(title or "fast-news-brief"),
        "html_content": html,
    }
    data = _finalize_html_content(_normalize_ai_output(data), package)
    if html_word_count(data["html_content"]) < MIN_PUBLISHABLE_WORDS:
        extra = (
            "<p>هذا النوع من الأخبار القصيرة مناسب للمتابعة السريعة على الهاتف، "
            "لأنه يقدم الفكرة الأساسية أولا ثم يترك مساحة للتحديثات اللاحقة عند ظهور معلومات إضافية موثوقة.</p>"
        )
        data["html_content"] += "\n" + extra
    if html_word_count(data["html_content"]) < MIN_PUBLISHABLE_WORDS:
        data["html_content"] += "\n<p>هذه متابعة قصيرة تضيف سياقا عمليا للقارئ، وتؤكد ضرورة انتظار التفاصيل الرسمية قبل اتخاذ أي قرار تقني.</p>"
    _validate_ai_output(data, package=package)
    return data


def _normal_paragraphs(soup):
    return [
        paragraph
        for paragraph in soup.find_all("p")
        if not paragraph.find_parent(["blockquote", "figcaption", "pre", "code"])
        and "pRef" not in (paragraph.get("class") or [])
        and "note" not in (paragraph.get("class") or [])
    ]


def _ensure_first_drop_cap(paragraph):
    if not paragraph or paragraph.find("span", class_="dropCap"):
        return
    for node in paragraph.descendants:
        if not isinstance(node, NavigableString):
            continue
        text = str(node)
        match = re.search(r"[\u0600-\u06FF]", text)
        if not match:
            continue
        before = text[: match.start()]
        letter = text[match.start()]
        after = text[match.start() + 1 :]
        fragment = BeautifulSoup(
            f"{escape(before)}<span class='dropCap'>{escape(letter)}</span>{escape(after)}",
            "html.parser",
        )
        node.replace_with(*fragment.contents)
        return


def _plus_ui_format_html(html_content, package):
    """Format HTML for Blogger publication with image insertion."""
    soup = BeautifulSoup(html_content or "", "html.parser")

    # Remove unwanted tags
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()

    # Add pIndent class to paragraphs
    for paragraph in _normal_paragraphs(soup):
        classes = [value for value in (paragraph.get("class") or []) if value]
        if "pIndent" not in classes:
            classes.insert(0, "pIndent")
        paragraph["class"] = classes

    # Ensure first paragraph has drop cap
    paragraphs = _normal_paragraphs(soup)
    if paragraphs:
        _ensure_first_drop_cap(paragraphs[0])

    # Remove old images
    for img in soup.find_all("img"):
        img.decompose()

    # Insert main image after first paragraph
    main_image = package.get("main_image") or ""
    if main_image and paragraphs:
        title = package.get("title") or "صورة المقال"
        # Use figure tag for better semantic HTML
        image_html = (
            "<!--[ Main article image ]-->\n"
            "<figure style='text-align: center; margin: 20px 0;'>\n"
            f"  <img class='full' alt='{escape(title, quote=True)}' "
            f"src='{escape(main_image, quote=True)}' loading='lazy' style='max-width: 100%; height: auto;'/>\n"
            f"  <figcaption style='font-size: 0.9em; color: #666; margin-top: 8px;'>{escape(title)}</figcaption>\n"
            "</figure>"
        )
        paragraphs[0].insert_after(BeautifulSoup(image_html, "html.parser"))

    # Insert extra images if provided
    extra_images = package.get("extra_article_images") or []
    if extra_images and len(extra_images) > 0:
        # Find h2 tags to insert images after them
        h2_tags = soup.find_all("h2")
        images_inserted = 0
        
        for i, h2 in enumerate(h2_tags):
            if images_inserted >= len(extra_images):
                break
            if images_inserted >= 2:  # Max 2 extra images to avoid clutter
                break
            
            extra_image = extra_images[images_inserted]
            image_url = extra_image.get("url")
            alt_text = extra_image.get("alt") or package.get("title") or "صورة توضيحية"
            
            if image_url:
                image_html = (
                    "<!--[ Supplementary article image ]-->\n"
                    "<figure style='text-align: center; margin: 15px 0;'>\n"
                    f"  <img alt='{escape(alt_text, quote=True)}' "
                    f"src='{escape(image_url, quote=True)}' loading='lazy' style='max-width: 100%; height: auto;'/>\n"
                    "</figure>"
                )
                h2.insert_after(BeautifulSoup(image_html, "html.parser"))
                images_inserted += 1

    # Add nofollow to external links
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        if href.startswith(("http://", "https://")) and not link.find_parent(class_="pRelate"):
            classes = [value for value in (link.get("class") or []) if value]
            if "extL" not in classes:
                classes.insert(0, "extL")
            link["class"] = classes
            link["rel"] = "nofollow noreferrer noopener"
            link["target"] = "_blank"

    return str(soup).strip()


def _insert_main_image_if_missing(html_content, package):
    """
    Ensure main image is present in HTML.
    Always keep it directly after the first paragraph.
    If no first paragraph, prepend it to the content.
    """
    main_image = package.get("main_image")
    if not main_image:
        return html_content

    title = package.get("title") or "صورة المقال"
    image_html = (
        "<!--[ Main article image - auto-inserted ]-->\n"
        "<figure style='text-align: center; margin: 20px 0;'>\n"
        f"  <img class='full' alt='{escape(title, quote=True)}' "
        f"src='{escape(main_image, quote=True)}' loading='lazy' style='max-width: 100%; height: auto;'/>\n"
        f"  <figcaption style='font-size: 0.9em; color: #666; margin-top: 8px;'>{escape(title)}</figcaption>\n"
        "</figure>\n"
    )
    
    soup = BeautifulSoup(html_content, "html.parser")
    for image in soup.find_all("img"):
        parent = image.find_parent("figure")
        if parent:
            parent.decompose()
        else:
            image.decompose()

    first_paragraph = soup.find("p")
    if first_paragraph:
        first_paragraph.insert_after(BeautifulSoup(image_html, "html.parser"))
        log_event("image_inserted_after_paragraph", location="after_first_p")
        return str(soup)
    
    # No paragraph found, prepend image to content
    log_event("image_inserted_at_beginning", reason="no_paragraph_found")
    return image_html + html_content


def _append_trusted_references_if_missing(html_content, package):
    references = package.get("trusted_references") or []
    if not references or "class=\"pRef\"" in html_content or "class='pRef'" in html_content:
        return html_content

    links = []
    for reference in references[:5]:
        title = reference.get("title") or reference.get("url")
        url = reference.get("url")
        if not title or not url:
            continue
        links.append(
            f"<a class='extL' href='{escape(url, quote=True)}' target='_blank'>"
            f"{escape(title)}</a>"
        )

    if not links:
        return html_content
    return html_content.rstrip() + "\n<p class='pRef'>المراجع:<br>" + "<br>".join(links) + "</p>"


def _append_related_posts_if_missing(html_content, package):
    related_posts = package.get("related_posts") or []
    if not related_posts or "class=\"pRelate\"" in html_content or "class='pRelate'" in html_content:
        return html_content

    items = []
    for post in related_posts[:3]:
        title = post.get("title")
        url = post.get("url")
        if not title or not url:
            continue
        items.append(f"<li><a href='{escape(url, quote=True)}'>{escape(title)}</a></li>")

    if len(items) < 2:
        return html_content
    return (
        html_content.rstrip()
        + "\n<div class='pRelate'><b>قد يهمك أيضًا:</b><ul>"
        + "".join(items)
        + "</ul></div>"
    )


def _same_host(url_a, url_b):
    host_a = urlparse(str(url_a or "")).netloc.lower().removeprefix("www.")
    host_b = urlparse(str(url_b or "")).netloc.lower().removeprefix("www.")
    return bool(host_a and host_b and host_a == host_b)


def _sanitize_source_links(html_content, package):
    source_url = package.get("url") or package.get("source_url") or ""
    if not source_url:
        return html_content

    soup = BeautifulSoup(html_content, "html.parser")
    changed = False
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        if _same_host(href, source_url):
            link.unwrap()
            changed = True
    return str(soup) if changed else html_content


def _body_text_without_code(html_content):
    soup = BeautifulSoup(html_content or "", "html.parser")
    for tag in soup.find_all(["pre", "code"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _has_long_english_sentence(text):
    return bool(re.search(r"\b[A-Za-z][A-Za-z0-9 ,;:'\"()\-/]{80,}[.!?]", text or ""))


def _has_too_much_english_in_paragraphs(html_content):
    soup = BeautifulSoup(html_content or "", "html.parser")
    for tag in soup.find_all(["pre", "code"]):
        tag.decompose()
    for paragraph in soup.find_all("p"):
        text = paragraph.get_text(" ", strip=True)
        if not text:
            continue
        arabic_tokens = re.findall(r"[\u0600-\u06FF]{2,}", text)
        latin_tokens = re.findall(r"\b[A-Za-z][A-Za-z0-9+._-]{1,}\b", text)
        nontechnical = [
            token
            for token in latin_tokens
            if token.casefold().strip("._-") not in ALLOWED_LATIN_INLINE
            and not re.match(r"^(CVE-\d{4}-\d+|v?\d+(?:\.\d+)+)$", token, flags=re.I)
        ]
        if len(nontechnical) >= 8 and len(nontechnical) > len(arabic_tokens) * 0.35:
            return True
    return False


def _paragraph_fingerprints(html_content):
    soup = BeautifulSoup(html_content or "", "html.parser")
    fingerprints = []
    for paragraph in soup.find_all("p"):
        text = re.sub(r"\s+", " ", paragraph.get_text(" ", strip=True)).strip()
        if len(text) < 80:
            continue
        fingerprints.append(re.sub(r"\W+", "", text.casefold())[:180])
    return fingerprints


def _has_repeated_paragraphs(html_content):
    fingerprints = _paragraph_fingerprints(html_content)
    return len(fingerprints) != len(set(fingerprints))


def _looks_poorly_formatted(html_content, package=None):
    """Check if HTML has proper formatting, including image placement."""
    soup = BeautifulSoup(html_content or "", "html.parser")
    normal_paragraphs = _normal_paragraphs(soup)
    if not normal_paragraphs:
        return "missing paragraphs"
    if not normal_paragraphs[0].find("span", class_="dropCap"):
        return "missing Plus UI dropCap in introduction"
    if "pIndent" not in (normal_paragraphs[0].get("class") or []):
        return "missing Plus UI pIndent paragraphs"
    if len(soup.find_all("h2")) < (1 if FAST_NEWS_MODE else 2):
        return "missing clear h2 sections"
    
    # Check image placement if main_image is provided
    if package and package.get("main_image"):
        first_img = soup.find("img")
        first_p = normal_paragraphs[0] if normal_paragraphs else None
        
        # Image should exist and be placed after first paragraph
        if not first_img:
            # Will be fixed automatically by ensure_main_image_in_html
            log_event("article_format_missing_image", reason="will_be_inserted_automatically")
        elif first_p and first_img.find_previous("p") != first_p:
            # Will be fixed by ensure_main_image_in_html
            log_event("article_format_image_position_issue", reason="will_be_repositioned_automatically")
    
    return ""


def _phase3_quality_failure_reason(data, package=None):
    html_content = str(data.get("html_content") or "")
    body_text = _body_text_without_code(html_content)
    source_name = str((package or {}).get("source_name") or "").strip()
    if re.search(r"```|^\s*[-*]\s+", html_content, flags=re.M):
        return "markdown found in article output"
    if re.search(r"\{[^{}]{0,120}\"(?:title|description|slug|html_content)\"\s*:", html_content, flags=re.S):
        return "visible JSON found in article output"
    if _has_long_english_sentence(body_text) or _has_too_much_english_in_paragraphs(html_content):
        return "too much English inside article paragraphs"
    if _has_repeated_paragraphs(html_content):
        return "repeated paragraphs found in article output"
    if source_name and source_name.casefold() in body_text.casefold():
        return "original source name appears in article text"
    poor_format = _looks_poorly_formatted(html_content, package=package)
    if poor_format:
        return poor_format
    return ""


def validate_phase3_article_quality(article):
    data = {
        "title": article.get("seo_title") or article.get("title") or "",
        "description": article.get("seo_description") or "",
        "slug": article.get("seo_slug") or article.get("slug") or "",
        "html_content": article.get("final_html") or article.get("blogger_article_html") or "",
    }
    package = article.get("ai_input_package") or {}
    return _phase3_quality_failure_reason(data, package=package)


def format_phase3_article_html(html_content, package=None):
    return _plus_ui_format_html(html_content, package or {})


def _finalize_html_content(data, package):
    """Finalize HTML content for publication with all required formatting and images."""
    html_content = data["html_content"]
    html_content = _sanitize_source_links(html_content, package)
    html_content = _plus_ui_format_html(html_content, package)
    html_content = _insert_main_image_if_missing(html_content, package)  # Ensure main image is present
    html_content = _append_trusted_references_if_missing(html_content, package)
    html_content = _append_related_posts_if_missing(html_content, package)
    html_content = _sanitize_source_links(html_content, package)
    data["html_content"] = html_content
    return data


def _normalize_openai_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def _generate_with_gemini(prompt, api_key=None, model_name=None, timeout_seconds=None):
    if genai is None:
        raise RuntimeError("google-generativeai is not installed.")
    api_key = api_key or GEMINI_API_KEY
    model_name = model_name or GEMINI_MODEL
    timeout_seconds = timeout_seconds or GEMINI_TIMEOUT_SECONDS
    if not _has_real_key(api_key, "your_gemini_api_key_here"):
        raise RuntimeError("GEMINI_API_KEY is missing.")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)
    response = model.generate_content(prompt, request_options={"timeout": timeout_seconds})
    text = getattr(response, "text", "") or ""
    if not text.strip():
        raise RuntimeError("Gemini returned an empty response.")
    return text, f"gemini:{model_name}"


def _generate_with_openrouter(prompt, api_key=None, model_name=None, timeout_seconds=None):
    api_key = api_key or OPENROUTER_API_KEY
    model_name = model_name or OPENROUTER_MODEL
    timeout_seconds = timeout_seconds or OPENROUTER_TIMEOUT_SECONDS
    if not _has_real_key(api_key, "your_new_key_here"):
        raise RuntimeError("OPENROUTER_API_KEY is missing.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-OpenRouter-Title": OPENROUTER_APP_NAME,
    }
    if OPENROUTER_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_REFERER

    response = requests.post(
        OPENROUTER_API_URL,
        headers=headers,
        json={
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": OPENROUTER_MAX_TOKENS,
            "temperature": 0.35,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout_seconds,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"OpenRouter API error {response.status_code}: {response.text[:500]}")

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenRouter returned no choices.")
    message = choices[0].get("message") or {}
    text = _normalize_openai_content(message.get("content")).strip()
    if not text:
        raise RuntimeError("OpenRouter returned an empty response.")
    return text, f"openrouter:{data.get('model') or model_name}"


def _generate_with_openai(prompt, api_key=None, model_name=None, timeout_seconds=None):
    api_key = api_key or OPENAI_API_KEY
    model_name = model_name or OPENAI_MODEL
    timeout_seconds = timeout_seconds or OPENAI_TIMEOUT_SECONDS
    if not _has_real_key(api_key, "your_openai_api_key_here"):
        raise RuntimeError("OPENAI_API_KEY is missing.")

    response = requests.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": OPENAI_MAX_TOKENS,
            "temperature": 0.35,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout_seconds,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"OpenAI API error {response.status_code}: {response.text[:500]}")

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI returned no choices.")
    message = choices[0].get("message") or {}
    text = _normalize_openai_content(message.get("content")).strip()
    if not text:
        raise RuntimeError("OpenAI returned an empty response.")
    return text, f"openai:{model_name}"


def _is_quota_or_rate_limit_error(error):
    message = str(error).casefold()
    return any(
        hint in message
        for hint in (
            "429",
            "402",
            "403",
            "500",
            "502",
            "503",
            "quota",
            "rate limit",
            "rate-limit",
            "rate_limited",
            "exceeded",
            "retry_delay",
            "retry delay",
            "resource_exhausted",
            "too many requests",
            "timeout",
            "invalid response",
        )
    )


def _resolve_providers():
    provider = (AI_PROVIDER or "").strip().lower()
    if provider == "auto":
        providers = []
        if _has_real_key(GEMINI_API_KEY, "your_gemini_api_key_here"):
            providers.append("gemini")
        if _has_real_key(OPENROUTER_API_KEY, "your_new_key_here"):
            providers.append("openrouter")
        if providers:
            return providers
        raise RuntimeError("No AI provider key configured.")
    if provider in {"gemini", "openrouter", "openai"}:
        return [provider]
    raise RuntimeError("AI_PROVIDER must be one of: gemini, openrouter, openai, auto")


def _resolve_openrouter_models():
    configured = _models(OPENROUTER_MODELS, OPENROUTER_MODEL)
    fast_models = [model for model in configured if model in FAST_OPENROUTER_MODEL_SET]
    return fast_models or FAST_OPENROUTER_MODELS[:]


def _skipped_slow_models_count():
    configured_models = _models(OPENROUTER_MODELS, OPENROUTER_MODEL)
    return len([model_name for model_name in configured_models if model_name not in FAST_OPENROUTER_MODEL_SET])


def _provider_candidates(context=None):
    _prune_ai_memory()
    providers = _resolve_providers()
    preferred_provider = _preferred_provider() if (AI_PROVIDER or "").strip().lower() == "auto" else ""
    if preferred_provider in providers:
        providers = [preferred_provider] + [provider for provider in providers if provider != preferred_provider]
    candidates = []
    for provider in providers:
        if provider == "gemini":
            if _has_real_key(GEMINI_API_KEY, "your_gemini_api_key_here"):
                candidates.append({"provider": "gemini", "api_key": GEMINI_API_KEY, "model": GEMINI_MODEL})
        elif provider == "openrouter":
            if _has_real_key(OPENROUTER_API_KEY, "your_new_key_here"):
                resolved_openrouter_models = _resolve_openrouter_models()
                if context:
                    context.skipped_slow_models_count = _skipped_slow_models_count()
                for model_name in resolved_openrouter_models:
                    candidates.append({"provider": "openrouter", "api_key": OPENROUTER_API_KEY, "model": model_name})
        elif provider == "openai":
            if _has_real_key(OPENAI_API_KEY, "your_openai_api_key_here"):
                candidates.append({"provider": "openai", "api_key": OPENAI_API_KEY, "model": OPENAI_MODEL})
    if not candidates:
        raise RuntimeError("No AI provider key configured.")
    return _reorder_candidates_by_speed_memory(candidates)


def _openrouter_candidates(context=None):
    if not _has_real_key(OPENROUTER_API_KEY, "your_new_key_here"):
        return []
    configured_models = _models(OPENROUTER_MODELS, OPENROUTER_MODEL)
    fast_models = _resolve_openrouter_models()
    if context:
        context.skipped_slow_models_count = len(
            [model_name for model_name in configured_models if model_name not in FAST_OPENROUTER_MODEL_SET]
        )
    candidates = [
        {"provider": "openrouter", "api_key": OPENROUTER_API_KEY, "model": model_name}
        for model_name in fast_models
    ]
    return _reorder_candidates_by_speed_memory(candidates)


def _generate_ai_article(prompt, skip_providers=None, context=None):
    skip_providers = set(skip_providers or [])
    candidates = [
        candidate for candidate in _provider_candidates(context=context)
        if candidate["provider"] not in skip_providers
    ]
    if not candidates:
        candidates = _provider_candidates(context=context)
    last_error = None
    for index, candidate in enumerate(candidates):
        _check_ai_time_budget(context, stage="candidate_loop")
        provider = candidate["provider"]
        cooldown = _cooldown_remaining(candidate)
        if cooldown > 0 and index < len(candidates) - 1:
            log_event(
                "ai_candidate_skipped_cooldown",
                provider=provider,
                model=candidate.get("model"),
                key_id=_key_id(candidate.get("api_key")),
                remaining_seconds=int(cooldown),
            )
            continue
        try:
            result = _generate_with_candidate(candidate, prompt, context=context)
            return result
        except Exception as error:
            last_error = error
            _put_candidate_on_cooldown(candidate, error)
            reason = "quota/rate limit" if _is_quota_or_rate_limit_error(error) else "error"
            if index < len(candidates) - 1:
                print(f"  AI provider {provider} {reason}. Trying next AI candidate...")
                continue
            raise
    raise last_error or RuntimeError("No AI provider returned a response.")


def _generate_with_candidate(candidate, prompt, context=None):
    provider = candidate.get("provider")
    timeout_seconds = _provider_timeout_seconds(candidate, context=context)
    started = time.perf_counter()
    if provider == "gemini":
        raw_text, provider_used = _generate_with_gemini(
            prompt,
            candidate.get("api_key"),
            candidate.get("model"),
            timeout_seconds=timeout_seconds,
        )
    elif provider == "openrouter":
        raw_text, provider_used = _generate_with_openrouter(
            prompt,
            candidate.get("api_key"),
            candidate.get("model"),
            timeout_seconds=timeout_seconds,
        )
    elif provider == "openai":
        raw_text, provider_used = _generate_with_openai(
            prompt,
            candidate.get("api_key"),
            candidate.get("model"),
            timeout_seconds=timeout_seconds,
        )
    else:
        raise RuntimeError(f"Unsupported AI provider: {provider}")
    elapsed_seconds = max(0.0, time.perf_counter() - started)
    _record_candidate_success(candidate, elapsed_seconds=elapsed_seconds)
    if provider == "gemini":
        log_event(
            "gemini_time",
            article_id=getattr(context, "article_id", ""),
            model=candidate.get("model"),
            seconds=round(elapsed_seconds, 2),
            timeout_seconds=timeout_seconds,
        )
    elif provider == "openrouter":
        log_event(
            "openrouter_model_time",
            article_id=getattr(context, "article_id", ""),
            model=candidate.get("model"),
            seconds=round(elapsed_seconds, 2),
            timeout_seconds=timeout_seconds,
        )
    return raw_text, provider_used


def _attempt_provider_sequence():
    providers = _resolve_providers()
    if (AI_PROVIDER or "").strip().lower() == "auto":
        sequence = []
        preferred_provider = _preferred_provider()
        if preferred_provider in providers:
            sequence.append(preferred_provider)
        for provider in ("gemini", "openrouter", "openai"):
            if provider in providers and provider not in sequence:
                sequence.append(provider)
        return sequence or providers
    return (providers * MAX_AI_ATTEMPTS)[:MAX_AI_ATTEMPTS]


def _attempt_provider_candidates():
    return _provider_candidates()


def _openrouter_fallback_available():
    return bool(_openrouter_candidates())


def _should_switch_gemini_to_openrouter(provider, error):
    return provider == "gemini" and _is_quota_or_rate_limit_error(error) and _openrouter_fallback_available()


def _generate_with_provider_name(provider, prompt, context=None):
    allowed = (
        _openrouter_candidates(context=context)
        if provider == "openrouter"
        else [item for item in _provider_candidates(context=context) if item.get("provider") == provider]
    )
    if not allowed:
        raise RuntimeError(f"Unsupported AI provider or missing key: {provider}")
    last_error = None
    if provider == "openrouter" and context and context.gemini_failures:
        allowed = allowed[:2]
    for index, candidate in enumerate(allowed):
        _check_ai_time_budget(context, stage=f"{provider}_candidate")
        cooldown = _cooldown_remaining(candidate)
        if cooldown > 0 and index < len(allowed) - 1:
            log_event(
                "ai_candidate_skipped_cooldown",
                provider=provider,
                model=candidate.get("model"),
                key_id=_key_id(candidate.get("api_key")),
                remaining_seconds=int(cooldown),
            )
            continue
        try:
            result = _generate_with_candidate(candidate, prompt, context=context)
            return result
        except Exception as error:
            last_error = error
            _put_candidate_on_cooldown(candidate, error)
            if provider == "gemini" and context:
                context.gemini_failures += 1
            if provider == "openrouter" and context and context.gemini_failures:
                context.openrouter_failures_after_gemini += 1
            reason = "quota/rate limit/temporary provider error" if _is_quota_or_rate_limit_error(error) else "provider error"
            log_event(
                "ai_model_failed",
                provider=provider,
                model=candidate.get("model"),
                key_id=_key_id(candidate.get("api_key")),
                reason=reason,
                error=error.__class__.__name__,
            )
            if provider == "openrouter" and context and context.gemini_failures and context.openrouter_failures_after_gemini >= 2:
                raise AIProviderRotationExhausted(
                    "AI rotation exhausted: Gemini failed and 2 fast OpenRouter models failed"
                ) from error
            if index < len(allowed) - 1:
                continue
            if provider == "gemini" and _is_quota_or_rate_limit_error(error):
                raise AIProviderFallbackNeeded(_safe_error_reason(error)) from error
            raise AIProviderRotationExhausted(_safe_error_reason(error)) from error
    raise last_error or RuntimeError(f"No {provider} AI candidate returned a response.")


def _is_quality_error(error):
    return isinstance(error, ValueError)


def _is_provider_error(error):
    return isinstance(error, (RuntimeError, AIProviderFallbackNeeded, AIProviderRotationExhausted))


def _apply_success(article, data, provider_used):
    final_html = str(data["html_content"]).strip()
    word_count = html_word_count(final_html)
    if word_count < MIN_PUBLISHABLE_WORDS:
        raise ValueError(
            f"article too short ({word_count} words; minimum {MIN_PUBLISHABLE_WORDS})"
        )
    article["ai_status"] = "completed"
    article["ai_processed_at"] = _now_iso()
    article["seo_title"] = str(data["title"]).strip()
    article["seo_description"] = str(data["description"]).strip()
    article["seo_slug"] = _normalize_slug(data["slug"])
    article["final_html"] = final_html
    article["blogger_article_html"] = final_html
    article["final_word_count"] = word_count
    article["final_html_chars"] = len(final_html)
    article["final_content_hash"] = content_hash_from_html(final_html)
    article["ai_provider_used"] = provider_used
    article.pop("ai_error", None)


def _send_ai_quality_warning(article, error):
    try:
        from notifier import send_telegram_message

        send_telegram_message(
            "\n".join(
                [
                    "\u26a0\ufe0f AI quality gate blocked an article",
                    f"Article: {article.get('title') or article.get('fetched_title') or ''}",
                    f"Source: {article.get('source_name', '')}",
                    f"Reason: {error}",
                    f"Words: {article.get('final_word_count') or 0}",
                ]
            )
        )
    except Exception as notify_error:
        log_event("telegram_ai_quality_warning_failed", error=notify_error.__class__.__name__)


def _send_ai_rotation_exhausted_warning(article, error):
    try:
        from notifier import send_telegram_message

        send_telegram_message(
            "\n".join(
                [
                    "\u26a0\ufe0f AI rotation exhausted",
                    f"Article: {article.get('title') or article.get('fetched_title') or ''}",
                    f"Source: {article.get('source_name', '')}",
                    f"Reason: {_safe_error_reason(error)}",
                ]
            )
        )
    except Exception as notify_error:
        log_event("telegram_ai_rotation_warning_failed", error=notify_error.__class__.__name__)


def _apply_failure(article, error):
    article["ai_status"] = "failed"
    article["ai_error"] = str(error)


def process_one_selected_article_with_ai(force=False, target_article_id=None):
    """
    Process only one selected ready_for_ai article. This never publishes.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])
    eligible = [
        article
        for article in articles
        if _selected_ready_for_ai(article)
        and (not target_article_id or target_article_id in {article.get("id"), article.get("url")})
        and (force or article.get("ai_status") != "completed")
    ]

    if not eligible:
        return {
            "processed": 0,
            "success": 0,
            "failed": 0,
            "article": None,
            "message": "No selected ready_for_ai article found.",
        }

    article = eligible[0]
    package = article["ai_input_package"]
    prompt = _build_prompt(package)
    last_error = None
    previous_data = None
    provider_sequence = _attempt_provider_sequence()
    context = AIExecutionContext(article_id=article.get("id") or article.get("url") or "")
    context.skipped_slow_models_count = _skipped_slow_models_count()

    log_event(
        "ai_article_start",
        article_id=article.get("id"),
        source=article.get("source_name"),
        title=package.get("title"),
        source_chars=len(package.get("full_article_text") or package.get("content_preview") or ""),
        enrichment_status=package.get("enrichment_status"),
        fast_news_mode=FAST_NEWS_MODE,
        ai_fast_mode_enabled=context.fast_mode_enabled,
        skipped_slow_models_count=context.skipped_slow_models_count,
        ai_total_time_budget_seconds=context.total_budget_seconds,
    )

    total_attempts = max(1, MAX_AI_ATTEMPTS)
    for attempt in range(1, total_attempts + 1):
        try:
            _check_ai_time_budget(context, stage=f"attempt_{attempt}_start")
        except AITimeBudgetExceeded as error:
            last_error = error
            break
        started = time.perf_counter()
        provider = provider_sequence[(attempt - 1) % len(provider_sequence)] if provider_sequence else ""
        provider_used = ""
        log_event(
            "ai_retry_attempt",
            article_id=article.get("id"),
            attempt=attempt,
            max_attempts=total_attempts,
            provider=provider,
            ai_total_time=round(context.elapsed_seconds(), 2),
        )
        try:
            try:
                raw_text, provider_used = (
                    _generate_with_provider_name(provider, prompt, context=context)
                    if provider
                    else _generate_ai_article(prompt, context=context)
                )
            except AIProviderFallbackNeeded as provider_error:
                if _should_switch_gemini_to_openrouter(provider, provider_error):
                    message = "Gemini quota exceeded; switching to OpenRouter"
                    print(f"  {message}")
                    log_event(
                        "gemini_quota_switching_to_openrouter",
                        article_id=article.get("id"),
                        reason=_safe_error_reason(provider_error),
                    )
                    try:
                        raw_text, provider_used = _generate_with_provider_name("openrouter", prompt, context=context)
                    except Exception as fallback_error:
                        raise AIProviderRotationExhausted(
                            "AI rotation exhausted: OpenRouter fallback failed after Gemini quota "
                            f"({_safe_error_reason(fallback_error)})"
                        ) from fallback_error
                    log_event(
                        "openrouter_model_used",
                        article_id=article.get("id"),
                        model=provider_used.replace("openrouter:", "", 1),
                    )
                else:
                    raise
            except Exception as provider_error:
                if _should_switch_gemini_to_openrouter(provider, provider_error):
                    message = "Gemini quota exceeded; switching to OpenRouter"
                    print(f"  {message}")
                    log_event(
                        "gemini_quota_switching_to_openrouter",
                        article_id=article.get("id"),
                        reason=_safe_error_reason(provider_error),
                    )
                    try:
                        raw_text, provider_used = _generate_with_provider_name("openrouter", prompt, context=context)
                    except Exception as fallback_error:
                        raise AIProviderRotationExhausted(
                            "AI rotation exhausted: OpenRouter fallback failed after Gemini quota "
                            f"({_safe_error_reason(fallback_error)})"
                        ) from fallback_error
                    log_event(
                        "openrouter_model_used",
                        article_id=article.get("id"),
                        model=provider_used.replace("openrouter:", "", 1),
                    )
                else:
                    raise
            data = _parse_ai_json(raw_text)
            previous_data = data
            data = _shorten_metadata_once_if_needed(data)
            data = _normalize_ai_output(data)
            data = _finalize_html_content(data, package)
            _validate_ai_output(data, package=package)
            _apply_success(article, data, provider_used)
            article["ai_rotation_exhausted"] = False
            article["ai_openrouter_fallback_used"] = provider_used.startswith("openrouter:")
            article["ai_quality_attempts"] = attempt
            article["ai_quality_status"] = "passed"
            article["ai_total_time_seconds"] = round(context.elapsed_seconds(), 2)
            save_article_queue(queue)
            log_event(
                "ai_article_success",
                article_id=article.get("id"),
                provider=provider_used,
                words=article.get("final_word_count"),
                chars=article.get("final_html_chars"),
                elapsed_ms=elapsed_ms(started),
                ai_total_time=round(context.elapsed_seconds(), 2),
                ai_fast_mode_enabled=context.fast_mode_enabled,
                skipped_slow_models_count=context.skipped_slow_models_count,
            )
            log_event(
                "article_passed_quality",
                article_id=article.get("id"),
                attempt=attempt,
                provider=provider_used,
            )
            return {
                "processed": 1,
                "success": 1,
                "failed": 0,
                "article": article,
                "message": "",
            }
        except Exception as error:
            last_error = error
            is_quality_failure = _is_quality_error(error)
            if is_quality_failure:
                article["ai_quality_last_error"] = str(error)
            else:
                article["ai_provider_last_error"] = _safe_error_reason(error)
            log_event(
                "ai_article_attempt_failed",
                article_id=article.get("id"),
                attempt=attempt,
                provider=provider or provider_used,
                error=error,
                elapsed_ms=elapsed_ms(started),
                ai_total_time=round(context.elapsed_seconds(), 2),
            )
            if is_quality_failure:
                log_event(
                    "quality_failed_reason",
                    article_id=article.get("id"),
                    attempt=attempt,
                    reason=error,
                )
            else:
                log_event(
                    "provider_failed_reason",
                    article_id=article.get("id"),
                    attempt=attempt,
                    reason=_safe_error_reason(error),
                )
            if provider == "gemini" and "openrouter" in provider_sequence:
                log_event("ai_openrouter_fallback_started", article_id=article.get("id"))
            if is_quality_failure:
                prompt = _build_expansion_retry_prompt(package, previous_data, str(error))
                continue
            if _is_provider_error(error):
                break

    _apply_failure(article, last_error)
    provider_exhausted = bool(
        last_error
        and not isinstance(last_error, AITimeBudgetExceeded)
        and _is_provider_error(last_error)
        and not _is_quality_error(last_error)
    )
    article["ai_rotation_exhausted"] = provider_exhausted
    article["ai_time_budget_exceeded"] = isinstance(last_error, AITimeBudgetExceeded)
    article["ai_total_time_seconds"] = round(context.elapsed_seconds(), 2)
    article["ai_quality_status"] = (
        "time_budget_exceeded"
        if isinstance(last_error, AITimeBudgetExceeded)
        else "provider_rotation_exhausted"
        if provider_exhausted
        else "failed_after_retries"
    )
    article["ai_quality_attempts"] = attempt if "attempt" in locals() else 0
    save_article_queue(queue)
    if isinstance(last_error, AITimeBudgetExceeded):
        log_event(
            "ai_time_budget_exceeded",
            article_id=article.get("id"),
            ai_total_time=round(context.elapsed_seconds(), 2),
            ai_fast_mode_enabled=context.fast_mode_enabled,
            skipped_slow_models_count=context.skipped_slow_models_count,
        )
        log_event("article_skipped", article_id=article.get("id"), reason="ai_time_budget_exceeded")
    elif provider_exhausted:
        log_event("ai_rotation_exhausted", article_id=article.get("id"), error=last_error)
        log_event("article_skipped", article_id=article.get("id"), reason="AI rotation exhausted")
        _send_ai_rotation_exhausted_warning(article, last_error)
    else:
        log_event("ai_quality_failed_after_retries", article_id=article.get("id"), error=last_error)
        log_event("article_skipped", article_id=article.get("id"), reason="AI quality failed after retries")
        if "too short" in str(last_error).lower():
            log_event(
                "article_skipped_too_short",
                article_id=article.get("id"),
                words=article.get("final_word_count") or 0,
                reason=last_error,
            )
        _send_ai_quality_warning(article, last_error)
    return {
        "processed": 1,
        "success": 0,
        "failed": 1,
        "article": article,
        "message": str(last_error),
    }
