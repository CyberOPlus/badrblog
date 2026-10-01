# ============================================================
# article_ai_processor.py - Phase 6 AI Article Processing
# ============================================================

import json
import random
import re
import hashlib
import time
import unicodedata
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from state_io import atomic_write_json

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
    FAST_OPENROUTER_MODELS,
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
    JOBS_AI_QUALITY_REPAIRS,
    JOBS_AI_TIMEOUT_RETRIES,
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
from job_core import is_application_url_bound_to_job, is_job_specific_url
from quality_gate import validate_ai_article_output

MAX_AI_ATTEMPTS = max(1, MAX_AI_RETRIES)
MIN_PUBLISHABLE_WORDS = 120
AI_MODEL_COOLDOWN_SECONDS = 30 * 60
_AI_COOLDOWNS = {}
_AI_MEMORY_CACHE = None
FAST_OPENROUTER_MODEL_SET = set(FAST_OPENROUTER_MODELS)
AI_TIMEOUT_RETRIES = (JOBS_AI_TIMEOUT_RETRIES)
MIN_PROVIDER_TIMEOUT_SECONDS = 30
LONG_FORM_ARTICLE_MIN_WORDS = 700
LONG_FORM_ARTICLE_TARGET_RANGE = "700-1000"
RICH_INPUT_MIN_SOURCE_WORDS = 180
RICH_INPUT_MIN_SOURCE_CHARS = 1200
REQUIRED_ARTICLE_FIELDS = ("title", "description", "slug", "html_content")
ALLOWED_JOB_NOTICE_TYPES = {
    "vacancy",
    "competition",
    "candidate_list",
    "results",
    "final_results",
    "update",
}
JOBS_REQUIRED_ARTICLE_FIELDS = REQUIRED_ARTICLE_FIELDS + ("notice_type",)


class AIProviderFallbackNeeded(RuntimeError):
    """Raised when a provider error must switch to another AI provider first."""


class AIProviderRotationExhausted(RuntimeError):
    """Raised when all available AI providers fail before quality validation."""


class AIProviderEmptyResponse(RuntimeError):
    """Raised when an AI provider returns no usable message content."""


class AITimeBudgetExceeded(RuntimeError):
    """Raised when the full AI generation budget is exhausted."""


class AIArticleInputError(RuntimeError):
    """Raised when this article/input cannot be processed by the provider request."""


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
    return {
        "avg_time": 0.0,
        "cooldowns": {},
        "provider_circuits": {},
        "global_circuit": {},
        "failure_fingerprints": {},
        "fastest_success_model": "",
        "stats": {},
    }


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
                data.setdefault("provider_circuits", {})
                data.setdefault("global_circuit", {})
                data.setdefault("failure_fingerprints", {})
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
        atomic_write_json(AI_PROVIDER_MEMORY_PATH, memory, sort_keys=True)
    except Exception as error:
        log_event("ai_memory_save_failed", error=error.__class__.__name__)


def _safe_error_reason(error):
    text = re.sub(r"\s+", " ", str(error or error.__class__.__name__)).strip()
    text = re.sub(r"(key|token|secret|password)[=:]\s*\S+", r"\1=***", text, flags=re.IGNORECASE)
    text = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "AIza***", text)
    text = re.sub(r"sk-[0-9A-Za-z_\-]{12,}", "sk-***", text)
    return text[:160] or error.__class__.__name__


def _normalized_failure_text(error):
    reason = _safe_error_reason(error).casefold()
    reason = re.sub(r"https?://\S+", "<url>", reason)
    reason = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", reason)
    reason = re.sub(r"\b\d+\b", "<n>", reason)
    reason = re.sub(r"\s+", " ", reason).strip()
    return reason[:180]


def _provider_error_category(error):
    message = str(error or "").casefold()
    if any(token in message for token in ("401", "403", "unauthorized", "forbidden", "invalid api key", "invalid key")):
        return "auth"
    if any(token in message for token in ("429", "402", "quota", "rate limit", "rate-limit", "rate_limited", "resource_exhausted", "too many requests")):
        return "quota"
    if any(token in message for token in ("500", "502", "503", "504", "service unavailable", "temporarily unavailable")):
        return "outage"
    if _is_timeout_error(error):
        return "timeout"
    if _is_empty_provider_response(error):
        return "empty"
    if "cooling down" in message or "cooldown" in message:
        return "cooldown"
    if "missing key" in message or "no ai provider key" in message:
        return "config"
    return "provider_error"


def _failure_fingerprint(error, *, scope="", provider=""):
    normalized = _normalized_failure_text(error)
    category = (
        _provider_error_category(error)
        if scope in {"provider", "global", "cycle_budget"}
        else "article_input"
        if scope == "article_input"
        else "quality"
    )
    seed = f"{scope}|{provider}|{category}|{normalized}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20], category


def _fingerprint_backoff_seconds(scope, category, count):
    count = max(1, int(count or 1))
    if scope == "quality":
        # Output-quality failures are stochastic/editorial, not source outages.
        # Retry promptly so a verified fresh job is not cooled past its
        # publication-age window. Repeated identical failures still back off
        # exponentially (5m, 10m, 20m...) with a bounded cap.
        base = 5 * 60
        cap = 60 * 60
    elif scope == "article_input":
        # Deterministic evidence/input failures need a slower retry because the
        # underlying verified package must change before another AI call helps.
        base = 30 * 60
        cap = 6 * 3600
    elif category in {"auth", "config"}:
        base = 6 * 3600
        cap = 24 * 3600
    elif category == "quota":
        base = 45 * 60
        cap = 6 * 3600
    elif category in {"outage", "timeout"}:
        base = 10 * 60
        cap = 2 * 3600
    elif category == "empty":
        base = 5 * 60
        cap = 60 * 60
    else:
        base = 15 * 60
        cap = 2 * 3600
    return min(cap, base * (2 ** min(count - 1, 5)))


def _record_failure_fingerprint(error, *, scope, provider="", retry_until=0):
    memory = _load_ai_memory()
    fingerprint, category = _failure_fingerprint(error, scope=scope, provider=provider)
    entries = memory.setdefault("failure_fingerprints", {})
    previous = entries.get(fingerprint) if isinstance(entries.get(fingerprint), dict) else {}
    count = int(previous.get("count") or 0) + 1
    now = time.time()
    computed_until = now + _fingerprint_backoff_seconds(scope, category, count)
    retry_until = max(float(retry_until or 0), computed_until)
    entries[fingerprint] = {
        "scope": scope,
        "provider": provider,
        "category": category,
        "reason": _safe_error_reason(error),
        "count": count,
        "first_seen_at": previous.get("first_seen_at") or _now_iso(),
        "last_seen_at": _now_iso(),
        "last_seen_epoch": now,
        "retry_until": retry_until,
    }
    _save_ai_memory(memory)
    return fingerprint, category, retry_until


def _failure_fingerprint_retry_until(fingerprint):
    fingerprint = str(fingerprint or "").strip()
    if not fingerprint:
        return 0.0
    entry = (_load_ai_memory().get("failure_fingerprints") or {}).get(fingerprint)
    if not isinstance(entry, dict):
        return 0.0
    try:
        return float(entry.get("retry_until") or 0)
    except (TypeError, ValueError):
        return 0.0


def _iso_retry_until(value):
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return float(parsed.timestamp())
    except (TypeError, ValueError):
        return 0.0


def _article_ai_retry_until(article):
    article = article or {}
    return max(
        _iso_retry_until(article.get("ai_retry_after")),
        _failure_fingerprint_retry_until(article.get("ai_failure_fingerprint")),
    )


def _provider_circuit_until(provider):
    entry = _load_ai_memory().get("provider_circuits", {}).get(str(provider or "").lower())
    return _cooldown_entry_until(entry)


def _provider_circuit_remaining(provider):
    return max(0.0, _provider_circuit_until(provider) - time.time())


def _global_circuit_until():
    return _cooldown_entry_until(_load_ai_memory().get("global_circuit", {}))


def _global_circuit_remaining():
    return max(0.0, _global_circuit_until() - time.time())


def _epoch_to_iso(value):
    try:
        return datetime.fromtimestamp(float(value)).astimezone().isoformat(timespec="seconds")
    except Exception:
        return ""


def _open_provider_circuit(provider, error):
    provider = str(provider or "").strip().lower()
    if not provider:
        return {"until": 0, "fingerprint": "", "category": ""}
    memory = _load_ai_memory()
    category = _provider_error_category(error)
    seconds = _cooldown_seconds_for_error(error)
    until = time.time() + seconds
    fingerprint, category, fingerprint_until = _record_failure_fingerprint(
        error,
        scope="provider",
        provider=provider,
        retry_until=until,
    )
    until = max(until, fingerprint_until)
    memory = _load_ai_memory()
    memory.setdefault("provider_circuits", {})[provider] = {
        "until": until,
        "provider": provider,
        "category": category,
        "fingerprint": fingerprint,
        "reason": _safe_error_reason(error),
        "opened_at": _now_iso(),
    }
    _save_ai_memory(memory)
    log_event(
        "ai_provider_circuit_opened",
        provider=provider,
        category=category,
        fingerprint=fingerprint,
        retry_after=_epoch_to_iso(until),
    )
    return {"until": until, "fingerprint": fingerprint, "category": category}


def _open_global_circuit(error, providers=None):
    providers = sorted({str(value or "").strip().lower() for value in (providers or []) if str(value or "").strip()})
    memory = _load_ai_memory()
    existing = memory.get("global_circuit") if isinstance(memory.get("global_circuit"), dict) else {}
    existing_until = _cooldown_entry_until(existing)
    if existing_until > time.time():
        return {
            "until": existing_until,
            "fingerprint": str(existing.get("fingerprint") or ""),
            "category": str(existing.get("category") or ""),
        }

    provider_untils = [
        _provider_circuit_until(provider)
        for provider in providers
        if _provider_circuit_until(provider) > time.time()
    ]
    base_until = min(provider_untils) if provider_untils else time.time() + 10 * 60
    fingerprint, category, fingerprint_until = _record_failure_fingerprint(
        error,
        scope="global",
        provider=",".join(providers),
        retry_until=base_until,
    )
    # When provider circuits already exist, reopen the global circuit as soon
    # as the earliest provider is eligible again. The persisted fingerprint may
    # live longer for repeated-failure backoff, but must not unnecessarily hide
    # a provider that has recovered.
    if provider_untils:
        until = base_until
    else:
        until = max(base_until, min(fingerprint_until, time.time() + 60 * 60))
    memory = _load_ai_memory()
    memory["global_circuit"] = {
        "until": until,
        "providers": providers,
        "category": category,
        "fingerprint": fingerprint,
        "reason": _safe_error_reason(error),
        "opened_at": _now_iso(),
    }
    _save_ai_memory(memory)
    log_event(
        "ai_global_circuit_opened",
        providers=",".join(providers),
        category=category,
        fingerprint=fingerprint,
        retry_after=_epoch_to_iso(until),
    )
    return {"until": until, "fingerprint": fingerprint, "category": category}


def ai_circuit_status():
    _prune_ai_memory()
    memory = _load_ai_memory()
    global_entry = memory.get("global_circuit") if isinstance(memory.get("global_circuit"), dict) else {}
    providers = {}
    for provider, entry in (memory.get("provider_circuits") or {}).items():
        remaining = max(0.0, _cooldown_entry_until(entry) - time.time())
        if remaining > 0:
            providers[provider] = {
                "remaining_seconds": int(remaining),
                "retry_after": _epoch_to_iso(_cooldown_entry_until(entry)),
                "fingerprint": str((entry or {}).get("fingerprint") or ""),
                "category": str((entry or {}).get("category") or ""),
            }
    global_remaining = max(0.0, _cooldown_entry_until(global_entry) - time.time())
    return {
        "global_open": global_remaining > 0,
        "global_remaining_seconds": int(global_remaining),
        "global_retry_after": _epoch_to_iso(_cooldown_entry_until(global_entry)) if global_remaining > 0 else "",
        "global_fingerprint": str(global_entry.get("fingerprint") or "") if global_remaining > 0 else "",
        "global_category": str(global_entry.get("category") or "") if global_remaining > 0 else "",
        "provider_circuits": providers,
    }


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


def _is_provider_model_capacity_error(error):
    """
    Provider/model context-capacity failures are not source-evidence failures.
    The same verified article may fit the next provider/model, so rotate instead
    of putting the article itself into input backoff.
    """
    message = str(error or "").casefold()
    return any(
        token in message
        for token in (
            "413",
            "context length",
            "context_length",
            "maximum context",
            "max context",
            "input too long",
            "prompt too long",
            "too many tokens",
            "request too large",
            "payload too large",
            "token limit",
        )
    )


def _is_article_input_error(error):
    # Article/source input failures are established before provider calls by
    # _jobs_pre_ai_evidence_error(). Provider/model capacity errors must rotate.
    return isinstance(error, AIArticleInputError)


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
    changed = False

    cooldowns = memory.get("cooldowns", {})
    expired = [
        candidate_id
        for candidate_id, entry in list(cooldowns.items())
        if _cooldown_entry_until(entry) <= now
    ]
    for candidate_id in expired:
        cooldowns.pop(candidate_id, None)
        changed = True

    circuits = memory.setdefault("provider_circuits", {})
    for provider, entry in list(circuits.items()):
        if _cooldown_entry_until(entry) <= now:
            circuits.pop(provider, None)
            changed = True

    global_entry = memory.get("global_circuit")
    if isinstance(global_entry, dict) and global_entry and _cooldown_entry_until(global_entry) <= now:
        memory["global_circuit"] = {}
        changed = True

    fingerprints = memory.setdefault("failure_fingerprints", {})
    retention_cutoff = now - (14 * 24 * 3600)
    for fingerprint, entry in list(fingerprints.items()):
        if not isinstance(entry, dict):
            fingerprints.pop(fingerprint, None)
            changed = True
            continue
        if float(entry.get("last_seen_epoch") or 0) < retention_cutoff:
            fingerprints.pop(fingerprint, None)
            changed = True

    if changed:
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
    _open_provider_circuit(candidate.get("provider"), error)
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
    evidence_parts = [source_text]

    for table in (package or {}).get("source_tables") or []:
        if not isinstance(table, dict):
            continue
        for row in table.get("rows") or []:
            if isinstance(row, (list, tuple)):
                evidence_parts.extend(str(cell or "") for cell in row)

    for page in (package or {}).get("job_document_texts") or []:
        if isinstance(page, dict):
            evidence_parts.append(str(page.get("text") or ""))

    evidence_text = "\n".join(part for part in evidence_parts if str(part or "").strip())
    words = len(re.findall(r"\b\w+\b", evidence_text, flags=re.UNICODE))
    return source_text, len(evidence_text), words


def _is_rich_input_package(package):
    _source_text, source_chars, source_words = _source_stats(package)
    return source_words >= RICH_INPUT_MIN_SOURCE_WORDS or source_chars >= RICH_INPUT_MIN_SOURCE_CHARS


def _minimum_article_words_for_package(package):
    return 0


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
    return f"""
You are the dedicated Arabic job-post editor for a Moroccan jobs publication.

Create a complete, factual, professionally structured Arabic employment article for Moroccan readers
from the VERIFIED JOB PACKAGE below. The source may be a new vacancy/competition,
a candidate list, a provisional list, a result, or a final result. The reader must
understand WHAT changed, WHO it concerns, the verified requirements and duties when available,
the deadline/status, and the strongest official action or document without having to search elsewhere.

OUTPUT
- Return JSON only with exactly: title, description, slug, html_content, notice_type.
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
- Publication dates/source timestamps are INTERNAL freshness metadata. Never show "تاريخ النشر",
  "تاريخ نشر الإعلان", Date de publication, Published on, or equivalent in the article body.
- ATS IDs, competition codes, source references and internal/external reference numbers are INTERNAL identity metadata.
  Never show "المرجع", "رمز المباراة", Référence/Reference/Ref, ats_reference, or job_external_reference in the article body.
- Use official PDF text to extract only useful job facts for the reader; do not copy the PDF verbatim.

LENGTH AND STYLE
- This is a concise complete JOB ARTICLE, not a social caption, teaser, database row, or keyword-stuffed landing page.
- Prefer the shortest article that fully answers the reader's practical questions. If the verified evidence is small,
  keep the article small; do not add generic context just to create more sections.
- DO NOT target a word count. There is no preferred minimum, maximum, or SEO word-count range for Jobs articles.
- Let the verified evidence determine the length. A notice with only a few useful facts may be short.
  A university/public competition with many specialties, positions, tests, conditions, required documents,
  dates, tables, and official PDF evidence may be much longer.
- Completeness is defined primarily by verified_fact_manifest. Every high-confidence required fact must be present
  accurately; medium/heuristic facts are optional supporting context and may be omitted when ambiguous.
- Use source_tables and job_document_texts to explain explicit relationships, but do not treat every raw row,
  number, keyword match, or OCR-like fragment as a mandatory fact. Preserve distinctions only when the evidence
  clearly supports them and they do not conflict with the manifest.
- Each fact should normally appear once in the clearest place. Prefer a table for structured comparisons,
  a short list for requirements/documents, and concise prose only when prose adds clarity.
- Do not inflate a short notice with generic explanations. Do not compress a rich notice merely to keep
  the article short. Never invent, speculate, repeat, pad, or keyword-stuff.
- Preserve materially important legal/eligibility conditions when they affect who may apply or how the
  competition works; omit only genuinely irrelevant navigation, promotional, or repeated boilerplate.
- Prefer verified facts over promotional language. Never add generic praise such as
  "الشركة الرائدة", "الشركة المرموقة", "فرصة مميزة", "فرصة رائعة",
  "أحدث معايير", "حماية قصوى", "مهام حيوية", "تحديات مثيرة", or similar
  marketing claims. Prefer plain factual wording even when the source itself uses marketing copy.
- Use clear Modern Standard Arabic and short mobile-friendly paragraphs.
- No filler, generic career advice, profession explanations, corporate history,
  motivational language, clickbait, emojis, or generic conclusion.
- Do not write phrases such as "في هذا المقال سنتعرف" or "تابع القراءة".
TITLE — AI EDITORIAL DECISION
- YOU are the headline editor. Do not build the headline from a fixed template and do not merely copy or mechanically shorten the raw source title.
- First understand exactly what this page is: a new vacancy, public recruitment competition, candidate/invited list, written/oral stage, provisional list, results, final results, admission competition, registration notice, or an update to an older campaign.
- Then write ONE natural Arabic headline in the editorial style of strong Moroccan employment/competition portals: factual, immediately understandable, search-friendly, and human.
- Preserve the real intent of THIS page. Never turn a list, result, invitation, admission notice, registration notice, or update into a fresh vacancy.
- Do not force a generic 45-75 character target. Keep the title as concise as possible while preserving useful verified meaning; the accepted backend range is 28-150 characters.
- New public recruitment competitions: naturally combine the institution with "مباراة توظيف" or "مباريات توظيف" and the clearest verified role/count information.
  Good style examples:
  "جامعة سيدي محمد بن عبد الله بفاس – مباريات توظيف تقنيين من الدرجة الثالثة (30 منصبا)"
  "جامعة عبد المالك السعدي: مباراة توظيف 41 أستاذ محاضر – دورة 14 أكتوبر 2026"
  "المديرية العامة للوقاية المدنية: مباراة توظيف 04 مهندسي دولة من الدرجة الأولى و12 تقنياً من الدرجة الثالثة"
- Multi-role campaigns: prefer the verified role breakdown when it is more useful than a vague total. If the total is the clearest distinguishing fact, include it once as "N منصب/منصبا" or "(N منصبا)".
- Private-sector vacancies: use natural Arabic such as "X توظف ..." or "شركة X تعلن عن توظيف ..." for a specific role. For a verified aggregate campaign, a natural "وظائف X في المغرب: ..." headline may be better.
- Candidate/invited lists: foreground "لوائح المدعوين" and the verified written/oral stage. Keep the campaign identity and verified position count when useful.
- Results: foreground "النتائج" or "النتائج النهائية" only when that exact status is verified. Never reuse the old vacancy headline as if applications reopened.
- Education/admission notices: distinguish clearly between "مباراة ولوج", "التسجيل في", "لوائح المدعوين", and "نتائج" according to the current page.
- Put the institution/company first when it is the strongest search entity; put the event/status first when the update itself is more important.
- Preserve a well-known verified acronym such as ONCF, CNSS, ANCFCC, OFPPT or SRM once when useful.
- Include a verified year/session/date only when it genuinely distinguishes the campaign. Do not force deadlines into titles.
- Never append domains, slugs, source-site fragments, raw IDs, "الإعلان 1", tracking-like text, or long source chains such as "آخر أجل... تاريخ إجراء المباراة...".
- Avoid duplicated employer names, duplicated counts, repeated "توظيف", keyword stuffing, database-row syntax, broken Arabic/French concatenation, and clickbait.
- Never invent a number, role, institution, stage, year, location, date, or status. If verified fields conflict, use only the unambiguous facts.
EVIDENCE AND NOTICE TYPE
- verified_fact_manifest is the PRIMARY factual contract for this article.
- Facts marked confidence="high" and required_in_output=true MUST be preserved accurately.
- Facts marked confidence="medium" or "heuristic" are supporting context, not mandatory output requirements.
  Use them only when the underlying evidence is clear; never invent relationships merely to include them.
- Treat job_notice_type/job_notice_type_hint as a heuristic hint unless the manifest marks notice_type high-confidence.
- Read the remaining source title, full_article_text, source_tables and job_document_texts as supporting evidence.
  Raw table rows and regex-like patterns are NOT automatically mandatory facts. Use row/cell relationships only
  when their meaning is explicit and consistent with the manifest.
- source_tables may help explain role breakdowns, specialties, tests and counts, but do not flatten unrelated cells
  into invented relationships and do not force every arbitrary row into the article.
- job_document_texts are page-numbered text extracted from official PDFs. Use them to understand context and verify
  manifest facts. If a PDF page has no extractable text, do not guess what the image says.
- Decide the final notice_type yourself and return exactly ONE of:
  "vacancy", "competition", "candidate_list", "results", "final_results", "update".
- vacancy = a private/company employment opening or ordinary job vacancy accepting applications.
- competition = a new public recruitment competition/match accepting applications, including multi-position campaigns.
- candidate_list = invited/accepted candidate lists or written/oral-stage summons; it is not a fresh opening.
- results = published non-final/intermediate results.
- final_results = explicitly final results.
- update = a material update to an existing campaign that is not itself a fresh opening, candidate list, or results page.
- job_notice_status may help with provisional/final wording, but never call something final unless the evidence says so.
- For vacancy/competition, explain how to apply only when the verified evidence supports an active application.
- For candidate_list/results/final_results/update, make the current update the focus and never tell readers to submit
  a new application unless the current evidence explicitly reopens applications.

INTRODUCTION AND SEMANTIC DEDUPLICATION
- Blogger already renders the page title as H1. NEVER output <h1> and NEVER restate, paraphrase,
  or expand the SEO title as a heading or opening sentence inside html_content.
- The introduction must be ONE short paragraph of ONE or TWO sentences only.
- The introduction must add useful context that is not already obvious from the title and must not
  preview a list of facts that will immediately appear in the structured table.
- Treat facts semantically, not lexically: changing wording does NOT make a repeated fact new.
  Example: "آخر أجل هو 16 أكتوبر" and a table row "آخر أجل للترشيح: 16 أكتوبر" are the SAME fact.
- Give each verified fact ONE primary home in the article:
  structured facts -> table; duties -> responsibilities section; eligibility/qualifications -> requirements;
  application documents -> application-file section; tests -> tests table/list; official links -> action/document area.
- If a fact is already clear in a table, do not repeat it in the introduction or a later paragraph merely
  with different wording. Repeat a fact only when a short reference is strictly necessary to explain a
  new consequence or instruction, and do not restate its full value.
- Before returning, compare the introduction, tables, lists, and prose sections and remove semantic duplicates,
  including repeated dates, deadlines, position counts, locations, diploma/experience requirements, test details,
  application-document requirements, and status/result facts.
- Do not describe the employer as leading, prestigious, exceptional, innovative, or similar
  unless that wording is itself a necessary verified fact. Avoid recruitment-marketing filler.

ADAPTIVE ARTICLE STRUCTURE
- YOU decide the article structure AFTER understanding notice_type and the available verified evidence.
- There is NO mandatory universal sequence such as "تفاصيل الوظيفة / المهام / الشروط". Do not create a section
  just because a template normally contains it.
- Choose only the sections that help this exact reader understand this exact notice. Section names must describe
  the actual content and may vary from one article to another.
- Use prose, <ul>/<ol>, or <table> according to the data:
  * use a table when rows/columns genuinely help compare specialties, position counts, tests, durations,
coefficients, institutions, categories, or other structured evidence;
  * use a short list for requirements, application-file documents, duties, steps, or grouped conditions;
  * use concise prose for context or explanations that do not benefit from a table/list.
- Do NOT force all generic fields into one summary table. A fact should appear where it is most useful and only once.
- Keep ONE short introduction paragraph of one or two sentences. The backend inserts the branded article cover
  after the introduction; do not add an image yourself.

STRUCTURE EXAMPLES — GUIDANCE, NOT TEMPLATES
- Public recruitment competition / multi-position university or administration notice:
  a natural structure may be:
  short introduction -> specialties/grades and position counts -> tests/exams when verified ->
  eligibility/conditions -> application file/documents -> how to apply -> deadline/important notices ->
  official files/links -> rendered official PDF pages.
  Use the actual section names supported by the evidence; skip any missing part.
- Private-company vacancy:
  a natural structure may focus on:
  short introduction -> role and useful context -> verified duties -> qualifications/skills ->
  location/contract/working conditions when useful -> how to apply.
  Do not add public-competition sections that do not exist.
- Candidate/invited list:
  focus on the current list/stage, who is concerned, written/oral stage details when verified,
  official list/document links, and the verified next step. Do not rewrite the old vacancy.
- Results/final results:
  focus on the result status, the competition/campaign concerned, useful result details,
  official result files/links, and any verified next step. Do not add application instructions unless applications
  are explicitly reopened.
- Update:
  make the changed fact itself the center of the article; include old campaign details only when needed to understand
  the update.

FACT PLACEMENT
- Preserve every useful verified fact, but let its meaning determine its place.
- A verified deadline must be clearly visible once, but it does NOT have to be inside a generic "تفاصيل الوظيفة" table.
- Position/specialty breakdowns from source_tables should remain structured when structure helps comprehension.
- Tests, durations and coefficients should stay together when they belong together.
- Application-file requirements should stay together and must not be scattered across unrelated sections.
- Duties belong in a duties/role section only when duties actually exist.
- Requirements belong together only when verified requirements actually exist.
- For candidate lists/results/updates, use wording and sections matching that status rather than employment-opening headings.

APPLICATION, RESULTS AND OFFICIAL FILES
- For an active vacancy/competition, the strongest verified application resource is essential.
- Private/company vacancies remain strict: use a job-specific direct Apply/Postuler/Candidature URL,
  an official application form tied to this exact vacancy, or the specific official job-detail page.
  NEVER substitute a generic careers/jobs/search/listing page.
- Public recruitment competitions may use a central official application platform that is not job-specific.
  This is allowed ONLY when the verified package explicitly classifies job_application_link_kind as
  "official_application_channel". In that case the specific notice/detail page identifies the campaign,
  while job_application_url is the verified official channel used to submit the application.
- If job_application_url exists, include it exactly once at the point in the article where application makes sense.
  The backend only upgrades that exact link visually in place; it must not decide the article section/order for you.
- If job_application_link_kind is "direct_apply", make its visible label clearly mean direct application.
- If job_application_link_kind is "official_application_channel", label it as "منصة الترشيح الرسمية"
  or equivalent wording. NEVER call it "التقديم المباشر" or imply that the generic portal URL identifies
  this exact vacancy by itself.
- For candidate_list/results/final_results, do NOT call a list/result link "التقديم" unless applications are truly open.
- If job_detail_url is a specific useful official detail page and differs from the application resource, include it once.
- EVERY useful URL in job_document_links must remain in the final article exactly once.
- When multiple official files are naturally comparable, a compact table may be useful; otherwise use concise descriptive links.
- Use verified link label/context to distinguish files, but never invent a diploma/specialty/category from a URL or vague context.
- Preserve URLs EXACTLY. Never shorten, rewrite, fabricate, or duplicate a URL.
- External links must use target="_blank" rel="nofollow noreferrer noopener".

IMAGES
- DO NOT add <img>, <picture>, <figure>, image URLs, logos, captions, or source images.
- The application generates ONE branded article cover separately.
- When a verified official PDF contains vacancy conditions/notice details, the backend
  may render its pages as sequential article images after validation. Do not generate
  or imitate those images yourself.
- Never use og:image or any source-page hero/content image.

SEO
- Meta description: neutral third-person Arabic, approximately 110-160 characters.
- Mention the employer, translated role, location when verified, and one useful verified
  fact such as contract/deadline/direct application. Do not write as if this site were the
  employer: never use phrases such as "نبحث عن", "عملائنا", "فريقنا", "انضم إلينا/لفريقنا".
- The JSON slug MUST be a concise ENGLISH SEO slug, not Arabic transliteration.
- Translate generic role/category/location wording into natural English; keep brand/proper names in Latin when useful.
- Use lowercase a-z letters and hyphens ONLY. NEVER use digits, underscores, dates, years, seat counts, salaries, deadlines, references, IDs, or random numeric suffixes.
- Use 3-7 descriptive words. Example: orange-business-cybersecurity-consultant-casablanca.
- Do NOT copy desired_slug when it is transliterated Arabic; desired_slug is only an identity hint.

PROFESSIONAL HTML ARTICLE BODY
- html_content is the BODY of the Blogger post only. The Blogger theme already renders the H1/title.
  NEVER add <h1>, the SEO title, meta description, JSON-LD, schema, scripts, CSS, style blocks,
  widgets, iframes, forms, tracking code, buttons made with JavaScript, or hidden text.
- Output clean semantic HTML only. No Markdown and no plain-text section labels outside HTML tags.
- Use ONLY ordinary content tags when needed:
  <p>, <h2>, <h3>, <strong>, <ul>, <ol>, <li>,
  <table>, <tbody>, <tr>, <th>, <td>, <a>.
- Do not use inline style= attributes. Do not use decorative <div> wrappers.
- Paragraphs must be short and readable on mobile: normally 1-3 sentences each.
- Never create a section merely to make the article longer. Omit sections whose facts are unavailable.

AI STRUCTURE AUTHORITY
- The AI editorial decision controls headings, section order, tables, lists, and prose based on the evidence.
- Do not force a "تفاصيل الوظيفة" section or any fixed sequence.
- Do not manufacture "المهام والمسؤوليات" or "الشروط والمؤهلات" when those facts do not exist.
- Do not reuse private-vacancy headings for a public competition, candidate list, results page, or update.
- Prefer descriptive headings specific to the evidence, for example:
  "التخصصات وعدد المناصب", "الاختبارات", "شروط الترشيح", "ملف الترشيح",
  "طريقة التقديم", "آخر أجل للترشيح", "لوائح المدعوين", "النتائج النهائية".
  These are examples only; choose them only when supported and useful.
- If the notice is so small that one or two short sections are clearer, keep it small. Do not create headings only to satisfy a template.
- The application/document link position chosen in html_content is intentional. The backend may style that link,
  but must preserve its editorial location.

LINK RULES
- Every verified official application/document/detail URL supplied in the package must remain present exactly once.
- External links must use target="_blank" rel="nofollow noreferrer noopener".
- Jobs articles must NOT contain automatic/internal promotional links to other site articles, category hubs, or label pages.
- Do NOT create "قد يهمك", "مقالات ذات صلة", "مواضيع ذات صلة", pRelate, related-posts, recommended-posts, or similar blocks.
- Do NOT turn ordinary words such as "وظائف", "التوظيف", "الوظيفة", "الترشيح", or "العمل" into internal links.
- related_posts is intentionally empty in Jobs mode. Do not invent internal recommendations.
- Never fabricate, shorten, redirect, or duplicate a URL.

SEO / ADSENSE-FRIENDLY EDITORIAL QUALITY
- Write for the user first: clear facts, useful structure, no keyword stuffing and no repeated employer/role phrases.
- The first paragraph must add useful context without mechanically repeating employer/role/location already obvious from the title.
- Headings must describe real sections, not repeat the title.
- Do not add generic conclusions, motivational text, career advice, corporate history, promotional filler,
  "فرصة لا تعوض", "انضم لفريقنا", or calls to click ads.
- No copied boilerplate solely to increase word count. A shorter complete factual article is better than filler.
- Avoid repeated paragraphs, repeated headings, repeated facts, and repeated URLs.

FINAL SILENT CHECK
Before returning JSON, verify:
- The article is complete against verified_fact_manifest: every high-confidence required fact is present once and accurate.
- Medium/heuristic manifest facts may be omitted when their meaning is uncertain; never invent text to satisfy them.
- Every factual claim must remain grounded in the manifest or clearly supported source evidence.
- Dates, position counts, salary and experience must never contradict high-confidence manifest values.
- Do not duplicate or silently merge explicit specialty/position/test relationships.
- No unsupported external URL is present.
- No duplicated paragraph, structured row, fact, or URL.
- If a verified deadline exists, its value is clearly present once; do not force a particular heading/label.
- Active vacancy/competition: strongest verified application link included and it belongs to this exact campaign.
- Candidate list/result: status is clear and it is NOT falsely presented as a new vacancy.
- Every useful verified official PDF/list link is preserved exactly once.
- Official document links are organized in the clearest structure for this notice; do not force a table when it is not useful.
- No image tag or image URL inside html_content; the backend owns the cover/PDF-page images.
- No fake salary, deadline, vacancies, diploma, requirement, list status, or result.
- No generic boilerplate, promotional filler, or template sentences that could fit any job notice.
- slug is natural English, lowercase letters/hyphens only, with no digits or Arabic transliteration.

OUTPUT JSON SHAPE:
{{
  "title": "Arabic SEO title",
  "description": "Arabic meta description",
  "slug": "english-company-role-location",
  "html_content": "clean semantic HTML",
  "notice_type": "vacancy | competition | candidate_list | results | final_results | update"
}}

VERIFIED JOB PACKAGE:
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


def _ensure_verified_position_count(data, package=None):
    """Deterministically preserve a verified required position count."""
    package = dict(package or {})
    manifest = package.get("verified_fact_manifest") or {}
    position_facts = (manifest.get("facts") or {}).get("positions") or []
    required = [
        fact for fact in position_facts
        if isinstance(fact, dict)
        and fact.get("required_in_output")
        and str(fact.get("confidence") or "").strip().lower() == "high"
    ]
    if not required:
        return data

    title = str((data or {}).get("title") or "")
    html_content = str((data or {}).get("html_content") or "")
    combined = BeautifulSoup(f"{title}\n{html_content}", "html.parser").get_text(" ", strip=True)

    missing = []
    for fact in required:
        try:
            number = int(fact.get("value") or 0)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        if re.search(
            rf"\b{number}\s*(?:منصب|مناصب|منصبا|poste|postes|position|positions)\b",
            combined,
            flags=re.I,
        ):
            continue
        missing.append(number)

    if not missing:
        return data

    soup = BeautifulSoup(html_content, "html.parser")
    insertion = soup.new_tag("p")
    insertion["class"] = ["jobVerifiedFact"]
    strong = soup.new_tag("strong")
    strong.string = "عدد المناصب:"
    insertion.append(strong)
    insertion.append(" " + "، ".join(f"{number} منصب" for number in missing))

    first_heading = soup.find(["h2", "h3"])
    if first_heading is not None:
        first_heading.insert_before(insertion)
    else:
        soup.append(insertion)

    data["html_content"] = str(soup)
    log_event(
        "ai_verified_position_count_injected",
        positions=",".join(str(number) for number in missing),
    )
    return data


def _validate_ai_output(data, package=None):
    required_fields = (JOBS_REQUIRED_ARTICLE_FIELDS)
    missing = [field for field in required_fields if not str(data.get(field, "")).strip()]
    if missing:
        raise AIIncompleteResponseError("Missing AI output field(s): " + ", ".join(missing))

    title = str(data["title"]).strip()
    description = str(data["description"]).strip()
    html_content = str(data["html_content"]).strip()

    notice_type = str(data.get("notice_type") or "").strip().lower()
    if notice_type not in ALLOWED_JOB_NOTICE_TYPES:
        raise AIIncompleteResponseError(
            "Jobs notice_type must be one of: " + ", ".join(sorted(ALLOWED_JOB_NOTICE_TYPES))
        )
    data["notice_type"] = notice_type
    slug = _normalize_job_english_slug(data.get("slug", ""))
    if not re.fullmatch(r"[a-z]+(?:-[a-z]+){1,6}", slug or ""):
        raise AIIncompleteResponseError(
            "Jobs slug must be 2-7 English words using lowercase letters and hyphens only; digits are forbidden"
        )
    data["slug"] = slug
    title_ok = 28 <= len(title) <= 150
    description_ok = 70 <= len(description) <= 190
    title_range, description_range = "28-150", "70-190"

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
    return result


def _build_expansion_retry_prompt(package, previous_data, previous_error):
    previous_html = ""
    if isinstance(previous_data, dict):
        previous_html = str(previous_data.get("html_content") or "")
    source_text = _source_text_for_package(package)
    return f"""
Return JSON only with title, description, slug, html_content, notice_type.

The previous compact job listing failed this quality rule:
{previous_error}

Rewrite ONLY as a complete, professional, verified job article.

MANDATORY JOB RETRY RULES:
- Do NOT aim for any word count and do NOT shorten or expand merely to hit a range.
- Let the verified evidence determine the final length: short when facts are few, longer when the notice is rich.
- Preserve every high-confidence required manifest fact and every clearly supported material instruction.
- Medium/heuristic facts are repair hints only. Do not force arbitrary source-table rows or ambiguous PDF fragments
  into the article merely because they contain numbers or recruitment keywords.
- Never invent, repeat, speculate, pad, or add boilerplate.
- Treat the Quality Gate failure reason above as a concrete repair instruction: correct that failure while preserving
  all other verified facts and links that were already correct.
- Reconcile every sensitive fact against verified_fact_manifest before returning.
  High-confidence facts are authoritative. Medium/heuristic facts are repair hints, not mandatory claims.
- Use source_tables/job_document_texts only to clarify relationships explicitly supported by the manifest/evidence.
  Do not preserve arbitrary rows merely because they contain numbers or keywords.
- Re-evaluate ALL evidence, including source_tables and job_document_texts, and return the correct notice_type:
  vacancy, competition, candidate_list, results, final_results, or update. The incoming job_notice_type is only a hint.
- Re-edit the title as a human Moroccan employment/competition editor: understand the current page type and stage first, then choose the clearest natural headline.
- Do not force a fixed formula or a 45-75-character target; preserve useful verified meaning within the accepted 28-150 range.
- A new public recruitment notice should read naturally as "مباراة توظيف/مباريات توظيف" when appropriate; a private role may use "توظف/تعلن عن توظيف"; candidate lists and results MUST foreground their verified stage and must never be rewritten as a fresh vacancy.
- Keep useful verified institution + role/count/stage information once, and remove raw source fragments, duplicated employer/role/count wording, IDs, domains, and deadline/date chains.
- Before returning, silently verify that the title accurately describes THIS page, is not mechanically copied from the source, and contains no invented fact.
- Blogger already displays the H1 title: do not output <h1> and do not repeat/paraphrase the title in the body.
- Keep one short introduction paragraph of one or two sentences that adds information instead of previewing the table.
- Perform a semantic deduplication pass before returning: the same fact must not appear in intro/table/sections
  merely with different wording. Keep each date, deadline, count, requirement, test detail, document requirement,
  location/status fact, and other structured value in its clearest single location unless a brief reference is
  strictly necessary to explain a new instruction.
- Re-plan the article structure from the evidence and notice_type; do not reuse a fixed Jobs template.
- Choose headings, tables, lists, and order according to the actual notice. A public competition, private vacancy,
  candidate list, results page, final results, and update should not share the same compulsory section sequence.
- Public competition data may naturally use sections for specialties/counts, tests, eligibility, application file,
  application method, deadline and official documents when those facts exist.
- A private vacancy may instead focus on role, duties, qualifications, location/contract and application.
- Lists/results/updates must focus on their current stage/status and verified next action.
- Omit any section with no verified facts; do not create "تفاصيل الوظيفة", "المهام", or "الشروط" merely because a template expects them.
- Include job_application_url exactly once when present.
- If job_application_link_kind is "official_application_channel", present that URL only as the official
  application/registration platform ("منصة الترشيح الرسمية"), never as a direct vacancy link.
- If job_detail_url differs from job_application_url, preserve the specific official notice/detail URL once as well.
- Include EVERY useful job_document_links URL exactly once when present.
- Do not add any internal related-post/category-hub link, pRelate block, "قد يهمك", "مقالات ذات صلة",
  or automatic link on ordinary words such as "وظائف", "التوظيف", "الوظيفة", "الترشيح", or "العمل".
- No <h1>, images, captions, scripts, JSON-LD, CSS/style attributes, iframes, forms,
  corporate history, generic career advice, filler, conclusion, or repeated facts.
- Do not invent any fact or URL.
- Return a natural English slug using lowercase a-z and hyphens only; no digits, IDs, years, or Arabic transliteration.
- Meta description 100-160 characters.
- Clean semantic Blogger HTML only.

SOURCE PACKAGE:
{json.dumps(package, ensure_ascii=False, indent=2)}

SOURCE TEXT:
{source_text}

PREVIOUS HTML FOR DIAGNOSIS ONLY:
{previous_html[:3500]}
""".strip()


def _build_excess_english_retry_prompt(package, previous_data, previous_error):
    previous_html = ""
    if isinstance(previous_data, dict):
        previous_html = str(previous_data.get("html_content") or "")
    source_text = _source_text_for_package(package)
    return f"""
Return JSON only with title, description, slug, html_content, notice_type.

The previous Jobs response failed because the article body contained too much English:
{previous_error}

Rewrite the SAME verified notice in natural Modern Standard Arabic.
- Re-read source_tables and job_document_texts as evidence.
- Decide notice_type again from the evidence: vacancy, competition, candidate_list, results, final_results, or update.
- Preserve necessary company names, acronyms, official role names, products, certifications, and technical terms in their original language only when useful.
- Preserve every verified fact, number, date, official link, role breakdown, and current notice stage.
- Do not invent, omit, or turn a list/result/update into a fresh opening.
- Return complete semantic HTML with no truncated tags.

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


_JOB_SLUG_ARABIC_PHRASES = (
    ("أستاذ محاضر", "lecturer"),
    ("أستاذ التعليم العالي", "professor"),
    ("أستاذ", "professor"),
    ("مهندس دولة", "engineer"),
    ("مهندس", "engineer"),
    ("تقني متخصص", "specialist technician"),
    ("تقني", "technician"),
    ("متصرف", "administrator"),
    ("مستشار", "consultant"),
    ("مدير", "manager"),
    ("محاسب", "accountant"),
    ("مطور", "developer"),
    ("الأمن السيبراني", "cybersecurity"),
    ("أمن سيبراني", "cybersecurity"),
    ("المعلوميات", "it"),
    ("الإعلاميات", "it"),
    ("شبكات", "network"),
    ("الموارد البشرية", "human resources"),
    ("موارد بشرية", "human resources"),
    ("التسويق", "marketing"),
    ("المبيعات", "sales"),
    ("جامعة", "university"),
    ("وزارة", "ministry"),
    ("وكالة", "agency"),
    ("المكتب", "office"),
    ("مكتب", "office"),
    ("المعهد", "institute"),
    ("معهد", "institute"),
    ("كلية", "faculty"),
)

_JOB_SLUG_LATIN_MAP = {
    "ingenieur": "engineer",
    "ingenieurs": "engineer",
    "technicien": "technician",
    "techniciens": "technician",
    "responsable": "manager",
    "developpeur": "developer",
    "developpeurs": "developer",
    "comptable": "accountant",
    "consultante": "consultant",
    "stagiaire": "intern",
    "stage": "internship",
    "securite": "security",
    "informatique": "it",
    "reseaux": "network",
    "reseau": "network",
    "emploi": "job",
    "recrutement": "recruitment",
}

_JOB_SLUG_PLACES = {
    "الدار البيضاء": "casablanca",
    "الرباط": "rabat",
    "مراكش": "marrakech",
    "فاس": "fes",
    "طنجة": "tangier",
    "وجدة": "oujda",
    "أكادير": "agadir",
    "اكادير": "agadir",
    "القنيطرة": "kenitra",
    "الجديدة": "el jadida",
    "تطوان": "tetouan",
    "مكناس": "meknes",
    "سلا": "sale",
}


def _job_english_words(value):
    text = str(value or "").strip().casefold()
    if not text:
        return []
    for source, target in _JOB_SLUG_PLACES.items():
        text = text.replace(source.casefold(), " " + target + " ")
    for source, target in _JOB_SLUG_ARABIC_PHRASES:
        text = text.replace(source.casefold(), " " + target + " ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    words = re.findall(r"[a-z]+", text)
    mapped = [_JOB_SLUG_LATIN_MAP.get(word, word) for word in words]
    stop = {
        "a", "an", "and", "at", "de", "des", "du", "en", "et", "for", "la",
        "le", "les", "of", "the", "to", "with", "job", "jobs", "recruitment",
    }
    return [word for word in mapped if len(word) > 1 and word not in stop]


def _normalize_job_english_slug(slug, max_words=7):
    cleaned = str(slug or "").strip().casefold()
    cleaned = unicodedata.normalize("NFKD", cleaned)
    cleaned = "".join(ch for ch in cleaned if not unicodedata.combining(ch))
    cleaned = cleaned.encode("ascii", "ignore").decode("ascii").lower()
    words = re.findall(r"[a-z]+", cleaned)
    words = [_JOB_SLUG_LATIN_MAP.get(word, word) for word in words]
    words = [word for word in words if word]
    if len(words) > max_words:
        words = words[:max_words]
    if len(words) == 1:
        words.append("job")
    return "-".join(words)[:80].strip("-")


def _fallback_job_english_slug(package):
    company_words = _job_english_words(package.get("job_company") or package.get("company"))[:2]
    role_words = _job_english_words(package.get("job_title") or package.get("title"))[:3]
    location_words = _job_english_words(package.get("job_location") or package.get("location"))[:2]
    words = []
    for word in company_words + role_words + location_words:
        if word not in words:
            words.append(word)
    if not words:
        words = ["employment", "opportunity"]
    elif len(words) == 1:
        words.append("job")
    return "-".join(words[:7])[:80].strip("-")


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

    if len(description) > 190:
        data["description"] = _trim_to_length(description, 180)
    return data


def _normalize_ai_output(data):
    data["title"] = str(data.get("title", "")).strip()
    data["description"] = str(data.get("description", "")).strip()
    data["slug"] = (
        (_normalize_job_english_slug(data.get("slug", ""), max_words=7))
    )
    data["html_content"] = str(data.get("html_content", "")).strip()
    return data


def _normal_paragraphs(soup):
    return [
        paragraph
        for paragraph in soup.find_all("p")
        if not paragraph.find_parent(["blockquote", "figcaption", "pre", "code"])
        and "pRef" not in (paragraph.get("class") or [])
        and "note" not in (paragraph.get("class") or [])
    ]


def _plus_ui_format_html(html_content, package):
    """Format HTML for Blogger publication with image insertion."""
    soup = BeautifulSoup(html_content or "", "html.parser")

    for section in soup.select("section.jobOfficialDocuments"):
        section.decompose()

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
        image_html = (
            "<figure>\n"
            f"  <img alt='{escape(title, quote=True)}'{size_attrs} "
            f"src='{escape(main_image, quote=True)}'/>\n"
            "</figure>"
        )
        paragraphs[0].insert_after(BeautifulSoup(image_html, "html.parser"))

    # Jobs must contain one cover only; source-page extra images are forbidden.
    extra_images = ([])
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
    return html_content


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
    package = package or {}
    formatted = _plus_ui_format_html(html_content, package)
    formatted = _remove_empty_job_fact_rows(formatted)
    formatted = _remove_internal_job_metadata(formatted)
    # PDF pages are rendered/persisted by the Blogger publisher after AI.
    # Attach those verified page images here, in document/page order, so the
    # final Blogger body always contains the visual copy of the official PDF.
    formatted = _append_job_document_page_images(formatted, package)
    return formatted


_INTERNAL_JOB_ROW_LABEL_RE = re.compile(
    r"^(?:"
    r"تاريخ\s+النشر|تاريخ\s+نشر\s+(?:الإعلان|الوظيفة)|"
    r"date\s+de\s+publication|publication\s+date|published\s+on|"
    r"المرجع|الرقم\s+المرجعي|رمز\s+المباراة|"
    r"r[eé]f(?:[ée]rence)?\.?|reference"
    r")\s*[:：-]?",
    flags=re.I,
)


def _remove_internal_job_metadata(html_content):
    """Keep freshness/identity metadata internal while preserving useful job facts."""
    soup = BeautifulSoup(html_content or "", "html.parser")
    changed = False

    for row in list(soup.find_all("tr")):
        cells = row.find_all(["th", "td"])
        if not cells:
            continue
        label = re.sub(r"\s+", " ", cells[0].get_text(" ", strip=True)).strip()
        if _INTERNAL_JOB_ROW_LABEL_RE.search(label):
            row.decompose()
            changed = True

    for tag in list(soup.find_all(["li", "p"])):
        text = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        if text and len(text) <= 320 and _INTERNAL_JOB_ROW_LABEL_RE.search(text):
            tag.decompose()
            changed = True

    for heading in list(soup.find_all(["h2", "h3"])):
        text = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip()
        if text and _INTERNAL_JOB_ROW_LABEL_RE.fullmatch(text.rstrip(":：- ")):
            heading.decompose()
            changed = True

    return str(soup) if changed else html_content


def _remove_empty_job_fact_rows(html_content):
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


def _job_link_key(url):
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    tracking = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "fbclid", "gclid", "mc_cid", "mc_eid",
    }
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in tracking
    ]
    return urlunparse(
        parsed._replace(
            scheme=parsed.scheme.casefold(),
            netloc=parsed.netloc.casefold(),
            query=urlencode(query, doseq=True),
            fragment="",
        )
    ).rstrip("/")


def _job_action_box(label, url, *, kind="apply"):
    if kind == "application_channel":
        heading = "منصة الترشيح الرسمية"
        button = "فتح منصة الترشيح الرسمية"
        box_class = "dlBox jobApplyBox jobApplicationChannelBox"
        button_class = "button extL jobApplyButton jobApplicationChannelButton"
    elif kind == "apply":
        heading = label or "التقديم الرسمي"
        button = "فتح رابط التقديم الرسمي"
        box_class = "dlBox jobApplyBox"
        button_class = "button extL jobApplyButton"
    else:
        heading = label or "الوثيقة الرسمية"
        button = "فتح أو تحميل الوثيقة الرسمية"
        box_class = "dlBox jobDocumentBox"
        button_class = "button extL jobDocumentButton"
    return (
        f"<div class='{box_class}'>"
        f"<p><strong>{escape(heading)}</strong></p>"
        f"<p><a class='{button_class}' href='{escape(url, quote=True)}' "
        "target='_blank' rel='nofollow noreferrer noopener' role='button'>"
        f"{escape(button)}</a></p>"
        "</div>"
    )


def _append_job_action_links_if_missing(html_content, package):
    """Style verified Jobs links in place without changing AI-chosen article structure."""

    application_url = str(package.get("job_application_url") or "").strip()
    application_kind = str(package.get("job_application_link_kind") or "").strip()
    detail_url = str(package.get("job_detail_url") or "").strip()

    specs = {}
    if application_url:
        key = _job_link_key(application_url)
        if key:
            if application_kind == "official_application_channel":
                specs[key] = {
                    "kind": "application_channel",
                    "label": "منصة الترشيح الرسمية",
                }
            else:
                specs[key] = {
                    "kind": "apply",
                    "label": "التقديم المباشر" if application_kind == "direct_apply" else "التقديم الرسمي",
                }

    if detail_url and detail_url != application_url:
        key = _job_link_key(detail_url)
        if key:
            specs[key] = {"kind": "detail", "label": "صفحة الإعلان الرسمية"}

    for index, item in enumerate(package.get("job_document_links") or [], start=1):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        key = _job_link_key(url)
        if not key or key in specs:
            continue
        label = re.sub(
            r"\s+",
            " ",
            str(item.get("label") or item.get("context") or f"الملف الرسمي {index}"),
        ).strip()
        specs[key] = {"kind": "document", "label": label[:180]}

    if not specs:
        return html_content

    soup = BeautifulSoup(html_content or "", "html.parser")
    styled = set()

    for anchor in soup.find_all("a", href=True):
        key = _job_link_key(anchor.get("href"))
        spec = specs.get(key)
        if not spec or key in styled:
            continue

        kind = spec["kind"]
        label = spec["label"]
        anchor["target"] = "_blank"
        anchor["rel"] = ["nofollow", "noreferrer", "noopener"]

        if kind == "detail":
            classes = list(anchor.get("class") or [])
            for value in ("extL", "jobOfficialDetailLink"):
                if value not in classes:
                    classes.append(value)
            anchor["class"] = classes
            styled.add(key)
            continue

        classes = list(anchor.get("class") or [])
        required_classes = (
            ("button", "extL", "jobApplyButton", "jobApplicationChannelButton")
            if kind == "application_channel"
            else ("button", "extL", "jobApplyButton")
            if kind == "apply"
            else ("button", "extL", "jobDocumentButton")
        )
        for value in required_classes:
            if value not in classes:
                classes.append(value)
        anchor["class"] = classes
        anchor["role"] = "button"
        if kind == "application_channel":
            anchor.clear()
            anchor.append("فتح منصة الترشيح الرسمية")

        # When AI put the link in a simple standalone paragraph, upgrade that exact
        # paragraph to the existing download-style box at the same editorial location.
        parent = anchor.parent
        if (
            parent is not None
            and parent.name == "p"
            and len(parent.find_all("a", recursive=False)) == 1
            and parent.get_text(" ", strip=True) == anchor.get_text(" ", strip=True)
        ):
            fragment = BeautifulSoup(
                _job_action_box(label, anchor.get("href"), kind=kind),
                "html.parser",
            )
            box = fragment.find("div")
            if box is not None:
                parent.replace_with(box)
        styled.add(key)

    # Missing verified links are intentionally NOT appended elsewhere. The quality
    # gate rejects missing URLs so AI must place them in the correct editorial section.
    return str(soup)


def _append_job_document_page_images(html_content, package):
    """Append sequential images rendered from verified official PDF pages."""
    pages = [
        row
        for row in (package.get("job_document_page_images") or [])
        if isinstance(row, dict) and row.get("url")
    ]
    if not pages:
        return html_content

    soup = BeautifulSoup(html_content or "", "html.parser")
    # Replace our complete section when a later rendering batch adds pages.
    # Repeated formatting must not accumulate duplicate headings or reorder a
    # partially rendered document after the following document.
    for section in soup.select("section.jobOfficialDocuments"):
        section.decompose()
    existing_sources = {
        str(img.get("src") or "").strip()
        for img in soup.find_all("img", src=True)
    }
    grouped = []
    by_document = {}
    for row in pages:
        src = str(row.get("url") or "").strip()
        if not src or src in existing_sources:
            continue
        doc_url = str(row.get("document_url") or "").strip()
        if doc_url not in by_document:
            by_document[doc_url] = []
            grouped.append((doc_url, by_document[doc_url]))
        by_document[doc_url].append(row)

    if not grouped:
        return html_content

    blocks = [
        "<section class='jobOfficialDocuments'>",
        "<h2>صفحات الوثيقة الرسمية</h2>",
    ]
    for _doc_url, rows in grouped:
        rows = sorted(rows, key=lambda row: int(row.get("page_number") or 0))
        label = str(rows[0].get("document_label") or "الوثيقة الرسمية").strip()
        blocks.append(f"<h3>{escape(label)}</h3>")
        for row in rows:
            src = str(row.get("url") or "").strip()
            alt = str(row.get("alt") or label).strip()
            page_number = int(row.get("page_number") or 0)
            blocks.append(
                "<figure class='jobDocPage'>"
                f"<img class='jobDocPageImage' src='{escape(src, quote=True)}' "
                f"alt='{escape(alt, quote=True)}' loading='lazy' decoding='async'/>"
                f"<figcaption>الصفحة {page_number}</figcaption>"
                "</figure>"
            )
    blocks.append("</section>")
    return str(soup).rstrip() + "\n" + "\n".join(blocks)

def _finalize_html_content(data, package):
    """Finalize HTML content for publication."""
    html_content = data["html_content"]
    html_content = _sanitize_source_links(html_content, package)
    html_content = _plus_ui_format_html(html_content, package)
    html_content = _remove_empty_job_fact_rows(html_content)
    html_content = _remove_internal_job_metadata(html_content)
    html_content = _append_job_action_links_if_missing(html_content, package)
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
        return []
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
    if _global_circuit_remaining() > 0:
        status = ai_circuit_status()
        raise AIProviderRotationExhausted(
            f"Global AI circuit open until {status.get('global_retry_after') or 'later'}"
        )
    candidates = [
        candidate for candidate in _provider_candidates(context=context)
        if candidate["provider"] not in skip_providers
        and (not JOBS_MODE or _provider_circuit_remaining(candidate["provider"]) <= 0)
    ]
    if not candidates:
        providers = _resolve_providers()
        _open_global_circuit(
            AIProviderRotationExhausted("all configured AI providers are unavailable or cooling down"),
            providers=providers,
        )
        raise AIProviderRotationExhausted(
            "All configured AI providers are unavailable or cooling down."
        )
    unique_candidates = []
    seen_providers = set()
    for candidate in candidates:
        provider_name = str(candidate.get("provider") or "")
        if provider_name in seen_providers:
            continue
        seen_providers.add(provider_name)
        unique_candidates.append(candidate)
    candidates = unique_candidates

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
    if _global_circuit_remaining() > 0:
        return []

    if (AI_PROVIDER or "").strip().lower() == "auto":
        # Auto mode has a fixed safety order. Provider circuits filter known
        # quota/outage failures before any new request is attempted.
        sequence = [
            provider
            for provider in ("gemini", "groq", "openrouter", "cloudflare", "mistral", "openai")
            if provider in providers
            and (not JOBS_MODE or _provider_circuit_remaining(provider) <= 0)
        ]
        if providers and (not sequence):
            _open_global_circuit(
                AIProviderRotationExhausted("all configured AI providers are cooling down"),
                providers=providers,
            )
        return sequence or (([]))

    sequence = [
        provider for provider in providers
        if _provider_circuit_remaining(provider) <= 0
    ]
    if providers and not sequence:
        _open_global_circuit(
            AIProviderRotationExhausted("configured AI provider is cooling down"),
            providers=providers,
        )
    return sequence


def _attempt_provider_candidates():
    return _provider_candidates()


def _openrouter_fallback_available():
    if _global_circuit_remaining() > 0 or _provider_circuit_remaining('openrouter') > 0:
        return False
    return bool(_openrouter_candidates())


def _should_switch_gemini_to_openrouter(provider, error):
    return provider == "gemini" and _is_quota_or_rate_limit_error(error) and _openrouter_fallback_available()


def _generate_with_provider_name(provider, prompt, context=None):
    if _global_circuit_remaining() > 0:
        raise AIProviderRotationExhausted("global AI circuit is open")
    if _provider_circuit_remaining(provider) > 0:
        raise AIProviderFallbackNeeded(f"{provider} provider circuit is open")
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
                if _is_provider_model_capacity_error(error):
                    _put_candidate_on_cooldown(candidate, error)
                    log_event(
                        "ai_provider_model_capacity_rejected",
                        article_id=getattr(context, "article_id", ""),
                        provider=provider,
                        model=candidate.get("model"),
                        reason=_safe_error_reason(error),
                    )
                    raise AIProviderFallbackNeeded(
                        f"{provider} model capacity failed: {_safe_error_reason(error)}"
                    ) from error
                if _is_article_input_error(error):
                    raise AIArticleInputError(_safe_error_reason(error)) from error
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
                raise AIProviderFallbackNeeded(
                    f"{provider} provider failed: {_safe_error_reason(error)}"
                ) from error
    raise last_error or RuntimeError(f"No {provider} AI candidate returned a response.")


def _jobs_pre_ai_evidence_error(package):
    package = dict(package or {})
    source_url = str(package.get("url") or package.get("source_url") or "").strip()
    if not source_url:
        return "source/evidence problem: missing verified job source URL before AI"

    notice_type = str(package.get("job_notice_type") or "").strip().lower()
    notice_type_source = str(package.get("job_notice_type_source") or "").strip().lower()
    application_url = str(package.get("job_application_url") or "").strip()
    # A heuristic notice type may be corrected by the AI (for example a result
    # notice initially looking like a vacancy). Do not pre-block that case.
    if (
        notice_type in {"vacancy", "competition"}
        and not application_url
        and notice_type_source in {"official", "verified", "structured", "ats", "source"}
    ):
        return "source/evidence problem: active job notice is missing a verified application resource"

    if application_url:
        if not is_application_url_bound_to_job(package, application_url):
            if not is_job_specific_url(application_url):
                return (
                    "source/evidence problem: generic application portal is not "
                    "a verified official channel for this competition"
                )
            return "source/evidence problem: job application URL belongs to a different vacancy"
        if (
            not is_job_specific_url(application_url)
            and str(package.get("job_application_link_kind") or "").strip().lower()
            != "official_application_channel"
        ):
            return (
                "source/evidence problem: generic application portal must be "
                "classified as official_application_channel"
            )

    evidence_stage = str(package.get("identity_evidence_stage_status") or "").strip().lower()
    try:
        pdf_failures = int(package.get("job_document_text_download_failures") or 0)
    except (TypeError, ValueError):
        pdf_failures = 0
    if evidence_stage == "incomplete" and pdf_failures > 0:
        return "source/evidence problem: official document evidence could not be retrieved"
    if evidence_stage == "incomplete":
        return "source/evidence problem: verified identity evidence stage is incomplete"

    return ""


def _is_nonrepairable_jobs_evidence_quality_error(error, package=None):
    reason = str(error or "").casefold()
    package = dict(package or {})
    original_notice_type = str(package.get("job_notice_type") or "").strip().lower()
    original_notice_source = str(package.get("job_notice_type_source") or "").strip().lower()

    if "missing job source url" in reason:
        return True
    if "generic application portal is not a verified official channel" in reason:
        return True
    if "job application url belongs to a different vacancy" in reason:
        return True
    if "generic application portal must be classified as official_application_channel" in reason:
        return True
    if (
        "active job notice is missing a verified application resource" in reason
        and original_notice_type in {"vacancy", "competition"}
        and original_notice_source in {"official", "verified", "structured", "ats", "source"}
    ):
        return True
    return False


def _is_quality_error(error):
    return isinstance(error, ValueError)


def _is_provider_error(error):
    if isinstance(error, AIArticleInputError):
        return False
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
    package = article.get("ai_input_package") or {}
    article["seo_slug"] = (
        (_normalize_job_english_slug(data["slug"])
        or _fallback_job_english_slug(package))
    )
    article["final_html"] = final_html
    article["blogger_article_html"] = final_html
    ai_notice_type = str(data.get("notice_type") or "").strip().lower()
    article["job_notice_type"] = ai_notice_type
    article["job_notice_type_source"] = "ai"
    package["job_notice_type"] = ai_notice_type
    package["job_notice_type_ai"] = ai_notice_type
    package["job_notice_type_source"] = "ai"
    article["ai_input_package"] = package
    article["final_word_count"] = word_count
    article["final_html_chars"] = len(final_html)
    article["final_content_hash"] = content_hash_from_html(final_html)
    article["ai_provider_used"] = provider_used
    article.pop("ai_error", None)
    article.pop("ai_deterministic_fallback", None)
    if article.get("ai_retry_origin") == "pre_publish_quality":
        article.pop("publish_status", None)
        article.pop("publish_error", None)
        article.pop("publish_blocked_reason", None)
        article.pop("pre_publish_quality", None)
    article.pop("ai_retry_pending", None)
    article.pop("ai_retry_reason", None)
    article.pop("ai_retry_origin", None)
    article.pop("ai_retry_provider", None)
    for field in (
        "candidate_retry_after",
        "candidate_failure_stage",
        "candidate_failure_reason",
        "candidate_failure_fingerprint",
        "candidate_failure_repeat_count",
        "candidate_failure_backoff_minutes",
        "candidate_failed_at",
    ):
        article.pop(field, None)
    article.pop("ai_failure_scope", None)
    article.pop("ai_previous_failure_scope", None)
    article.pop("ai_failure_fingerprint", None)
    article.pop("ai_failure_category", None)
    article.pop("ai_retry_after", None)


def _apply_failure(article, error):
    article["ai_status"] = "failed"
    article["ai_error"] = str(error)
    article.pop("ai_deterministic_fallback", None)


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

    if not force:
        retry_until = _article_ai_retry_until(article)
        if retry_until > time.time():
            retry_after = _epoch_to_iso(retry_until)
            article["ai_retry_pending"] = True
            article["ai_quality_status"] = "retry_backoff"
            article["ai_previous_failure_scope"] = str(article.get("ai_failure_scope") or "")
            article["ai_failure_scope"] = "retry_backoff"
            article["ai_retry_after"] = retry_after
            save_article_queue(queue)
            log_event(
                "ai_retry_backoff_preflight",
                article_id=article.get("id"),
                failure_scope=article.get("ai_failure_scope", ""),
                failure_fingerprint=article.get("ai_failure_fingerprint", ""),
                retry_after=retry_after,
            )
            return {
                "processed": 0,
                "success": 0,
                "failed": 0,
                "article": article,
                "message": f"AI retry backoff active until {retry_after}",
                "failure_scope": "retry_backoff",
                "failure_fingerprint": article.get("ai_failure_fingerprint", ""),
                "failure_category": article.get("ai_failure_category", ""),
                "retry_after": retry_after,
            }

    package = article["ai_input_package"]
    prompt = ""
    previous_data = None
    last_error = None

    pre_ai_evidence_error = _jobs_pre_ai_evidence_error(package)
    if pre_ai_evidence_error:
        last_error = AIArticleInputError(pre_ai_evidence_error)
        log_event(
            "ai_source_evidence_failure_deferred",
            article_id=article.get("id"),
            reason=pre_ai_evidence_error,
        )
    else:
        prompt = _build_prompt(package)

    if (
        article.get('ai_retry_origin') == 'pre_publish_quality' and str(article.get('ai_retry_reason') or '').strip()
    ):
        previous_data = {
            "title": article.get("seo_title") or "",
            "description": article.get("seo_description") or "",
            "slug": article.get("seo_slug") or "",
            "html_content": article.get("final_html") or "",
            "notice_type": article.get("job_notice_type") or "",
        }
        prompt = _build_expansion_retry_prompt(
            package,
            previous_data,
            str(article.get("ai_retry_reason") or ""),
        )

    provider_sequence = []
    if last_error is None:
        try:
            provider_sequence = _attempt_provider_sequence()
        except Exception as error:
            last_error = error
            try:
                configured_providers = _resolve_providers()
            except Exception:
                configured_providers = []
            _open_global_circuit(error, providers=configured_providers)
            log_event(
                "ai_provider_preflight_failed",
                article_id=article.get("id"),
                category=_provider_error_category(error),
                reason=_safe_error_reason(error),
            )

    failed_provider_names = set()
    provider_failure_categories = {}
    quality_retry_counts = {}
    quality_repairs_used = 0
    forced_next_provider = ""
    preferred_repair_provider = str(article.get("ai_retry_provider") or "").strip().lower()
    if preferred_repair_provider:
        forced_next_provider = preferred_repair_provider
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
        ai_fast_mode_enabled=context.fast_mode_enabled,
        skipped_slow_models_count=context.skipped_slow_models_count,
        ai_total_time_budget_seconds=context.total_budget_seconds,
    )

    total_attempts = (
        0
        if JOBS_MODE and not provider_sequence
        else max(
            1,
            len(provider_sequence) * (1 + max(0, JOBS_AI_QUALITY_REPAIRS)),
        )
    )
    if not provider_sequence:
        if last_error is None:
            if _global_circuit_remaining() > 0:
                last_error = AIProviderRotationExhausted("global AI circuit is open")
            else:
                last_error = RuntimeError(
                    "No AI provider is currently available; Jobs article will remain queued for a later AI retry."
                )
        log_event(
            "ai_providers_unavailable_jobs_retry_pending",
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
            data = _parse_complete_ai_json(
                raw_text,
                (JOBS_REQUIRED_ARTICLE_FIELDS),
                "AI article response",
            )
            previous_data = data
            data = _shorten_metadata_once_if_needed(data)
            data = _normalize_ai_output(data)
            finalize_package = package
            ai_notice_type = str(data.get("notice_type") or "").strip().lower()
            if ai_notice_type not in ALLOWED_JOB_NOTICE_TYPES:
                raise AIIncompleteResponseError(
                    "Jobs notice_type must be one of: " + ", ".join(sorted(ALLOWED_JOB_NOTICE_TYPES))
                )
            finalize_package = dict(package)
            finalize_package["job_notice_type"] = ai_notice_type
            finalize_package["job_notice_type_ai"] = ai_notice_type
            finalize_package["job_notice_type_source"] = "ai"
            data = _finalize_html_content(data, finalize_package)
            data = _ensure_verified_position_count(data, finalize_package)
            validation_result = _validate_ai_output(data, package=finalize_package)
            manifest_warnings = list(getattr(validation_result, "warnings", ()) or ())
            article["ai_quality_warnings"] = manifest_warnings
            if manifest_warnings:
                log_event(
                    "ai_manifest_warnings",
                    article_id=article.get("id"),
                    provider=provider_used or provider,
                    warnings=" | ".join(manifest_warnings[:6]),
                    warning_count=len(manifest_warnings),
                )
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
            is_article_input_failure = isinstance(error, AIArticleInputError)
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
                if _is_nonrepairable_jobs_evidence_quality_error(error, package):
                    last_error = AIArticleInputError(
                        f"source/evidence problem: {_safe_error_reason(error)}"
                    )
                    log_event(
                        "ai_source_evidence_failure_deferred",
                        article_id=article.get("id"),
                        provider=provider or provider_used,
                        reason=_safe_error_reason(error),
                    )
                    break

                provider_key = str(
                    provider or provider_used.split(":", 1)[0] or "default"
                ).strip().lower()
                provider_repairs_used = int(quality_retry_counts.get(provider_key) or 0)

                if attempt >= total_attempts:
                    log_event(
                        "ai_quality_repair_limit_reached",
                        article_id=article.get("id"),
                        provider=provider_key,
                        repairs_used=provider_repairs_used,
                        total_repairs_used=quality_repairs_used,
                        max_repairs=JOBS_AI_QUALITY_REPAIRS,
                        reason=str(error),
                    )
                    break

                if provider_repairs_used >= JOBS_AI_QUALITY_REPAIRS:
                    next_provider = next(
                        (
                            candidate
                            for candidate in provider_sequence
                            if candidate != provider_key
                            and candidate not in failed_provider_names
                            and int(quality_retry_counts.get(candidate) or 0)
                            < JOBS_AI_QUALITY_REPAIRS
                        ),
                        "",
                    )
                    if not next_provider:
                        log_event(
                            "ai_quality_repair_limit_reached",
                            article_id=article.get("id"),
                            provider=provider_key,
                            repairs_used=provider_repairs_used,
                            total_repairs_used=quality_repairs_used,
                            max_repairs=JOBS_AI_QUALITY_REPAIRS,
                            reason=str(error),
                        )
                        break
                    prompt = _build_expansion_retry_prompt(
                        package,
                        previous_data,
                        str(error),
                    )
                    forced_next_provider = next_provider
                    log_event(
                        "ai_quality_provider_switch",
                        article_id=article.get("id"),
                        from_provider=provider_key,
                        to_provider=next_provider,
                        reason=str(error),
                    )
                    continue

                quality_retry_counts[provider_key] = provider_repairs_used + 1
                quality_repairs_used += 1
                article["ai_quality_repairs_used"] = quality_repairs_used
                if "too much english inside article paragraphs" in str(error).casefold():
                    article["ai_excess_english_retry_used"] = True
                    prompt = _build_excess_english_retry_prompt(
                        package,
                        previous_data,
                        str(error),
                    )
                else:
                    prompt = _build_expansion_retry_prompt(
                        package,
                        previous_data,
                        str(error),
                    )
                forced_next_provider = provider_key
                log_event(
                    "ai_quality_repair_retry",
                    article_id=article.get("id"),
                    provider=forced_next_provider,
                    repair=quality_retry_counts[provider_key],
                    total_repairs=quality_repairs_used,
                    max_repairs=JOBS_AI_QUALITY_REPAIRS,
                    reason=str(error),
                )
                continue

            if is_article_input_failure:
                log_event(
                    "ai_article_input_backoff",
                    article_id=article.get("id"),
                    provider=provider or provider_used,
                    reason=_safe_error_reason(error),
                )
                break
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
                    provider_failure_categories[provider] = _provider_error_category(error)

                infrastructure_failures = {
                    name
                    for name, category in provider_failure_categories.items()
                    if category in {"outage", "timeout"}
                }
                if len(infrastructure_failures) >= 2:
                    try:
                        configured_providers = _resolve_providers()
                    except Exception:
                        configured_providers = sorted(failed_provider_names)
                    circuit = _open_global_circuit(
                        error,
                        providers=configured_providers,
                    )
                    log_event(
                        "ai_global_outage_detected_early",
                        article_id=article.get("id"),
                        failed_providers=",".join(sorted(infrastructure_failures)),
                        category=circuit.get("category", ""),
                        failure_fingerprint=circuit.get("fingerprint", ""),
                        retry_after=_epoch_to_iso(circuit.get("until", 0)),
                    )
                    break

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
                        _open_global_circuit(
                            error,
                            providers=_resolve_providers(),
                        )
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

    _apply_failure(article, last_error)
    article["ai_retry_pending"] = True
    article["ai_retry_reason"] = _safe_error_reason(last_error) if last_error else "AI generation failed"
    provider_exhausted = bool(
        last_error
        and not isinstance(last_error, AITimeBudgetExceeded)
        and _is_provider_error(last_error)
        and not _is_quality_error(last_error)
    )
    article["ai_rotation_exhausted"] = provider_exhausted
    article["ai_time_budget_exceeded"] = isinstance(last_error, AITimeBudgetExceeded)
    article["ai_total_time_seconds"] = round(context.elapsed_seconds(), 2)

    failure_scope = "quality"
    failure_provider = str(locals().get("provider") or "").strip().lower()
    failure_retry_until = 0
    if isinstance(last_error, AIArticleInputError):
        failure_scope = "article_input"
        fingerprint, failure_category, failure_retry_until = _record_failure_fingerprint(
            last_error,
            scope="article_input",
            provider=failure_provider,
        )
    elif isinstance(last_error, AITimeBudgetExceeded):
        failure_scope = "cycle_budget"
        fingerprint, failure_category, failure_retry_until = _record_failure_fingerprint(
            last_error,
            scope="cycle_budget",
            provider=failure_provider,
        )
    elif provider_exhausted:
        failure_scope = "global_outage"
        current_circuit = ai_circuit_status()
        if current_circuit.get("global_open"):
            fingerprint = str(current_circuit.get("global_fingerprint") or "")
            failure_category = str(current_circuit.get("global_category") or "provider_error")
            failure_retry_until = _global_circuit_until()
        else:
            circuit = _open_global_circuit(
                last_error or AIProviderRotationExhausted("AI provider rotation exhausted"),
                providers=_resolve_providers(),
            )
            fingerprint = str(circuit.get("fingerprint") or "")
            failure_category = str(circuit.get("category") or "provider_error")
            failure_retry_until = float(circuit.get("until") or 0)
    else:
        fingerprint, failure_category, failure_retry_until = _record_failure_fingerprint(
            last_error or ValueError("AI quality failed"),
            scope="quality",
            provider=failure_provider,
        )

    article["ai_failure_scope"] = failure_scope
    article["ai_failure_fingerprint"] = fingerprint
    article["ai_failure_category"] = failure_category
    article["ai_retry_after"] = _epoch_to_iso(failure_retry_until)
    article["ai_quality_status"] = (
        "article_input_backoff"
        if isinstance(last_error, AIArticleInputError)
        else "time_budget_exceeded"
        if isinstance(last_error, AITimeBudgetExceeded)
        else "provider_rotation_exhausted"
        if provider_exhausted
        else "failed_after_retries"
    )
    article["ai_quality_attempts"] = attempt if "attempt" in locals() else 0
    save_article_queue(queue)
    if isinstance(last_error, AIArticleInputError):
        log_event(
            "ai_article_input_failure_deferred",
            article_id=article.get("id"),
            failure_fingerprint=article.get("ai_failure_fingerprint", ""),
            retry_after=article.get("ai_retry_after", ""),
            reason=_safe_error_reason(last_error),
        )
        log_event(
            "ai_article_skipped_after_ai_failure",
            article_id=article.get("id"),
            reason="article_input_backoff",
        )
    elif isinstance(last_error, AITimeBudgetExceeded):
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
        "failure_scope": article.get("ai_failure_scope", ""),
        "failure_fingerprint": article.get("ai_failure_fingerprint", ""),
        "failure_category": article.get("ai_failure_category", ""),
        "retry_after": article.get("ai_retry_after", ""),
    }
