# Jobs production cycle

Updated 2026-10-01. Times and daily counters use Africa/Casablanca.

Production checks at minutes 01, 07, 13, 19, 25, 31, 37, 43, 49 and 55. Each production run requests its successor after a two-minute pause, unless another cycle is already active. The watchdog checks every six minutes and also wakes on completed production runs. A successful Jobs Core Tests run on main also wakes production; failed deployment tests do not wake it. The shared production lock prevents overlapping publishers. Full regression checks are optional on manual dispatch and run in a temporary copy, as do production smoke checks.

Only a verified official publication time within the last 12 hours qualifies a new job. Discovery time is not publication time. Known stale rows do not consume enrichment slots. Unknown dates require detail-page evidence before publication. Fresh cyber, IT, developer and student/internship opportunities take priority while valid general jobs remain eligible.

Every source refreshes its listing head even when a backlog cursor exists. Durable source identities filter previously discovered URLs across generic HTML/feed and ATS adapters. Job identity and campaign memory prevent duplicate articles while preserving specific application links.

Every successful Blogger article enters the Facebook pending queue. Production enables the Jobs-only follow-article policy: the new article gets its own immediate attempt, with a five-minute minimum interval and a daily ceiling of 240 matching Blogger capacity. Pacing, API failures and ambiguous delivery preserve pending/retry protections. General-news modes and their separate Facebook schedule have been removed. Pending social delivery and comments are retried even if discovery or AI fails; social errors do not fail Blogger work. Expired vacancies leave only the social queue. The capacity settings are not a guarantee about platform enforcement.

Verified employer logos are recovered before rendering and preserved in campaign memory. The four background templates follow verified job facts and visual rotation state. Official PDFs are rendered into sequential JPEG images inside the article, with a per-pass budget of six documents and 48 new pages; subsequent passes resume until all documents/pages are rendered, with original download links retained. Failed visual work is retried against the same Blogger post.

Runtime state is persisted to main with push-race recovery and a three-day failure artifact. Large queue inspection falls back to Git blob content when the Contents API omits files larger than 1 MB. Inspection failures request recovery instead of disabling the watchdog. The daily heartbeat keeps the schedule active during quiet periods. GitHub runners, external services and credentials can still cause delays; the watchdog retries recoverable failures. The PC and this chat do not need to stay open.
