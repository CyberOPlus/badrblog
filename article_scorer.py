# ============================================================
# article_scorer.py - Phase 2 Queue Scoring
# ============================================================

import re

from article_queue import load_article_queue, save_article_queue

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
    text = f"{title} {source_name}".casefold()

    high_matches = _keyword_matches(text, HIGH_PRIORITY_KEYWORDS)
    medium_matches = _keyword_matches(text, MEDIUM_PRIORITY_KEYWORDS)
    skip_matches = _keyword_matches(text, LOW_PRIORITY_SKIP_SIGNALS)

    score = 0
    score += min(len(high_matches) * 3, 8)
    score += min(len(medium_matches) * 2, 4)

    source_boosted = any(source in source_name.casefold() for source in HIGH_VALUE_SOURCE_HINTS)
    if source_boosted:
        score += 1

    if skip_matches:
        score -= min(len(skip_matches) * 3, 6)

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

        article["score"] = score
        article["priority"] = priority
        article["status"] = "ready" if score >= 6 else "skipped"

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
