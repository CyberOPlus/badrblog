# GitHub Actions deployment

This repository is exclusively the Jobs publisher. `.github/workflows/auto-cycle.yml` runs only on `main`, uses one shared production lock and persists state even if a stage fails. Keep `sources.json` as the single source configuration.

## Credentials and target

Use repository Actions secrets for `BLOGGER_BLOG_ID`, `BLOGGER_CLIENT_SECRET_JSON`, `BLOGGER_TOKEN_JSON`, `FACEBOOK_PAGE_ID`, `FACEBOOK_PAGE_ACCESS_TOKEN`, and at least one configured AI provider. Auto mode rotates among Gemini, Groq, OpenRouter, Cloudflare and Mistral when configured. See the workflow for provider secret names.

The workflow maps `BLOGGER_BLOG_ID` to runtime `BLOG_ID`. `JOBS_EXPECTED_BLOG_HOST` must match the intended blog; the publisher checks the target before writing. This migration does not change the selected blog or Page. OAuth must be initialized separately; a GitHub runner with unusable credentials returns a retryable failure instead of opening an interactive browser. Local files never count as published posts.

Never commit `.env`, `client_secret.json`, `data/token.json` or access tokens. The workflow removes temporary credentials before saving state. Status commands and logs redact secrets.

## Publishing behavior

Production checks ten times per hour, chains completed cycles, and has a separate watchdog. GitHub scheduling and external services may delay execution; this is recovery automation, not a guarantee of uninterrupted service. No PC or chat session needs to stay open.

Each cycle handles at most one new article. Verified official publication age is capped at 12 hours, including the final Blogger write. Discovery time cannot substitute for publication time. Old or unknown dates are never made fresh by rediscovery. Job and campaign memory prevent duplicate articles while preserving the specific application URL.

Every successful Blogger article enters the Facebook queue. New articles receive an immediate attempt when limits permit, with a minimum interval of five minutes and a configurable daily ceiling capped at 240. Pending Page posts/comments retry even when a later discovery or AI stage fails. These caps are capacity limits, not posting targets.

A Facebook photo ID is saved before the first comment. Definite comment failures retry only the comment. Timeouts and ambiguous server failures are recorded as uncertain delivery and are not blindly reposted. `facebook-backfill` uses the same limits and creates at most one new feed post per invocation.

## Visuals and PDFs

The four backgrounds in `assets/facebook/` follow verified job facts and visual rotation state. A selected template is pinned before upload so retries preserve it. Verified employer logos are retained in job memory.

Official PDFs render to images inside the same Blogger article, keeping original download links. Six documents and 48 **new** pages are a per-pass work budget, not a final truncation limit. Later passes resume remaining work and retry failures against the existing post. A failed optional image does not discard a verified job or create a second article.

## Checks and recovery

```bash
python main.py deployment-check
python main.py sources-check
python main.py publish-status
python main.py facebook-limits-status
python -m unittest discover -s tests -v
```

The full Jobs suite runs in `.github/workflows/jobs-tests.yml`. Runtime smoke checks use an isolated copy. Durable state is saved with atomic writes and persisted to `main`; Git race recovery merges queue snapshots instead of overwriting unseen remote changes. The workflow retains a short-lived recovery artifact on persistence failure.

Do not delete `jobs_article_queue.json`, `data/job_memory/`, discovery cursors or delivery IDs to force a rerun: they prevent duplicates. A cycle with no verified fresh job is a healthy skip. Provider outages defer work with backoff and allow later cycles to recover. See [cycle policy](docs/publishing-schedule.md).
