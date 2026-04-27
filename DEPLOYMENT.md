# GitHub Actions Deployment

This bot is configured for permanent GitHub Actions operation. The production workflow runs every 5 minutes, rotates sources with persistent state, skips duplicates/ads/affiliate/old stories, and publishes at most one live Blogger post per run.

## Required GitHub Secrets

In your private repository, open:

`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

Add these required secrets:

- `GEMINI_API_KEY`
- `OPENROUTER_API_KEY`
- `BLOGGER_BLOG_ID`
- `BLOGGER_CLIENT_SECRET_JSON`
- `BLOGGER_TOKEN_JSON`
- `FACEBOOK_PAGE_ID`
- `FACEBOOK_PAGE_ACCESS_TOKEN`

Optional Telegram alert secrets:

- `TELEGRAM_ALERTS_ENABLED`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional extra AI secret:

- `OPENAI_API_KEY`

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

GitHub can delay scheduled jobs during busy periods, so a `*/5 * * * *` workflow may not start at the exact minute every time. The workflow is still configured for automatic 5-minute operation.

## Production Defaults

The workflow creates a runtime `.env` with these production defaults:

```env
PUBLISH_MODE=live
SAFE_MODE=false
FAST_NEWS_MODE=true
CATEGORY_ROTATION_MODE=true
PROCESS_FULL_CATEGORY_PER_RUN=true
FRESH_QUEUE_MODE=false
FIRST_VALID_ARTICLE_MODE=true
RECENT_NEWS_ONLY=true
RECENT_ONLY=true
RECENT_NEWS_MAX_AGE_HOURS=6
RECENT_HOURS=6
FRESHNESS_SAFETY_MARGIN_MINUTES=10
FALLBACK_FIRST_RUN_LOOKBACK_HOURS=6
CRAWL_INTERVAL_MINUTES=5
CRAWL_OVERLAP_MINUTES=10
ALLOW_UNKNOWN_DATE_IN_FAST_MODE=false
FACEBOOK_AUTO_POST=true
LOCAL_PUBLISH_FALLBACK=false
MAX_POSTS_PER_RUN=1
MAX_ARTICLES_PER_RUN=1
MAX_SOURCES_PER_RUN=999
PUBLISH_WEAK_ARTICLES=true
ALLOW_SHORT_ARTICLES=true
MIN_ARTICLE_WORDS=80
TARGET_ARTICLE_WORDS=250
MIN_EXTRACTED_CHARS=80
SOURCE_TIMEOUT_SECONDS=12
ARTICLE_TIMEOUT_SECONDS=15
AI_TIMEOUT_SECONDS=60
MAX_AI_RETRIES=3
SOURCE_HEALTH_ENABLED=true
SOURCE_FAILURE_COOLDOWN_MINUTES=45
SKIP_ADS_AFFILIATE_SPONSORED=true
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

In this mode the bot publishes live only, never creates drafts, accepts weak or short real news, and runs category rotation. Each 5-minute run selects one category, checks every source in that category, queues valid extra candidates, and publishes at most one article.

Category rotation order:

- `Cyber-Security`
- `AI-Tools`
- `Tech-News`
- `Apps-Programs`

If the selected category has no valid fresh candidate and no queued article, the bot tries the next category once before ending the run. Blogger labels are restricted to those four English slugs.

With `RECENT_HOURS=6` and `FRESHNESS_SAFETY_MARGIN_MINUTES=10`, the bot accepts practical fast-news items up to nearly 6 hours old. It no longer blocks stories just because they are near the old 2-hour window.

The bot rejects:

- duplicate URLs and canonical URLs
- repeated topics within the topic cooldown window
- sponsored, affiliate, coupon, daily-deal, discount, and promotional pages
- articles older than the configured recent window

The bot accepts:

- short real news
- weak extraction when title plus summary/metadata is available
- RSS-summary-only stories
- normal business or partnership news that uses words like "deal" but is not promotional
- Blogger posts without images when no valid image exists

## AI Fallback

Set `AI_PROVIDER=auto` with Gemini and OpenRouter secrets. The production sequence is:

1. Gemini (`GEMINI_MODEL=gemini-2.5-flash`)
2. OpenRouter (`OPENROUTER_MODEL=openrouter/auto`)
3. Gemini retry
4. Basic safe Arabic HTML fallback from title and summary when both providers fail

The fallback does not invent sensitive technical details and only uses available title, summary, metadata, and source context.

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

The command verifies required environment variable names, Blogger JSON availability, GitHub Actions workflow presence, the `*/5 * * * *` schedule, live publish settings, the 6-hour recent window, the freshness safety margin, and Facebook/Telegram safety flags. It never prints secret values.

You can verify production activity from:

- GitHub repository `Actions` tab
- Telegram success/skip/failure reports
- Blogger post URLs in the workflow logs
- Facebook status in Telegram and the queue record

On GitHub Actions, `.env` is generated at runtime from GitHub Secrets. A committed `.env` file is not required and must not be committed.

## Reset Runtime State

Use the built-in reset command when you need a clean production restart:

```bash
python main.py reset-state
```

This clears the runtime queue, crawl timestamps, published-history files, topic fingerprints, source health cooldowns, failed queue state, and the cached auto-cycle run log without touching code, tests, workflows, or secrets.

## Security Warnings

- Never commit `.env`.
- Never commit `client_secret.json`.
- Never commit `data/token.json`.
- Never commit copied Facebook or Google token JSON.
- Never print API keys or tokens in logs.
- Use GitHub Actions Secrets only.
- Keep the repository private.
