# GitHub Actions Deployment

This bot can run from a private GitHub repository with GitHub Actions. Keep the default mode safe: draft-only Blogger publishing and Facebook disabled.

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
5. The workflow `.github/workflows/auto-cycle.yml` runs every 60 minutes.
6. You can also run it manually from `Actions` -> `Safe Auto Cycle` -> `Run workflow`.

## Default Safe Mode

The workflow creates a runtime `.env` with these safe defaults:

```env
PUBLISH_MODE=draft
FACEBOOK_AUTO_POST=false
SAFE_CYCLE_MAX_ARTICLES=1
SAFE_CYCLE_DRAFT_ONLY=true
MAX_DRAFTS_PER_DAY=10
MIN_MINUTES_BETWEEN_DRAFTS=30
MAX_LIVE_POSTS_PER_DAY=5
MIN_MINUTES_BETWEEN_LIVE_POSTS=60
MAX_FACEBOOK_POSTS_PER_DAY=5
MIN_MINUTES_BETWEEN_FACEBOOK_POSTS=60
TELEGRAM_ALERTS_ENABLED=false
```

In this mode the bot creates or updates Blogger drafts only. It does not publish live and does not post to Facebook.

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

## Switch To Live Blogger Safely

To publish live from GitHub Actions, edit the workflow defaults intentionally:

```env
PUBLISH_MODE=live
SAFE_CYCLE_MAX_ARTICLES=1
MAX_LIVE_POSTS_PER_DAY=5
MIN_MINUTES_BETWEEN_LIVE_POSTS=60
```

Before enabling live mode, run:

```bash
python main.py deployment-check
python main.py publish-status
```

Keep live limits enabled. Start with low values while testing.

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

The Blogger link is posted only in the first Facebook comment, using the real Blogger API URL. The bot must never guess URLs from `seo_slug`.

## Local Deployment Check

Run:

```bash
python main.py deployment-check
```

The command verifies required environment variable names, safe publish settings, Blogger JSON availability, and Facebook safety flags. It never prints secret values.

On GitHub Actions, `.env` is generated at runtime from GitHub Secrets. A committed `.env` file is not required and must not be committed.

## Manual Workflow Run

1. Go to the repository on GitHub.
2. Open `Actions`.
3. Select `Safe Auto Cycle`.
4. Click `Run workflow`.
5. Choose the branch.
6. Click `Run workflow`.

## Security Warnings

- Never commit `.env`.
- Never commit `client_secret.json`.
- Never commit `data/token.json`.
- Never commit copied Facebook or Google token JSON.
- Never print API keys or tokens in logs.
- Use GitHub Actions Secrets only.
- Keep the repository private.
