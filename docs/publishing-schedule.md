# Morocco publishing schedule

Updated 2026-09-28. These are research-informed starting times, not measured
optimal times for this Page. No Page Insights dataset was available during this
change. Do not describe them as guaranteed best hours for all Moroccans.

All slots use `Africa/Casablanca`, every week of every year. The IANA timezone
handles Morocco clock changes; no fixed UTC offset or guessed Ramadan dates.

| Day | Facebook local times |
| --- | --- |
| Monday | 12:30, 19:30 |
| Tuesday | 12:30, 19:00 |
| Wednesday | 12:30, 19:00 |
| Thursday | 12:30, 20:00 |
| Friday | 10:30, 19:30 |
| Saturday | 11:00 |
| Sunday | 19:00 |

The midday/early-evening weekday slots use the global engagement benchmark.
Friday and weekend slots are conservative editorial choices for testing, not
Morocco-specific measured peaks. Weekend volume is lower. Existing Blogger
day/month caps remain in force; verified time-sensitive jobs need not wait for
a Facebook slot. Existing bounded urgent-job policy remains in force.

The workflow checks at minutes 07, 22, 37 and 52. Facebook accepts a delayed
check up to 50 minutes after a slot and permits one post per consumed slot.
Independent safety rails also enforce at least 45 minutes between Page posts
and a hard ceiling of three Page posts per local day, including urgent
overrides. The normal configured target remains two posts per day.

An independent backlog pass retries a published article's Facebook delivery
even when there is no new Blogger article or the AI fails. Manual backfill uses
the same limits and can create at most one new feed post per invocation.
Network/5xx outcomes are marked delivery-uncertain instead of being blindly
retried, preventing duplicate photos or duplicate first comments.

Git-tracked state is authoritative. A queued run checks out current main rather
than restoring an older state cache. Each cycle saves state and retains a
seven-day recovery artifact without credentials. Real AI/publication failures
are visible as failed workflow runs; the next scheduled run still executes.

## Evidence and limits

- Sprout Social, March 2026, global sample of nearly 2 billion engagements and
  307,000 profiles: https://sproutsocial.com/insights/best-times-to-post-on-facebook/
  Stronger Monday–Thursday engagement; Tuesday/Wednesday midday to evening.
- Meta recommends using the Page's own Active times and Insights:
  https://www.facebook.com/business/help/942827662903020
  https://www.facebook.com/business/help/397273916756139
- Ipsos Morocco Ramadan 2026 documents seasonal changes but does not establish
  the best publishing hour for this Page:
  https://www.ipsos.com/sites/default/files/ct/news/documents/2026-02/The%202026%20Ramadan%20Handbook%20-%20Morocco%20Edition.pdf
- GitHub says scheduled jobs may be delayed/dropped and inactive public repos
  may have schedules disabled after 60 days. Normal saved runtime activity
  keeps this repository active; this is not a promise of zero outages:
  https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule

Use actual Page insights before claiming seasonal optimization or changing
Ramadan/holiday times. The PC and this chat do not need to stay open.
