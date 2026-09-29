# GitHub Actions Deployment

This repository is designed for unattended GitHub Actions operation. The Jobs
workflow checks at minutes 07, 22, 37 and 52 of every hour. A scheduled check
does not mean a post is created: Blogger caps, Facebook slots, duplicate
protection, job freshness and delivery safety decide what is allowed.

## Required GitHub Secrets

Configure these in repository Actions secrets:

- `GEMINI_API_KEY` or `OPENROUTER_API_KEY`
- `BLOGGER_BLOG_ID`
- `BLOGGER_CLIENT_SECRET_JSON`
- `BLOGGER_TOKEN_JSON`
- `FACEBOOK_PAGE_ID`
- `FACEBOOK_PAGE_ACCESS_TOKEN`

Never commit OAuth files, access tokens, `.env`, or copied secret JSON.

## Jobs runtime

The workflow creates its runtime environment only inside the GitHub runner. The
important publishing controls are:

```env
JOBS_MODE=true
JOBS_TIMEZONE=Africa/Casablanca
PUBLISH_MODE=live
SAFE_MODE=false
MAX_POSTS_PER_RUN=1
MAX_ARTICLES_PER_RUN=1
SAFE_CYCLE_MAX_ARTICLES=1

FACEBOOK_AUTO_POST=true
META_GRAPH_API_VERSION=v26.0
FACEBOOK_LINK_MODE=comment
MAX_FACEBOOK_POSTS_PER_DAY=2
FACEBOOK_HARD_MAX_POSTS_PER_DAY=3
FACEBOOK_SAFETY_MIN_INTERVAL_MINUTES=45
```

The normal Facebook target is at most two Page posts per local day. The hard
ceiling is three, including urgent overrides. Even urgent posts must respect the
independent safety interval, so a bad environment value cannot turn the bot
into a rapid-fire publisher.

The Page schedule and Morocco timezone policy live in
`docs/publishing-schedule.md`.

## Facebook delivery safety

A Facebook post is attempted only for a successfully published Blogger item
with a real permalink. Jobs publishing always calls Facebook with posting
limits enabled.

The bot stores the Facebook post ID before attempting the first comment. If the
comment fails with a definite API rejection, only the comment may be retried;
the photo is not reposted.

Network timeouts, connection failures, HTTP 408, and server-side 5xx responses
are different: the remote outcome may be unknown. Those attempts are marked
`delivery_uncertain` (or `posted_comment_uncertain`) and are not blindly
retried. This prevents a timeout after a successful remote publish from
creating a duplicate on the next run.

Manual `facebook-backfill` uses the same schedule, daily cap and interval
guardrails and can create at most one new Page post per invocation. It is not a
bulk-publish bypass.

## Facebook visuals

The four runtime templates are under `assets/facebook/`. Their meaning and
selection rules are defined in `job_visual_policy.py`.

Template selection is semantic first:

- new vacancy -> new template
- verified deadline within 72 hours -> deadline template
- candidate/results notices -> alert template
- verified direct-apply vacancy -> new/apply rotation

Once selected, the template key is pinned to the article before upload. Any
retry therefore keeps the same visual instead of changing it randomly.

## Checks

Useful read-only/status commands:

```bash
python main.py deployment-check
python main.py publish-status
python main.py facebook-status
python main.py facebook-limits-status
python main.py facebook-preview
```

Core regressions run in `.github/workflows/jobs-tests.yml`, including visual
selection, Facebook pacing and duplicate-delivery protections.

## Runtime state

Git-tracked runtime state is persisted after scheduled jobs, with a temporary
workflow artifact kept as a recovery checkpoint. The runner removes temporary
credential files before state persistence.

Concurrent code edits can make a runtime-state push lose a Git race. The
workflow retries/rebases state persistence; such a Git race is separate from a
Facebook delivery failure.

## Security

- Keep credentials only in GitHub Actions secrets.
- Do not commit `.env`, OAuth client files, token files, or copied API output.
- Do not print access tokens in logs.
- Keep Graph API versions configurable through `META_GRAPH_API_VERSION`.
- Review Page status and Insights before increasing publishing volume.
