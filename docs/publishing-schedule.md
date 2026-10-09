# CyberOPlus Jobs: 7-day publishing pilot

Updated: 2026-10-09. All hours below refer to **Africa/Casablanca**, including
local timezone changes. These are experimental editorial choices, not a
claim about the site's real audience engagement, which has not been measured.

## Two independent clocks

**Job discovery, evidence extraction, Blogger:** continuous GitHub Actions
production loop, with a watchdog every six minutes. A verified eligible
job can go to Blogger at any hour once the anti-spam interval has elapsed.
No artificial morning-only/office-hours rule delays a new vacancy. Blogger
pilot limit starts at **8** per day and can grow gradually to **12** after
healthy production days. Weekend editorial ceiling: **8**. Minimum Blogger
spacing: **20 minutes**. These are upper bounds, not publication quotas.
Zero valid jobs = zero published articles. A closing-soon vacancy gets the
existing urgent priority, but never skips source, diploma, date, or apply-link
verification.

**Facebook:** independent pending queue follows Blogger, with local weekly
pilot windows, a hard **4 posts/day on weekdays**, **2/day on weekends**, and
**70 minutes minimum** between Facebook uploads. Each social window stays open
50 minutes after its stated start. Publish at most one social upload per run.
Urgent near-deadline vacancies may bypass the *time-of-day window*, but may
not bypass daily totals, spacing, or job quality/duplicate checks.
A candidate delayed until the next window remains pending. Expired vacancies
are removed from Facebook pending, not recycled as stale jobs.

### Pilot social calendar (Morocco time)

| Day | Facebook posting-window starts | Maximum posts |
| --- | --- | ---: |
| Monday | 09:00, 12:30, 19:00 | 3 |
| Tuesday | 09:00, 13:00, 18:30, 20:30 | 4 |
| Wednesday | 09:00, 13:00, 18:30, 20:30 | 4 |
| Thursday | 09:00, 13:00, 18:30, 20:30 | 4 |
| Friday | 09:30, 16:00, 19:00 | 3 |
| Saturday | 10:00, 18:00 | 2 |
| Sunday | 09:30, 18:00 | 2 |

Experimental rationale: Facebook 2026 industry reports disagree. Buffer's
14-million-post analysis favors weekday mornings, especially Thursday 09:00,
while Sprout Social emphasizes Tuesday/Wednesday afternoon-to-evening.
A split-morning/evening calendar tests these patterns without claiming they
describe CyberOPlus's actual Morocco audience. After 2-4 weeks, inspect
Meta Business Suite **Active times** and engagement/reach/click performance
for this particular Page. Change one variable at a time. Blogger freshness
and direct candidate access always outrank social posting times.

References:
- https://buffer.com/resources/best-time-to-post-on-facebook/
- https://sproutsocial.com/insights/best-times-to-post-on-facebook/

## Non-negotiable publication gates

1. Only sources expressly approved in `sources.json`. Three verified Emploi Public
   sections (public institutions, state services, local authorities) are enabled.
   The local-authority section passed the 2026-10-09 GitHub runner listing/detail smoke; every individual job still needs proof of an application channel and diploma. Unverified other sources remain disabled. ANAPEC stays preserved but disabled while
   GitHub runner/direct-apply checks do not pass.
2. Newly announced positions have a **verified publication time within
   24 hours**. Unverified/expired or duplicated vacancies are not published.
3. Verified **open application closing deadline** and **qualification strictly
   below Bac+3** (Bac, Bac+1, Bac+2 or the evidenced equivalent).
   Unknown or mixed higher-level requirements do not pass.
4. A specific apply form/official PDF-prescribed platform or official
   submission email must be shown as the actionable link. Never send candidates
   through source portals, registration detours, or inferred links.
5. Generate original **Arabic MSA articles using an available AI provider**.
   If AI is unavailable, defer; do not publish templated substitute text.
   Use English-only URL slugs. Do not add attribution to aggregator sources.
   Official PDFs may be included and are rendered as sequential images.
6. Blogger first. Facebook delivery (and first comment) is independently
   retried, without creating duplicate social uploads or blocking Blogger.

## Operations

- The `Jobs Auto Cycle` workflow self-dispatches; the watchdog revives it
  after failures. Production concurrency prevents overlapping simultaneous
  publishers, but GitHub-hosted infrastructure and third-party services can
  delay the next run.
- Runtime state, source/candidate backoff, publication history and pending
  social work are persisted. Historical published posts are not deleted.
- Each workflow run summary explicitly distinguishes a functioning crawler
  from **having zero verified jobs eligible to publish**. No health counter is
  evidence of a real successful publication without a confirmed Blogger URL.
- Production compilation is mandatory. Full unit/regression suite runs for
  PRs before rollout. Monitor real Blogger/Facebook confirmations separately.
- **Weekly scheduling pilot feature flag:**
  `JOBS_WEEKLY_FACEBOOK_SCHEDULE_ENABLED=true` in production; when absent,
  the prior immediate Facebook behavior remains for local/tests.
- **Rollback:** set that flag to `false` and restore original production
  daily limits if real audience results or new regressions justify rollback.
  Keep all publication verification gates during any rollback.
