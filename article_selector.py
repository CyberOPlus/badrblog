# ============================================================
# article_selector.py - Phase 4 Next Article Selection
# ============================================================

import re
from datetime import datetime

from article_queue import load_article_queue, save_article_queue

CATEGORY_AI_TOOLS = "أدوات الذكاء الاصطناعي"
CATEGORY_CYBERSECURITY = "الأمن السيبراني"
CATEGORY_TECH_NEWS = "أخبار التقنية"
CATEGORY_APPS = "برامج وتطبيقات"

PRIORITY_RANK = {
    "high": 3,
    "medium": 2,
    "low": 1,
}

CYBERSECURITY_KEYWORDS = (
    "breach",
    "malware",
    "ransomware",
    "phishing",
    "cve",
    "exploit",
    "vulnerability",
    "hack",
    "hacked",
    "token",
    "infostealer",
    "zero-day",
    "backdoor",
    "threat actor",
    "spyware",
    "botnet",
)

AI_TOOL_KEYWORDS = (
    "ai tool",
    "chatgpt",
    "gemini",
    "openai",
    "midjourney",
    "sora",
    "llm",
    "artificial intelligence",
)

APP_KEYWORDS = (
    "app",
    "software",
    "android",
    "ios",
    "windows",
    "tool",
    "download",
)

NEWS_KEYWORDS = (
    "announce",
    "announced",
    "launch",
    "launched",
    "update",
    "company",
    "platform",
    "rolls out",
    "released",
)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _combined_text(article):
    return " ".join(
        str(article.get(field, ""))
        for field in ("title", "fetched_title", "meta_description", "content_preview")
    ).casefold()


def _has_keyword(text, keywords):
    for keyword in keywords:
        pattern = r"\b" + re.escape(keyword).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, text, flags=re.IGNORECASE):
            return True
    return False


def suggest_category(article):
    """
    Rule-based category suggestion for the selected article.
    Cybersecurity wins when it is a meaningful topic.
    """
    text = _combined_text(article)

    if _has_keyword(text, CYBERSECURITY_KEYWORDS):
        return CATEGORY_CYBERSECURITY
    if _has_keyword(text, AI_TOOL_KEYWORDS):
        return CATEGORY_AI_TOOLS
    if _has_keyword(text, APP_KEYWORDS):
        return CATEGORY_APPS
    if _has_keyword(text, NEWS_KEYWORDS):
        return CATEGORY_TECH_NEWS
    return CATEGORY_TECH_NEWS


def _selection_sort_key(article):
    return (
        int(article.get("score") or 0),
        PRIORITY_RANK.get(article.get("priority", ""), 0),
        1 if article.get("main_image") else 0,
        article.get("discovered_at", ""),
    )


def _selection_reason(article):
    parts = [
        f"score {article.get('score', 0)}",
        f"{article.get('priority', 'unknown')} priority",
    ]
    if article.get("main_image"):
        parts.append("has main image")
    if article.get("discovered_at"):
        parts.append(f"newest discovered_at tie-breaker: {article.get('discovered_at')}")
    return "; ".join(parts)


def select_next_article():
    """
    Select exactly one ready, enriched article for the next publishing slot.
    This does not publish, translate, or modify article content.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])
    eligible = [
        article
        for article in articles
        if article.get("status") == "ready"
        and article.get("content_fetch_status") == "success"
    ]

    if not eligible:
        return None

    selected = max(eligible, key=_selection_sort_key)
    category = suggest_category(selected)
    reason = _selection_reason(selected)

    selected["status"] = "selected"
    selected["selected_at"] = _now_iso()
    selected["suggested_category"] = category
    selected["selection_reason"] = reason

    save_article_queue(queue)
    return selected
