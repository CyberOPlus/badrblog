from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
PROMPTS_DIR = ROOT / "prompts"
ASSETS_DIR = ROOT / "assets"
DATA_DIR = ROOT / "data"
STATE_PATH = DATA_DIR / "state.json"
MEMORY_DIR = DATA_DIR / "memory"

JOBS_SOURCE_REGISTRY = REPO_ROOT / "jobs_sources.json"

# Quality over quantity: never publish more than one article per calendar day.
DAILY_PUBLISH_LIMIT = int(os.getenv("JOBS_DAILY_PUBLISH_LIMIT", "1"))
MIN_SELECTION_SCORE = int(os.getenv("JOBS_MIN_SELECTION_SCORE", "75"))
URGENT_EXTRA_DAILY_LIMIT = int(os.getenv("JOBS_URGENT_EXTRA_DAILY_LIMIT", "1"))
MOROCCO_TIMEZONE = os.getenv("JOBS_TIMEZONE", "Africa/Casablanca").strip() or "Africa/Casablanca"
GOOGLE_INDEXING_ENABLED = os.getenv("JOBS_GOOGLE_INDEXING_ENABLED", "false").strip().lower() in {"1","true","yes","on"}
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

# AI provider order mirrors the working Cybero Plus news bot.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

GEMINI_MODELS = tuple(
    x.strip() for x in os.getenv(
        "JOBS_GEMINI_MODELS",
        "gemini-3.7-flash,gemini-3.5-flash"
    ).split(",") if x.strip()
)
GROQ_MODELS = tuple(
    x.strip() for x in os.getenv(
        "JOBS_GROQ_MODELS",
        "qwen/qwen3.8-27b,openai/gpt-oss-120b,openai/gpt-oss-20b"
    ).split(",") if x.strip()
)
OPENROUTER_MODELS = tuple(
    x.strip() for x in os.getenv("JOBS_OPENROUTER_MODELS", "openrouter/free").split(",") if x.strip()
)

AI_TIMEOUT_SECONDS = int(os.getenv("JOBS_AI_TIMEOUT_SECONDS", "60"))

# Publishing targets.
# cyberopluss.blogspot.com is a disposable test target. The expected host is
# checked before downstream promotion so a test run cannot silently target the
# real Cybero Plus site.
TEST_BLOG_URL = os.getenv(
    "JOBS_TEST_BLOG_URL",
    "https://cyberopluss.blogspot.com/",
).strip()
EXPECTED_BLOG_HOST = os.getenv(
    "JOBS_EXPECTED_BLOG_HOST",
    "cyberopluss.blogspot.com",
).strip().casefold()
BLOG_ID = os.getenv("BLOGGER_BLOG_ID", os.getenv("BLOG_ID", "")).strip()
FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "").strip()
FACEBOOK_PAGE_ACCESS_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()

WHATSAPP_CHANNEL_URL = os.getenv(
    "WHATSAPP_CHANNEL_URL",
    "https://whatsapp.com/channel/0029VaDv5d05vKADlup5761h",
).strip()

BLOG_LABEL = os.getenv("JOBS_BLOG_LABEL", "jobs").strip() or "jobs"
SITE_NAME = os.getenv("JOBS_SITE_NAME", "Cybero Plus").strip() or "Cybero Plus"

# Safety: v2 remains preview-only until explicitly enabled.
LIVE_PUBLISH = os.getenv("JOBS_LIVE_PUBLISH", "false").strip().lower() in {"1","true","yes","on"}
LIVE_FACEBOOK = os.getenv("JOBS_LIVE_FACEBOOK", "false").strip().lower() in {"1","true","yes","on"}
