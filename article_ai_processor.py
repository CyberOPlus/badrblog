# ============================================================
# article_ai_processor.py - Phase 6 AI Article Processing
# ============================================================

import json
import re
import time
import warnings
from datetime import datetime
from html import escape

import requests
from bs4 import BeautifulSoup

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
    FAST_NEWS_MODE,
    GEMINI_API_KEY,
    GEMINI_MODEL,
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

MAX_AI_ATTEMPTS = 2


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _has_real_key(value, placeholder):
    return bool(value and value != placeholder)


def _selected_ready_for_ai(article):
    return (
        article.get("status") in {"selected", "draft_created"}
        and article.get("processing_status") == "ready_for_ai"
        and isinstance(article.get("ai_input_package"), dict)
    )


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
- Do not add CSS, scripts, unsupported widgets, fake images, or source/reference blocks unless trusted_references are provided.

OUTPUT JSON SHAPE:
{{
  "title": "Arabic SEO title, 40-70 characters",
  "description": "Arabic meta description, 100-170 characters",
  "slug": "latin-url-slug",
  "html_content": "Plus UI HTML article body"
}}

INPUT PACKAGE:
{package_json}
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
- Place the main image after the first paragraph using <img class='full' alt='meaningful Arabic alt' src='image_link'/> if main_image is available.
- If article_images contains more useful images, insert them naturally after relevant sections. Do not invent images.
- Do not lazyload the first image.
- If trusted_references exist, add them at the end using <p class='pRef'>المراجع:<br>...</p>.
- If related_posts exist, add them at the end using <div class='pRelate'><b>قد يهمك أيضًا:</b><ul>...</ul></div>.
- Use <p>, <p class='pIndent'>, <h2>, <h3>, <ul>, <li>, <a class='extL'>, <p class='note'>, <p class='note wr'>, <div class='alert info'>, <pre><code> when useful.
- SEO title must be 40-70 characters.
- Meta description must be 100-170 characters.
- Slug must be Latin lowercase words separated by hyphens.

OUTPUT JSON SHAPE:
{{
  "title": "Arabic SEO title, 40-70 characters",
  "description": "Arabic meta description, 100-170 characters",
  "slug": "latin-url-slug",
  "html_content": "Plus UI HTML article body"
}}

INPUT PACKAGE:
{package_json}
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

    if not 40 <= len(title) <= 70:
        raise ValueError(f"SEO title length must be 40-70 characters; got {len(title)}")
    if not 100 <= len(description) <= 170:
        raise ValueError(
            f"Meta description length must be 100-170 characters; got {len(description)}"
        )
    if not html_content:
        raise ValueError("html_content is empty")

    result = validate_ai_article_output(data, package=package)
    if not result.passed:
        raise ValueError(result.reason)


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


def _insert_main_image_if_missing(html_content, package):
    main_image = package.get("main_image")
    if not main_image or re.search(r"<img\b", html_content, flags=re.IGNORECASE):
        return html_content

    title = package.get("title") or "صورة المقال"
    image_html = (
        f"<img class='full' alt='{escape('صورة توضيحية عن ' + title, quote=True)}' "
        f"src='{escape(main_image, quote=True)}'/>"
    )

    soup = BeautifulSoup(html_content, "html.parser")
    first_paragraph = soup.find("p")
    if not first_paragraph:
        return image_html + "\n" + html_content

    first_paragraph.insert_after(BeautifulSoup(image_html, "html.parser"))
    return str(soup)


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

    if not items:
        return html_content
    return (
        html_content.rstrip()
        + "\n<div class='pRelate'><b>قد يهمك أيضًا:</b><ul>"
        + "".join(items)
        + "</ul></div>"
    )


def _finalize_html_content(data, package):
    html_content = data["html_content"]
    html_content = _insert_main_image_if_missing(html_content, package)
    html_content = _append_trusted_references_if_missing(html_content, package)
    html_content = _append_related_posts_if_missing(html_content, package)
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


def _generate_with_gemini(prompt):
    if genai is None:
        raise RuntimeError("google-generativeai is not installed.")
    if not _has_real_key(GEMINI_API_KEY, "your_gemini_api_key_here"):
        raise RuntimeError("GEMINI_API_KEY is missing.")

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    response = model.generate_content(prompt)
    text = getattr(response, "text", "") or ""
    if not text.strip():
        raise RuntimeError("Gemini returned an empty response.")
    return text, f"gemini:{GEMINI_MODEL}"


def _generate_with_openrouter(prompt):
    if not _has_real_key(OPENROUTER_API_KEY, "your_new_key_here"):
        raise RuntimeError("OPENROUTER_API_KEY is missing.")

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "X-OpenRouter-Title": OPENROUTER_APP_NAME,
    }
    if OPENROUTER_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_REFERER

    response = requests.post(
        OPENROUTER_API_URL,
        headers=headers,
        json={
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": OPENROUTER_MAX_TOKENS,
            "temperature": 0.35,
            "response_format": {"type": "json_object"},
        },
        timeout=OPENROUTER_TIMEOUT_SECONDS,
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
    return text, f"openrouter:{data.get('model') or OPENROUTER_MODEL}"


def _generate_with_openai(prompt):
    if not _has_real_key(OPENAI_API_KEY, "your_openai_api_key_here"):
        raise RuntimeError("OPENAI_API_KEY is missing.")

    response = requests.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": OPENAI_MAX_TOKENS,
            "temperature": 0.35,
            "response_format": {"type": "json_object"},
        },
        timeout=OPENAI_TIMEOUT_SECONDS,
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
    return text, f"openai:{OPENAI_MODEL}"


def _is_quota_or_rate_limit_error(error):
    message = str(error).casefold()
    return any(
        hint in message
        for hint in (
            "429",
            "quota",
            "rate limit",
            "rate-limit",
            "rate_limited",
            "resource_exhausted",
            "too many requests",
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
        if _has_real_key(OPENAI_API_KEY, "your_openai_api_key_here"):
            providers.append("openai")
        if providers:
            return providers
        raise RuntimeError("No AI provider key configured.")
    if provider in {"gemini", "openrouter", "openai"}:
        return [provider]
    raise RuntimeError("AI_PROVIDER must be one of: gemini, openrouter, openai, auto")


def _generate_ai_article(prompt):
    providers = _resolve_providers()
    last_error = None
    for index, provider in enumerate(providers):
        try:
            if provider == "gemini":
                return _generate_with_gemini(prompt)
            if provider == "openrouter":
                return _generate_with_openrouter(prompt)
            if provider == "openai":
                return _generate_with_openai(prompt)
        except Exception as error:
            last_error = error
            has_next_provider = index < len(providers) - 1
            if has_next_provider and _is_quota_or_rate_limit_error(error):
                print(f"  AI provider {provider} quota/rate limit reached. Trying next provider...")
                continue
            raise
    raise last_error or RuntimeError("No AI provider returned a response.")


def _apply_success(article, data, provider_used):
    final_html = str(data["html_content"]).strip()
    article["ai_status"] = "completed"
    article["ai_processed_at"] = _now_iso()
    article["seo_title"] = str(data["title"]).strip()
    article["seo_description"] = str(data["description"]).strip()
    article["seo_slug"] = _normalize_slug(data["slug"])
    article["final_html"] = final_html
    article["blogger_article_html"] = final_html
    article["final_word_count"] = html_word_count(final_html)
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

    log_event(
        "ai_article_start",
        article_id=article.get("id"),
        source=article.get("source_name"),
        title=package.get("title"),
        source_chars=len(package.get("full_article_text") or package.get("content_preview") or ""),
        enrichment_status=package.get("enrichment_status"),
        fast_news_mode=FAST_NEWS_MODE,
    )

    for attempt in range(1, MAX_AI_ATTEMPTS + 1):
        started = time.perf_counter()
        try:
            raw_text, provider_used = _generate_ai_article(prompt)
            data = _parse_ai_json(raw_text)
            previous_data = data
            data = _shorten_metadata_once_if_needed(data)
            data = _normalize_ai_output(data)
            data = _finalize_html_content(data, package)
            _validate_ai_output(data, package=package)
            _apply_success(article, data, provider_used)
            save_article_queue(queue)
            log_event(
                "ai_article_success",
                article_id=article.get("id"),
                provider=provider_used,
                words=article.get("final_word_count"),
                chars=article.get("final_html_chars"),
                elapsed_ms=elapsed_ms(started),
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
            log_event(
                "ai_article_attempt_failed",
                article_id=article.get("id"),
                attempt=attempt,
                error=error,
                elapsed_ms=elapsed_ms(started),
            )
            prompt = _build_expansion_retry_prompt(package, previous_data, str(error))

    _apply_failure(article, last_error)
    save_article_queue(queue)
    log_event("ai_article_failed", article_id=article.get("id"), error=last_error)
    _send_ai_quality_warning(article, last_error)
    return {
        "processed": 1,
        "success": 0,
        "failed": 1,
        "article": article,
        "message": str(last_error),
    }
