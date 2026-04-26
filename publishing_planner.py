# ============================================================
# publishing_planner.py - Phase 8 Category-Balanced Planning
# ============================================================

from datetime import datetime

from article_queue import load_article_queue, save_article_queue
from article_selector import PRIORITY_RANK, suggest_category

CATEGORY_AI_TOOLS = "أدوات الذكاء الاصطناعي"
CATEGORY_CYBERSECURITY = "الأمن السيبراني"
CATEGORY_TECH_NEWS = "أخبار التقنية"
CATEGORY_APPS = "برامج وتطبيقات"

CATEGORY_TARGETS = {
    CATEGORY_CYBERSECURITY: 0.40,
    CATEGORY_AI_TOOLS: 0.25,
    CATEGORY_TECH_NEWS: 0.20,
    CATEGORY_APPS: 0.15,
}

COUNTED_STATUSES = {"draft_created", "published"}
ELIGIBLE_STATUSES = {"ready", "selected"}
BLOCKED_STATUSES = {"draft_created", "published", "skipped", "failed"}


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _is_today(value):
    if not value:
        return False
    return str(value)[:10] == datetime.now().date().isoformat()


def _article_category(article):
    return article.get("suggested_category") or suggest_category(article)


def _count_today_by_category(articles):
    counts = {category: 0 for category in CATEGORY_TARGETS}
    total = 0

    for article in articles:
        if article.get("status") not in COUNTED_STATUSES:
            continue

        date_value = (
            article.get("draft_created_at")
            or article.get("published_at")
            or article.get("selected_at")
            or article.get("discovered_at")
        )
        if not _is_today(date_value):
            continue

        category = _article_category(article)
        if category not in counts:
            continue

        counts[category] += 1
        total += 1

    return total, counts


def _under_target_categories(total, counts):
    if total <= 0:
        return sorted(CATEGORY_TARGETS, key=lambda category: CATEGORY_TARGETS[category], reverse=True)

    under = []
    for category, target in CATEGORY_TARGETS.items():
        current_ratio = counts.get(category, 0) / total
        if current_ratio < target:
            under.append((category, target - current_ratio))

    under.sort(key=lambda item: (item[1], CATEGORY_TARGETS[item[0]]), reverse=True)
    return [category for category, _gap in under]


def _eligible_articles(articles):
    eligible = []
    for article in articles:
        if article.get("status") in BLOCKED_STATUSES:
            continue
        if article.get("status") not in ELIGIBLE_STATUSES:
            continue
        if article.get("content_fetch_status") != "success":
            continue

        article["suggested_category"] = _article_category(article)
        eligible.append(article)
    return eligible


def _candidate_sort_key(article):
    return (
        int(article.get("score") or 0),
        PRIORITY_RANK.get(article.get("priority", ""), 0),
        1 if article.get("main_image") else 0,
        article.get("discovered_at", ""),
    )


def _choose_candidate(eligible, target_category):
    if target_category:
        category_matches = [
            article for article in eligible if article.get("suggested_category") == target_category
        ]
        if category_matches:
            return max(category_matches, key=_candidate_sort_key), "matched under-target category"

    if eligible:
        return max(eligible, key=_candidate_sort_key), "highest score eligible article"

    return None, "no eligible ready enriched articles found"


def plan_next_article(lock=False):
    """
    Plan the next article without modifying status unless lock=True.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])

    today_total, counts = _count_today_by_category(articles)
    under_targets = _under_target_categories(today_total, counts)
    eligible = _eligible_articles(articles)

    selected = None
    reason = "no eligible ready enriched articles found"
    target_category = under_targets[0] if under_targets else ""

    if eligible:
        selected, reason = _choose_candidate(eligible, target_category)
        if selected and target_category and selected.get("suggested_category") != target_category:
            reason = f"{reason}; no eligible article found for target category {target_category}"

    if selected:
        reason = (
            f"{reason}; score {selected.get('score', 0)}; "
            f"{selected.get('priority', 'unknown')} priority"
        )
        selected["planning_reason"] = reason

        if lock:
            selected["status"] = "selected"
            selected["selected_at"] = _now_iso()
            selected["selection_reason"] = reason
            save_article_queue(queue)

    return {
        "today_total": today_total,
        "counts": counts,
        "target_category": target_category,
        "selected": selected,
        "reason": reason,
        "lock": lock,
        "eligible_count": len(eligible),
    }
