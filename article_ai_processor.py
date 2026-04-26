# ============================================================
# article_ai_processor.py - Phase 6 AI Article Processing
# ============================================================

import json
import re
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
from config import (
    AI_PROVIDER,
    GEMINI_API_KEY,
    GEMINI_MODEL,
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
    package_json = json.dumps(package, ensure_ascii=False, indent=2)
    return f"""
You are a professional Arabic technology and cybersecurity editor.

Create a fully ready Arabic Blogger article from the input package.

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


def _validate_ai_output(data):
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


def _resolve_provider():
    provider = (AI_PROVIDER or "").strip().lower()
    if provider == "auto":
        if _has_real_key(GEMINI_API_KEY, "your_gemini_api_key_here"):
            return "gemini"
        if _has_real_key(OPENROUTER_API_KEY, "your_new_key_here"):
            return "openrouter"
        if _has_real_key(OPENAI_API_KEY, "your_openai_api_key_here"):
            return "openai"
        raise RuntimeError("No AI provider key configured.")
    if provider in {"gemini", "openrouter", "openai"}:
        return provider
    raise RuntimeError("AI_PROVIDER must be one of: gemini, openrouter, openai, auto")


def _generate_ai_article(prompt):
    provider = _resolve_provider()
    if provider == "gemini":
        return _generate_with_gemini(prompt)
    if provider == "openrouter":
        return _generate_with_openrouter(prompt)
    if provider == "openai":
        return _generate_with_openai(prompt)
    raise RuntimeError(f"Unsupported AI provider: {provider}")


def _apply_success(article, data, provider_used):
    article["ai_status"] = "completed"
    article["ai_processed_at"] = _now_iso()
    article["seo_title"] = str(data["title"]).strip()
    article["seo_description"] = str(data["description"]).strip()
    article["seo_slug"] = _normalize_slug(data["slug"])
    article["final_html"] = str(data["html_content"]).strip()
    article["ai_provider_used"] = provider_used
    article.pop("ai_error", None)


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

    for _attempt in range(1, MAX_AI_ATTEMPTS + 1):
        try:
            raw_text, provider_used = _generate_ai_article(prompt)
            data = _parse_ai_json(raw_text)
            data = _shorten_metadata_once_if_needed(data)
            data = _normalize_ai_output(data)
            data = _finalize_html_content(data, package)
            _validate_ai_output(data)
            _apply_success(article, data, provider_used)
            save_article_queue(queue)
            return {
                "processed": 1,
                "success": 1,
                "failed": 0,
                "article": article,
                "message": "",
            }
        except Exception as error:
            last_error = error

    _apply_failure(article, last_error)
    save_article_queue(queue)
    return {
        "processed": 1,
        "success": 0,
        "failed": 1,
        "article": article,
        "message": str(last_error),
    }
