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
# This is used to translate English articles to Arabic
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

# The website URL we scrape articles from
SOURCE_URL = os.getenv("SOURCE_URL", "https://darkwebinformer.com/tag/tools/")

# Allow the automation to finish by saving posts locally when
# Blogger OAuth credentials are not available.
LOCAL_PUBLISH_FALLBACK = os.getenv("LOCAL_PUBLISH_FALLBACK", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


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



SOURCE_URLS = _env_csv("SOURCES")
if not SOURCE_URLS:
    SOURCE_URLS = [SOURCE_URL]

TRUSTED_SECURITY_SOURCES = [
    "cisa.gov",
    "nist.gov",
    "bleepingcomputer.com",
    "thehackernews.com",
    "darkreading.com",
    "securityweek.com",
    "krebsonsecurity.com",
    "therecord.media",
    "unit42.paloaltonetworks.com",
    "blog.talosintelligence.com",
    "mandiant.com",
    "malwarebytes.com",
    "isc.sans.edu",
    "troyhunt.com",
    "exploit-db.com",
    "googleprojectzero.blogspot.com",
]


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
TRANSLATION_DELAY_SECONDS = _env_int("TRANSLATION_DELAY_SECONDS", 3)
PUBLISH_DELAY_SECONDS = _env_int("PUBLISH_DELAY_SECONDS", 5)
BLOGGER_LIVE_ENABLED = _env_bool_any(["BLOGGER_LIVE_ENABLED"], True)
PUBLISH_MODE = os.getenv("PUBLISH_MODE", "live" if BLOGGER_LIVE_ENABLED else "draft").strip().lower()
SAFE_MODE = _env_bool_any(["SAFE_MODE"], False)
FAST_NEWS_MODE = _env_bool_any(["FAST_NEWS_MODE"], True)
FRESH_QUEUE_MODE = _env_bool_any(["FRESH_QUEUE_MODE"], False)
FIRST_VALID_ARTICLE_MODE = _env_bool_any(["FIRST_VALID_ARTICLE_MODE"], True)
CATEGORY_ROTATION_MODE = _env_bool_any(["CATEGORY_ROTATION_MODE"], True)
PROCESS_FULL_CATEGORY_PER_RUN = _env_bool_any(["PROCESS_FULL_CATEGORY_PER_RUN"], True)
PUBLISH_WEAK_ARTICLES = _env_bool_any(["PUBLISH_WEAK_ARTICLES"], True)
ALLOW_SHORT_ARTICLES = _env_bool_any(["ALLOW_SHORT_ARTICLES"], True)
SKIP_ADS_AFFILIATE_SPONSORED = _env_bool_any(["SKIP_ADS_AFFILIATE_SPONSORED"], True)
MIN_ARTICLE_WORDS = _env_int("MIN_ARTICLE_WORDS", 80)
TARGET_ARTICLE_WORDS = os.getenv("TARGET_ARTICLE_WORDS", "700-1000").strip()
MAX_SOURCE_RETRIES = _env_int("MAX_SOURCE_RETRIES", 1)
SOURCE_RETRY_DELAY_SECONDS = _env_int("SOURCE_RETRY_DELAY_SECONDS", 3)
SOURCE_TIMEOUT_SECONDS = _env_int("SOURCE_TIMEOUT_SECONDS", 12)
ARTICLE_TIMEOUT_SECONDS = _env_int("ARTICLE_TIMEOUT_SECONDS", 15)
MAX_SOURCES_PER_RUN = _env_int("MAX_SOURCES_PER_RUN", 4)
FALLBACK_FIRST_RUN_LOOKBACK_HOURS = _env_int("FALLBACK_FIRST_RUN_LOOKBACK_HOURS", 6)
CRAWL_INTERVAL_MINUTES = _env_int("CRAWL_INTERVAL_MINUTES", 5)
CRAWL_OVERLAP_MINUTES = _env_int("CRAWL_OVERLAP_MINUTES", 10)
RECENT_NEWS_ONLY = _env_bool_any(["RECENT_ONLY", "RECENT_NEWS_ONLY"], True)
RECENT_NEWS_MAX_AGE_HOURS = _env_int_any(["RECENT_HOURS", "RECENT_NEWS_MAX_AGE_HOURS"], 2)
RECENT_HOURS = RECENT_NEWS_MAX_AGE_HOURS
FRESHNESS_SAFETY_MARGIN_MINUTES = _env_int("FRESHNESS_SAFETY_MARGIN_MINUTES", 10)
MAX_AI_ARTICLE_AGE_HOURS = max(
    0.0,
    RECENT_NEWS_MAX_AGE_HOURS - (max(0, FRESHNESS_SAFETY_MARGIN_MINUTES) / 60),
)
ALLOW_UNKNOWN_DATE_IN_FAST_MODE = _env_bool_any(["ALLOW_UNKNOWN_DATE_IN_FAST_MODE"], False)
ENABLE_SCRAPLING_FALLBACK = _env_bool_any(["ENABLE_SCRAPLING_FALLBACK"], False)
MAX_AI_RETRIES = _env_int("MAX_AI_RETRIES", 2)
JOBS_AI_QUALITY_REPAIRS = max(0, min(2, _env_int("JOBS_AI_QUALITY_REPAIRS", 1)))
JOBS_AI_TIMEOUT_RETRIES = max(0, min(1, _env_int("JOBS_AI_TIMEOUT_RETRIES", 0)))
JOBS_AI_CROSS_CANDIDATE_RETRIES = max(0, min(1, _env_int("JOBS_AI_CROSS_CANDIDATE_RETRIES", 1)))
AI_TIMEOUT_SECONDS = _env_int("AI_TIMEOUT_SECONDS", 180)
AI_TOTAL_TIME_BUDGET_SECONDS = _env_int("AI_TOTAL_TIME_BUDGET_SECONDS", AI_TIMEOUT_SECONDS)
GEMINI_TIMEOUT_SECONDS = _env_int("GEMINI_TIMEOUT_SECONDS", 45)
AI_MODEL_TIMEOUT_SECONDS = _env_int("AI_MODEL_TIMEOUT_SECONDS", 40)
DUPLICATE_PROTECTION = _env_bool_any(["DUPLICATE_PROTECTION"], True)
QUEUE_PERSIST = _env_bool_any(["QUEUE_PERSIST"], True)
SOURCE_HEALTH_ENABLED = _env_bool_any(["SOURCE_HEALTH_ENABLED"], True)
SOURCE_FAILURE_COOLDOWN_MINUTES = _env_int("SOURCE_FAILURE_COOLDOWN_MINUTES", 45)
SOURCE_FAILURE_THRESHOLD = _env_int("SOURCE_FAILURE_THRESHOLD", 3)
SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES = _env_int("SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES", 15)

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
FACEBOOK_LINK_MODE = os.getenv("FACEBOOK_LINK_MODE", "comment").strip().lower()
if FACEBOOK_LINK_MODE not in {"caption", "comment", "both"}:
    FACEBOOK_LINK_MODE = "comment"
MAX_FACEBOOK_POSTS_PER_DAY = _env_int("MAX_FACEBOOK_POSTS_PER_DAY", 2)
MIN_MINUTES_BETWEEN_FACEBOOK_POSTS = _env_int("MIN_MINUTES_BETWEEN_FACEBOOK_POSTS", 0)
# Independent safety rails: a bad env value or urgent override must not flood a Page.
FACEBOOK_HARD_MAX_POSTS_PER_DAY = max(1, _env_int("FACEBOOK_HARD_MAX_POSTS_PER_DAY", 3))
FACEBOOK_SAFETY_MIN_INTERVAL_MINUTES = max(
    15,
    _env_int("FACEBOOK_SAFETY_MIN_INTERVAL_MINUTES", 45),
)
WHATSAPP_CHANNEL_URL = os.getenv(
    "WHATSAPP_CHANNEL_URL",
    "https://whatsapp.com/channel/0029Vb7MdMfBVJl1kmmV4T0e",
).strip()
JOBS_MODE = _env_bool_any(["JOBS_MODE"], True)
JOBS_TIMEZONE = os.getenv("JOBS_TIMEZONE", "Africa/Casablanca").strip() or "Africa/Casablanca"
JOBS_MIN_SELECTION_SCORE = _env_int("JOBS_MIN_SELECTION_SCORE", 65)
JOBS_QUEUE_SCORE = _env_int("JOBS_QUEUE_SCORE", 50)
JOBS_URGENT_EXTRA_DAILY_LIMIT = _env_int("JOBS_URGENT_EXTRA_DAILY_LIMIT", 1)
# Legacy compatibility only: Facebook no longer filters Jobs by score; score is queue priority only.
JOBS_FACEBOOK_MIN_SCORE = _env_int("JOBS_FACEBOOK_MIN_SCORE", 65)
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
JOBS_STATE_PATH = BASE_DIR / "data" / "job_state.json"
JOB_VISUAL_STATE_PATH = BASE_DIR / "data" / "job_visual_state.json"


# Phase 9 lightweight-cycle controls.
CATEGORY_POSTS_PER_HOUR = _env_int("CATEGORY_POSTS_PER_HOUR", 1)
HOURLY_POST_LIMIT = _env_int("HOURLY_POST_LIMIT", 1)
SAFE_CYCLE_MAX_ARTICLES = _env_int("SAFE_CYCLE_MAX_ARTICLES", 1)
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

# Quality-first publishing controls. The bot fetches a larger candidate pool,
# saves every usable article to a backlog, then publishes a balanced batch.
ARTICLE_SELECTION_MULTIPLIER = _env_int("ARTICLE_SELECTION_MULTIPLIER", 4)
ARTICLE_SELECTION_POOL_MIN = _env_int("ARTICLE_SELECTION_POOL_MIN", 20)
MIN_ARTICLE_BODY_CHARS = _env_int_any(["MIN_EXTRACTED_CHARS", "MIN_ARTICLE_BODY_CHARS"], 80)
MIN_EXTRACTED_CHARS = MIN_ARTICLE_BODY_CHARS
MIN_ARTICLE_WORDS = _env_int("MIN_ARTICLE_WORDS", 80)
MAX_ARTICLES_PER_SOURCE_PER_RUN = _env_int("MAX_ARTICLES_PER_SOURCE_PER_RUN", 2)


# ============================================================
# 5) File Paths (auto-calculated, no editing needed)
# ============================================================

# The JSON file that tracks which articles have already been published.
# This prevents the bot from posting the same article twice.
PUBLISHED_DB_PATH = BASE_DIR / "data" / "published_ids.json"

# The JSON file that stores fetched-but-not-yet-published articles.
# This keeps daily articles from being forgotten when only 5 are published/hour.
ARTICLE_BACKLOG_PATH = BASE_DIR / "data" / "article_backlog.json"
CRAWL_STATE_PATH = BASE_DIR / "data" / "crawl_state.json"
TOPIC_FINGERPRINTS_PATH = BASE_DIR / "data" / "topic_fingerprints.json"
SOURCE_HEALTH_PATH = BASE_DIR / "data" / "source_health.json"

# Phase 1 ingestion source configuration and safe article queue.
SOURCES_CONFIG_PATH = BASE_DIR / "sources.json"
ARTICLE_QUEUE_PATH = BASE_DIR / ("jobs_article_queue.json" if JOBS_MODE else "article_queue.json")

# The folder where log files are stored
LOGS_DIR = BASE_DIR / "logs"

# Fallback output folder for locally saved posts
LOCAL_PUBLISH_DIR = BASE_DIR / "output" / "published_posts"

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
GEMINI_MODELS = _env_csv("GEMINI_MODELS")
if not GEMINI_MODELS and GEMINI_MODEL:
    GEMINI_MODELS = [GEMINI_MODEL]

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
OPENAI_MODELS = _env_csv("OPENAI_MODELS")
if not OPENAI_MODELS and OPENAI_MODEL:
    OPENAI_MODELS = [OPENAI_MODEL]
OPENAI_API_URL = os.getenv(
    "OPENAI_API_URL",
    "https://api.openai.com/v1/chat/completions",
)
OPENAI_MAX_TOKENS = _env_int("OPENAI_MAX_TOKENS", 8192)
OPENAI_TIMEOUT_SECONDS = _env_int("OPENAI_TIMEOUT_SECONDS", AI_MODEL_TIMEOUT_SECONDS)

# Facebook image generation settings.
ASSETS_DIR = BASE_DIR / "assets"
FACEBOOK_IMAGE_TEMPLATE_PATH = ASSETS_DIR / "facebook_template.png"
FACEBOOK_FALLBACK_ARTICLE_IMAGE_PATH = ASSETS_DIR / "fallback_article.png"
FACEBOOK_IMAGE_OUTPUT_DIR = BASE_DIR / "output" / "facebook_images"
FACEBOOK_IMAGE_SIZE = _env_int("FACEBOOK_IMAGE_SIZE", 1080)
FACEBOOK_IMAGE_BOX_X = _env_int("FACEBOOK_IMAGE_BOX_X", 180)
FACEBOOK_IMAGE_BOX_Y = _env_int("FACEBOOK_IMAGE_BOX_Y", 170)
FACEBOOK_IMAGE_BOX_W = _env_int("FACEBOOK_IMAGE_BOX_W", 720)
FACEBOOK_IMAGE_BOX_H = _env_int("FACEBOOK_IMAGE_BOX_H", 520)
FACEBOOK_TITLE_BOX_X = _env_int("FACEBOOK_TITLE_BOX_X", 130)
FACEBOOK_TITLE_BOX_Y = _env_int("FACEBOOK_TITLE_BOX_Y", 740)
FACEBOOK_TITLE_BOX_W = _env_int("FACEBOOK_TITLE_BOX_W", 820)
FACEBOOK_TITLE_BOX_H = _env_int("FACEBOOK_TITLE_BOX_H", 210)
FACEBOOK_TITLE_FONT_SIZE = _env_int("FACEBOOK_TITLE_FONT_SIZE", 56)

# Lightweight runtime memory. These files are intentionally runtime state and
# remain ignored by git through data/*.json.
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


# ============================================================
# 8) Gemini Translation Prompt (the instruction sent to the AI)
# ============================================================
# This prompt is carefully written to produce professional Arabic
# blog posts with HTML that matches the user's Blogger template.

BLOG_CATEGORIES = [
    "AI-Tools",
    "Cyber-Security",
    "Tech-News",
    "Apps-Programs",
]

BLOG_CATEGORY_RULES = """
   - AI-Tools:
     Reviews and explainers for tools such as ChatGPT, Gemini, Midjourney, Sora,
     and practical AI uses at work and in daily life.
   - Cyber-Security:
     Account protection, fraud detection, phone security, simple vulnerability
     explainers, and practical security advice.
   - Tech-News:
     Latest technology news, platform updates, company announcements, and simple
     fast analysis.
   - Apps-Programs:
     App reviews, best free apps, and alternatives to paid software.
"""

TRANSLATION_PROMPT = """
You are a professional Arabic technology and cybersecurity editor preparing Blogger posts.
Translate the following English article into polished Arabic.

STRICT RULES - Follow these exactly:

1. Title:
   - Create an attractive Arabic title under 80 characters.
   - Return ONLY the title on the first line, prefixed with "TITLE: "

2. Content:
   - Read and understand the full article before writing.
   - Write the title and the full body in Arabic. English is allowed only for
     product names, malware names, company names, commands, CVE IDs, and short
     technical terms that are normally written in English.
   - Rewrite the article in fluent, professional Arabic; do not translate literally.
   - Use a natural human editorial voice: clear, confident, engaging, and free
     from robotic repetition, generic filler, and awkward AI-like phrasing.
   - Explain the context, why the story matters, and the practical takeaway for
     the reader when the source material supports it.
   - If the source article is short or thin, do not produce a thin translation.
     Build a useful article from the available facts: clarify the event, explain
     the background, highlight what readers should watch for, and keep the piece
     honest about what is known.
   - Keep every technical detail accurate. Do not invent facts, numbers, quotes,
     dates, product claims, or security details.
   - The final article must be polished and free of language, grammar, and
     technical mistakes.
   - Format the body as clean HTML only (NOT markdown).
   - Organize the article with meaningful <h2> and <h3> headings when useful.

3. Blogger template components:
   - The user's Blogger template already contains all CSS.
   - DO NOT add any CSS, <style> tags, inline styles, <script> tags, or custom classes.
   - Use the template-compatible components below when they fit naturally:
     - Normal paragraph:
       <!--[ Paragraph ]-->
       <p>text_here</p>
     - Intro paragraph with drop cap (use for the opening paragraph when it reads naturally):
       <!--[ Drop Cap paragraph ]-->
       <p><span class='dropCap'>A</span>rabic text_here</p>
     - Important information:
       <div class='alert info'><b>معلومة مهمة!</b> text_here</div>
     - Warning or risk:
       <div class='alert warning'><b>تحذير!</b> text_here</div>
     - Quotation from the original article:
       <blockquote class='s1'><p>quote_here</p><span>speaker_here</span></blockquote>
     - Sequential steps:
       <ol class='steps'><li>step_1</li><li>step_2</li></ol>
     - Simple expandable explanation only when the article genuinely needs it:
       <details class='ac'><summary>question_here</summary><div><p>answer_here</p></div></details>
   - Prefer a clean article structure: short intro, clear sections, useful alerts when relevant, and a concise conclusion.
   - Do NOT invent images, tables, download boxes, buttons, YouTube embeds, or any component unsupported by the source material.

4. References and links:
   - Do NOT add any source/reference block yourself.
   - Do NOT mention that the article was copied, translated, or sourced from another article.
   - Do NOT add a "Source:" or "المصدر:" section.
   - The application will insert the article image, trusted official links, and internal related links automatically.

5. Classification and labels:
   - Classify the article automatically based on its content into exactly ONE
     main Blogger category from the following fixed categories:
""" + BLOG_CATEGORY_RULES + """
   - The first label MUST be exactly one of the category names above.
   - Do not invent, translate differently, shorten, or add emoji to the main category name.
   - After the HTML content, add one final line starting with "LABELS: "
   - Provide 3-5 relevant Arabic labels separated by commas.
   - Format the labels as: main category first, then 2-4 specific supporting labels.
   - The application will publish the article to Blogger using these labels, so
     the first label is the article's blog section.

6. Output discipline:
   - Output ONLY:
     1) the title line,
     2) the HTML article body,
     3) the labels line.
   - Do not add explanations, notes, markdown fences, or extra text.

Here is the article to translate:

---
TITLE: {title}
URL: {url}
BODY:
{body}

EDITORIAL NOTES:
{editorial_notes}
---
"""

TRANSLATION_PROMPT = """
You are a senior Arabic editor and content translation architect for a Blogger
automation pipeline. Your job is to turn the fetched article into a polished,
human-written Arabic article with zero avoidable errors.

Never mention translation, rewriting, AI, prompts, the pipeline, or the original
source. The reader must feel the article was written naturally in Arabic.

MANDATORY INTERNAL WORKFLOW:

1. Content analysis:
   - Understand the entire article before writing.
   - Detect the content type: cybersecurity, tool review, tech news, tutorial,
     breach report, malware analysis, product update, or general technology.
   - Estimate the language complexity and technical level.
   - Notice links, tools, product names, CVE IDs, commands, images, and named
     entities that must remain accurate.

2. Strategy selection:
   - Use direct human-style Arabic rewriting when the source is clean.
   - Use multi-step reasoning when the source is dense, technical, or security-heavy.
   - Simplify the meaning internally before writing when the source is messy,
     fragmented, repetitive, or unclear.
   - Do not translate word for word. Preserve meaning, not sentence shape.

3. Error prevention:
   - Fix grammar, weak phrasing, awkward literal structures, and robotic patterns.
   - Remove repeated ideas unless repetition is necessary for clarity.
   - Keep technical facts, numbers, dates, names, versions, CVE IDs, commands,
     URLs, malware names, company names, and tool names accurate.
   - Do not invent facts, claims, quotes, statistics, links, image URLs, or
     security details.
   - If the source is incomplete, write a coherent article from the available
     facts only. You may clarify context and practical meaning, but do not add
     unsupported facts.

4. Arabic writing style:
   - Write like a professional Arabic technology blogger.
   - Use a strong intro, then a clear first paragraph.
   - Make the body structured, readable, and engaging.
   - Write a compact fast-news Blogger article, usually 120-250 Arabic words.
   - If the available source material is thin, a clean article of at least 80
     Arabic words is acceptable when it honestly uses only the title, summary,
     metadata, and known source context.
   - Include a required section with this exact heading:
     <h2>ماذا يعني هذا لك</h2>
   - End with a strong conclusion that summarizes the practical meaning.
   - Prefer short paragraphs, useful headings, and practical takeaways.
   - Use bullet points only when they improve clarity.
   - End naturally with a concise conclusion or takeaway.
   - English is allowed only for product names, malware names, company names,
     commands, CVE IDs, code terms, URLs, and technical terms normally kept in
     English.

5. Human Arabic rewrite discipline:
   - Rewrite every Arabic sentence so it sounds fully natural, fluent, and
     human-written.
   - Preserve the exact meaning and content. Do not add, remove, summarize, or
     expand information beyond what the source supports.
   - Improve sentence structure, transitions, and paragraph flow without changing
     the facts.
   - Remove literal translation traces, robotic phrasing, repetition, and awkward
     wording.
   - Use clear, simple, professional Modern Standard Arabic.
   - Keep technical terms in English when they are normally written in English,
     such as Malware, API, SQL, CVE, GitHub, Docker, and command names.
   - Preserve links exactly and keep the intended paragraph/list structure.

6. Links and media:
   - Keep every original URL exactly as provided when you use it.
   - Do not alter, shorten, translate, or decorate URLs.
   - Use links only when they are relevant inside the article body.
   - Do not create a source/reference section.
   - Do not add images or <img> tags. The application inserts the main image
     automatically after the first paragraph when an image exists.

7. SEO metadata:
   - Generate a unique SEO title, meta description, and URL slug from the final
     article.
   - The SEO title must be 50-65 characters, include the main keyword naturally,
     and feel click-worthy without exaggeration.
   - Vary the SEO title style every time: question, warning, guide, news angle,
     number/list, or discovery angle. Do not repeat predictable templates.
   - The meta description must be 120-160 characters, clear, engaging, and include
     the main keyword without stuffing.
   - Avoid generic phrases like "في هذا المقال سنتحدث".
   - The slug must use Latin characters only, 3-6 words, lowercase, hyphenated,
     short, and based on the main keyword.
   - Slug must not include stop words such as and, the, of, a, an, in, on, for,
     to, with.

8. Output format:
   - First line: TITLE: followed by an attractive Arabic article title under 80 characters.
   - Then output the article body as clean HTML only, not markdown.
   - After the HTML body, output exactly these metadata lines:
     SEO_TITLE: optimized Arabic SEO title
     META_DESCRIPTION: optimized Arabic meta description
     SLUG: latin-url-slug
     LABELS: 3-5 Arabic labels separated by commas
   - Output nothing else.

CLEAN BLOGGER HTML RULES:
   - Output clean, semantic HTML ready for direct Blogger publishing.
   - Do not add CSS, <style>, inline styles, <script>, iframes, tracking code,
     or JavaScript event attributes.
   - Use ordinary HTML elements only. Do not depend on theme-specific classes.
   - Paragraphs: <p>.
   - Section headings: <h2> and <h3>.
   - Emphasis: <strong>, <b>, <em>.
   - Lists: <ul><li>...</li></ul> and <ol><li>...</li></ol>.
   - Tables are allowed whenever structured information is clearer in a table:
     <table><thead><tr><th>...</th></tr></thead><tbody><tr><td>...</td></tr></tbody></table>.
     Keep tables compact and never invent missing values.
   - Quotations may use <blockquote> when the source actually contains a quotation.
   - Expandable explanations may use <details><summary>...</summary><p>...</p></details>
     when genuinely useful.
   - Links must use ordinary anchors:
     <a href='exact_url_here' target='_blank' rel='nofollow noreferrer noopener'>link_title</a>.
   - Preserve every real URL exactly as supplied. Never invent, shorten, or alter URLs.
   - Official application/download/reference links may be presented as a normal
     descriptive <a> element; do not create fake buttons or fake links.
   - Code blocks may use <pre><code>escaped_code_here</code></pre>.
   - Inline commands, filenames, IDs, versions, and short technical tokens may use <code>.
   - <br> may be used sparingly where a real line break is needed.
   - Do not add <img> tags yourself. The application owns image selection,
     downloading, fallback generation, resizing, and insertion.
   - Do not add a source/reference block. The application appends trusted links
     automatically.
   - When the source contains structured facts such as company, location, contract
     type, salary, requirements, deadline, eligibility, or application steps,
     use a concise table or list when that improves readability.
   - Never add a table merely for decoration and never fabricate empty fields.

CLASSIFICATION:
   - Analyze the article meaning and classify it into exactly ONE main Blogger
     category from this fixed list only:
""" + BLOG_CATEGORY_RULES + """
   - The first label MUST be exactly one of the category names above.
   - Do not create new categories.
   - Do not use the original source category.
   - Do not rename, translate differently, shorten, or add emoji to the main category.
   - Choose based on the main topic, not minor mentions:
     1) AI-Tools: AI tools, ChatGPT, Gemini, Midjourney, AI work
        uses, daily-life AI uses, AI tutorials, and AI guides.
     2) Cyber-Security: hacking, breaches, leaks, Malware, phishing, exploits,
        vulnerabilities, privacy, protection tips, and security risks.
     3) Tech-News: announcements, product or platform updates, company news,
        industry updates, and general tech events.
     4) Apps-Programs: mobile/PC apps, software reviews, non-AI tools,
        alternatives, and downloads.
   - Priority rule: if the article includes hacking or security as a meaningful
     topic, choose Cyber-Security. If it is clearly about AI tools, choose
     AI-Tools. If the main angle is an announcement/update/news
     story, choose Tech-News. If it is mainly a non-AI software/tool review,
     choose Apps-Programs.
   - Add 2-4 specific supporting Arabic labels after the main category.

FINAL QUALITY CHECK BEFORE ANSWERING:
   - Human Arabic sound: yes.
   - Meaning preserved: 100%.
   - No added, removed, summarized, or expanded information.
   - AI-like or literal phrasing removed.
   - Weak sentences improved.
   - Links kept exact.
   - Technical terms kept in their proper English form when appropriate.
   - SEO title, meta description, and slug are unique in style and wording.
   - No invented information.

ARTICLE INPUT:
---
TITLE: {title}
URL: {url}

BODY:
{body}

ORIGINAL ARTICLE LINKS:
{original_links}

EDITORIAL NOTES:
{editorial_notes}
---
"""


# ============================================================
# 9) Validation Function
# ============================================================

def validate_config():
    """
    Check that all required settings are present in the .env file.
    Call this at startup to catch missing configuration early.
    Returns True if everything is OK, False if something is missing.
    """
    errors = []
    warnings = []

    supported_ai_providers = {"auto", "gemini", "groq", "openrouter", "cloudflare", "mistral", "openai"}
    supported_publish_modes = {"draft", "live"}
    has_gemini_key = bool(GEMINI_API_KEY and GEMINI_API_KEY != "your_gemini_api_key_here")
    has_openrouter_key = bool(
        OPENROUTER_API_KEY and OPENROUTER_API_KEY != "your_new_key_here"
    )
    has_openai_key = bool(OPENAI_API_KEY and OPENAI_API_KEY != "your_openai_api_key_here")
    has_groq_key = bool(GROQ_API_KEY)
    has_mistral_key = bool(MISTRAL_API_KEY)
    has_cloudflare_key = bool(CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID)

    if AI_PROVIDER not in supported_ai_providers:
        errors.append(
            "  ❌ AI_PROVIDER must be one of: auto, gemini, groq, openrouter, cloudflare, mistral, openai"
        )

    if PUBLISH_MODE not in supported_publish_modes:
        warnings.append("  WARNING: PUBLISH_MODE is not draft or live; draft mode will be used.")

    if AI_PROVIDER == "gemini" and not has_gemini_key:
        errors.append("  ❌ GEMINI_API_KEY is missing or not set in .env")

    if AI_PROVIDER == "openrouter" and not has_openrouter_key:
        errors.append("  ❌ OPENROUTER_API_KEY is missing or still set to a placeholder in .env")

    if AI_PROVIDER == "groq" and not has_groq_key:
        errors.append("  ❌ GROQ_API_KEY is missing in .env")
    if AI_PROVIDER == "cloudflare" and not has_cloudflare_key:
        errors.append("  ❌ CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID are required.")
    if AI_PROVIDER == "mistral" and not has_mistral_key:
        errors.append("  ❌ MISTRAL_API_KEY is missing in .env")
    if AI_PROVIDER == "openai" and not has_openai_key:
        errors.append("  ❌ OPENAI_API_KEY is missing or still set to a placeholder in .env")

    if AI_PROVIDER == "auto":
        available = (
            has_gemini_key
            or has_groq_key
            or has_openrouter_key
            or has_cloudflare_key
            or has_mistral_key
            or has_openai_key
        )
        if not available:
            if JOBS_MODE:
                warnings.append(
                    "  ⚠️  No AI provider key is configured; Jobs AI will defer behind circuit/backoff until a provider is available."
                )
            else:
                errors.append("  ❌ Configure at least one supported AI provider key.")
        elif sum(bool(x) for x in (
            has_gemini_key,
            has_groq_key,
            has_openrouter_key,
            has_cloudflare_key,
            has_mistral_key,
            has_openai_key,
        )) < 2:
            warnings.append("  ⚠️  Only one AI provider is configured; failover redundancy is limited.")

    if not BLOG_ID or BLOG_ID == "your_blog_id_here":
        errors.append("  ❌ BLOG_ID is missing or not set in .env")

    has_env_oauth = bool(BLOGGER_CLIENT_ID and BLOGGER_CLIENT_SECRET)
    has_blogger_auth = CREDENTIALS_FILE.exists() or has_env_oauth

    if not has_blogger_auth and not LOCAL_PUBLISH_FALLBACK:
        errors.append(f"  ❌ client_secret.json not found at: {CREDENTIALS_FILE}")
        errors.append("     → Add client_secret.json or set BLOGGER_CLIENT_ID and BLOGGER_CLIENT_SECRET in .env")

    if errors:
        print("\n" + "=" * 60)
        print("⚠️  CONFIGURATION ERRORS FOUND:")
        print("=" * 60)
        for err in errors:
            print(err)
        print("=" * 60)
        print("Please fix the errors above and run the bot again.\n")
        return False

    if warnings:
        print("\n" + "=" * 60)
        print("⚠️  CONFIGURATION WARNINGS:")
        print("=" * 60)
        for warning in warnings:
            print(warning)
        print("=" * 60)

    # Ensure required directories exist
    PUBLISHED_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTICLE_BACKLOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CRAWL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOPIC_FINGERPRINTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    FACEBOOK_IMAGE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("✅ Configuration validated successfully!")
    return True
