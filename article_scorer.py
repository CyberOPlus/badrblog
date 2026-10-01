# ============================================================
# article_scorer.py - Phase 2 Queue Scoring
# ============================================================


from article_queue import load_article_queue, save_article_queue
from config import (
    JOBS_MODE,
)


def score_article(article):
    """
    Score one queued article from 0 to 10 using title and source only.
    """
    score = 7 if article.get("official_source") else 2
    priority = str(article.get("source_priority") or "").strip().lower()
    score += {"s+": 3, "s": 3, "a+": 2, "a": 1}.get(priority, 0)
    return max(0, min(10, score))


def priority_for_score(score):
    if score >= 8:
        return "high"
    if score >= 5:
        return "medium"
    return "low"


def score_new_articles():
    """
    Score only articles still marked as new, then mark them ready for Job enrichment.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])

    analyzed = 0
    ready = 0
    skipped = 0
    priority_counts = {"high": 0, "medium": 0, "low": 0}

    for article in articles:
        if article.get("status") != "new" or article.get("archived"):
            continue

        analyzed += 1
        score = score_article(article)
        priority = priority_for_score(score)

        article["score"] = score
        article["priority"] = priority
        article["status"] = "ready"

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
