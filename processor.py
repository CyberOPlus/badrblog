# ============================================================
# processor.py - AI Translation & Formatting Module
# ============================================================

import re
import sys
import time
import warnings

import requests

with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    import google.generativeai as genai

from config import (
    AI_PROVIDER,
    FAST_NEWS_MODE,
    BLOG_CATEGORIES,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    MAX_RETRIES,
    MIN_ARTICLE_WORDS,
    OPENROUTER_API_KEY,
    OPENROUTER_API_URL,
    OPENROUTER_APP_NAME,
    OPENROUTER_MAX_TOKENS,
    OPENROUTER_MODEL,
    OPENROUTER_REFERER,
    OPENROUTER_TIMEOUT_SECONDS,
    RETRY_DELAY,
    TRANSLATION_DELAY_SECONDS,
    TRANSLATION_PROMPT,
)
from production_logging import html_to_text, html_word_count, log_event

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


DEFAULT_BLOG_CATEGORY = "أخبار التقنية"
AI_TOOLS_CATEGORY = BLOG_CATEGORIES[0]
CYBERSECURITY_CATEGORY = BLOG_CATEGORIES[1]
TECH_NEWS_CATEGORY = BLOG_CATEGORIES[2]
SOFTWARE_CATEGORY = BLOG_CATEGORIES[3]
MIN_BLOGGER_ARTICLE_WORDS = 800
REQUIRED_READER_SECTION = "\u0645\u0627\u0630\u0627 \u064a\u0639\u0646\u064a \u0647\u0630\u0627 \u0644\u0643"

CATEGORY_ALIASES = {
    "ai": "أدوات الذكاء الاصطناعي",
    "artificial intelligence": "أدوات الذكاء الاصطناعي",
    "ذكاء اصطناعي": "أدوات الذكاء الاصطناعي",
    "الذكاء الاصطناعي": "أدوات الذكاء الاصطناعي",
    "ادوات الذكاء الاصطناعي": "أدوات الذكاء الاصطناعي",
    "أدوات ai": "أدوات الذكاء الاصطناعي",
    "cybersecurity": "الأمن السيبراني",
    "cyber security": "الأمن السيبراني",
    "security": "الأمن السيبراني",
    "أمن سيبراني": "الأمن السيبراني",
    "أمن السيبراني": "الأمن السيبراني",
    "امن سيبراني": "الأمن السيبراني",
    "الأمن الإلكتروني": "الأمن السيبراني",
    "الامن السيبراني": "الأمن السيبراني",
    "tech news": "أخبار التقنية",
    "أخبار تقنية": "أخبار التقنية",
    "اخبار التقنية": "أخبار التقنية",
    "الأخبار التقنية": "أخبار التقنية",
    "apps": "برامج وتطبيقات",
    "software": "برامج وتطبيقات",
    "applications": "برامج وتطبيقات",
    "تطبيقات": "برامج وتطبيقات",
    "برامج": "برامج وتطبيقات",
    "برامج وتطبيقات": "برامج وتطبيقات",
}

CATEGORY_KEYWORDS = {
    "أدوات الذكاء الاصطناعي": [
        "ai",
        "artificial intelligence",
        "chatgpt",
        "gemini",
        "midjourney",
        "sora",
        "llm",
        "large language model",
        "prompt",
        "machine learning",
        "generative",
        "ذكاء اصطناعي",
        "الذكاء الاصطناعي",
        "نموذج لغوي",
        "نماذج لغوية",
        "تعلم آلي",
        "توليد الصور",
    ],
    "الأمن السيبراني": [
        "cybersecurity",
        "cyber security",
        "security",
        "vulnerability",
        "exploit",
        "malware",
        "phishing",
        "fraud",
        "breach",
        "password",
        "account protection",
        "pentest",
        "penetration testing",
        "osint",
        "threat",
        "ransomware",
        "أمن سيبراني",
        "الأمن السيبراني",
        "ثغرة",
        "ثغرات",
        "اختراق",
        "احتيال",
        "تصيد",
        "برمجيات خبيثة",
        "حماية الحسابات",
        "أمان الهواتف",
        "كلمات المرور",
        "اختبار الاختراق",
        "تهديد",
    ],
    "أخبار التقنية": [
        "news",
        "announced",
        "announcement",
        "launch",
        "launched",
        "release",
        "update",
        "platform",
        "company",
        "startup",
        "technology",
        "tech",
        "أخبار",
        "خبر",
        "تقنية",
        "تكنولوجيا",
        "إعلان",
        "أعلنت",
        "إطلاق",
        "تحديث",
        "منصة",
        "شركة",
    ],
    "برامج وتطبيقات": [
        "app",
        "apps",
        "application",
        "software",
        "program",
        "tool",
        "tools",
        "free app",
        "alternative",
        "open source",
        "github",
        "تطبيق",
        "تطبيقات",
        "برنامج",
        "برامج",
        "أداة",
        "أدوات",
        "بديل",
        "مجاني",
        "مفتوح المصدر",
    ],
}

CATEGORY_FALLBACK_LABELS = {
    "أدوات الذكاء الاصطناعي": ["ذكاء اصطناعي", "أدوات تقنية"],
    "الأمن السيبراني": ["حماية رقمية", "نصائح أمنية"],
    "أخبار التقنية": ["تقنية", "تحليل تقني"],
    "برامج وتطبيقات": ["تطبيقات", "برامج مجانية"],
}


def _has_real_openrouter_key():
    return bool(OPENROUTER_API_KEY and OPENROUTER_API_KEY != "your_new_key_here")


def _has_real_gemini_key():
    return bool(GEMINI_API_KEY and GEMINI_API_KEY != "your_gemini_api_key_here")


def _format_original_links(links):
    if not links:
        return "No original article links were extracted."

    formatted_links = []
    for index, link in enumerate(links, 1):
        url = str(link.get("url", "")).strip()
        if not url:
            continue

        text = str(link.get("text", "")).strip() or url
        formatted_links.append(f"{index}. {text} -> {url}")

    return "\n".join(formatted_links) or "No original article links were extracted."


def _normalize_slug(value):
    slug = (value or "").strip().lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug[:80]


def _is_quota_or_rate_limit_error(error):
    message = str(error).casefold()
    return any(
        hint in message
        for hint in (
            "429",
            "quota",
            "rate limit",
            "rate-limit",
            "resource_exhausted",
            "too many requests",
        )
    )


def _normalize_openrouter_content(content):
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


class TranslationAIClient:
    def __init__(self, providers, gemini_model=None):
        self.providers = providers
        self.gemini_model = gemini_model

    def generate(self, prompt):
        last_error = None

        for index, provider in enumerate(self.providers):
            try:
                if provider == "gemini":
                    return self._generate_with_gemini(prompt)
                if provider == "openrouter":
                    return self._generate_with_openrouter(prompt)
            except Exception as error:
                last_error = error
                has_next_provider = index < len(self.providers) - 1
                if (
                    provider == "gemini"
                    and has_next_provider
                    and _is_quota_or_rate_limit_error(error)
                ):
                    print("  ⚠️  Gemini quota/rate limit reached. Switching to OpenRouter...")
                    continue
                raise

        raise last_error or RuntimeError("No AI provider returned a response.")

    def _generate_with_gemini(self, prompt):
        if not self.gemini_model:
            raise RuntimeError("Gemini model is not initialized.")

        response = self.gemini_model.generate_content(prompt)
        raw_text = getattr(response, "text", "") or ""
        if not raw_text.strip():
            raise RuntimeError("Gemini returned an empty response.")

        return raw_text, f"Gemini ({GEMINI_MODEL})"

    def _generate_with_openrouter(self, prompt):
        if not _has_real_openrouter_key():
            raise RuntimeError("OpenRouter API key is missing.")

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
            },
            timeout=OPENROUTER_TIMEOUT_SECONDS,
        )

        if response.status_code >= 400:
            detail = response.text.strip().replace("\n", " ")
            raise RuntimeError(
                f"OpenRouter API error {response.status_code}: {detail[:500]}"
            )

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("OpenRouter returned no choices.")

        message = choices[0].get("message") or {}
        raw_text = _normalize_openrouter_content(message.get("content")).strip()
        if not raw_text:
            raise RuntimeError("OpenRouter returned an empty response.")

        used_model = data.get("model") or OPENROUTER_MODEL
        return raw_text, f"OpenRouter ({used_model})"


def _squash_spaces(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _match_blog_category(label):
    normalized = _squash_spaces(label)
    if not normalized:
        return None

    normalized_key = normalized.casefold()
    for category in BLOG_CATEGORIES:
        if normalized_key == category.casefold():
            return category

    return CATEGORY_ALIASES.get(normalized_key)


def _keyword_count(text, keyword):
    keyword = keyword.casefold()
    if keyword.isascii() and re.fullmatch(r"[a-z0-9 ]+", keyword):
        pattern = r"\b" + re.escape(keyword).replace(r"\ ", r"\s+") + r"\b"
        return len(re.findall(pattern, text))
    return text.count(keyword)


def _score_blog_categories(original_article, arabic_title, html_content, labels):
    text = " ".join(
        _squash_spaces(part)
        for part in [
            original_article.get("title", ""),
            original_article.get("body", ""),
            arabic_title,
            html_content,
            " ".join(labels),
        ]
        if part
    ).casefold()

    scores = {category: 0 for category in BLOG_CATEGORIES}
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            scores[category] += _keyword_count(text, keyword)

    return scores


def _priority_blog_category(scores):
    """
    Keep category selection aligned with the blog taxonomy rules.
    Security signals win first, then clear AI-tool content, then news, then apps.
    """
    if scores.get(CYBERSECURITY_CATEGORY, 0) > 0:
        return CYBERSECURITY_CATEGORY
    if scores.get(AI_TOOLS_CATEGORY, 0) > 0:
        return AI_TOOLS_CATEGORY
    if scores.get(TECH_NEWS_CATEGORY, 0) > 0:
        return TECH_NEWS_CATEGORY
    if scores.get(SOFTWARE_CATEGORY, 0) > 0:
        return SOFTWARE_CATEGORY
    return None


def _classify_blog_category(original_article, arabic_title, html_content, labels):
    scores = _score_blog_categories(original_article, arabic_title, html_content, labels)
    priority_category = _priority_blog_category(scores)
    if priority_category:
        return priority_category

    best_category = max(
        BLOG_CATEGORIES,
        key=lambda category: (scores.get(category, 0), -BLOG_CATEGORIES.index(category)),
    )
    if scores.get(best_category, 0) == 0:
        return DEFAULT_BLOG_CATEGORY
    return best_category


def _select_blog_category(labels, original_article, arabic_title, html_content):
    scores = _score_blog_categories(original_article, arabic_title, html_content, labels)
    priority_category = _priority_blog_category(scores)
    if priority_category:
        return priority_category

    best_category = max(
        BLOG_CATEGORIES,
        key=lambda category: (scores.get(category, 0), -BLOG_CATEGORIES.index(category)),
    )

    for label in labels:
        ai_category = _match_blog_category(label)
        if not ai_category:
            continue

        best_score = scores.get(best_category, 0)
        ai_score = scores.get(ai_category, 0)
        if best_category != ai_category and best_score >= max(3, ai_score + 2):
            return best_category
        return ai_category

    if scores.get(best_category, 0) == 0:
        return DEFAULT_BLOG_CATEGORY
    return best_category


def _dedupe_labels(labels):
    deduped = []
    seen = set()

    for label in labels:
        cleaned = _squash_spaces(label)
        if not cleaned:
            continue

        key = cleaned.casefold()
        if key in seen:
            continue

        seen.add(key)
        deduped.append(cleaned)

    return deduped


def _ensure_blog_category_labels(labels, original_article, arabic_title, html_content):
    cleaned_labels = _dedupe_labels(labels)
    category = _select_blog_category(
        cleaned_labels,
        original_article=original_article,
        arabic_title=arabic_title,
        html_content=html_content,
    )

    supporting_labels = [
        label for label in cleaned_labels if not _match_blog_category(label)
    ]

    for fallback_label in CATEGORY_FALLBACK_LABELS.get(category, ["تقنية"]):
        if len([category] + supporting_labels) >= 3:
            break
        supporting_labels.append(fallback_label)

    return _dedupe_labels([category] + supporting_labels)[:5]


def initialize_gemini():
    """
    Configure and return the AI translation client.
    """
    print("\n🤖 Initializing AI translation providers...")

    providers = []
    gemini_model = None

    if AI_PROVIDER in {"auto", "gemini"} and _has_real_gemini_key():
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel(GEMINI_MODEL)
        providers.append("gemini")
        print(f"  ✅ Gemini ready: {GEMINI_MODEL}")

    if AI_PROVIDER in {"auto", "openrouter"} and _has_real_openrouter_key():
        providers.append("openrouter")
        print(f"  ✅ OpenRouter ready: {OPENROUTER_MODEL}")

    if not providers:
        print("  ❌ No AI provider is configured.")
        return None

    if AI_PROVIDER == "auto" and providers == ["gemini"]:
        print("  ⚠️  OpenRouter fallback is not active until OPENROUTER_API_KEY is set.")

    print(f"✅ AI provider mode: {AI_PROVIDER} | order: {', '.join(providers)}")
    return TranslationAIClient(providers=providers, gemini_model=gemini_model)


def translate_article(model, article, retry_count=0):
    """
    Translate a single article from English to Arabic using the configured AI provider.
    """
    print(f'\n🔄 Translating article: "{article["title"][:60]}..."')

    prompt = TRANSLATION_PROMPT.format(
        title=article["title"],
        url=article["url"],
        body=article["body"][:15000],
        original_links=_format_original_links(article.get("original_links", [])),
        editorial_notes=article.get("editorial_notes", "No special notes."),
    )

    try:
        raw_text, provider_name = model.generate(prompt)
        result = parse_ai_response(raw_text, article)

        if result:
            print(f"  ✅ Translation complete via {provider_name}!")
            print(f'  📝 Arabic title: {result["title"]}')
            print(f'  🏷️  Labels: {", ".join(result["labels"])}')
            return result

        print("  ⚠️  Could not parse AI response correctly")

    except Exception as e:
        print(f"  ❌ Translation failed: {e}")

    if retry_count < MAX_RETRIES:
        wait = RETRY_DELAY * (retry_count + 1)
        print(f"  ⏳ Retrying in {wait} seconds... (Attempt {retry_count + 1}/{MAX_RETRIES})")
        time.sleep(wait)
        return translate_article(model, article, retry_count + 1)

    print("  🛑 All retries exhausted for this article.")
    return None


def _clean_response_text(raw_text):
    text = raw_text.strip()
    text = re.sub(r"^```(?:html|markdown)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text


def _strip_inline_styles(html_content):
    without_style_blocks = re.sub(
        r"<style\b[^>]*>.*?</style>",
        "",
        html_content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(
        r"\sstyle=(['\"]).*?\1",
        "",
        without_style_blocks,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _remove_unwanted_source_blocks(html_content):
    # Remove legacy explicit source blocks. Final trusted links are appended later.
    html_content = re.sub(
        r"<p\b[^>]*class=(['\"])pRef\1[^>]*>.*?</p>",
        "",
        html_content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(
        r"<div\b[^>]*class=(['\"]).*?\balert\b.*?\1[^>]*>.*?(?:المصدر الأصلي|Original source|Source:).*?</div>",
        "",
        html_content,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _replace_legacy_paragraph(match):
    before = (match.group("before") or "").strip()
    after = (match.group("after") or "").strip()
    attrs = " ".join(part for part in (before, after) if part)
    if attrs:
        return f"<p {attrs}>"
    return "<p>"


def _normalize_template_html(html_content):
    content = _strip_inline_styles(html_content.strip())
    content = _remove_unwanted_source_blocks(content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def _arabic_text_ratio(text):
    letters = re.findall(r"[A-Za-z\u0600-\u06FF]", text or "")
    if not letters:
        return 0

    arabic_letters = [letter for letter in letters if "\u0600" <= letter <= "\u06FF"]
    return len(arabic_letters) / len(letters)


def _looks_arabic_enough(title, html_content):
    combined = f"{title}\n{html_content}"
    return _arabic_text_ratio(combined) >= 0.35


def parse_ai_response(raw_text, original_article):
    """
    Parse the AI response into title, HTML content, and labels.
    """
    lines = _clean_response_text(raw_text).splitlines()

    arabic_title = ""
    seo_title = ""
    meta_description = ""
    slug = ""
    content_lines = []
    labels = []
    found_title = False

    for line in lines:
        stripped = line.strip()

        if not stripped and not found_title:
            continue

        if stripped.upper().startswith("TITLE:"):
            arabic_title = stripped[6:].strip()
            found_title = True
            continue

        if stripped.upper().startswith("SEO_TITLE:"):
            seo_title = stripped[10:].strip()
            continue

        if stripped.upper().startswith("META_DESCRIPTION:"):
            meta_description = stripped[17:].strip()
            continue

        if stripped.upper().startswith("SLUG:"):
            slug = _normalize_slug(stripped[5:].strip())
            continue

        if stripped.upper().startswith("LABELS:"):
            labels_str = stripped[7:].strip()
            labels = [
                label.strip()
                for label in re.split(r"[,;\u060C]+", labels_str)
                if label.strip()
            ]
            continue

        if stripped:
            content_lines.append(stripped)

    html_content = _normalize_template_html("\n".join(content_lines))

    if not arabic_title:
        print("  ⚠️  AI did not return a title, using the original title")
        arabic_title = original_article["title"]

    if not html_content or len(html_content) < 50:
        print("  ⚠️  AI content is too short, something likely went wrong")
        return None

    word_count = html_word_count(html_content)
    minimum_words = MIN_ARTICLE_WORDS if FAST_NEWS_MODE else MIN_BLOGGER_ARTICLE_WORDS
    if word_count < minimum_words:
        print(
            f"  AI content is too short for Blogger "
            f"({word_count} words; minimum {minimum_words})"
        )
        log_event(
            "ai_article_rejected",
            original_url=original_article.get("url"),
            reason="too_short",
            words=word_count,
        )
        return None

    body_text = html_to_text(html_content)
    if (
        not FAST_NEWS_MODE
        and REQUIRED_READER_SECTION not in html_content
        and REQUIRED_READER_SECTION not in body_text
    ):
        print("  AI content is missing the required reader-impact section")
        log_event(
            "ai_article_rejected",
            original_url=original_article.get("url"),
            reason="missing_reader_section",
            words=word_count,
        )
        return None

    if not _looks_arabic_enough(arabic_title, html_content):
        print("  ⚠️  AI response is not Arabic enough, rejecting it")
        return None

    labels = _ensure_blog_category_labels(
        labels,
        original_article=original_article,
        arabic_title=arabic_title,
        html_content=html_content,
    )

    return {
        "title": arabic_title,
        "seo_title": seo_title or arabic_title,
        "meta_description": meta_description,
        "slug": slug,
        "content": html_content,
        "word_count": word_count,
        "labels": labels,
        "original_url": original_article["url"],
        "original_title": original_article["title"],
        "image": original_article.get("image"),
        "original_links": original_article.get("original_links", []),
        "trusted_sources": original_article.get("trusted_sources", []),
    }


def process_articles(model, articles, delay_seconds=TRANSLATION_DELAY_SECONDS):
    """
    Translate a list of articles one by one.
    """
    print("\n" + "=" * 60)
    print("🤖 STEP 2: Translating articles with AI")
    print("=" * 60)

    translated = []

    for i, article in enumerate(articles, 1):
        print(f"\n--- Processing article {i}/{len(articles)} ---")
        result = translate_article(model, article)

        if result:
            translated.append(result)
        else:
            print("  ⚠️  Skipping article (translation failed)")

        if i < len(articles) and delay_seconds > 0:
            print(f"  ⏳ Waiting {delay_seconds} seconds before next translation...")
            time.sleep(delay_seconds)

    print(f"\n🎉 Successfully translated {len(translated)}/{len(articles)} article(s)!")
    return translated
