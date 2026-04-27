# GitHub Actions Deployment

This bot is configured for permanent GitHub Actions operation. The production workflow runs every 5 minutes, scans all enabled sources, keeps per-source crawl state, queues every fresh article, and publishes only one live Blogger post per run.

## Required GitHub Secrets

In your private repository, open:

`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

Add these required secrets:

- `GEMINI_API_KEY`
- `OPENROUTER_API_KEY`
- `OPENAI_API_KEY`
- `BLOGGER_BLOG_ID`
- `BLOGGER_CLIENT_SECRET_JSON`
- `BLOGGER_TOKEN_JSON`
- `FACEBOOK_PAGE_ID`
- `FACEBOOK_PAGE_ACCESS_TOKEN`

Optional Telegram alert secrets:

- `TELEGRAM_ALERTS_ENABLED`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Do not paste these values into workflow logs, issues, commits, or README files.

## Blogger OAuth Secrets

`BLOGGER_CLIENT_SECRET_JSON` should contain the full OAuth client JSON from Google Cloud.

`BLOGGER_TOKEN_JSON` should contain the full authorized token JSON from `data/token.json` after you have authenticated locally once.

Never upload `client_secret.json`, `data/token.json`, or `.env` directly to GitHub.

## Enable GitHub Actions

1. Push this repository to a private GitHub repository.
2. Add all required repository secrets.
3. Open the `Actions` tab.
4. Enable workflows if GitHub asks for confirmation.
5. The workflow `.github/workflows/auto-cycle.yml` runs automatically every 5 minutes.
6. Scheduled runs continue even when your computer is off.

## Production Defaults

The workflow creates a runtime `.env` with these production defaults:

```env
PUBLISH_MODE=live
SAFE_MODE=false
FAST_NEWS_MODE=true
FRESH_QUEUE_MODE=true
FIRST_VALID_ARTICLE_MODE=false
RECENT_NEWS_ONLY=true
RECENT_NEWS_MAX_AGE_HOURS=2
FALLBACK_FIRST_RUN_LOOKBACK_HOURS=2
CRAWL_INTERVAL_MINUTES=5
CRAWL_OVERLAP_MINUTES=10
ALLOW_UNKNOWN_DATE_IN_FAST_MODE=false
FACEBOOK_AUTO_POST=true
LOCAL_PUBLISH_FALLBACK=false
MAX_POSTS_PER_RUN=1
MAX_ARTICLES_PER_RUN=1
SAFE_CYCLE_MAX_ARTICLES=1
SAFE_CYCLE_DRAFT_ONLY=false
MAX_DRAFTS_PER_DAY=10
MIN_MINUTES_BETWEEN_DRAFTS=30
MAX_LIVE_POSTS_PER_DAY=288
MIN_MINUTES_BETWEEN_LIVE_POSTS=5
MAX_FACEBOOK_POSTS_PER_DAY=144
MIN_MINUTES_BETWEEN_FACEBOOK_POSTS=10
TELEGRAM_ALERTS_ENABLED=true
```

In this mode the bot publishes live only, never creates drafts, rejects old or undated fast-news articles, and keeps the remaining queued articles for later scheduled runs.

## Enable Telegram Alerts Safely

Telegram alerts are disabled by default. To enable auto-cycle success, blocked, and failure notifications, add these GitHub Actions secrets:

```env
TELEGRAM_ALERTS_ENABLED=true
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Check local configuration without sending a message:

```bash
python main.py alert-status
```

Send a test alert only after `TELEGRAM_ALERTS_ENABLED=true` is configured:

```bash
python main.py test-alert
```

Never paste the bot token into logs, issues, commits, or chat messages.

## Deployment Checks

Before or after deployment, run:

```bash
python main.py deployment-check
python main.py publish-status
```

## Enable Facebook Safely

Facebook posting only runs after a successful live Blogger publish with a real `blogger_post_url`.

To enable it, set:

```env
FACEBOOK_AUTO_POST=true
MAX_FACEBOOK_POSTS_PER_DAY=5
MIN_MINUTES_BETWEEN_FACEBOOK_POSTS=60
```

Use the preview and status commands before posting:

```bash
python main.py facebook-status
python main.py facebook-limits-status
python main.py facebook-preview
```

If Blogger succeeds but Facebook fails, the workflow still succeeds and records the Facebook problem as a warning in the run summary and Telegram report.

## Local Deployment Check

Run:

```bash
python main.py deployment-check
```

The command verifies required environment variable names, safe publish settings, Blogger JSON availability, and Facebook safety flags. It never prints secret values.

On GitHub Actions, `.env` is generated at runtime from GitHub Secrets. A committed `.env` file is not required and must not be committed.

## Reset Runtime State

Use the built-in reset command when you need a clean production restart:

```bash
python main.py reset-state
```

This clears the runtime queue, crawl timestamps, published-history files, topic fingerprints, failed queue state, and the cached auto-cycle run log without touching code, tests, workflows, or secrets.

## Security Warnings

- Never commit `.env`.
- Never commit `client_secret.json`.
- Never commit `data/token.json`.
- Never commit copied Facebook or Google token JSON.
- Never print API keys or tokens in logs.
- Use GitHub Actions Secrets only.
- Keep the repository private.
