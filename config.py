# ============================================================
# config.py - Central Configuration Manager
# ============================================================
# This file loads all settings from the .env file and provides
# them to every other module in the project. You only need to
# edit the .env file - never edit this file directly.
# ============================================================

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# --------------------------------------------------
# 1) Figure out where the project folder is located
# --------------------------------------------------
# BASE_DIR = the folder that contains this config.py file.
# This makes paths work correctly no matter where you
# run the script from (desktop, terminal, cron job, etc.)
BASE_DIR = Path(__file__).resolve().parent

# --------------------------------------------------
# 2) Load environment variables from the .env file
# --------------------------------------------------
# load_dotenv() reads the .env file and puts all values
# into os.environ so we can access them with os.getenv()
dotenv_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=dotenv_path)


# ============================================================
# 3) API Keys & Credentials (loaded from .env)
# ============================================================

# Your Google Gemini AI API key
# Used to write Arabic articles from verified Job facts
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# AI provider mode:
# - auto: rotate across configured providers; Jobs defer behind circuit/backoff when AI is unavailable
# - a named provider: use that provider only when explicitly requested
AI_PROVIDER = os.getenv("AI_PROVIDER", "auto").strip().lower()

# Your Blogger Blog ID (looks like a long number)
# This tells Blogger which blog to publish to
BLOG_ID = os.getenv("BLOG_ID", "")

# Optional OAuth client ID for Blogger desktop authentication.
# If you prefer environment variables, set both the client ID
# and client secret in .env.
BLOGGER_CLIENT_ID = os.getenv("BLOGGER_CLIENT_ID", "")
BLOGGER_CLIENT_SECRET = os.getenv("BLOGGER_CLIENT_SECRET", "")



def _env_int(name, default):
    """
    Read an integer environment variable with a safe fallback.
    """
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        return default


def _env_int_any(names, default):
    for name in names:
        value = os.getenv(name, "").strip()
        if not value:
            continue
        try:
            return int(value)
        except ValueError:
            continue
    return default


def _env_bool_any(names, default=False):
    for name in names:
        raw = os.getenv(name, "").strip()
        if raw:
            return raw.lower() in {"1", "true", "yes", "on"}
    return bool(default)


def _env_csv(name, default=""):
    """
    Read a comma-separated environment variable into a de-duplicated list.
    """
    raw_value = os.getenv(name, default)
    items = []
    seen = set()

    for part in raw_value.split(","):
        item = part.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        items.append(item)

    return items


# ============================================================
# 4) Timing & Retry Settings (with safe defaults)
# ============================================================

# How long (in seconds) to wait before checking for new articles again
# Default: 3600 seconds = 1 hour
CHECK_INTERVAL = _env_int("CHECK_INTERVAL", 3600)

# How long (in seconds) to wait before retrying a failed API call
# Default: 30 seconds
RETRY_DELAY = _env_int("RETRY_DELAY", 30)

# Maximum number of times to retry a failed operation before giving up
# Default: 3 attempts
MAX_RETRIES = _env_int("MAX_RETRIES", 3)

# Optional limits and pacing controls
# The fast recent live pipeline publishes at most one article per run by default.
MAX_POSTS_PER_RUN = _env_int("MAX_POSTS_PER_RUN", 1)
MAX_ARTICLES_PER_RUN = _env_int("MAX_ARTICLES_PER_RUN", MAX_POSTS_PER_RUN)
SCRAPE_DELAY_SECONDS = _env_int("SCRAPE_DELAY_SECONDS", 2)
BLOGGER_LIVE_ENABLED = _env_bool_any(["BLOGGER_LIVE_ENABLED"], True)
PUBLISH_MODE = os.getenv("PUBLISH_MODE", "live" if BLOGGER_LIVE_ENABLED else "draft").strip().lower()
SAFE_MODE = _env_bool_any(["SAFE_MODE"], False)
MAX_SOURCE_RETRIES = _env_int("MAX_SOURCE_RETRIES", 1)
SOURCE_RETRY_DELAY_SECONDS = _env_int("SOURCE_RETRY_DELAY_SECONDS", 3)
SOURCE_TIMEOUT_SECONDS = _env_int("SOURCE_TIMEOUT_SECONDS", 12)
ARTICLE_TIMEOUT_SECONDS = _env_int("ARTICLE_TIMEOUT_SECONDS", 15)
MAX_SOURCES_PER_RUN = _env_int("MAX_SOURCES_PER_RUN", 4)
FALLBACK_FIRST_RUN_LOOKBACK_HOURS = _env_int("FALLBACK_FIRST_RUN_LOOKBACK_HOURS", 6)
CRAWL_OVERLAP_MINUTES = _env_int("CRAWL_OVERLAP_MINUTES", 10)
FRESHNESS_SAFETY_MARGIN_MINUTES = _env_int("FRESHNESS_SAFETY_MARGIN_MINUTES", 10)
ENABLE_SCRAPLING_FALLBACK = _env_bool_any(["ENABLE_SCRAPLING_FALLBACK"], False)
MAX_AI_RETRIES = _env_int("MAX_AI_RETRIES", 2)
JOBS_AI_QUALITY_REPAIRS = max(0, min(2, _env_int("JOBS_AI_QUALITY_REPAIRS", 1)))
JOBS_AI_TIMEOUT_RETRIES = max(0, min(1, _env_int("JOBS_AI_TIMEOUT_RETRIES", 0)))
JOBS_AI_CROSS_CANDIDATE_RETRIES = max(0, min(1, _env_int("JOBS_AI_CROSS_CANDIDATE_RETRIES", 1)))
AI_TIMEOUT_SECONDS = _env_int("AI_TIMEOUT_SECONDS", 180)
AI_TOTAL_TIME_BUDGET_SECONDS = _env_int("AI_TOTAL_TIME_BUDGET_SECONDS", AI_TIMEOUT_SECONDS)
GEMINI_TIMEOUT_SECONDS = _env_int("GEMINI_TIMEOUT_SECONDS", 45)
AI_MODEL_TIMEOUT_SECONDS = _env_int("AI_MODEL_TIMEOUT_SECONDS", 40)
SOURCE_HEALTH_ENABLED = _env_bool_any(["SOURCE_HEALTH_ENABLED"], True)
SOURCE_FAILURE_COOLDOWN_MINUTES = _env_int("SOURCE_FAILURE_COOLDOWN_MINUTES", 45)
SOURCE_FAILURE_THRESHOLD = _env_int("SOURCE_FAILURE_THRESHOLD", 3)
SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES = _env_int("SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES", 15)
JOBS_ENRICH_MAX_TARGETS_PER_CYCLE = max(
    1,
    min(50, _env_int("JOBS_ENRICH_MAX_TARGETS_PER_CYCLE", 12)),
)

# Publish only opportunities whose official publication time proves they are fresh.
# Unknown publication time is not treated as fresh. The configured value is
# capped at the agreed 12h window, including manual runs.
JOBS_MAX_PUBLISH_AGE_HOURS = max(
    1,
    min(12, _env_int("JOBS_MAX_PUBLISH_AGE_HOURS", 12)),
)

# Jobs discovery pagination. fetch_limit_per_run remains a compatibility/page-size
# hint; it is no longer the total number of vacancies a source may expose.
JOBS_DISCOVERY_PAGE_SIZE = max(5, min(50, _env_int("JOBS_DISCOVERY_PAGE_SIZE", 20)))
JOBS_DISCOVERY_MAX_PAGES = max(1, min(25, _env_int("JOBS_DISCOVERY_MAX_PAGES", 12)))
JOBS_DISCOVERY_SEEN_STREAK = max(3, min(50, _env_int("JOBS_DISCOVERY_SEEN_STREAK", 8)))
JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE = max(
    JOBS_DISCOVERY_PAGE_SIZE,
    min(500, _env_int("JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE", 250)),
)
# Keep a deep durable per-source discovery memory. Large official portals can
# expose hundreds or thousands of active vacancies; forgetting IDs too quickly
# makes later cycles waste time rediscovering old listing rows.
JOBS_DISCOVERY_SEEN_MEMORY = max(
    JOBS_DISCOVERY_SEEN_STREAK * 4,
    min(20000, _env_int("JOBS_DISCOVERY_SEEN_MEMORY", 5000)),
)

# Facebook Page auto-posting is disabled by default and only runs after a
# successful live Blogger publish.
FACEBOOK_AUTO_POST = _env_bool_any(["FACEBOOK_AUTO_POST"], False)
FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "").strip()
FACEBOOK_PAGE_ACCESS_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
META_GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v26.0").strip().lower() or "v26.0"
if not META_GRAPH_API_VERSION.startswith("v"):
    META_GRAPH_API_VERSION = "v" + META_GRAPH_API_VERSION
FACEBOOK_GRAPH_API_URL = (
    os.getenv("FACEBOOK_GRAPH_API_URL", "").strip()
    or f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
)
# Independent safety rails: a bad env value or urgent override must not flood a Page.
WHATSAPP_CHANNEL_URL = os.getenv(
    "WHATSAPP_CHANNEL_URL",
    "https://whatsapp.com/channel/0029Vb7MdMfBVJl1kmmV4T0e",
).strip()
# Repository identity, not an environment-selectable news mode.
JOBS_MODE = True
JOBS_TIMEZONE = os.getenv("JOBS_TIMEZONE", "Africa/Casablanca").strip() or "Africa/Casablanca"
# Every successful Jobs article is eligible for Facebook, subject to delivery limits.
JOBS_FACEBOOK_FOLLOW_ARTICLE = True
JOBS_FACEBOOK_MAX_POSTS_PER_DAY = max(1, min(240, _env_int("JOBS_FACEBOOK_MAX_POSTS_PER_DAY", 240)))
JOBS_FACEBOOK_MIN_INTERVAL_MINUTES = max(5, _env_int("JOBS_FACEBOOK_MIN_INTERVAL_MINUTES", 5))
JOBS_ADAPTIVE_PUBLISHING = _env_bool_any(["JOBS_ADAPTIVE_PUBLISHING"], False)
JOBS_ADAPTIVE_MIN_DAILY_CAP = max(1, _env_int("JOBS_ADAPTIVE_MIN_DAILY_CAP", 240))
JOBS_ADAPTIVE_MAX_DAILY_CAP = max(
    JOBS_ADAPTIVE_MIN_DAILY_CAP,
    _env_int("JOBS_ADAPTIVE_MAX_DAILY_CAP", 240),
)
JOBS_ACTIVE_START_HOUR = min(23, max(0, _env_int("JOBS_ACTIVE_START_HOUR", 7)))
JOBS_ACTIVE_END_HOUR = min(24, max(JOBS_ACTIVE_START_HOUR + 1, _env_int("JOBS_ACTIVE_END_HOUR", 23)))
JOBS_MIN_PUBLISH_INTERVAL_MINUTES = max(
    5,
    _env_int("JOBS_MIN_PUBLISH_INTERVAL_MINUTES", 10),
)
JOBS_EXPECTED_BLOG_HOST = os.getenv("JOBS_EXPECTED_BLOG_HOST", "example.invalid").strip().casefold()
JOBS_TEST_MODE = _env_bool_any(["JOBS_TEST_MODE"], True)
JOBS_GOOGLE_INDEXING_ENABLED = _env_bool_any(["JOBS_GOOGLE_INDEXING_ENABLED"], False)
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
JOBS_MEMORY_DIR = BASE_DIR / "data" / "job_memory"
JOB_VISUAL_STATE_PATH = BASE_DIR / "data" / "job_visual_state.json"


# Phase 9 lightweight-cycle controls.
SAFE_CYCLE_MAX_ARTICLES = 1
SAFE_CYCLE_DRAFT_ONLY = os.getenv("SAFE_CYCLE_DRAFT_ONLY", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Phase 10 safe-cycle schedule controls.
MAX_DRAFTS_PER_DAY = _env_int("MAX_DRAFTS_PER_DAY", 10)
MIN_MINUTES_BETWEEN_DRAFTS = _env_int("MIN_MINUTES_BETWEEN_DRAFTS", 30)
MAX_LIVE_POSTS_PER_DAY = _env_int("MAX_LIVE_POSTS_PER_DAY", 3)
TARGET_LIVE_POSTS_PER_DAY = _env_int("TARGET_LIVE_POSTS_PER_DAY", 3)
MIN_MINUTES_BETWEEN_LIVE_POSTS = _env_int("MIN_MINUTES_BETWEEN_LIVE_POSTS", 0)

# Minimum extraction input; verified facts and Job quality gates decide publication.
MIN_ARTICLE_BODY_CHARS = _env_int_any(["MIN_EXTRACTED_CHARS", "MIN_ARTICLE_BODY_CHARS"], 80)
MIN_EXTRACTED_CHARS = MIN_ARTICLE_BODY_CHARS


# ============================================================
# 5) File Paths (auto-calculated, no editing needed)
# ============================================================

# Durable discovery cursors and per-source health.
CRAWL_STATE_PATH = BASE_DIR / "data" / "crawl_state.json"
SOURCE_HEALTH_PATH = BASE_DIR / "data" / "source_health.json"

# Phase 1 ingestion source configuration and safe article queue.
SOURCES_CONFIG_PATH = BASE_DIR / "sources.json"
ARTICLE_QUEUE_PATH = BASE_DIR / "jobs_article_queue.json"

# The folder where log files are stored
LOGS_DIR = BASE_DIR / "logs"

# The path to your Google OAuth2 credentials file
# You must download this from the Google Cloud Console
CREDENTIALS_FILE = BASE_DIR / "client_secret.json"

# The path where the OAuth2 refresh token is saved after first login
# This file is created automatically - you don't need to create it
TOKEN_FILE = BASE_DIR / "data" / "token.json"


# ============================================================
# 6) Blogger API Scopes & Settings
# ============================================================

# The OAuth2 "scope" tells Google what permissions we need.
# We need permission to read the user's profile and manage their blog.
SCOPES = [
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/blogger",
]

# The Gemini model we'll use for translation.
# You can override it from .env if needed.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# OpenRouter fallback settings.
# OPENROUTER_API_KEY should be kept in .env only.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
FAST_OPENROUTER_MODELS = [
    "openrouter/free",
]
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", FAST_OPENROUTER_MODELS[0]).strip()
_configured_openrouter_models = _env_csv("OPENROUTER_MODELS")
OPENROUTER_MODELS = [
    model
    for model in (_configured_openrouter_models or FAST_OPENROUTER_MODELS)
    if model in FAST_OPENROUTER_MODELS
]
if not OPENROUTER_MODELS:
    OPENROUTER_MODELS = FAST_OPENROUTER_MODELS[:]
OPENROUTER_API_URL = os.getenv(
    "OPENROUTER_API_URL",
    "https://openrouter.ai/api/v1/chat/completions",
)
OPENROUTER_MAX_TOKENS = _env_int("OPENROUTER_MAX_TOKENS", 8192)
OPENROUTER_TIMEOUT_SECONDS = _env_int("OPENROUTER_TIMEOUT_SECONDS", AI_MODEL_TIMEOUT_SECONDS)
OPENROUTER_REFERER = os.getenv("OPENROUTER_REFERER", "")
OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "Blogger Automation Bot")

# Additional independent providers used by Jobs auto-failover.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
GROQ_API_URL = os.getenv(
    "GROQ_API_URL",
    "https://api.groq.com/openai/v1/chat/completions",
).strip()
GROQ_MAX_TOKENS = _env_int("GROQ_MAX_TOKENS", 4096)
GROQ_TIMEOUT_SECONDS = _env_int("GROQ_TIMEOUT_SECONDS", AI_MODEL_TIMEOUT_SECONDS)

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-small-latest").strip()
MISTRAL_API_URL = os.getenv(
    "MISTRAL_API_URL",
    "https://api.mistral.ai/v1/chat/completions",
).strip()
MISTRAL_MAX_TOKENS = _env_int("MISTRAL_MAX_TOKENS", 4096)
MISTRAL_TIMEOUT_SECONDS = _env_int("MISTRAL_TIMEOUT_SECONDS", AI_MODEL_TIMEOUT_SECONDS)

CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_MODEL = os.getenv(
    "CLOUDFLARE_MODEL",
    "@cf/meta/llama-3.1-8b-instruct",
).strip()
CLOUDFLARE_MAX_TOKENS = _env_int("CLOUDFLARE_MAX_TOKENS", 4096)
CLOUDFLARE_TIMEOUT_SECONDS = _env_int("CLOUDFLARE_TIMEOUT_SECONDS", AI_MODEL_TIMEOUT_SECONDS)

# OpenAI settings for Phase 6 AI processing.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_URL = os.getenv(
    "OPENAI_API_URL",
    "https://api.openai.com/v1/chat/completions",
)
OPENAI_MAX_TOKENS = _env_int("OPENAI_MAX_TOKENS", 8192)
OPENAI_TIMEOUT_SECONDS = _env_int("OPENAI_TIMEOUT_SECONDS", AI_MODEL_TIMEOUT_SECONDS)

# Facebook image generation settings.
FACEBOOK_IMAGE_OUTPUT_DIR = BASE_DIR / "output" / "facebook_images"

# Runtime memory is atomically saved and persisted by the Jobs workflow.
AI_PROVIDER_MEMORY_PATH = BASE_DIR / "data" / "ai_provider_memory.json"
FACEBOOK_STYLE_MEMORY_PATH = BASE_DIR / "data" / "facebook_style_memory.json"
INTERNAL_LINK_CACHE_PATH = BASE_DIR / "data" / "internal_link_cache.json"


# ============================================================
# 7) HTTP Headers for Web Scraping
# ============================================================
# These headers make our scraper look like a real web browser.
# Without them, many websites will block us automatically.

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "DNT": "1",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}
