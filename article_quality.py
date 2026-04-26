# ============================================================
# article_quality.py - Candidate Scoring & Selection
# ============================================================

import re
from collections import defaultdict
from urllib.parse import urlparse

from config import (
    MAX_ARTICLES_PER_SOURCE_PER_RUN,
    MIN_ARTICLE_BODY_CHARS,
    MIN_ARTICLE_WORDS,
)


SOURCE_REPUTATION = {
    "bleepingcomputer.com": 36,
    "thehackernews.com": 32,
    "darkreading.com": 34,
    "securityweek.com": 34,
    "krebsonsecurity.com": 38,
    "therecord.media": 35,
    "gbhackers.com": 24,
    "cybersecuritynews.com": 24,
    "sentinelone.com": 36,
    "unit42.paloaltonetworks.com": 38,
    "blog.talosintelligence.com": 38,
    "googleprojectzero.blogspot.com": 40,
    "malwarebytes.com": 32,
    "isc.sans.edu": 38,
    "troyhunt.com": 35,
    "exploit-db.com": 30,
    "cisa.gov": 42,
    "mandiant.com": 38,
    "heimdalsecurity.com": 28,
    "grahamcluley.com": 30,
    "darkwebinformer.com": 24,
}

WEAK_TITLE_PATTERNS = (
    "daily dose",
    "weekly recap",
    "week in review",
    "roundup",
    "newsletter",
    "podcast",
    "webinar",
    "sponsored",
    "press release",
    "in brief",
)


def get_candidate_fetch_limit(publish_limit, multiplier, minimum):
    """
    Return how many articles to collect before quality filtering.
    """
    if not publish_limit or publish_limit <= 0:
        return None

    return max(minimum, publish_limit * max(1, multiplier))


def _hostname(url):
    host = urlparse(url or "").netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _source_score(article):
    host = _hostname(article.get("source_url") or article.get("url"))
    if host in SOURCE_REPUTATION:
        return SOURCE_REPUTATION[host]

    for source_host, score in SOURCE_REPUTATION.items():
        if host.endswith(f".{source_host}"):
            return score

    return 20


def _word_count(text):
    return len(re.findall(r"\b[\w'-]+\b", text or "", flags=re.UNICODE))


def _title_has_weak_pattern(title):
    title_lower = (title or "").lower()
    return any(pattern in title_lower for pattern in WEAK_TITLE_PATTERNS)


def evaluate_article(article):
    """
    Score one fully fetched article and explain how it should be handled.
    """
    title = article.get("title", "")
    body = re.sub(r"\s+", " ", article.get("body", "")).strip()
    body_chars = len(body)
    words = _word_count(body)
    trusted_links = len(article.get("trusted_sources") or [])
    has_image = bool(article.get("image"))

    reasons = []
    needs_enrichment = False

    if body_chars < MIN_ARTICLE_BODY_CHARS:
        needs_enrichment = True
        reasons.append(f"too short ({body_chars} chars)")

    if words < MIN_ARTICLE_WORDS:
        needs_enrichment = True
        reasons.append(f"too few words ({words})")

    if _title_has_weak_pattern(title):
        reasons.append("low-value roundup/newsletter title")
        needs_enrichment = True

    score = 0
    score += _source_score(article)
    score += min(35, body_chars // 220)
    score += min(18, trusted_links * 6)
    score += 6 if has_image else 0
    score += 8 if 45 <= len(title) <= 140 else 3
    score -= 20 if _title_has_weak_pattern(title) else 0
    score -= 12 if needs_enrichment else 0

    if not needs_enrichment and not reasons:
        reasons.append("strong enough for editorial processing")

    if needs_enrichment:
        editorial_notes = (
            "The source article is short, thin, or roundup-like. Do not skip it. "
            "Turn it into a worthwhile human article by clearly explaining the core event, "
            "context, why it matters, and practical reader takeaways, while using only the "
            "facts present in the source material and the provided trusted links. Do not pad "
            "with generic filler and do not invent unsupported details."
        )
    else:
        editorial_notes = (
            "The source article has enough substance. Rewrite it as a polished human article, "
            "preserving technical accuracy and making the value clear to the reader."
        )

    article["editorial_notes"] = editorial_notes
    article["quality"] = {
        "score": score,
        "body_chars": body_chars,
        "words": words,
        "trusted_links": trusted_links,
        "source_score": _source_score(article),
        "needs_enrichment": needs_enrichment,
        "reasons": reasons,
    }
    return article["quality"]


def select_best_articles(articles, limit):
    """
    Rank all candidates and choose a balanced publishing set.
    """
    if not limit or limit <= 0:
        limit = len(articles)

    for article in articles:
        evaluate_article(article)

    ranked = sorted(
        articles,
        key=lambda item: (
            item["quality"]["score"],
            item["quality"]["body_chars"],
            item.get("title", ""),
        ),
        reverse=True,
    )

    selected = []
    source_counts = defaultdict(int)

    for article in ranked:
        if len(selected) >= limit:
            break

        source = _hostname(article.get("source_url") or article.get("url"))
        if source_counts[source] >= MAX_ARTICLES_PER_SOURCE_PER_RUN:
            continue

        selected.append(article)
        source_counts[source] += 1

    if len(selected) < limit:
        selected_urls = {article["url"] for article in selected}
        for article in ranked:
            if len(selected) >= limit:
                break
            if article["url"] not in selected_urls:
                selected.append(article)
                selected_urls.add(article["url"])

    selected_urls = {article["url"] for article in selected}
    deferred = [article for article in ranked if article["url"] not in selected_urls]

    return selected, deferred, ranked


def print_quality_report(selected, deferred, ranked):
    """
    Print a concise editorial selection report for the run logs.
    """
    print("\n" + "=" * 60)
    print("🧭 Editorial quality selection")
    print("=" * 60)
    print(f"  Backlog candidates considered: {len(ranked)}")
    print(f"  Deferred for later: {len(deferred)}")
    print(f"  Selected for processing: {len(selected)}")

    if selected:
        print("\n  Selected articles:")
        for index, article in enumerate(selected, 1):
            quality = article["quality"]
            print(
                f"  {index}. score={quality['score']} chars={quality['body_chars']} "
                f"needs_enrichment={quality['needs_enrichment']} "
                f"source={_hostname(article.get('source_url'))} | {article['title'][:80]}"
            )

    if deferred:
        print("\n  Deferred examples:")
        for article in deferred[:5]:
            quality = article["quality"]
            print(f"  - {article['title'][:80]} ({'; '.join(quality['reasons'])})")

    print("=" * 60)
