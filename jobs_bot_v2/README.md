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
