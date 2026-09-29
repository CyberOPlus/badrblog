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
    GROQ_API_KEY,
    GROQ_API_URL,
    GROQ_MAX_TOKENS,
    GROQ_MODEL,
    GROQ_TIMEOUT_SECONDS,
    MISTRAL_API_KEY,
    MISTRAL_API_URL,
    MISTRAL_MAX_TOKENS,
    MISTRAL_MODEL,
    MISTRAL_TIMEOUT_SECONDS,
    CLOUDFLARE_API_TOKEN,
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_MODEL,
    CLOUDFLARE_MAX_TOKENS,
    CLOUDFLARE_TIMEOUT_SECONDS,
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
    JOBS_MODE,
)
from production_logging import elapsed_ms, html_word_count, log_event
from quality_gate import (
    REQUIRED_READER_SECTION,
    REQUIRED_READER_SECTION_WITH_QUESTION,
    validate_ai_article_output,
)

MAX_AI_ATTEMPTS = max(1, MAX_AI_RETRIES)
MIN_PUBLISHABLE_WORDS = 120
AI_MODEL_COOLDOWN_SECONDS = 30 * 60
_AI_COOLDOWNS = {}
_AI_MEMORY_CACHE = None
FAST_OPENROUTER_MODEL_SET = set(FAST_OPENROUTER_MODELS)
AI_TIMEOUT_RETRIES = 2
MIN_PROVIDER_TIMEOUT_SECONDS = 30
LONG_FORM_ARTICLE_MIN_WORDS = 700
LONG_FORM_ARTICLE_TARGET_RANGE = "700-1000"
RICH_INPUT_MIN_SOURCE_WORDS = 180
RICH_INPUT_MIN_SOURCE_CHARS = 1200
REQUIRED_ARTICLE_FIELDS = ("title", "description", "slug", "html_content")


class AIProviderFallbackNeeded(RuntimeError):
    """Raised when a provider error must switch to another AI provider first."""


class AIProviderRotationExhausted(RuntimeError):
    """Raised when all available AI providers fail before quality validation."""


class AIProviderEmptyResponse(RuntimeError):
    """Raised when an AI provider returns no usable message content."""


class AITimeBudgetExceeded(RuntimeError):
    """Raised when the full AI generation budget is exhausted."""


class AIIncompleteResponseError(ValueError):
    """Raised when the AI response is incomplete or structurally invalid."""


class AIOutputRejectedShortError(ValueError):
    """Raised when the AI output is too short to be publishable."""


@dataclass
class AIExecutionContext:
    article_id: str = ""
    started_at: float = field(default_factory=time.perf_counter)
    total_budget_seconds: int = AI_TOTAL_TIME_BUDGET_SECONDS
    gemini_failures: int = 0
    openrouter_failures_after_gemini: int = 0
    skipped_slow_models_count: int = 0
    fast_mode_enabled: bool = True
    current_stage: str = "article_generation"

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

GENERAL_ENGLISH_ARABIC_REPLACEMENTS = {
    "movie": "فيلم",
    "movies": "أفلام",
    "film": "فيلم",
    "films": "أفلام",
    "classic": "كلاسيكي",
    "oscar": "الأوسكار",
    "oscars": "الأوسكار",
    "stream": "مشاهدة",
    "streaming": "البث",
    "today": "اليوم",
    "watch": "شاهد",
    "shows": "عروض",
    "show": "عرض",
    "thriller": "إثارة",
    "drama": "دراما",
    "comedy": "كوميديا",
    "feature": "ميزة",
    "features": "ميزات",
    "workflow": "سير العمل",
    "productivity": "الإنتاجية",
    "enterprise": "المؤسسات",
    "teams": "الفرق",
    "update": "تحديث",
    "updates": "تحديثات",
    "tool": "أداة",
    "tools": "أدوات",
    "account": "الحساب",
    "protection": "الحماية",
    "privacy": "الخصوصية",
    "security": "الأمان",
    "software": "برنامج",
    "bug": "خلل",
    "data": "بيانات",
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
    if provider == "gemini":
        base_timeout = GEMINI_TIMEOUT_SECONDS
    elif provider == "openrouter":
        base_timeout = OPENROUTER_TIMEOUT_SECONDS
    elif provider == "groq":
        base_timeout = GROQ_TIMEOUT_SECONDS
    elif provider == "mistral":
        base_timeout = MISTRAL_TIMEOUT_SECONDS
    elif provider == "cloudflare":
        base_timeout = CLOUDFLARE_TIMEOUT_SECONDS
    elif provider == "openai":
        base_timeout = OPENAI_TIMEOUT_SECONDS
    else:
        base_timeout = AI_MODEL_TIMEOUT_SECONDS
    remaining = _remaining_budget_seconds(context)
    if remaining <= 0:
        raise AITimeBudgetExceeded("ai_time_budget_exceeded")
    desired_timeout = max(int(base_timeout or 0), MIN_PROVIDER_TIMEOUT_SECONDS)
    remaining_seconds = int(remaining) if remaining >= 1 else 1
    return max(1, min(desired_timeout, remaining_seconds))


def _is_timeout_error(error):
    if isinstance(error, (requests.Timeout, TimeoutError)):
        return True
    message = str(error).casefold()
    return any(
        hint in message
        for hint in (
            "timeout",
            "timed out",
            "deadline exceeded",
            "read timed out",
            "connect timeout",
            "request timed out",
        )
    )


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


def _cooldown_seconds_for_error(error):
    message = str(error or "").casefold()
    if any(token in message for token in ("401", "403", "unauthorized", "forbidden", "invalid api key")):
        return 6 * 3600
    if any(token in message for token in ("429", "402", "quota", "rate limit", "too many requests", "resource_exhausted")):
        return 45 * 60
    if any(token in message for token in ("500", "502", "503", "504", "timeout", "timed out")):
        return 10 * 60
    if _is_empty_provider_response(error):
        return 5 * 60
    return AI_MODEL_COOLDOWN_SECONDS


def _put_candidate_on_cooldown(candidate, error):
    candidate_id = _candidate_id(candidate)
    cooldown_seconds = _cooldown_seconds_for_error(error)
    until = time.time() + cooldown_seconds + random.uniform(0, 5)
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
        cooldown_seconds=cooldown_seconds,
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


def _source_text_for_package(package):
    package = dict(package or {})
    return str(
        package.get("full_article_text")
        or package.get("content_preview")
        or package.get("rss_summary")
        or ""
    ).strip()


def _source_stats(package):
    source_text = _source_text_for_package(package)
    words = len(re.findall(r"\b\w+\b", source_text, flags=re.UNICODE))
    return source_text, len(source_text), words


def _is_rich_input_package(package):
    _source_text, source_chars, source_words = _source_stats(package)
    return source_words >= RICH_INPUT_MIN_SOURCE_WORDS or source_chars >= RICH_INPUT_MIN_SOURCE_CHARS


def _minimum_article_words_for_package(package):
    if JOBS_MODE:
        return 100
    return LONG_FORM_ARTICLE_MIN_WORDS if _is_rich_input_package(package) else MIN_PUBLISHABLE_WORDS


def _next_provider_in_sequence(provider_sequence, current_provider):
    if not provider_sequence:
        return ""
    if current_provider not in provider_sequence:
        return provider_sequence[0]
    if len(provider_sequence) == 1:
        return current_provider
    current_index = provider_sequence.index(current_provider)
    return provider_sequence[(current_index + 1) % len(provider_sequence)]


def _raw_html_incomplete_reason(html_content):
    text = str(html_content or "").strip()
    if not text:
        return "html_content is empty"
    if re.search(r"<[^>]*$", text):
        return "html_content ended before a tag was closed"
    if re.search(r"&(?:[A-Za-z]+|#\d+|#x[0-9A-Fa-f]+)?$", text):
        return "html_content ended before an HTML entity was completed"
    for tag in ("p", "h2", "h3", "ul", "ol", "li", "div", "a", "table", "thead", "tbody", "tr", "th", "td", "blockquote", "details", "summary"):
        opening_count = len(re.findall(rf"<{tag}\b[^>]*>", text, flags=re.I))
        closing_count = len(re.findall(rf"</{tag}>", text, flags=re.I))
        if opening_count != closing_count:
            return f"html_content has unbalanced <{tag}> tags"
    return ""


def _parse_complete_ai_json(raw_text, required_fields, stage_label):
    try:
        data = _parse_ai_json(raw_text)
    except json.JSONDecodeError as error:
        raise AIIncompleteResponseError(f"{stage_label} returned incomplete JSON.") from error

    if not isinstance(data, dict):
        raise AIIncompleteResponseError(f"{stage_label} returned a non-object JSON payload.")

    missing_fields = [field for field in required_fields if not str(data.get(field, "")).strip()]
    if missing_fields:
        raise AIIncompleteResponseError(
            f"{stage_label} is missing required field(s): {', '.join(missing_fields)}"
        )

    if "html_content" in required_fields:
        html_issue = _raw_html_incomplete_reason(data.get("html_content", ""))
        if html_issue:
            raise AIIncompleteResponseError(html_issue)

    return data


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
    source_text = _source_text_for_package(package)
    package["blogger_source_text"] = source_text
    package_json = json.dumps(package, ensure_ascii=False, indent=2)
    source_is_rich = _is_rich_input_package(package)
    if JOBS_MODE:
        return f"""
You are the dedicated Arabic job-post editor for a Moroccan jobs publication.

Create a short, factual, highly useful Arabic employment notice for Moroccan readers
from the VERIFIED JOB PACKAGE below. The source may be a new vacancy/competition,
a candidate list, a provisional list, a result, or a final result. The reader must
understand WHAT changed, WHO it concerns, the deadline/status, and the strongest
official action or document immediately.

OUTPUT
- Return JSON only with exactly: title, description, slug, html_content.
- No markdown fences, notes, commentary, or extra keys.

STRICT ACCURACY
- Use ONLY facts explicitly present in the verified package or official source text.
- Never invent or guess salary, deadline, diploma, degree, experience, age, city,
  country, contract type, number of positions, eligibility, remote status,
  visa sponsorship, company information, application links, PDF links, email,
  phone number, requirement, date, or urgency.
- If a fact is missing, OMIT it. Do not fill missing fields with "غير محدد".
- job_title from the verified package is the factual source title, but the
  READER-FACING position name MUST be translated into clear natural Arabic so
  a Moroccan reader immediately understands which job they are applying for.
- When the original French/English title is useful for recognition or contains
  a product/technical term, keep it once in parentheses after the Arabic meaning.
  Example: مدير تقني ServiceNow (Technical Lead ServiceNow).
- Never mistranslate a specialized title. Keep product names, certifications,
  company names and necessary technical terms such as ServiceNow unchanged.
- Never mention scraping, rewriting, AI, automation, or the source-processing pipeline.

LENGTH AND STYLE
- This is a JOB LISTING, not a long-form article.
- Keep the explanatory prose compact, normally 135-200 Arabic words.
- Aim for 135-200 words; the shared production minimum is 100 words.
  Never invent information or repeat facts to meet the target length.
- Multi-specialization campaigns or candidate/result notices may contain a factual
  official-links table beyond that prose target. Never add filler, but never delete
  a useful verified official row merely to hit a word count.
- Use clear Modern Standard Arabic and short mobile-friendly paragraphs.
- No filler, generic career advice, profession explanations, corporate history,
  motivational language, clickbait, emojis, or generic conclusion.
- Do not write phrases such as "في هذا المقال سنتعرف" or "تابع القراءة".

TITLE
- Write the headline like a professional Moroccan employment/competition portal,
  not like a database row and not like "company: translated title (English title)".
- The headline should immediately answer: WHO/WHAT + WHAT HAPPENED + the most useful
  verified distinguishing fact (positions, roles, stage, location, or campaign year).
- The JSON "title" is the Blogger/SEO headline. Aim for 45-75 characters;
  preserve clear role/employer/stage meaning (accepted range 28-150).
- For a vacancy, the title MUST contain a clear employment action such as
  "توظف" or "تعلن عن توظيف" or "فرصة توظيف"; do not return only the role name.
- Keep it natural and specific: employer + employment action + Arabic role
  (+ verified location when it fits).
- Start with the institution/company/topic when that is the clearest search entity.
- vacancy / single private role:
  prefer natural Arabic such as "inwi توظف مديرًا تقنيًا لمنصة ServiceNow بالدار البيضاء"
  or "شركة X تعلن عن توظيف ...". Translate the role into clear Arabic.
  Keep an English/French technical term only when it is itself a product, acronym,
  certification, or essential recognized role term; do NOT automatically repeat the
  whole official title in parentheses.
- public competition / multi-position campaign:
  prefer "الجهة: مباراة توظيف ..." or "الجهة – مباراة توظيف ..." and include the
  verified number/role breakdown when it is genuinely useful.
  Example pattern: "المكتب الجهوي ... – مباراة توظيف 5 مهندسي دولة و4 متصرفين و11 تقنيًا".
- candidate_list:
  prefer "الجهة: لوائح المدعوين لاجتياز ..." and name the written/oral stage when verified.
- results/final_results:
  prefer "الجهة: النتائج ..." or "النتائج النهائية ..." and preserve the campaign identity.
- guides / seasonal-work opportunities when such a source is explicitly verified:
  prefer a descriptive search title such as "عقود العمل الموسمية في أوروبا 2026:
  الشروط، الرواتب، وطريقة التقديم" rather than a vague "فرص عمل في أوروبا".
- University/education result notices should start with the result intent when verified,
  e.g. "نتائج ماسترات جامعة ... للموسم 2026/2027".
- Use Arabic punctuation naturally: colon ":" or dash "–" only when it improves readability.
- Avoid redundant wording, duplicate employer names, raw concatenations, and awkward
  mixtures such as "Technical Lead ServiceNowSiège...".
- Never invent a number, stage, year, location, or result status.
- Never add a deadline, salary, or seat count merely for freshness.
- A verified competition/campaign year may appear when it is part of the official notice itself.

NOTICE TYPE
- Read job_notice_type and job_notice_status before writing.
- vacancy: write an active opportunity/competition article and explain how to apply.
- candidate_list: this is NOT a new vacancy. State that candidate/invited lists were
  published and direct readers to the official lists; never tell them to submit a new application.
- results/final_results: state the published result status accurately; never present it as a new opening.
- provisional: explicitly say the list/result is provisional and may be updated when
  that status is verified. final: describe it as final only when verified.
- If the source text clearly represents an update to an existing competition, make
  the update itself the focus instead of rewriting the old vacancy as new.

INTRODUCTION
- Start with ONE short paragraph naming the employer, the clearly translated Arabic
  job title (with the original title in parentheses when useful), and verified location when available.
- Do not repeat all table facts in the introduction.

DETAILS
- Prefer ONE compact semantic <table> for verified structured facts.
- Include only available rows such as employer, translated position + original title,
  location, contract, number of positions, published date, deadline, experience, diploma,
  competition/list status, and notice type when useful to the reader.
- Never create rows for missing information.
- DEADLINE IS IMPORTANT: when job_deadline_display or job_deadline exists, it MUST
  appear clearly in the article in a row labelled "آخر أجل للترشيح". Do not bury it
  inside prose. If an exact clock time is verified, preserve it too.
- If no deadline is verified, omit the deadline row completely; never write "غير محدد".

REQUIREMENTS
- Add <h2>الشروط والمؤهلات</h2> only when verified requirements exist.
- Summarize the useful candidate requirements in a short <ul>.
- Keep education, experience, technical skills, languages, certifications, or
  essential responsibilities only when explicitly supported.
- Never copy long corporate descriptions or repeat the same fact.

APPLICATION, RESULTS AND OFFICIAL FILES
- For an active vacancy, the strongest verified application resource is essential.
- Prefer, in order: direct Apply/Postuler/Candidature URL, official application
  form, official PDF/conditions/notice file, specific official job page, then a
  general careers page only when nothing more specific exists.
- If job_application_url exists, include it exactly once in html_content.
- If job_application_link_kind is "direct_apply", label it clearly as "التقديم المباشر".
- For candidate_list/results/final_results, do NOT call the link "التقديم" unless a
  real application is still open. Label it according to its real purpose: "تحميل اللائحة",
  "اللائحة الرسمية", "النتائج الرسمية", "الإعلان الرسمي", etc.
- If job_document_links contains ONE useful official file, include its exact URL once.
- If job_document_links contains MULTIPLE files/lists, build one compact table instead
  of a long paragraph/list. Use verified link label/context to create useful columns
  such as الدبلوم، التخصص/الفئة، والرابط الرسمي ONLY when those facts are actually supported.
- Preserve EVERY useful verified official PDF/list URL needed by the notice; do not
  silently drop specializations just to make the article shorter.
- The link "context" field describes the surrounding official table/list row. Use it
  to distinguish documents, but never invent a diploma/specialty that context does not state.
- Preserve URLs EXACTLY. Never shorten, rewrite, fabricate, or duplicate a URL.
- External links must use target="_blank" rel="nofollow noreferrer noopener".

IMAGES
- DO NOT add <img>, <picture>, <figure>, image URLs, logos, captions, or source images.
- The application generates exactly ONE article cover separately from the owner
  template + employer logo + job title.
- Never use og:image or any source-page hero/content image.

SEO
- Meta description: natural Arabic, approximately 100-160 characters.
- Use desired_slug EXACTLY when supplied.
- Never add mutable values such as dates, deadline, salary, number of positions,
  or temporary campaign details to the slug.

HTML
- Clean semantic Blogger HTML only.
- Useful tags: <p>, <h2>, <ul>, <li>, <table>, <tbody>, <tr>, <th>, <td>, <a>.
- No CSS, style attributes, scripts, iframes, fake buttons, forms, or tracking code.
- Avoid repeated paragraphs, repeated headings, repeated facts, and repeated URLs.

FINAL SILENT CHECK
Before returning JSON, verify:
- Explanatory prose is concise and useful; multi-row official tables are allowed when needed.
- No unsupported information.
- No duplicated paragraph or URL.
- If a verified deadline exists, "آخر أجل للترشيح" is visible with that deadline.
- Active vacancy: strongest verified application link included.
- Candidate list/result: status is clear and it is NOT falsely presented as a new vacancy.
- All useful verified official PDF/list links are preserved when available.
- Multiple official documents are organized in a table, not dumped as raw links.
- No image tag or image URL inside html_content.
- No fake salary, deadline, vacancies, diploma, requirement, list status, or result.
- desired_slug preserved exactly when provided.

OUTPUT JSON SHAPE:
{{
  "title": "Arabic SEO title",
  "description": "Arabic meta description",
  "slug": "{package.get('desired_slug') or 'stable-job-slug'}",
  "html_content": "clean semantic HTML"
}}

VERIFIED JOB PACKAGE:
{package_json}
""".strip()
    if FAST_NEWS_MODE:
        return f"""
You are a fast Arabic technology news editor for a Blogger automation pipeline.

Create blogger_article_html: a useful, publish-ready Arabic news article. This is
not facebook_post_text, not a social report, and not a short social caption.

STRICT FAST NEWS RULES:
- Return JSON only. No markdown fences, notes, or explanations.
- Write natural human Arabic. Do not translate literally.
- The article must be mostly Arabic. Keep English only for essential technical terms:
  AI, API, CVE, Malware, Android, iOS, Windows, Linux, VPN, GitHub, OpenAI, Microsoft, Google.
- Translate ordinary English words, entertainment terms, and generic verbs/nouns into Arabic.
- Preserve facts exactly. Do not invent numbers, dates, quotes, incidents, claims, or links.
- Keep technical names normally written in English.
- The source package richness is {"rich" if source_is_rich else "thin"}.
- If the source package is rich, html_content must be a complete Arabic Blogger article of {LONG_FORM_ARTICLE_TARGET_RANGE} words.
- If the source package is thin, still write the fullest accurate article possible and never return a thin brief, teaser, or social caption.
- Never accept a short article when the source package already contains enough detail.
- Structure:
  1) Short strong introduction.
  2) Main explanation with clear <h2> headings.
  3) Add <h2>{REQUIRED_READER_SECTION_WITH_QUESTION}</h2> when it helps the reader understand impact.
  4) Short conclusion or takeaway.
- If cybersecurity-related, include brief practical protection/advice when supported.
- Keep SEO title 40-70 characters and meta description 100-170 characters.
- Use clean clean semantic Blogger HTML only.
- Start with a strong ordinary <p> introduction.
- Use short <p> paragraphs and clear <h2>/<h3> headings.
- Use <strong>/<b>/<em> for emphasis only when useful.
- Use <ul>/<ol> lists for requirements, steps, benefits, or grouped facts.
- Use a semantic <table> with <thead>/<tbody>/<tr>/<th>/<td> whenever structured facts are clearer in rows and columns.
- Use <blockquote> only for real quotations from the source.
- Use <details><summary>...</summary>...</details> only when an expandable explanation genuinely helps.
- Use ordinary <a href='exact_url' target='_blank' rel='nofollow noreferrer noopener'>...</a> links and preserve URLs exactly.
- Never add CSS, inline styles, scripts, iframes, tracking code, or invented links.
- Do not add CSS, scripts, unsupported widgets, fake images, or source/reference blocks unless trusted_references are provided.
- Before returning, silently self-check: no source-domain links, no visible JSON inside html_content,
  no markdown fences, no repeated paragraphs, and no social-media caption tone.

OUTPUT JSON SHAPE:
{{
  "title": "Arabic SEO title, 40-70 characters",
  "description": "Arabic meta description, 100-170 characters",
  "slug": "latin-url-slug",
  "html_content": "clean semantic HTML article body"
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
post, social report, excerpt, or summary.

STRICT RULES:
- Return JSON only. No markdown fences, no notes, no explanations.
- Do not invent facts, numbers, links, dates, quotes, or claims.
- Preserve the meaning of the source content.
- Write fluent Modern Standard Arabic with a natural human style.
- The article must be mostly Arabic. Keep English only for essential technical terms:
  AI, API, CVE, Malware, Android, iOS, Windows, Linux, VPN, GitHub, OpenAI, Microsoft, Google.
- Translate ordinary English words, entertainment terms, and generic verbs/nouns into Arabic.
- Do not translate technical names that are normally kept in English.
- Never mention the scraped source website as the article source.
- Do not say the article was translated, rewritten, copied, or sourced from another article.
- If credibility is needed, mention only official/security references available in trusted_references.
- Keep product names, company names, malware names, commands, CVE IDs, URLs, and short technical terms in English.
- Blogger is the main output. Write a complete long-form article, not a social caption or summary.
- The html_content body should be a complete Arabic article in the {LONG_FORM_ARTICLE_TARGET_RANGE} word range whenever the source package is rich.
- If the source package is thin, expand responsibly and still avoid returning a short article.
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
- Format html_content using clean semantic HTML only.
- Do not add CSS, <style>, <script>, or unsupported components.
- Start with a strong ordinary <p> introduction.
- Use clean semantic HTML: <p>, <h2>, <h3>, <strong>, <b>, <em>, <ul>, <ol>, <li>, <table>, <thead>, <tbody>, <tr>, <th>, <td>, <blockquote>, <details>, <summary>, <a>, <pre>, and <code> when useful.
- Use tables only for genuinely structured information and never invent missing values.
- Preserve real URLs exactly and use ordinary external anchors with target='_blank' and rel='nofollow noreferrer noopener'.
- Do not insert images yourself. The application owns image selection, resizing, fallback generation, and insertion.
- If trusted_references exist, the application appends them automatically.
- If related_posts exist, the application appends them automatically.
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
  "html_content": "clean semantic HTML article body"
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
    missing = [field for field in REQUIRED_ARTICLE_FIELDS if not str(data.get(field, "")).strip()]
    if missing:
        raise AIIncompleteResponseError("Missing AI output field(s): " + ", ".join(missing))

    title = str(data["title"]).strip()
    description = str(data["description"]).strip()
    html_content = str(data["html_content"]).strip()

    if JOBS_MODE:
        title_ok = 28 <= len(title) <= 150
        description_ok = 70 <= len(description) <= 190
        title_range, description_range = "28-150", "70-190"
    elif FAST_NEWS_MODE and ALLOW_SHORT_ARTICLES:
        title_ok = 10 <= len(title) <= 90
        description_ok = 40 <= len(description) <= 190
        title_range, description_range = "10-90", "40-190"
    else:
        title_ok = 40 <= len(title) <= 70
        description_ok = 100 <= len(description) <= 170
        title_range, description_range = "40-70", "100-170"

    if not title_ok:
        raise AIIncompleteResponseError(f"SEO title length must be {title_range} characters; got {len(title)}")
    if not description_ok:
        raise AIIncompleteResponseError(
            f"Meta description length must be {description_range} characters; got {len(description)}"
        )
    if not html_content:
        raise AIIncompleteResponseError("html_content is empty")
    word_count = html_word_count(html_content)
    minimum_words = _minimum_article_words_for_package(package)
    if word_count < minimum_words:
        raise AIOutputRejectedShortError(
            f"article too short ({word_count} words; minimum {minimum_words})"
        )

    result = validate_ai_article_output(data, package=package)
    if not result.passed:
        if "too short" in str(result.reason).casefold():
            raise AIOutputRejectedShortError(result.reason)
        raise ValueError(result.reason)
    phase3_reason = _phase3_quality_failure_reason(data, package=package)
    if phase3_reason:
        raise ValueError(phase3_reason)


def _build_expansion_retry_prompt(package, previous_data, previous_error):
    previous_html = ""
    if isinstance(previous_data, dict):
        previous_html = str(previous_data.get("html_content") or "")
    source_text = _source_text_for_package(package)
    if JOBS_MODE:
        return f"""
Return JSON only with title, description, slug, html_content.

The previous compact job listing failed this quality rule:
{previous_error}

Rewrite ONLY as a concise verified job listing.

MANDATORY JOB RETRY RULES:
- Target 135-200 Arabic words; the shared accepted minimum is 100 words.
- Aim for a 45-75 character title; keep clear meaning (accepted range 28-150).
- For vacancy notices, the title MUST explicitly contain an employment action:
  "توظف", "تعلن عن توظيف", "فرصة توظيف", or "مباراة توظيف" as appropriate.
- Before returning, silently count the article words and title characters.
- Do not expand into a long article.
- Keep one short introduction.
- Keep one compact facts table with verified values only.
- Keep at most one short requirements <ul> when supported.
- Include job_application_url exactly once when present.
- Include useful job_document_links exactly once when present.
- No images, captions, corporate history, career advice, filler, conclusion, or repeated facts.
- Do not invent any fact or URL.
- Preserve desired_slug exactly when supplied.
- Meta description 100-160 characters.
- Clean semantic HTML only.

SOURCE PACKAGE:
{json.dumps(package, ensure_ascii=False, indent=2)}

SOURCE TEXT:
{source_text}

PREVIOUS HTML FOR DIAGNOSIS ONLY:
{previous_html[:3500]}
""".strip()

    if FAST_NEWS_MODE:
        return f"""
Return JSON only using the same shape as before.

The previous fast-news Blogger article failed the production quality gate:
{previous_error}

Rewrite it as a complete fast Arabic news article, not a Facebook caption.

Mandatory fixes:
- The response is rejected unless title, description, slug, and html_content are all present.
- html_content must be structurally complete Blogger HTML with no truncated tags.
- If the source package is rich, html_content must be a complete Arabic article of {LONG_FORM_ARTICLE_TARGET_RANGE} words.
- If the source package is thin, still expand responsibly and never return a short brief or social caption.
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
not a social report.

Mandatory fixes:
- The response is rejected unless title, description, slug, and html_content are all present.
- html_content must be structurally complete Blogger HTML with no truncated tags.
- html_content should stay in the {LONG_FORM_ARTICLE_TARGET_RANGE} word range for a complete article.
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


def _build_excess_english_retry_prompt(package, previous_data, previous_error):
    previous_html = ""
    if isinstance(previous_data, dict):
        previous_html = str(previous_data.get("html_content") or "")
    source_text = _source_text_for_package(package)
    return f"""
Return JSON only using the same shape as before.

The previous Blogger article failed because it contained too much English:
{previous_error}

Rewrite the article in natural Modern Standard Arabic.

Strict language rules:
- Arabic must dominate every paragraph.
- Keep English only for essential technical terms and names:
  AI, API, CVE, Malware, Android, iOS, Windows, Linux, VPN, GitHub, OpenAI, Microsoft, Google.
- Translate generic English words such as movies, streaming, feature, update, workflow, security, privacy,
  account, protection, tool, software, and similar non-brand terms into Arabic.
- Do not leave long English phrases or sentences inside paragraph text.
- The response is rejected unless title, description, slug, and html_content are all present.
- html_content must be structurally complete Blogger HTML with no truncated tags.
- If the source package is rich, aim for a complete article in the {LONG_FORM_ARTICLE_TARGET_RANGE} word range.
- Preserve facts from the source; do not invent claims, numbers, dates, quotes, or links.
- Keep clean clean semantic HTML and a concise fast-news structure.

SOURCE PACKAGE:
{json.dumps(package, ensure_ascii=False, indent=2)}

SOURCE TEXT:
{source_text}

PREVIOUS HTML, for diagnosis only:
{previous_html[:4000]}
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


def _shorten_metadata_once_if_needed(data):
    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "")).strip()

    if JOBS_MODE:
        # Do not cut the employer, translated role or result stage to hit a
        # generic SEO character target. Meaning is checked by the Jobs gate.
        if len(description) > 190:
            data["description"] = _trim_to_length(description, 180)
        return data

    if len(title) <= 70 and len(description) <= 170:
        return data

    if len(title) > 70:
        data["title"] = _trim_to_length(title, 65)

    if len(description) > 170:
        data["description"] = _trim_to_length(description, 160)

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

    # Keep article markup semantic and theme-independent.
    # Strip inline event handlers/classes/styles that an AI response may have invented.
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on") or attr.lower() in {"style", "class", "id"}:
                tag.attrs.pop(attr, None)

    paragraphs = _normal_paragraphs(soup)

    # Remove old images
    for img in soup.find_all("img"):
        img.decompose()

    # Insert the one approved main image after the first paragraph.
    main_image = package.get("main_image") or ""
    if main_image and paragraphs:
        title = package.get("cover_alt") or package.get("title") or "صورة المقال"
        width = str(package.get("cover_width") or "").strip()
        height = str(package.get("cover_height") or "").strip()
        size_attrs = (
            f" width='{escape(width, quote=True)}' height='{escape(height, quote=True)}'"
            if width and height
            else ""
        )
        if JOBS_MODE:
            image_html = (
                "<figure>\n"
                f"  <img alt='{escape(title, quote=True)}'{size_attrs} "
                f"src='{escape(main_image, quote=True)}'/>\n"
                "</figure>"
            )
        else:
            image_html = (
                "<figure>\n"
                f"  <img alt='{escape(title, quote=True)}'{size_attrs} "
                f"src='{escape(main_image, quote=True)}'/>\n"
                f"  <figcaption>{escape(title)}</figcaption>\n"
                "</figure>"
            )
        paragraphs[0].insert_after(BeautifulSoup(image_html, "html.parser"))

    # Jobs must contain one cover only; source-page extra images are forbidden.
    extra_images = [] if JOBS_MODE else (package.get("extra_article_images") or [])
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
                    "<figure>\n"
                    f"  <img alt='{escape(alt_text, quote=True)}' "
                    f"src='{escape(image_url, quote=True)}' loading='lazy'/>\n"
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

    title = package.get("cover_alt") or package.get("title") or "صورة المقال"
    width = str(package.get("cover_width") or "").strip()
    height = str(package.get("cover_height") or "").strip()
    size_attrs = (
        f" width='{escape(width, quote=True)}' height='{escape(height, quote=True)}'"
        if width and height
        else ""
    )
    image_html = (
        "<figure>\n"
        f"  <img alt='{escape(title, quote=True)}'{size_attrs} "
        f"src='{escape(main_image, quote=True)}'/>\n"
        + ("" if JOBS_MODE else f"  <figcaption>{escape(title)}</figcaption>\n")
        + "</figure>\n"
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


TRUSTED_OFFICIAL_LINK_HOSTS = (
    "cisa.gov",
    "microsoft.com",
    "google.com",
    "openai.com",
    "github.com",
    "nvd.nist.gov",
    "mitre.org",
    "cve.org",
)


def _official_reference_host(url):
    host = urlparse(str(url or "")).netloc.lower().removeprefix("www.")
    return any(host == trusted or host.endswith("." + trusted) for trusted in TRUSTED_OFFICIAL_LINK_HOSTS)


def _sanitize_source_links(html_content, package):
    if JOBS_MODE:
        return html_content
    source_url = package.get("url") or package.get("source_url") or ""
    if not source_url:
        return html_content

    soup = BeautifulSoup(html_content, "html.parser")
    changed = False
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        if _same_host(href, source_url) and not _official_reference_host(href):
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


def _clean_general_english_in_paragraphs(html_content):
    soup = BeautifulSoup(html_content or "", "html.parser")
    changed = False
    allowed = {item.casefold() for item in ALLOWED_LATIN_INLINE}

    def replace_text(text):
        nonlocal changed

        def repl(match):
            token = match.group(0)
            stripped = token.strip()
            key = stripped.casefold().strip("._-")
            if key in allowed or re.match(r"^(CVE-\d{4}-\d+|v?\d+(?:\.\d+)+)$", stripped, flags=re.I):
                return token
            replacement = GENERAL_ENGLISH_ARABIC_REPLACEMENTS.get(key)
            if replacement:
                changed = True
                return replacement
            return token

        return re.sub(r"\b[A-Za-z][A-Za-z0-9+._-]{1,}\b", repl, text)

    for paragraph in soup.find_all("p"):
        for node in list(paragraph.descendants):
            if isinstance(node, NavigableString) and not node.find_parent(["code", "pre", "a"]):
                cleaned = replace_text(str(node))
                if cleaned != str(node):
                    node.replace_with(cleaned)

    return str(soup) if changed else html_content


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
    if len(soup.find_all("h2")) < (1 if (FAST_NEWS_MODE or JOBS_MODE) else 2):
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
    if (not JOBS_MODE) and source_name and source_name.casefold() in body_text.casefold():
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
    formatted = _plus_ui_format_html(html_content, package or {})
    if JOBS_MODE:
        formatted = _remove_empty_job_fact_rows(formatted)
    return formatted


def _remove_empty_job_fact_rows(html_content):
    if not JOBS_MODE:
        return html_content
    soup = BeautifulSoup(html_content or "", "html.parser")
    empty_values = {"", "0", "0.0", "unknown", "none", "null", "غير محدد", "غير متوفر"}
    changed = False
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        value = cells[-1].get_text(" ", strip=True).casefold()
        if value in empty_values:
            row.decompose()
            changed = True
    return str(soup) if changed else html_content


def _append_job_action_links_if_missing(html_content, package):
    if not JOBS_MODE:
        return html_content

    soup = BeautifulSoup(html_content or "", "html.parser")
    existing = {
        str(link.get("href") or "").strip()
        for link in soup.find_all("a", href=True)
        if str(link.get("href") or "").strip()
    }
    rows = []
    seen = set(existing)

    application_url = str(package.get("job_application_url") or "").strip()
    if application_url and application_url not in seen:
        label = (
            "التقديم المباشر"
            if package.get("job_application_link_kind") == "direct_apply"
            else "صفحة الإعلان أو التقديم الرسمية"
        )
        rows.append((label, application_url))
        seen.add(application_url)

    for index, item in enumerate(package.get("job_document_links") or [], start=1):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        label = str(item.get("label") or "").strip() or f"الملف الرسمي {index}"
        rows.append((label, url))
        seen.add(url)

    if not rows:
        return html_content

    has_application_heading = any(
        "تقديم" in heading.get_text(" ", strip=True)
        for heading in soup.find_all(["h2", "h3"])
    )
    block = []
    if not has_application_heading:
        block.append("<h2>طريقة التقديم</h2>")
    for label, url in rows:
        block.append(
            "<p><a class='extL' "
            f"href='{escape(url, quote=True)}' "
            "target='_blank' rel='nofollow noreferrer noopener'>"
            f"{escape(label)}</a></p>"
        )
    return html_content.rstrip() + "\n" + "\n".join(block)


def _finalize_html_content(data, package):
    """Finalize HTML content for publication."""
    html_content = data["html_content"]
    html_content = _sanitize_source_links(html_content, package)
    html_content = _plus_ui_format_html(html_content, package)
    if JOBS_MODE:
        # The single branded job cover is generated later by the Blogger publisher.
        # AI output never imports or inserts images from the source job page.
        html_content = _remove_empty_job_fact_rows(html_content)
        html_content = _append_job_action_links_if_missing(html_content, package)
    else:
        html_content = _insert_main_image_if_missing(html_content, package)
        html_content = _append_trusted_references_if_missing(html_content, package)
        html_content = _append_related_posts_if_missing(html_content, package)
    html_content = _sanitize_source_links(html_content, package)
    html_content = _clean_general_english_in_paragraphs(html_content)
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
        raise AIProviderEmptyResponse("Gemini returned an empty response.")
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
        raise AIProviderEmptyResponse("OpenRouter returned no choices.")
    message = choices[0].get("message") or {}
    text = _normalize_openai_content(message.get("content")).strip()
    if not text:
        raise AIProviderEmptyResponse("OpenRouter returned an empty response.")
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
        raise AIProviderEmptyResponse("OpenAI returned no choices.")
    message = choices[0].get("message") or {}
    text = _normalize_openai_content(message.get("content")).strip()
    if not text:
        raise AIProviderEmptyResponse("OpenAI returned an empty response.")
    return text, f"openai:{model_name}"


def _generate_openai_compatible(
    prompt,
    *,
    api_key,
    api_url,
    model_name,
    max_tokens,
    timeout_seconds,
    provider_name,
):
    if not str(api_key or "").strip():
        raise RuntimeError(f"{provider_name.upper()} API key is missing.")
    response = requests.post(
        api_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.30,
        },
        timeout=timeout_seconds,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"{provider_name} API error {response.status_code}: {response.text[:500]}"
        )
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise AIProviderEmptyResponse(f"{provider_name} returned no choices.")
    message = choices[0].get("message") or {}
    text = _normalize_openai_content(message.get("content")).strip()
    if not text:
        raise AIProviderEmptyResponse(f"{provider_name} returned an empty response.")
    return text, f"{provider_name}:{data.get('model') or model_name}"


def _generate_with_groq(prompt, api_key=None, model_name=None, timeout_seconds=None):
    return _generate_openai_compatible(
        prompt,
        api_key=api_key or GROQ_API_KEY,
        api_url=GROQ_API_URL,
        model_name=model_name or GROQ_MODEL,
        max_tokens=GROQ_MAX_TOKENS,
        timeout_seconds=timeout_seconds or GROQ_TIMEOUT_SECONDS,
        provider_name="groq",
    )


def _generate_with_mistral(prompt, api_key=None, model_name=None, timeout_seconds=None):
    return _generate_openai_compatible(
        prompt,
        api_key=api_key or MISTRAL_API_KEY,
        api_url=MISTRAL_API_URL,
        model_name=model_name or MISTRAL_MODEL,
        max_tokens=MISTRAL_MAX_TOKENS,
        timeout_seconds=timeout_seconds or MISTRAL_TIMEOUT_SECONDS,
        provider_name="mistral",
    )


def _generate_with_cloudflare(prompt, api_key=None, model_name=None, timeout_seconds=None):
    api_key = api_key or CLOUDFLARE_API_TOKEN
    model_name = model_name or CLOUDFLARE_MODEL
    if not api_key or not CLOUDFLARE_ACCOUNT_ID:
        raise RuntimeError("Cloudflare Workers AI credentials are missing.")
    api_url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        + CLOUDFLARE_ACCOUNT_ID
        + "/ai/run/"
        + model_name
    )
    response = requests.post(
        api_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "prompt": prompt,
            "max_tokens": CLOUDFLARE_MAX_TOKENS,
            "temperature": 0.30,
        },
        timeout=timeout_seconds or CLOUDFLARE_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Cloudflare API error {response.status_code}: {response.text[:500]}"
        )
    data = response.json()
    if data.get("success") is False:
        raise RuntimeError(f"Cloudflare API returned failure: {str(data.get('errors') or '')[:300]}")
    result = data.get("result")
    if isinstance(result, dict):
        text = str(result.get("response") or result.get("text") or "").strip()
    else:
        text = str(result or "").strip()
    if not text:
        raise AIProviderEmptyResponse("Cloudflare returned an empty response.")
    return text, f"cloudflare:{model_name}"


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


def _is_empty_provider_response(error):
    if isinstance(error, AIProviderEmptyResponse):
        return True
    message = str(error).casefold()
    return any(
        hint in message
        for hint in (
            "no choices",
            "empty response",
            "no usable message",
            "returned no response",
        )
    )


def _resolve_providers():
    provider = (AI_PROVIDER or "").strip().lower()
    if provider == "auto":
        providers = []
        if _has_real_key(GEMINI_API_KEY, "your_gemini_api_key_here"):
            providers.append("gemini")
        if _has_real_key(GROQ_API_KEY, ""):
            providers.append("groq")
        if _has_real_key(OPENROUTER_API_KEY, "your_new_key_here"):
            providers.append("openrouter")
        if _has_real_key(CLOUDFLARE_API_TOKEN, "") and str(CLOUDFLARE_ACCOUNT_ID or "").strip():
            providers.append("cloudflare")
        if _has_real_key(MISTRAL_API_KEY, ""):
            providers.append("mistral")
        if _has_real_key(OPENAI_API_KEY, "your_openai_api_key_here"):
            providers.append("openai")
        if providers:
            return providers
        if JOBS_MODE:
            return []
        raise RuntimeError("No AI provider key configured.")
    if provider in {"gemini", "groq", "openrouter", "cloudflare", "mistral", "openai"}:
        return [provider]
    raise RuntimeError(
        "AI_PROVIDER must be one of: gemini, groq, openrouter, cloudflare, mistral, openai, auto"
    )


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
    auto_mode = (AI_PROVIDER or "").strip().lower() == "auto"
    candidates = []
    for provider in providers:
        if provider == "gemini":
            if _has_real_key(GEMINI_API_KEY, "your_gemini_api_key_here"):
                candidates.append({"provider": "gemini", "api_key": GEMINI_API_KEY, "model": GEMINI_MODEL})
        elif provider == "groq":
            if _has_real_key(GROQ_API_KEY, ""):
                candidates.append({"provider": "groq", "api_key": GROQ_API_KEY, "model": GROQ_MODEL})
        elif provider == "openrouter":
            if _has_real_key(OPENROUTER_API_KEY, "your_new_key_here"):
                resolved_openrouter_models = _resolve_openrouter_models()
                if context:
                    context.skipped_slow_models_count = _skipped_slow_models_count()
                for model_name in resolved_openrouter_models:
                    candidates.append({"provider": "openrouter", "api_key": OPENROUTER_API_KEY, "model": model_name})
        elif provider == "cloudflare":
            if _has_real_key(CLOUDFLARE_API_TOKEN, "") and str(CLOUDFLARE_ACCOUNT_ID or "").strip():
                candidates.append({"provider": "cloudflare", "api_key": CLOUDFLARE_API_TOKEN, "model": CLOUDFLARE_MODEL})
        elif provider == "mistral":
            if _has_real_key(MISTRAL_API_KEY, ""):
                candidates.append({"provider": "mistral", "api_key": MISTRAL_API_KEY, "model": MISTRAL_MODEL})
        elif provider == "openai":
            if _has_real_key(OPENAI_API_KEY, "your_openai_api_key_here"):
                candidates.append({"provider": "openai", "api_key": OPENAI_API_KEY, "model": OPENAI_MODEL})
    if not candidates:
        raise RuntimeError("No AI provider key configured.")
    # In auto mode keep provider priority deterministic (Gemini -> OpenRouter).
    # Speed memory may still be used when the user explicitly locks one provider.
    return candidates if auto_mode else _reorder_candidates_by_speed_memory(candidates)


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
    active_candidates = []
    for candidate in candidates:
        cooldown = _cooldown_remaining(candidate)
        if cooldown > 0:
            log_event(
                "ai_candidate_skipped_cooldown",
                provider=candidate.get("provider"),
                model=candidate.get("model"),
                key_id=_key_id(candidate.get("api_key")),
                remaining_seconds=int(cooldown),
            )
            continue
        active_candidates.append(candidate)

    if not active_candidates:
        raise AIProviderRotationExhausted(
            "All configured AI candidates are cooling down."
        )

    for index, candidate in enumerate(active_candidates):
        _check_ai_time_budget(context, stage="candidate_loop")
        provider = candidate["provider"]
        try:
            result = _generate_with_candidate(candidate, prompt, context=context)
            return result
        except Exception as error:
            last_error = error
            if _is_empty_provider_response(error):
                log_event(
                    "ai_provider_empty_response",
                    provider=provider,
                    model=candidate.get("model"),
                    article_id=getattr(context, "article_id", ""),
                )
            _put_candidate_on_cooldown(candidate, error)
            reason = "quota/rate limit" if _is_quota_or_rate_limit_error(error) else "error"
            if index < len(active_candidates) - 1:
                print(f"  AI provider {provider} {reason}. Trying next AI candidate...")
                log_event(
                    "ai_provider_switch",
                    article_id=getattr(context, "article_id", ""),
                    from_provider=provider,
                    to_provider=active_candidates[index + 1].get("provider"),
                    reason=_safe_error_reason(error),
                )
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
    elif provider == "groq":
        raw_text, provider_used = _generate_with_groq(
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
    elif provider == "cloudflare":
        raw_text, provider_used = _generate_with_cloudflare(
            prompt,
            candidate.get("api_key"),
            candidate.get("model"),
            timeout_seconds=timeout_seconds,
        )
    elif provider == "mistral":
        raw_text, provider_used = _generate_with_mistral(
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
        # Auto mode has a fixed safety order: Gemini first, OpenRouter fallback.
        # Runtime speed memory must never promote a flaky fallback provider
        # ahead of the primary provider.
        sequence = [
            provider
            for provider in ("gemini", "groq", "openrouter", "cloudflare", "mistral", "openai")
            if provider in providers
        ]
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

    active_allowed = []
    for candidate in allowed:
        cooldown = _cooldown_remaining(candidate)
        if cooldown > 0:
            log_event(
                "ai_candidate_skipped_cooldown",
                provider=provider,
                model=candidate.get("model"),
                key_id=_key_id(candidate.get("api_key")),
                remaining_seconds=int(cooldown),
            )
            continue
        active_allowed.append(candidate)

    if not active_allowed:
        raise AIProviderFallbackNeeded(
            f"{provider} candidates are cooling down"
        )

    for index, candidate in enumerate(active_allowed):
        timeout_retry_count = 0
        while True:
            _check_ai_time_budget(context, stage=f"{provider}_candidate")
            log_event(
                "ai_waiting_for_complete_response",
                article_id=getattr(context, "article_id", ""),
                provider=provider,
                model=candidate.get("model"),
                stage=getattr(context, "current_stage", "article_generation"),
                timeout_seconds=_provider_timeout_seconds(candidate, context=context),
                timeout_retry=timeout_retry_count,
            )
            try:
                result = _generate_with_candidate(candidate, prompt, context=context)
                return result
            except Exception as error:
                last_error = error
                if _is_timeout_error(error) and timeout_retry_count < AI_TIMEOUT_RETRIES:
                    timeout_retry_count += 1
                    log_event(
                        "ai_timeout_retry",
                        article_id=getattr(context, "article_id", ""),
                        provider=provider,
                        model=candidate.get("model"),
                        stage=getattr(context, "current_stage", "article_generation"),
                        retry=timeout_retry_count,
                        max_retries=AI_TIMEOUT_RETRIES,
                        reason=_safe_error_reason(error),
                    )
                    time.sleep(min(timeout_retry_count, 2))
                    continue
                if _is_empty_provider_response(error):
                    log_event(
                        "ai_provider_empty_response",
                        provider=provider,
                        model=candidate.get("model"),
                        article_id=getattr(context, "article_id", ""),
                    )
                _put_candidate_on_cooldown(candidate, error)
                if provider == "gemini" and context:
                    context.gemini_failures += 1
                if provider == "openrouter" and context and context.gemini_failures:
                    context.openrouter_failures_after_gemini += 1
                reason = (
                    "quota/rate limit/temporary provider error"
                    if _is_quota_or_rate_limit_error(error)
                    else "provider error"
                )
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
                if index < len(active_allowed) - 1:
                    log_event(
                        "ai_provider_switch",
                        article_id=getattr(context, "article_id", ""),
                        from_provider=provider,
                        to_provider=active_allowed[index + 1].get("provider"),
                        reason=_safe_error_reason(error),
                    )
                    break
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
    minimum_words = _minimum_article_words_for_package(article.get("ai_input_package") or {})
    if word_count < minimum_words:
        raise ValueError(
            f"article too short ({word_count} words; minimum {minimum_words})"
        )
    article["ai_status"] = "completed"
    article["ai_processed_at"] = _now_iso()
    article["seo_title"] = str(data["title"]).strip()
    article["seo_description"] = str(data["description"]).strip()
    article["seo_slug"] = (
        str((article.get("ai_input_package") or {}).get("desired_slug") or "").strip()
        if JOBS_MODE
        else _normalize_slug(data["slug"])
    ) or _normalize_slug(data["slug"])
    article["final_html"] = final_html
    article["blogger_article_html"] = final_html
    article["final_word_count"] = word_count
    article["final_html_chars"] = len(final_html)
    article["final_content_hash"] = content_hash_from_html(final_html)
    article["ai_provider_used"] = provider_used
    article.pop("ai_error", None)



def _apply_failure(article, error):
    article["ai_status"] = "failed"
    article["ai_error"] = str(error)


def _deterministic_job_article(package):
    """Build a publishable Arabic Jobs article from verified extracted facts only."""
    package = dict(package or {})
    role = str(package.get("job_title") or package.get("title") or "فرصة عمل").strip()
    company = str(package.get("job_company") or package.get("source_name") or "الجهة المعلنة").strip()
    location = str(package.get("job_location") or "").strip()
    notice = str(package.get("job_notice_type") or "vacancy").strip().lower()

    if notice == "candidate_list":
        title = f"لوائح المدعوين لمباراة توظيف {role} لدى {company}"
    elif notice in {"results", "final_results"}:
        title = f"نتائج مباراة توظيف {role} لدى {company}"
    else:
        title = f"فرصة توظيف {role} لدى {company}"
    if len(title) < 28:
        title += " وفق الإعلان الرسمي"

    detail_rows = []
    facts = (
        ("الجهة المشغلة", company),
        ("المنصب", role),
        ("مكان العمل", location),
        ("نوع العقد", package.get("job_contract_type")),
        ("عدد المناصب", package.get("job_number_of_positions")),
        ("المؤهل المطلوب", package.get("job_diploma")),
        ("الخبرة", package.get("job_experience")),
    )
    for label, value in facts:
        value = str(value or "").strip()
        if value and value not in {"0", "None"}:
            detail_rows.append(
                f"<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>"
            )

    deadline = str(
        package.get("job_deadline_display")
        or package.get("job_deadline")
        or ""
    ).strip()
    if deadline:
        detail_rows.append(
            f"<tr><th>آخر أجل للترشيح</th><td>{escape(deadline)}</td></tr>"
        )

    intro = (
        f"يهم هذا الإعلان فرصة مرتبطة بمنصب {escape(role)} لدى {escape(company)}"
        + (f" في {escape(location)}" if location else "")
        + ". ويعرض هذا الملخص المعلومات التي أمكن التحقق منها من المصدر الرسمي، "
          "مع الحفاظ على تفاصيل الترشيح كما وردت دون إضافة شروط أو أرقام غير مؤكدة."
    )
    guidance = (
        "قبل إرسال طلب الترشيح، راجع الإعلان الرسمي كاملا وتأكد من مطابقة بياناتك "
        "للشروط المذكورة فيه. جهز الوثائق المطلوبة بصيغ واضحة، وتحقق من صحة معلومات "
        "الاتصال والسيرة الذاتية قبل الإرسال. إذا كانت الجهة توفر استمارة إلكترونية، "
        "استعمل الرابط الرسمي فقط ولا ترسل وثائقك عبر صفحات أو حسابات غير موثوقة. "
        "احتفظ بنسخة من طلبك أو رسالة التأكيد بعد الإرسال، وراقب البريد الإلكتروني "
        "والصفحة الرسمية للجهة لأي تحديث يخص الاختبارات أو المقابلات أو النتائج. "
        "المعلومات المتغيرة مثل الأجل وعدد المناصب ونوع العقد تعتمد حصرا على ما هو "
        "موثق في الإعلان الأصلي، لذلك يبقى المصدر الرسمي هو المرجع النهائي."
    )
    status_text = ""
    if package.get("job_notice_status"):
        status_text = (
            "<p><strong>حالة الإعلان:</strong> "
            + escape(str(package.get("job_notice_status")))
            + "</p>"
        )

    html = (
        f"<p>{intro}</p>"
        "<h2>المعلومات الأساسية</h2>"
        "<table><tbody>"
        + "".join(detail_rows)
        + "</tbody></table>"
        + status_text
        + "<h2>طريقة التقديم والمتابعة</h2>"
        + f"<p>{guidance}</p>"
    )
    description = (
        f"فرصة مرتبطة بمنصب {role} لدى {company}. "
        "اطلع على المعلومات الموثقة وطريقة التقديم عبر المصدر الرسمي للإعلان."
    )
    if len(description) < 70:
        description += " راجع الشروط والآجال بعناية قبل إرسال طلب الترشيح."
    return {
        "title": title[:150],
        "description": description[:190],
        "slug": str(package.get("desired_slug") or _normalize_slug(title)).strip(),
        "html_content": html,
    }


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
    failed_provider_names = set()
    quality_retry_counts = {}
    forced_next_provider = ""
    excess_english_retry_used = False
    context = AIExecutionContext(article_id=article.get("id") or article.get("url") or "")
    context.skipped_slow_models_count = _skipped_slow_models_count()
    _source_text, source_chars, source_words = _source_stats(package)

    log_event(
        "ai_article_start",
        article_id=article.get("id"),
        source=article.get("source_name"),
        title=package.get("title"),
        source_chars=source_chars,
        source_words=source_words,
        rich_input_source=_is_rich_input_package(package),
        enrichment_status=package.get("enrichment_status"),
        fast_news_mode=FAST_NEWS_MODE,
        ai_fast_mode_enabled=context.fast_mode_enabled,
        skipped_slow_models_count=context.skipped_slow_models_count,
        ai_total_time_budget_seconds=context.total_budget_seconds,
    )

    total_attempts = (
        0
        if JOBS_MODE and not provider_sequence
        else max(1, MAX_AI_ATTEMPTS, len(provider_sequence))
        if JOBS_MODE
        else max(1, MAX_AI_ATTEMPTS)
    )
    if JOBS_MODE and not provider_sequence:
        last_error = RuntimeError(
            "No AI provider is currently available; using deterministic Jobs fallback."
        )
        log_event(
            "ai_providers_unavailable_deterministic_fallback",
            article_id=article.get("id"),
        )
    for attempt in range(1, total_attempts + 1):
        try:
            _check_ai_time_budget(context, stage=f"attempt_{attempt}_start")
        except AITimeBudgetExceeded as error:
            last_error = error
            break
        started = time.perf_counter()
        provider = forced_next_provider or (provider_sequence[(attempt - 1) % len(provider_sequence)] if provider_sequence else "")
        forced_next_provider = ""
        provider_used = ""
        log_event(
            "ai_retry",
            article_id=article.get("id"),
            attempt=attempt,
            max_attempts=total_attempts,
            provider=provider,
        )
        log_event(
            "ai_retry_attempt",
            article_id=article.get("id"),
            attempt=attempt,
            max_attempts=total_attempts,
            provider=provider,
            ai_total_time=round(context.elapsed_seconds(), 2),
        )
        try:
            context.current_stage = "article_generation"
            raw_text, provider_used = (
                _generate_with_provider_name(provider, prompt, context=context)
                if provider
                else _generate_ai_article(prompt, context=context)
            )
            data = _parse_complete_ai_json(raw_text, REQUIRED_ARTICLE_FIELDS, "AI article response")
            previous_data = data
            data = _shorten_metadata_once_if_needed(data)
            data = _normalize_ai_output(data)
            data = _finalize_html_content(data, package)
            _validate_ai_output(data, package=package)
            log_event(
                "ai_complete_response_accepted",
                article_id=article.get("id"),
                attempt=attempt,
                provider=provider_used or provider,
                stage=context.current_stage,
                words=html_word_count(data.get("html_content", "")),
            )
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
            is_incomplete_response = isinstance(error, AIIncompleteResponseError)
            is_short_output = isinstance(error, AIOutputRejectedShortError)
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
                if is_incomplete_response:
                    log_event(
                        "ai_response_incomplete_retry",
                        article_id=article.get("id"),
                        attempt=attempt,
                        provider=provider or provider_used,
                        reason=str(error),
                    )
                if is_short_output:
                    log_event(
                        "ai_output_rejected_short",
                        article_id=article.get("id"),
                        attempt=attempt,
                        provider=provider or provider_used,
                        reason=str(error),
                    )
            else:
                log_event(
                    "provider_failed_reason",
                    article_id=article.get("id"),
                    attempt=attempt,
                    reason=_safe_error_reason(error),
                )
            if is_quality_failure:
                if "too much english inside article paragraphs" in str(error).casefold():
                    if not excess_english_retry_used and attempt < total_attempts:
                        excess_english_retry_used = True
                        article["ai_excess_english_retry_used"] = True
                        log_event(
                            "article_regenerated_due_to_excess_english",
                            article_id=article.get("id"),
                            attempt=attempt,
                            reason=error,
                        )
                        prompt = _build_excess_english_retry_prompt(package, previous_data, str(error))
                        continue
                    log_event(
                        "article_skipped_excess_english_after_retry",
                        article_id=article.get("id"),
                        attempt=attempt,
                        reason=error,
                    )
                    break
                prompt = _build_expansion_retry_prompt(package, previous_data, str(error))
                if provider:
                    quality_retry_counts[provider] = quality_retry_counts.get(provider, 0) + 1
                if attempt < total_attempts and provider:
                    if JOBS_MODE and provider == "gemini":
                        # A valid Gemini response that misses a formatting/quality
                        # constraint is not a provider outage. Repair it with Gemini;
                        # reserve OpenRouter for real Gemini provider/quota failures.
                        forced_next_provider = "gemini"
                    elif quality_retry_counts.get(provider, 0) < 2:
                        forced_next_provider = provider
                    else:
                        forced_next_provider = _next_provider_in_sequence(provider_sequence, provider)
                continue
            if _is_provider_error(error):
                if _is_empty_provider_response(error):
                    log_event(
                        "ai_provider_empty_response",
                        article_id=article.get("id"),
                        provider=provider,
                        reason=_safe_error_reason(error),
                    )
                if _should_switch_gemini_to_openrouter(provider, error) and "openrouter" not in provider_sequence:
                    provider_sequence.append("openrouter")
                    log_event(
                        "gemini_quota_switching_to_openrouter",
                        article_id=article.get("id"),
                        reason=_safe_error_reason(error),
                    )
                if provider:
                    failed_provider_names.add(provider)
                if attempt < total_attempts:
                    next_provider = ""
                    if provider_sequence:
                        start_index = provider_sequence.index(provider) if provider in provider_sequence else -1
                        for offset in range(1, len(provider_sequence) + 1):
                            candidate_provider = provider_sequence[(start_index + offset) % len(provider_sequence)]
                            if candidate_provider not in failed_provider_names:
                                next_provider = candidate_provider
                                break
                    if not next_provider:
                        break
                    forced_next_provider = next_provider
                    log_event(
                        "ai_provider_switch",
                        article_id=article.get("id"),
                        from_provider=provider,
                        to_provider=next_provider,
                        reason=_safe_error_reason(error),
                    )
                    if provider == "gemini" and next_provider == "openrouter":
                        log_event("ai_openrouter_fallback_started", article_id=article.get("id"))
                    continue
                break

    if JOBS_MODE:
        try:
            fallback_data = _deterministic_job_article(package)
            fallback_data = _finalize_html_content(fallback_data, package)
            _validate_ai_output(fallback_data, package=package)
            _apply_success(article, fallback_data, "deterministic:verified-job-template")
            article["ai_rotation_exhausted"] = bool(last_error)
            article["ai_deterministic_fallback"] = True
            article["ai_quality_attempts"] = attempt if "attempt" in locals() else 0
            article["ai_quality_status"] = "passed_deterministic_fallback"
            article["ai_total_time_seconds"] = round(context.elapsed_seconds(), 2)
            save_article_queue(queue)
            log_event(
                "job_deterministic_ai_fallback_success",
                article_id=article.get("id"),
                previous_error=_safe_error_reason(last_error) if last_error else "",
                words=article.get("final_word_count"),
            )
            return {
                "processed": 1,
                "success": 1,
                "failed": 0,
                "article": article,
                "message": "AI providers unavailable; verified deterministic Jobs template used.",
            }
        except Exception as fallback_error:
            log_event(
                "job_deterministic_ai_fallback_failed",
                article_id=article.get("id"),
                reason=_safe_error_reason(fallback_error),
            )

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
        log_event(
            "ai_article_skipped_after_ai_failure",
            article_id=article.get("id"),
            reason="ai_time_budget_exceeded",
        )
    elif provider_exhausted:
        log_event("ai_rotation_exhausted", article_id=article.get("id"), error=last_error)
        log_event("article_skipped", article_id=article.get("id"), reason="AI rotation exhausted")
        log_event(
            "ai_article_skipped_after_ai_failure",
            article_id=article.get("id"),
            reason="AI rotation exhausted",
        )
    else:
        log_event("ai_quality_failed_after_retries", article_id=article.get("id"), error=last_error)
        log_event("article_skipped", article_id=article.get("id"), reason="AI quality failed after retries")
        if "too much english inside article paragraphs" in str(last_error).casefold():
            log_event(
                "article_skipped_excess_english_after_retry",
                article_id=article.get("id"),
                reason=last_error,
            )
        log_event(
            "ai_article_skipped_after_ai_failure",
            article_id=article.get("id"),
            reason="AI quality failed after retries",
        )
        if "too short" in str(last_error).lower():
            log_event(
                "article_skipped_too_short",
                article_id=article.get("id"),
                words=article.get("final_word_count") or 0,
                reason=last_error,
            )
    return {
        "processed": 1,
        "success": 0,
        "failed": 1,
        "article": article,
        "message": str(last_error),
    }
