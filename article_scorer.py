# ============================================================
# article_scorer.py - Phase 2 Queue Scoring
# ============================================================

import re

from article_queue import load_article_queue, save_article_queue
from config import CATEGORY_ROTATION_MODE, FAST_NEWS_MODE, FIRST_VALID_ARTICLE_MODE, FRESH_QUEUE_MODE, SKIP_ADS_AFFILIATE_SPONSORED, JOBS_MODE
from content_filter import is_non_technical_entertainment_article, is_promotional_article

HIGH_PRIORITY_KEYWORDS = [
    "breach",
    "hacked",
    "ransomware",
    "malware",
    "zero-day",
    "vulnerability",
    "cve",
    "exploit",
    "data leak",
    "phishing",
    "infostealer",
    "microsoft",
    "google",
    "apple",
    "openai",
    "chatgpt",
    "gemini",
    "ai",
]

MEDIUM_PRIORITY_KEYWORDS = [
    "update",
    "patch",
    "warning",
    "advisory",
    "research",
    "report",
    "campaign",
    "threat actor",
    "spyware",
    "botnet",
]

LOW_PRIORITY_SKIP_SIGNALS = [
    "podcast",
    "webinar",
    "sponsored",
    "press release",
    "opinion",
    "event",
    "conference",
]

HIGH_VALUE_SOURCE_HINTS = [
    "bleepingcomputer",
    "the hacker news",
    "securityweek",
    "dark reading",
    "cisa",
    "google project zero",
    "mandiant",
    "unit 42",
    "talos",
    "krebsonsecurity",
]


def _keyword_matches(text, keywords):
    matches = []
    for keyword in keywords:
        pattern = r"\b" + re.escape(keyword).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, text, flags=re.IGNORECASE):
            matches.append(keyword)
    return matches


def score_article(article):
    """
    Score one queued article from 0 to 10 using title and source only.
    """
    title = article.get("title", "")
    source_name = article.get("source_name", "")
    if JOBS_MODE:
        score = 7 if article.get("official_source") else 2
        priority = str(article.get("source_priority") or "").strip().lower()
        score += {"s+": 3, "s": 3, "a+": 2, "a": 1}.get(priority, 0)
        return max(0, min(10, score))
    text = f"{title} {source_name}".casefold()

    high_matches = _keyword_matches(text, HIGH_PRIORITY_KEYWORDS)
    medium_matches = _keyword_matches(text, MEDIUM_PRIORITY_KEYWORDS)
    skip_matches = _keyword_matches(text, LOW_PRIORITY_SKIP_SIGNALS)
    promotional, _reason = is_promotional_article(article)
    non_technical, _non_technical_reason = is_non_technical_entertainment_article(article)

    score = 0
    score += min(len(high_matches) * 3, 8)
    score += min(len(medium_matches) * 2, 4)

    source_boosted = any(source in source_name.casefold() for source in HIGH_VALUE_SOURCE_HINTS)
    if source_boosted:
        score += 1

    if skip_matches:
        score -= min(len(skip_matches) * 3, 6)
    if promotional and SKIP_ADS_AFFILIATE_SPONSORED:
        score = -10
    if non_technical:
        score = -10

    score = max(0, min(10, score))
    return score


def priority_for_score(score):
    if score >= 8:
        return "high"
    if score >= 5:
        return "medium"
    return "low"


def score_new_articles():
    """
    Score only articles still marked as new, then mark them ready or skipped.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])

    analyzed = 0
    ready = 0
    skipped = 0
    priority_counts = {"high": 0, "medium": 0, "low": 0}

    for article in articles:
        if article.get("status") != "new":
            continue

        analyzed += 1
        score = score_article(article)
        priority = priority_for_score(score)
        promotional, promo_reason = (False, "") if JOBS_MODE else is_promotional_article(article)
        non_technical, non_technical_reason = (False, "") if JOBS_MODE else is_non_technical_entertainment_article(article)

        article["score"] = score
        article["priority"] = priority
        ready_threshold = 0 if JOBS_MODE else (0 if FAST_NEWS_MODE and (FIRST_VALID_ARTICLE_MODE or FRESH_QUEUE_MODE or CATEGORY_ROTATION_MODE) else 6)
        if promotional and SKIP_ADS_AFFILIATE_SPONSORED:
            article["status"] = "skipped"
            article["skip_reason"] = promo_reason
        elif non_technical:
            article["status"] = "skipped"
            article["skip_reason"] = non_technical_reason
        else:
            article["status"] = "ready" if score >= ready_threshold else "skipped"

        priority_counts[priority] += 1
        if article["status"] == "ready":
            ready += 1
        else:
            skipped += 1

    if analyzed:
        save_article_queue(queue)

    return {
        "analyzed": analyzed,
        "ready": ready,
        "skipped": skipped,
        "high": priority_counts["high"],
        "medium": priority_counts["medium"],
        "low": priority_counts["low"],
        "total_queued": len(articles),
    }
