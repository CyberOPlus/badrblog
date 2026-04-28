# ============================================================
# facebook_publisher.py - Phase 12 Facebook Page Auto-Posting
# ============================================================

import random
import re
import json
import time
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

import requests

from article_queue import load_article_queue, save_article_queue
from config import (
    FACEBOOK_AUTO_POST,
    FACEBOOK_GRAPH_API_URL,
    FACEBOOK_IMAGE_OUTPUT_DIR,
    FACEBOOK_LINK_MODE,
    FACEBOOK_STYLE_MEMORY_PATH,
    MAX_FACEBOOK_POSTS_PER_DAY,
    MIN_MINUTES_BETWEEN_FACEBOOK_POSTS,
    FACEBOOK_PAGE_ACCESS_TOKEN,
    FACEBOOK_PAGE_ID,
)
from notifier import notify_facebook_result
from production_logging import elapsed_ms, log_event
from utils.facebook_image_generator import generate_facebook_image


CAPTION_STYLES = (
    "ai_tools",
    "cybersecurity",
    "tech_news",
    "apps_programs",
)

FORBIDDEN_CAPTION_PHRASES = (
    "مقال",
    "اقرأ المزيد",
    "في هذا المقال",
    "اضغط على الرابط",
    "افتح الرابط",
)

FACEBOOK_CTA = "🔗 الرابط في أول تعليق"
FACEBOOK_LINK_MODE_ENFORCED = "comment"
STYLE_BY_CATEGORY = {
    "AI-Tools": "ai_tools",
    "Cyber-Security": "cybersecurity",
    "Tech-News": "tech_news",
    "Apps-Programs": "apps_programs",
}
STYLE_FALLBACKS = {
    "ai_tools": ("tech_news", "apps_programs"),
    "cybersecurity": ("tech_news", "apps_programs"),
    "tech_news": ("ai_tools", "apps_programs"),
    "apps_programs": ("ai_tools", "tech_news"),
}
STYLE_HEADERS = {
    "ai_tools": ("💡 ما الذي يميّزه؟", "⚙️ الأهم", "📌 بمعنى آخر"),
    "cybersecurity": ("ماذا حدث؟", "لماذا هذا مهم؟", "ماذا يعني للمستخدم؟"),
    "tech_news": ("💡 ما الجديد؟", "لماذا هذا مهم؟", "الخلاصة"),
    "apps_programs": ("ماذا تقدم؟", "لمن تصلح؟", "لماذا تجربها؟"),
}
STYLE_STRUCTURES = {
    "ai_tools": {
        "value_focus": {
            "lead": "",
            "prefixes": (
                "الفكرة الملفتة هنا أن ",
                "الأهم في الاستخدام العملي هو أن ",
                "بمعنى أبسط، ",
            ),
        },
        "workflow_focus": {
            "lead": "",
            "prefixes": (
                "ما يجعله مختلفًا أنه ",
                "على مستوى التنفيذ، ",
                "ولو أردنا تلخيص الصورة: ",
            ),
        },
        "maker_focus": {
            "lead": "",
            "prefixes": (
                "التميّز الحقيقي يظهر عندما ",
                "النقطة التي تستحق الانتباه هي أن ",
                "الخلاصة للمستخدم أو المطوّر: ",
            ),
        },
    },
    "cybersecurity": {
        "risk_focus": {
            "lead": "",
            "prefixes": (
                "المشهد باختصار: ",
                "الخطورة هنا أن ",
                "للمستخدم أو الفريق، هذا يعني أن ",
            ),
        },
        "response_focus": {
            "lead": "",
            "prefixes": (
                "الحدث في سطرين: ",
                "سبب الأهمية المباشرة هو أن ",
                "عمليًا، المطلوب الآن هو أن ",
            ),
        },
        "impact_focus": {
            "lead": "",
            "prefixes": (
                "ما حدث يمكن تلخيصه في أن ",
                "هذه ليست نقطة تقنية هامشية لأن ",
                "على مستوى الأثر اليومي، هذا يعني أن ",
            ),
        },
    },
    "tech_news": {
        "announce_focus": {
            "lead": "أعلنت الجهة المعنية خطوة جديدة تستحق التوقف عندها.",
            "prefixes": (
                "الجديد هذه المرة أن ",
                "قيمة هذا التطور تظهر لأن ",
                "وفي الخلاصة، ",
            ),
        },
        "shift_focus": {
            "lead": "كشفت التطورات الأخيرة عن اتجاه تقني يتشكل بسرعة.",
            "prefixes": (
                "المهم في الخبر أن ",
                "هذا مهم لأن ",
                "الخلاصة العملية أن ",
            ),
        },
        "momentum_focus": {
            "lead": "بدأت ملامح تغير واضح تظهر في هذا الملف التقني.",
            "prefixes": (
                "ما الجديد فعلًا؟ ",
                "السبب وراء أهمية الخبر هو أن ",
                "باختصار، ",
            ),
        },
    },
    "apps_programs": {
        "utility_focus": {
            "lead": "أداة جديدة تدخل المشهد بفكرة عملية وواضحة.",
            "prefixes": (
                "هي تقدم باختصار ",
                "تبدو مناسبة أكثر لمن ",
                "وقد تستحق التجربة لأنها ",
            ),
        },
        "audience_focus": {
            "lead": "أداة جديدة قد تختصر خطوات كانت تتطلب وقتًا أطول.",
            "prefixes": (
                "أبرز ما تقدمه أنها ",
                "وهي مناسبة خصوصًا لمن ",
                "سبب التجربة هنا هو أنها ",
            ),
        },
        "productivity_focus": {
            "lead": "أداة جديدة تراهن على البساطة بدل التعقيد.",
            "prefixes": (
                "ما تضيفه فعليًا هو ",
                "وقد تفيد المستخدم الذي ",
                "وتستحق التجربة عندما تكون الحاجة إلى ",
            ),
        },
    },
}
HOOK_TEMPLATES = {
    "ai_tools": (
        "ما يلفت النظر هنا ليس الضجة، بل الفائدة العملية الواضحة.",
        "أداة جديدة تقترب من الاستخدام الحقيقي أكثر من الشعارات.",
        "فكرة ذكية تتحول هذه المرة إلى شيء يمكن الاستفادة منه فورًا.",
    ),
    "cybersecurity": (
        "🚨 التحذير هذه المرة ليس نظريًا، بل قريب من الاستخدام اليومي.",
        "🚨 خبر أمني جديد يذكّر بأن دقائق التأخير قد تصنع فرقًا كبيرًا.",
        "🚨 ليست كل التنبيهات الأمنية متشابهة، وهذا واحد من الأخبار التي تستحق الانتباه السريع.",
    ),
    "tech_news": (
        "ما يحدث الآن لا يبدو تحديثًا عابرًا، بل إشارة إلى اتجاه أكبر.",
        "خطوة جديدة قد تغيّر شكل المنافسة أسرع مما يبدو.",
        "إعلان تقني جديد، لكن قيمته الحقيقية فيما قد يفتحه لاحقًا.",
    ),
    "apps_programs": (
        "أداة جديدة تعد بفائدة واضحة بدل الوعود العامة.",
        "ليس كل تطبيق جديد يستحق التجربة، لكن هذا يلفت النظر عمليًا.",
        "إذا كنت تبحث عن حل أبسط، فهذا النوع من الأدوات يستحق المتابعة.",
    ),
}
DEFAULT_HASHTAGS = {
    "ai_tools": ["#AI", "#AITools", "#OpenSource", "#ذكاء_اصطناعي", "#أدوات_تقنية", "#تقنية"],
    "cybersecurity": ["#CyberSecurity", "#Security", "#DataProtection", "#الأمن_السيبراني", "#أمن_رقمي", "#تقنية"],
    "tech_news": ["#TechNews", "#Innovation", "#Digital", "#أخبار_التقنية", "#مستجدات", "#تقنية"],
    "apps_programs": ["#Apps", "#Productivity", "#Software", "#تطبيقات", "#برامج", "#تقنية"],
}


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _log_notification_result(label, result):
    if result.get("sent") or result.get("skipped"):
        return
    print(f"Telegram {label} notification failed: {result.get('reason', 'unknown error')}")


def _parse_local_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _is_configured():
    return bool(FACEBOOK_AUTO_POST and FACEBOOK_PAGE_ID and FACEBOOK_PAGE_ACCESS_TOKEN)


def _has_blogger_live_publish(article):
    return (
        article.get("status") == "published"
        and article.get("publish_status") == "published"
        and bool(_blogger_post_url(article))
    )


def _valid_public_blogger_url(url):
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return ""
    parsed = urlparse(url)
    if not parsed.netloc or parsed.path.strip("/") == "":
        return ""
    return url


def _blogger_post_url(article):
    return _valid_public_blogger_url((article or {}).get("blogger_post_url"))


def _eligible_for_facebook(article):
    return (
        _has_blogger_live_publish(article)
        and not article.get("facebook_post_id")
        and article.get("facebook_status") in {None, "", "failed"}
    )


def _find_latest_eligible_article(articles):
    eligible = [article for article in articles if _eligible_for_facebook(article)]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda article: (
            article.get("published_at", ""),
            article.get("selected_at", ""),
            article.get("discovered_at", ""),
        ),
    )


def _target_article(articles, target_article_id=None):
    if target_article_id:
        for article in articles:
            if target_article_id in {article.get("id"), article.get("url")}:
                return article
        return None
    return _find_latest_eligible_article(articles)


def _has_blogger_draft(article):
    return (
        article.get("status") == "draft_created"
        and article.get("publish_status") == "draft_created"
        and bool(_valid_public_blogger_url(article.get("blogger_draft_url")) or _blogger_post_url(article))
    )


def _eligible_for_preview(article, include_drafts=False):
    if _has_blogger_live_publish(article):
        return True
    return bool(include_drafts and _has_blogger_draft(article))


def _find_latest_preview_article(articles, include_drafts=False):
    eligible = [
        article
        for article in articles
        if _eligible_for_preview(article, include_drafts=include_drafts)
        and not article.get("facebook_post_id")
        and article.get("facebook_status") in {None, "", "failed"}
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda article: (
            article.get("published_at", ""),
            article.get("draft_created_at", ""),
            article.get("selected_at", ""),
            article.get("discovered_at", ""),
        ),
    )


def _short_summary(article):
    description = str(article.get("seo_description") or article.get("meta_description") or "").strip()
    if description:
        return description

    preview = " ".join(str(article.get("content_preview") or "").split())
    if len(preview) > 260:
        return preview[:257].rstrip() + "..."
    return preview


def _last_caption_style(articles):
    posted = [
        article
        for article in articles
        if article.get("facebook_posted_at") and article.get("facebook_caption_pattern")
    ]
    if not posted:
        return ""
    latest = max(posted, key=lambda article: article.get("facebook_posted_at", ""))
    return latest.get("facebook_caption_pattern", "")


def _empty_style_memory():
    return {
        "global_styles": [],
        "recent": {},
        "recent_hooks": [],
        "recent_structures": [],
        "stats": {},
    }


def _load_style_memory():
    try:
        if FACEBOOK_STYLE_MEMORY_PATH.exists():
            with FACEBOOK_STYLE_MEMORY_PATH.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("global_styles", [])
                data.setdefault("recent", {})
                data.setdefault("recent_hooks", [])
                data.setdefault("recent_structures", [])
                data.setdefault("stats", {})
                return data
    except Exception as error:
        log_event("facebook_style_memory_load_failed", error=error.__class__.__name__)
    return _empty_style_memory()


def _save_style_memory(memory):
    try:
        FACEBOOK_STYLE_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with FACEBOOK_STYLE_MEMORY_PATH.open("w", encoding="utf-8") as handle:
            json.dump(memory, handle, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception as error:
        log_event("facebook_style_memory_save_failed", error=error.__class__.__name__)


def _caption_memory_category(article):
    return str(article.get("suggested_category") or "general").strip() or "general"


def _caption_style_score(memory, category, style):
    stats = memory.get("stats", {}).get(category, {}).get(style, {})
    used = int(stats.get("used", 0))
    failed = int(stats.get("failed", 0))
    posted = int(stats.get("posted", 0))
    return (used + failed * 2 - min(posted, 5) * 0.25, random.random())


def _structure_score(memory, category, style, structure_id):
    style_stats = memory.get("stats", {}).get(category, {}).get(style, {})
    structures = style_stats.get("structures", {})
    stats = structures.get(structure_id, {})
    used = int(stats.get("used", 0))
    posted = int(stats.get("posted", 0))
    return (used - min(posted, 3) * 0.2, random.random())


def _normalize_memory_text(value):
    return re.sub(r"[^\w\u0600-\u06FF]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _plain_text_from_html(html):
    text = re.sub(r"<[^>]+>", " ", str(html or ""))
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _dedupe_preserve(items):
    result = []
    seen = set()
    for item in items:
        normalized = _normalize_memory_text(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(item)
    return result


def _limit_text(text, limit=180):
    text = _clean_caption_line(text)
    if len(text) <= limit:
        return text
    cut_at = max(text.rfind(" ", 0, limit), text.rfind("،", 0, limit), text.rfind(".", 0, limit))
    if cut_at < 50:
        cut_at = limit
    return text[:cut_at].rstrip(" ،.") + "..."


def _article_sentences(article):
    sources = [
        article.get("seo_description", ""),
        article.get("meta_description", ""),
        _plain_text_from_html(article.get("final_html") or article.get("blogger_article_html")),
        article.get("content_preview", ""),
        (article.get("ai_input_package") or {}).get("content_preview", ""),
    ]
    candidates = []
    for source in sources:
        text = _clean_caption_line(source)
        if not text:
            continue
        for sentence in re.split(r"[\n.!؟]+", text):
            sentence = _clean_caption_line(sentence)
            if len(sentence) >= 28:
                candidates.append(sentence)
    return _dedupe_preserve(candidates)


def _choose_caption_pattern(article, articles):
    memory = _load_style_memory()
    category = _caption_memory_category(article)
    primary_style = STYLE_BY_CATEGORY.get(category, "tech_news")
    choices = [primary_style, *STYLE_FALLBACKS.get(primary_style, ())]
    recent = list(memory.get("recent", {}).get(category, []))[-3:]
    global_styles = list(memory.get("global_styles", []))[-3:]
    if len(global_styles) >= 2 and global_styles[-1] == global_styles[-2] == primary_style:
        choices = [style for style in choices if style != primary_style] or choices
    choices = [style for style in choices if style not in recent[-2:]] or choices
    selected = min(choices, key=lambda style: _caption_style_score(memory, category, style))
    log_event(
        "facebook_caption_pattern_selected",
        category=category,
        pattern=selected,
        recent_count=len(global_styles),
    )
    return selected


def _remember_caption_pattern(article, pattern, posted, structure_id="", hook=""):
    if pattern not in CAPTION_STYLES:
        return
    memory = _load_style_memory()
    category = _caption_memory_category(article)
    recent = list(memory.setdefault("recent", {}).get(category, []))
    recent.append(pattern)
    memory["recent"][category] = recent[-8:]
    global_styles = list(memory.get("global_styles", []))
    global_styles.append(pattern)
    memory["global_styles"] = global_styles[-8:]
    if hook:
        recent_hooks = list(memory.get("recent_hooks", []))
        recent_hooks.append(_normalize_memory_text(hook))
        memory["recent_hooks"] = recent_hooks[-20:]
    if structure_id:
        recent_structures = list(memory.get("recent_structures", []))
        recent_structures.append(structure_id)
        memory["recent_structures"] = recent_structures[-12:]
    category_stats = memory.setdefault("stats", {}).setdefault(category, {})
    stats = category_stats.setdefault(pattern, {})
    stats["used"] = int(stats.get("used", 0)) + 1
    if structure_id:
        structure_stats = stats.setdefault("structures", {}).setdefault(structure_id, {})
        structure_stats["used"] = int(structure_stats.get("used", 0)) + 1
    if posted:
        stats["posted"] = int(stats.get("posted", 0)) + 1
        stats["last_success_at"] = _now_iso()
        if structure_id:
            structure_stats["posted"] = int(structure_stats.get("posted", 0)) + 1
            structure_stats["last_success_at"] = _now_iso()
    else:
        stats["failed"] = int(stats.get("failed", 0)) + 1
        stats["last_failure_at"] = _now_iso()
        if structure_id:
            structure_stats["failed"] = int(structure_stats.get("failed", 0)) + 1
            structure_stats["last_failure_at"] = _now_iso()
    _save_style_memory(memory)


def _clean_caption_line(line):
    cleaned = unescape(str(line or "")).strip()
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    for phrase in FORBIDDEN_CAPTION_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"[*_`]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:،")
    return cleaned


def _short_title(article):
    title = article.get("seo_title") or article.get("fetched_title") or article.get("title", "")
    return _clean_caption_line(title)


def _human_summary(article):
    summary = _short_summary(article) or _plain_text_from_html(article.get("final_html") or article.get("blogger_article_html"))
    return _limit_text(summary, limit=190)


def _subject_fragment(article):
    title = _short_title(article)
    if not title:
        return ""
    words = title.split()
    return " ".join(words[: min(len(words), 6)])


def _article_context(article):
    sentences = _article_sentences(article)
    title = _short_title(article)
    summary = _human_summary(article) or title
    primary = sentences[0] if sentences else summary
    secondary = sentences[1] if len(sentences) > 1 else summary
    tertiary = sentences[2] if len(sentences) > 2 else secondary
    style = STYLE_BY_CATEGORY.get(_caption_memory_category(article), "tech_news")
    reader_takeaway = {
        "ai_tools": "القيمة الحقيقية هنا تظهر عندما تتحول الفكرة إلى وقت أقل وجهد أقل في العمل اليومي.",
        "cybersecurity": "المستخدم أو الفريق يحتاج إلى متابعة سريعة للتحديثات ومراجعة النقاط الحساسة قبل اتساع الأثر.",
        "tech_news": "أهمية الخبر لا تقف عند الإعلان نفسه، بل تمتد إلى ما قد يغيّره لاحقًا في السوق أو الاستخدام.",
        "apps_programs": "أفضلية هذه الأداة تظهر عند الحاجة إلى إنجاز أسرع وتجربة أبسط من الحلول المعقدة.",
    }[style]
    audience = {
        "ai_tools": "يبحث عن أداة عملية يمكن إدخالها مباشرة في سير العمل.",
        "cybersecurity": "يعتمد على حساباته أو بياناته أو أنظمته في العمل اليومي.",
        "tech_news": "يراقب أين تتجه المنصات والشركات والتجارب الرقمية المقبلة.",
        "apps_programs": "يريد حلًا واضحًا وسريعًا من دون إعدادات مرهقة.",
    }[style]
    experiment_reason = {
        "ai_tools": "تقدم زاوية عملية بدل إعادة نفس الوعود المعتادة.",
        "cybersecurity": "توضح أثر الحدث الأمني بعبارات أقرب إلى الواقع اليومي.",
        "tech_news": "تختصر ما يستحق المتابعة بعيدًا عن الضجيج المعتاد حول الأخبار السريعة.",
        "apps_programs": "تلمح إلى فائدة مباشرة يمكن ملاحظتها من الاستخدام الأول.",
    }[style]
    return {
        "title": title,
        "summary": summary,
        "subject": _subject_fragment(article),
        "primary": _limit_text(primary),
        "secondary": _limit_text(secondary),
        "tertiary": _limit_text(tertiary),
        "reader_takeaway": reader_takeaway,
        "audience": audience,
        "experiment_reason": experiment_reason,
    }


def _choose_structure_variant(article, style, memory):
    category = _caption_memory_category(article)
    options = list(STYLE_STRUCTURES.get(style, {}).keys())
    recent_structures = list(memory.get("recent_structures", []))[-2:]
    choices = [structure_id for structure_id in options if structure_id not in recent_structures] or options
    return min(choices, key=lambda structure_id: _structure_score(memory, category, style, structure_id))


def _fallback_hook(article):
    title = _short_title(article)
    for sentence in _article_sentences(article):
        if _normalize_memory_text(sentence) != _normalize_memory_text(title):
            return _limit_text(f"الخلاصة السريعة: {sentence}", limit=120)
    return "تفصيل صغير اليوم قد يصنع فرقًا واضحًا غدًا."


def _generate_hook(article, style, memory):
    title_key = _normalize_memory_text(_short_title(article))
    subject = _subject_fragment(article)
    candidates = []
    if subject:
        subject_templates = {
            "ai_tools": f"{subject} يبدو مختلفًا هذه المرة، لأن الفائدة فيه أقرب إلى الواقع.",
            "cybersecurity": f"🚨 {subject} يضع عامل السرعة في الواجهة من جديد.",
            "tech_news": f"{subject} قد يكون بداية لتحول أوسع مما يبدو.",
            "apps_programs": f"{subject} يقترب من الأداة العملية أكثر من الضجة المؤقتة.",
        }
        candidates.append(subject_templates[style])
    candidates.extend(HOOK_TEMPLATES.get(style, ()))
    recent_hooks = set(memory.get("recent_hooks", []))
    for candidate in candidates:
        normalized = _normalize_memory_text(candidate)
        if normalized and normalized != title_key and normalized not in recent_hooks:
            return _limit_text(candidate, limit=120)
    return _fallback_hook(article)


def _build_style_sections(style, structure_id, context):
    structure = STYLE_STRUCTURES[style][structure_id]
    prefixes = structure["prefixes"]
    headers = STYLE_HEADERS[style]
    if style == "ai_tools":
        bodies = (
            prefixes[0] + context["primary"],
            prefixes[1] + context["secondary"],
            prefixes[2] + context["reader_takeaway"],
        )
    elif style == "cybersecurity":
        bodies = (
            prefixes[0] + context["primary"],
            prefixes[1] + context["secondary"],
            prefixes[2] + context["reader_takeaway"],
        )
    elif style == "tech_news":
        bodies = (
            prefixes[0] + context["primary"],
            prefixes[1] + context["secondary"],
            prefixes[2] + context["reader_takeaway"],
        )
    else:
        bodies = (
            prefixes[0] + context["primary"],
            prefixes[1] + context["audience"],
            prefixes[2] + context["experiment_reason"],
        )
    return structure["lead"], list(zip(headers, [_limit_text(body, limit=220) for body in bodies]))


def _hashtags(article, style=None, text=""):
    style = style or STYLE_BY_CATEGORY.get(_caption_memory_category(article), "tech_news")
    text = " ".join(
        [
            text,
            _short_title(article),
            _human_summary(article),
            str(article.get("suggested_category", "")),
            str(article.get("content_preview", "")),
        ]
    ).casefold()
    tags = []
    seen = set()

    def add(tag):
        normalized = tag.casefold()
        if normalized not in seen and len(tags) < 10:
            seen.add(normalized)
            tags.append(tag)

    for tag in DEFAULT_HASHTAGS.get(style, DEFAULT_HASHTAGS["tech_news"]):
        add(tag)

    keyword_map = (
        (("openai",), "#OpenAI"),
        (("gemini",), "#Gemini"),
        (("github",), "#GitHub"),
        (("microsoft",), "#Microsoft"),
        (("google",), "#Google"),
        (("windows",), "#Windows"),
        (("android",), "#Android"),
        (("ios",), "#iOS"),
        (("api",), "#API"),
        (("malware",), "#Malware"),
        (("cve",), "#CVE"),
        (("privacy", "data"), "#Data"),
        (("opensource", "open source"), "#OpenSource"),
    )
    for keywords, tag in keyword_map:
        if any(keyword in text for keyword in keywords):
            add(tag)

    fallback_tags = ["#Digital", "#Tech", "#أخبار_تقنية", "#تحول_رقمي"]
    for tag in fallback_tags:
        if len(tags) >= 6:
            break
        add(tag)

    return tags[:10]


def _render_facebook_post(article, style, structure_id, hook, blogger_url=None):
    context = _article_context(article)
    lead, sections = _build_style_sections(style, structure_id, context)
    blocks = [_limit_text(hook, limit=120)]
    if lead:
        blocks.append(_limit_text(lead, limit=140))
    for header, body in sections:
        blocks.append(f"{header}\n{_clean_caption_line(body)}")
    hashtags = _hashtags(article, style=style, text=" ".join([context["primary"], context["secondary"], hook]))
    blocks.append(FACEBOOK_CTA)
    blocks.append(" ".join(hashtags))
    caption = "\n\n".join(block for block in blocks if block)
    if len(caption) > 1200:
        caption = caption[:1197].rstrip() + "..."
    return {
        "caption": caption,
        "hashtags": hashtags,
        "hook": hook,
        "lead": lead,
        "sections": sections,
        "style": style,
        "structure": structure_id,
        "blogger_url": blogger_url or _blogger_post_url(article),
    }


def _build_post_blueprint(article, style=None, memory=None, retry_index=0, force_structure=None, force_hook=None, articles=None):
    memory = memory or _load_style_memory()
    style = style or _choose_caption_pattern(article, articles or [])
    structure_id = force_structure or _choose_structure_variant(article, style, memory)
    if retry_index and not force_structure:
        alternatives = [item for item in STYLE_STRUCTURES[style] if item != structure_id]
        if alternatives:
            structure_id = alternatives[0]
    hook = force_hook or _generate_hook(article, style, memory)
    if retry_index and not force_hook:
        hook = _fallback_hook(article)
    return _render_facebook_post(article, style, structure_id, hook)


def _build_caption(article, pattern, blogger_url=None):
    blueprint = _build_post_blueprint(article, style=pattern)
    return blueprint["caption"]


def _main_image_url(article):
    if article.get("main_image"):
        return article["main_image"]
    package = article.get("ai_input_package") or {}
    if package.get("main_image"):
        return package["main_image"]
    for image in article.get("article_images") or package.get("article_images") or []:
        if isinstance(image, dict) and image.get("url"):
            return image["url"]
        if isinstance(image, str) and image:
            return image
    return ""


def _post_to_graph(path, payload):
    url = f"{FACEBOOK_GRAPH_API_URL.rstrip('/')}/{path.lstrip('/')}"
    started = time.perf_counter()
    log_event("facebook_graph_start", path=path)
    response = requests.post(url, data=payload, timeout=60)
    if response.status_code >= 400:
        log_event(
            "facebook_graph_end",
            path=path,
            status=response.status_code,
            error=response.text[:200],
            elapsed_ms=elapsed_ms(started),
        )
        raise RuntimeError(f"Facebook Graph API error {response.status_code}: {response.text[:500]}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Facebook Graph API returned an unexpected response.")
    log_event(
        "facebook_graph_end",
        path=path,
        status=response.status_code,
        elapsed_ms=elapsed_ms(started),
    )
    return data


def _post_photo_file(path, payload, image_path):
    url = f"{FACEBOOK_GRAPH_API_URL.rstrip('/')}/{path.lstrip('/')}"
    started = time.perf_counter()
    log_event("facebook_graph_start", path=path, upload="photo")
    with open(image_path, "rb") as handle:
        response = requests.post(
            url,
            data=payload,
            files={"source": handle},
            timeout=60,
        )
    if response.status_code >= 400:
        log_event(
            "facebook_graph_end",
            path=path,
            status=response.status_code,
            error=response.text[:200],
            elapsed_ms=elapsed_ms(started),
        )
        raise RuntimeError(f"Facebook Graph API error {response.status_code}: {response.text[:500]}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Facebook Graph API returned an unexpected response.")
    log_event(
        "facebook_graph_end",
        path=path,
        status=response.status_code,
        elapsed_ms=elapsed_ms(started),
    )
    return data


def _facebook_image_output_path(article):
    article_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(article.get("id") or article.get("url") or "post")).strip("-")
    if not article_id:
        article_id = "post"
    return FACEBOOK_IMAGE_OUTPUT_DIR / f"{article_id[:80]}.jpg"


def _split_caption_parts(caption):
    lines = [line for line in str(caption or "").splitlines() if line.strip()]
    hashtags = lines[-1] if lines and lines[-1].startswith("#") else ""
    post_text = "\n".join(lines[:-1]) if hashtags else "\n".join(lines)
    return post_text.strip(), hashtags.strip()


def _prepare_facebook_post(article, articles, blogger_url):
    last_error = None
    preferred_style = _choose_caption_pattern(article, articles)
    for retry_index in range(2):
        blueprint = _build_post_blueprint(
            article,
            style=preferred_style,
            retry_index=retry_index,
            articles=articles,
        )
        try:
            _validate_facebook_caption(
                blueprint["caption"],
                blogger_url=blogger_url,
                style=blueprint["style"],
                hook=blueprint["hook"],
                structure_id=blueprint["structure"],
                title=_short_title(article),
            )
            return blueprint
        except Exception as error:
            last_error = error
    raise last_error or RuntimeError("Facebook post quality gate failed.")


def _publish_facebook_post(article, blueprint):
    blogger_url = _blogger_post_url(article)
    if not blogger_url:
        raise RuntimeError("Missing live Blogger URL for Facebook post.")

    caption = blueprint["caption"]
    image_url = _main_image_url(article)
    image_result = generate_facebook_image(
        _short_title(article),
        image_url,
        _facebook_image_output_path(article),
        hook_text=blueprint["hook"],
    )
    base_payload = {
        "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
    }

    if image_result.get("ok") and Path(image_result["path"]).exists():
        payload = {
            **base_payload,
            "caption": caption,
            "published": "true",
        }
        data = _post_photo_file(f"{FACEBOOK_PAGE_ID}/photos", payload, image_result["path"])
        return data.get("post_id") or data.get("id") or "", "photo", image_result

    log_event(
        "facebook_image_generation_failed_safe_text_only",
        article_id=article.get("id"),
        error=image_result.get("error", ""),
    )

    payload = {
        **base_payload,
        "message": caption,
    }
    data = _post_to_graph(f"{FACEBOOK_PAGE_ID}/feed", payload)
    return data.get("id") or "", "feed", image_result


def _post_first_comment(facebook_post_id, blogger_post_url):
    comment = _first_comment_text(blogger_post_url)
    data = _post_to_graph(
        f"{facebook_post_id}/comments",
        {
            "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
            "message": comment,
        },
    )
    return data.get("id") or ""


def _first_comment_text(blogger_post_url):
    return f"🔗 الرابط الحقيقي للمنشور:\n{blogger_post_url}"


def _validate_facebook_caption(caption, blogger_url="", style="", hook="", structure_id="", title=""):
    if not str(caption or "").strip():
        raise RuntimeError("Facebook caption is empty.")
    if "```" in caption or re.search(r'"\s*(title|description|html_content|facebook_post_text)\s*"\s*:', caption):
        raise RuntimeError("Facebook caption contains visible JSON/markdown.")
    if re.search(r"https?://\S+", caption):
        raise RuntimeError("Facebook caption contains a URL.")
    if re.search(r"^\s*[-*]\s+", caption, flags=re.MULTILINE):
        raise RuntimeError("Facebook caption contains markdown bullets.")
    hashtags = re.findall(r"#[\w\u0600-\u06FF_]+", caption, flags=re.UNICODE)
    if len(set(hashtags)) != len(hashtags):
        raise RuntimeError("Facebook caption contains duplicate hashtags.")
    if not (6 <= len(hashtags) <= 10):
        raise RuntimeError("Facebook caption must contain 6 to 10 hashtags.")
    if blogger_url and "أول تعليق" not in caption:
        raise RuntimeError("Facebook caption must say the link is in the first comment.")
    if not hook or _normalize_memory_text(hook) == _normalize_memory_text(title):
        raise RuntimeError("Facebook caption hook is missing or identical to the title.")
    if style and structure_id:
        for header in STYLE_HEADERS.get(style, ()):
            if header not in caption:
                raise RuntimeError("Facebook caption is missing its required structure.")


def _apply_failure(article, error):
    article["facebook_status"] = "failed"
    article["facebook_error"] = str(error)
    article.pop("telegram_facebook_notified", None)
    article.pop("telegram_facebook_event_key", None)


def _failure_result(queue, article, error, checked=1, extra=None):
    if article:
        _apply_failure(article, error)
        save_article_queue(queue)
        result = {
            "checked": checked,
            "posted": False,
            "article": article,
            "error": article.get("facebook_error", ""),
        }
        if extra:
            result.update(extra)
        _log_notification_result("Facebook", notify_facebook_result(queue, article, result))
        return result

    result = {
        "checked": checked,
        "posted": False,
        "article": None,
        "error": str(error),
    }
    if extra:
        result.update(extra)
    return result


def get_facebook_limits_status(now=None):
    now = now or datetime.now()
    queue = load_article_queue()
    posted_times = []

    for article in queue.get("articles", []):
        if article.get("facebook_status") not in {"posted", "posted_comment_failed"}:
            continue
        posted_at = _parse_local_datetime(article.get("facebook_posted_at"))
        if posted_at:
            posted_times.append(posted_at)

    today_posts = [posted_at for posted_at in posted_times if posted_at.date() == now.date()]
    last_post_time = max(posted_times) if posted_times else None
    minutes_since_last = (
        max(0, int((now - last_post_time).total_seconds() // 60))
        if last_post_time
        else None
    )

    daily_blocked = len(today_posts) >= MAX_FACEBOOK_POSTS_PER_DAY
    interval_next_allowed = now
    if last_post_time:
        interval_next_allowed = last_post_time + timedelta(minutes=MIN_MINUTES_BETWEEN_FACEBOOK_POSTS)
    interval_blocked = bool(last_post_time and interval_next_allowed > now)
    daily_next_allowed = (
        datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        if daily_blocked
        else now
    )
    allowed_now = not daily_blocked and not interval_blocked
    next_allowed_time = now if allowed_now else max(daily_next_allowed, interval_next_allowed)

    reasons = []
    if daily_blocked:
        reasons.append("daily Facebook post limit reached")
    if interval_blocked:
        reasons.append("minimum minutes between Facebook posts has not elapsed")

    return {
        "facebook_posts_today": len(today_posts),
        "max_facebook_posts_per_day": MAX_FACEBOOK_POSTS_PER_DAY,
        "last_facebook_post_time": last_post_time,
        "minutes_since_last_facebook_post": minutes_since_last,
        "min_minutes_between_facebook_posts": MIN_MINUTES_BETWEEN_FACEBOOK_POSTS,
        "allowed_now": allowed_now,
        "next_allowed_time": next_allowed_time,
        "reasons": reasons,
    }


def post_one_article_to_facebook(target_article_id=None, respect_limits=True):
    """
    Post exactly one live Blogger article to a Facebook Page.
    This is a no-op unless Facebook auto-posting is explicitly enabled.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])
    article = _target_article(articles, target_article_id=target_article_id)

    if not FACEBOOK_AUTO_POST:
        return _failure_result(queue, article, "FACEBOOK_AUTO_POST is disabled.", checked=0)

    if not FACEBOOK_PAGE_ID:
        return _failure_result(queue, article, "FACEBOOK_PAGE_ID is not configured.", checked=0)

    if not FACEBOOK_PAGE_ACCESS_TOKEN:
        return _failure_result(queue, article, "FACEBOOK_PAGE_ACCESS_TOKEN is not configured.", checked=0)

    if not article:
        return {
            "checked": 0,
            "posted": False,
            "article": None,
            "error": "No eligible published article without Facebook post found.",
        }

    if article.get("facebook_post_id"):
        return {
            "checked": 1,
            "posted": False,
            "article": article,
            "error": "Article already has facebook_post_id; refusing duplicate post.",
        }

    if not _has_blogger_live_publish(article):
        return _failure_result(
            queue,
            article,
            "Article is not a successful live Blogger publish with blogger_post_url.",
        )

    blogger_url = _blogger_post_url(article)
    if not blogger_url:
        return _failure_result(queue, article, "Missing valid live Blogger URL for Facebook.")

    if respect_limits:
        limits = get_facebook_limits_status()
        if not limits["allowed_now"]:
            return _failure_result(
                queue,
                article,
                "; ".join(limits["reasons"]) or "Facebook posting limits blocked this run.",
                extra={"limits": limits},
            )

    try:
        blueprint = _prepare_facebook_post(article, articles, blogger_url)
        caption_pattern = blueprint["style"]
        log_event(
            "facebook_post_start",
            article_id=article.get("id"),
            blogger_url=blogger_url,
            pattern=caption_pattern,
        )
        log_event(
            "facebook_style_used",
            article_id=article.get("id"),
            style=blueprint["style"],
            structure=blueprint["structure"],
        )
        log_event(
            "facebook_hook_generated",
            article_id=article.get("id"),
            hook=blueprint["hook"],
        )
        log_event(
            "facebook_hashtags_count",
            article_id=article.get("id"),
            count=len(blueprint["hashtags"]),
        )
        facebook_post_id, post_type, image_result = _publish_facebook_post(article, blueprint)
        if not facebook_post_id:
            raise RuntimeError("Facebook Graph API did not return a post id.")

        article["facebook_status"] = "posted"
        article["facebook_post_id"] = facebook_post_id
        article["facebook_posted_at"] = _now_iso()
        article["facebook_post_type"] = post_type
        article["facebook_image_status"] = "generated" if image_result.get("ok") else "failed"
        article["facebook_image_path"] = image_result.get("path", "")
        article["facebook_image_used_fallback"] = bool(image_result.get("used_fallback"))
        if image_result.get("error"):
            article["facebook_image_error"] = image_result.get("error", "")[:300]
        else:
            article.pop("facebook_image_error", None)
        article["facebook_caption_pattern"] = caption_pattern
        article["facebook_style_used"] = blueprint["style"]
        article["facebook_hook_generated"] = blueprint["hook"]
        article["facebook_structure_used"] = blueprint["structure"]
        article["facebook_hashtags_count"] = len(blueprint["hashtags"])
        article["facebook_link_mode"] = FACEBOOK_LINK_MODE_ENFORCED
        article["facebook_post_text"] = blueprint["caption"]
        article.pop("facebook_error", None)
        article.pop("telegram_facebook_notified", None)
        article.pop("telegram_facebook_event_key", None)

        try:
            comment_id = _post_first_comment(facebook_post_id, blogger_url)
            article["facebook_comment_id"] = comment_id
            log_event(
                "facebook_comment_success",
                article_id=article.get("id"),
                success=True,
                comment_id=comment_id,
            )
        except Exception as comment_error:
            article["facebook_status"] = "posted_comment_failed"
            article["facebook_error"] = f"First comment failed: {comment_error}"
            log_event(
                "facebook_comment_success",
                article_id=article.get("id"),
                success=False,
                error=comment_error.__class__.__name__,
            )

        _remember_caption_pattern(
            article,
            caption_pattern,
            posted=article.get("facebook_status") == "posted",
            structure_id=blueprint["structure"],
            hook=blueprint["hook"],
        )
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": article.get("facebook_status") == "posted",
            "article": article,
            "error": article.get("facebook_error", ""),
        }
        log_event(
            "facebook_post_result",
            status=article.get("facebook_status"),
            article_id=article.get("id"),
            facebook_post_id=article.get("facebook_post_id"),
            comment_id=article.get("facebook_comment_id"),
            blogger_url=blogger_url,
            error=article.get("facebook_error", ""),
        )
        log_event(
            "facebook_post_success",
            article_id=article.get("id"),
            success=article.get("facebook_status") in {"posted", "posted_comment_failed"},
            post_id=article.get("facebook_post_id"),
        )
        _log_notification_result("Facebook", notify_facebook_result(queue, article, result))
        return result

    except Exception as error:
        _apply_failure(article, error)
        if "caption_pattern" in locals():
            _remember_caption_pattern(
                article,
                caption_pattern,
                posted=False,
                structure_id=blueprint.get("structure", "") if "blueprint" in locals() else "",
                hook=blueprint.get("hook", "") if "blueprint" in locals() else "",
            )
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": False,
            "article": article,
            "error": article.get("facebook_error", ""),
        }
        log_event(
            "facebook_post_success",
            article_id=article.get("id"),
            success=False,
            error=error.__class__.__name__,
        )
        log_event(
            "facebook_post_result",
            status="failed",
            article_id=article.get("id"),
            blogger_url=blogger_url,
            error=article.get("facebook_error", ""),
        )
        _log_notification_result("Facebook", notify_facebook_result(queue, article, result))
        return result


def _facebook_backfill_candidates(articles):
    new_post_candidates = [
        article
        for article in articles
        if _has_blogger_live_publish(article)
        and not article.get("facebook_post_id")
        and article.get("facebook_status") in {None, "", "failed"}
    ]
    comment_retry_candidates = [
        article
        for article in articles
        if _has_blogger_live_publish(article)
        and article.get("facebook_post_id")
        and not article.get("facebook_comment_id")
        and article.get("facebook_status") == "posted_comment_failed"
    ]
    sort_key = lambda article: (
        article.get("published_at", ""),
        article.get("selected_at", ""),
        article.get("discovered_at", ""),
    )
    return sorted(new_post_candidates, key=sort_key), sorted(comment_retry_candidates, key=sort_key)


def retry_facebook_first_comment(target_article_id):
    queue = load_article_queue()
    articles = queue.get("articles", [])
    article = _target_article(articles, target_article_id=target_article_id)
    if not article:
        return {
            "checked": 0,
            "posted": False,
            "article": None,
            "error": "No matching article found for Facebook comment retry.",
        }
    if not article.get("facebook_post_id"):
        return _failure_result(queue, article, "Article has no Facebook post ID for comment retry.")
    blogger_url = _blogger_post_url(article)
    if not blogger_url:
        return _failure_result(queue, article, "Article has no real Blogger URL for comment retry.")

    try:
        comment_id = _post_first_comment(article["facebook_post_id"], blogger_url)
        article["facebook_comment_id"] = comment_id
        article["facebook_status"] = "posted"
        article.pop("facebook_error", None)
        article.pop("telegram_facebook_notified", None)
        article.pop("telegram_facebook_event_key", None)
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": True,
            "article": article,
            "error": "",
            "comment_retry": True,
        }
        log_event("facebook_comment_success", article_id=article.get("id"), success=True, comment_id=comment_id)
    except Exception as error:
        article["facebook_status"] = "posted_comment_failed"
        article["facebook_error"] = f"First comment failed: {error}"
        article.pop("telegram_facebook_notified", None)
        article.pop("telegram_facebook_event_key", None)
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": False,
            "article": article,
            "error": article.get("facebook_error", ""),
            "comment_retry": True,
        }
        log_event("facebook_comment_success", article_id=article.get("id"), success=False, error=error.__class__.__name__)

    _log_notification_result("Facebook", notify_facebook_result(queue, article, result))
    return result


def backfill_facebook_posts():
    """
    Post every live Blogger article that is still missing a Facebook post.
    This intentionally bypasses spacing limits because each Blogger publish is
    expected to have a matching Facebook post as soon as possible.
    """
    queue = load_article_queue()
    new_post_candidates, comment_retry_candidates = _facebook_backfill_candidates(
        queue.get("articles", [])
    )
    stats = {
        "checked": len(new_post_candidates) + len(comment_retry_candidates),
        "created": 0,
        "failed": 0,
        "skipped": 0,
        "comments_created": 0,
        "latest_facebook_post_id": "",
        "latest_facebook_comment_id": "",
        "results": [],
    }

    for article in new_post_candidates:
        target_article_id = article.get("id") or article.get("url")
        result = post_one_article_to_facebook(
            target_article_id=target_article_id,
            respect_limits=False,
        )
        result_article = result.get("article") or {}
        stats["results"].append(result)
        if result_article.get("facebook_post_id"):
            stats["created"] += 1
            stats["latest_facebook_post_id"] = result_article.get("facebook_post_id", "")
            stats["latest_facebook_comment_id"] = result_article.get("facebook_comment_id", "")
        if result_article.get("facebook_comment_id"):
            stats["comments_created"] += 1
        if not result.get("posted"):
            stats["failed"] += 1

    for article in comment_retry_candidates:
        target_article_id = article.get("id") or article.get("url")
        result = retry_facebook_first_comment(target_article_id=target_article_id)
        result_article = result.get("article") or {}
        stats["results"].append(result)
        if result_article.get("facebook_post_id"):
            stats["latest_facebook_post_id"] = result_article.get("facebook_post_id", "")
        if result_article.get("facebook_comment_id"):
            stats["comments_created"] += 1
            stats["latest_facebook_comment_id"] = result_article.get("facebook_comment_id", "")
        if not result.get("posted"):
            stats["failed"] += 1

    return stats


def preview_next_facebook_post(target_article_id=None, include_drafts=False):
    """
    Build a read-only preview for an eligible Facebook article.
    This never calls Facebook, Blogger, or any publishing API.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])
    if target_article_id:
        article = _target_article(articles, target_article_id=target_article_id)
        if article and not _eligible_for_preview(article, include_drafts=include_drafts):
            article = None
    else:
        article = _find_latest_preview_article(articles, include_drafts=include_drafts)

    if not article:
        return {
            "available": False,
            "article": None,
            "error": "No eligible Blogger article for Facebook preview found.",
        }

    blueprint = _prepare_facebook_post(article, articles, blogger_url=_blogger_post_url(article) or _valid_public_blogger_url(article.get("blogger_draft_url")))
    caption_pattern = blueprint["style"]
    blogger_url = _blogger_post_url(article) or _valid_public_blogger_url(article.get("blogger_draft_url"))
    if not blogger_url:
        return {
            "available": False,
            "article": article,
            "error": "Selected Blogger article does not have a usable post permalink.",
        }
    post_text, hashtags = _split_caption_parts(blueprint["caption"])

    return {
        "available": True,
        "article": article,
        "selected_style": caption_pattern,
        "selected_structure": blueprint["structure"],
        "hook": blueprint["hook"],
        "post_text": post_text,
        "hashtags": hashtags,
        "first_comment_text": _first_comment_text(blogger_url),
        "link_mode": FACEBOOK_LINK_MODE_ENFORCED,
        "image_url": _main_image_url(article),
        "error": "",
    }


def get_facebook_status():
    queue = load_article_queue()
    articles = queue.get("articles", [])
    published = [article for article in articles if _has_blogger_live_publish(article)]
    without_post = [
        article
        for article in published
        if not article.get("facebook_post_id")
        and article.get("facebook_status") in {None, "", "failed"}
    ]
    posted = [article for article in published if article.get("facebook_post_id")]

    return {
        "auto_post_enabled": FACEBOOK_AUTO_POST,
        "page_id_configured": bool(FACEBOOK_PAGE_ID),
        "token_configured": bool(FACEBOOK_PAGE_ACCESS_TOKEN),
        "published_without_facebook": len(without_post),
        "posted_to_facebook": len(posted),
        "latest_eligible": _find_latest_eligible_article(articles),
    }
