# Cybero Plus Jobs v2

إعادة بناء نظيفة ومستقلة لبوت الوظائف، مبنية على منطق البوت الإخباري العامل ولكن مع فصل كل مرحلة:

1. Source collection
2. Normalization
3. Morocco eligibility verification
4. Deduplication
5. Quality scoring
6. Best-candidate selection
7. Arabic article generation
8. SEO title / meta / slug
9. Blogger publishing
10. Facebook copy
11. Image resolution / branded fallback card
12. Facebook publishing
13. First comment: Blogger + WhatsApp
14. Persistent state

## Philosophy
- Quality over quantity.
- Maximum 1 live article per day by default.
- Official source first.
- Unknown eligibility is held, not guessed.
- No live publishing until JOBS_LIVE_PUBLISH=true and JOBS_LIVE_FACEBOOK=true.

## Current stage
Core content/AI/quality/state architecture is active.
Source-specific collectors, Blogger adapter, Facebook adapter and image adapter are added in the next integration stage.


## Agreed operating policy

The bot is quality-first, not quota-first.

- Standard live limit: 1 strong job article per Morocco calendar day.
- A candidate must score at least 75/100 and pass Morocco eligibility verification.
- One extra urgent override per day is allowed only for a verified official opportunity:
  - deadline within 48 hours, or
  - 100+ positions in a large official campaign with a near deadline.
- If no candidate passes the gate, publishing is skipped for that day.
- Blogger is published when the selected job is ready.
- Facebook is scheduled separately; urgent verified jobs can publish immediately.
- Initial Facebook timing alternates morning/evening windows so page-specific data can replace generic timing after the learning period.
- Expired jobs are kept useful as pages, but the apply CTA must be disabled, the visible status changed to "انتهى أجل الترشيح", JobPosting validThrough kept accurate, and Google notified of the update when Indexing API is enabled.

### Quality score (100)

- Official source: 25
- Fresh under 24h: 15
- High-priority source: 10
- Large hiring (10+ positions): 10
- Clear deadline: 10
- Clear location: 5
- Clear diploma: 5
- Valid application URL: 10
- Salary listed: 5
- Entry-level/student friendly: 5

Eligibility and a valid application URL remain hard gates even if the numeric score is high.

## Google Jobs

Each published article is one job page. Jobs v2 prepares JobPosting JSON-LD only from verified facts. Google Indexing API support is present but disabled by default until the service account is configured.
