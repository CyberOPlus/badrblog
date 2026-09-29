# ============================================================
# facebook_publisher.py - Phase 12 Facebook Page Auto-Posting
# ============================================================

import hashlib
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
    FACEBOOK_HARD_MAX_POSTS_PER_DAY,
    FACEBOOK_SAFETY_MIN_INTERVAL_MINUTES,
    JOB_VISUAL_STATE_PATH,
    FACEBOOK_PAGE_ACCESS_TOKEN,
    FACEBOOK_PAGE_ID,
    WHATSAPP_CHANNEL_URL,
    JOBS_MODE,
)
from production_logging import elapsed_ms, log_event
from job_visual_policy import choose_job_template
from utils.facebook_image_generator import generate_facebook_image
from job_core import facebook_slot_status, _local as jobs_local_time, _parse_date as parse_job_date, classify_urgency
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

FACEBOOK_LINK_MODE_ENFORCED = "comment"


class FacebookDeliveryUncertain(RuntimeError):
    """Remote Facebook outcome is unknown; never auto-retry the same post."""


ALLOWED_ENGLISH_TERMS = {
    "AI",
    "Android",
    "CVE",
    "Malware",
    "VPN",
    "API",
    "OpenAI",
    "Microsoft",
    "Google",
    "GitHub",
    "Windows",
    "Linux",
    "iOS",
}
CTA_VARIANTS = (
    "التفاصيل كاملة في أول تعليق 👇",
    "وضعت رابط التفاصيل في أول تعليق 👇",
    "لمن يريد القراءة الكاملة، الرابط في أول تعليق 👇",
    "الرابط الكامل ستجده في أول تعليق 👇",
    "تابع التفاصيل من الرابط الموجود في أول تعليق 👇",
    "المزيد من التفاصيل في أول تعليق 👇",
)
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

ARABIC_HASHTAG_MAP = (
    (("ذكاء", "اصطناعي", "ai", "openai", "gemini"), "#ذكاء_اصطناعي"),
    (("أمن", "سيبراني", "ثغرة", "cve", "malware", "vulnerability"), "#أمن_سيبراني"),
    (("تطبيق", "android", "ios"), "#تطبيقات"),
    (("برامج", "software", "windows", "linux"), "#برامج"),
    (("خصوصية", "بيانات", "privacy", "data"), "#خصوصية"),
    (("google",), "#Google"),
    (("microsoft",), "#Microsoft"),
    (("openai",), "#OpenAI"),
    (("android",), "#Android"),
    (("api",), "#API"),
    (("vpn",), "#VPN"),
    (("cve",), "#CVE"),
    (("malware",), "#Malware"),
)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")



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


def _facebook_retry_ready(article, now_epoch=None):
    try:
        retry_after = float(article.get("facebook_retry_after_epoch") or 0)
    except (TypeError, ValueError):
        retry_after = 0
    return retry_after <= float(now_epoch if now_epoch is not None else time.time())


def _facebook_comment_retry_ready(article, now_epoch=None):
    try:
        retry_after = float(article.get("facebook_comment_retry_after_epoch") or 0)
    except (TypeError, ValueError):
        retry_after = 0
    return retry_after <= float(now_epoch if now_epoch is not None else time.time())


def _eligible_for_facebook(article):
    return (
        _has_blogger_live_publish(article)
        and not article.get("facebook_post_id")
        and article.get("facebook_status") in {None, "", "failed"}
        and _facebook_retry_ready(article)
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
        "recent_ctas": [],
        "recent_hashtag_sets": [],
        "recent_fingerprints": [],
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
                data.setdefault("recent_ctas", [])
                data.setdefault("recent_hashtag_sets", [])
                data.setdefault("recent_fingerprints", [])
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


def _caption_fingerprint(caption):
    normalized = re.sub(r"\s+", " ", str(caption or "").casefold()).strip()
    normalized = re.sub(r"https?://\S+", "", normalized)
    normalized = re.sub(r"#[\w\u0600-\u06FF_]+", "", normalized, flags=re.UNICODE)
    return hashlib.sha256(_normalize_memory_text(normalized).encode("utf-8")).hexdigest()[:20]


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


def _remember_caption_pattern(article, pattern, posted, structure_id="", hook="", cta="", hashtags=None, fingerprint=""):
    if pattern not in CAPTION_STYLES and pattern != "jobs":
        return
    memory = _load_style_memory()
    category = _caption_memory_category(article)

    # Recency memory represents content that may actually be visible on the
    # Page. Definite pre-publish/API failures must not poison the fingerprint
    # cache or make a safe retry look like a duplicate.
    if posted:
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
        if cta:
            recent_ctas = list(memory.get("recent_ctas", []))
            recent_ctas.append(_normalize_memory_text(cta))
            memory["recent_ctas"] = recent_ctas[-12:]
        if hashtags:
            recent_hashtag_sets = list(memory.get("recent_hashtag_sets", []))
            recent_hashtag_sets.append("|".join(sorted(str(tag).casefold() for tag in hashtags)))
            memory["recent_hashtag_sets"] = recent_hashtag_sets[-12:]
        if fingerprint:
            recent_fingerprints = list(memory.get("recent_fingerprints", []))
            recent_fingerprints.append(fingerprint)
            memory["recent_fingerprints"] = recent_fingerprints[-30:]
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


_VISUAL_LOCATION_ALIASES = {
    "casablanca": ("Casablanca", "الدار البيضاء"),
    "rabat": ("Rabat", "الرباط"),
    "marrakech": ("Marrakech", "Marrakesh", "مراكش"),
    "tanger": ("Tanger", "Tangier", "طنجة"),
    "tangier": ("Tanger", "Tangier", "طنجة"),
    "agadir": ("Agadir", "أكادير"),
    "fes": ("Fès", "Fes", "Fez", "فاس"),
    "fez": ("Fès", "Fes", "Fez", "فاس"),
    "meknes": ("Meknès", "Meknes", "مكناس"),
    "kenitra": ("Kénitra", "Kenitra", "القنيطرة"),
    "oujda": ("Oujda", "وجدة"),
    "tetouan": ("Tétouan", "Tetouan", "تطوان"),
    "el jadida": ("El Jadida", "الجديدة"),
    "settat": ("Settat", "سطات"),
}

_GENERIC_JOB_TITLES = {
    "job",
    "jobs",
    "vacancy",
    "vacancies",
    "career",
    "careers",
    "recruitment",
    "recrutement",
    "offre",
    "offres",
    "offres d emploi",
    "فرص عمل",
    "وظائف",
}


def _visual_location_aliases(article):
    value = _clean_caption_line(article.get("job_location") or "")
    aliases = set()
    if value:
        aliases.add(value)
        for part in re.split(r"[,/|؛]+", value):
            part = part.strip()
            if part:
                aliases.add(part)
        lowered = value.casefold()
        for key, values in _VISUAL_LOCATION_ALIASES.items():
            if key in lowered:
                aliases.update(values)
    return sorted(aliases, key=len, reverse=True)


def _is_generic_job_title(value):
    normalized = re.sub(
        r"[^\w\u0600-\u06ff]+",
        " ",
        str(value or "").casefold(),
        flags=re.UNICODE,
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return not normalized or normalized in _GENERIC_JOB_TITLES


def _compact_visual_role(value):
    value = _clean_caption_line(value)
    if len(value) <= 112:
        return value

    # Long official titles often append grade, duration and administrative
    # details after a comma. The role before the first comma is the part a
    # scrolling Facebook user needs to recognize first.
    comma_head = re.split(r"[,،]", value, maxsplit=1)[0].strip(" -–—:")
    if len(comma_head) >= 10:
        return comma_head

    # Remove a trailing parenthetical qualifier only when the role remains clear.
    no_tail = re.sub(r"\s*\([^()]{8,}\)\s*$", "", value).strip(" -–—:")
    if 10 <= len(no_tail) < len(value):
        return no_tail
    return value


def _job_visual_title(article):
    notice_type = str(article.get("job_notice_type") or "vacancy").strip().lower()
    company = _clean_caption_line(
        article.get("job_company") or article.get("source_name") or ""
    )
    seo_title = _short_title(article)
    raw_role = _clean_caption_line(article.get("job_title") or "")

    if notice_type == "candidate_list" and not seo_title:
        return "لوائح المدعوين لاجتياز مباراة التوظيف"
    if notice_type == "final_results" and not seo_title:
        return "النتائج النهائية لمباراة التوظيف"
    if notice_type == "results" and not seo_title:
        return "نتائج مباراة التوظيف"

    visual = seo_title or raw_role
    if not visual:
        return "فرصة عمل جديدة"

    if company:
        escaped = re.escape(company)
        # Remove the employer together with grammatical glue first so we do not
        # leave fragments such as "لدى في" after a plain name replacement.
        patterns = (
            rf"^\s*{escaped}\s+(?:توظف|تعلن\s+عن\s+توظيف|تعلن\s+توظيف|recrute|recrutement|is\s+hiring|hiring)\s*[:\-–—]*\s*",
            rf"\s+(?:لدى|عند|مع|chez|at)\s+{escaped}\b",
            rf"\s*[-–—:]?\s*{escaped}\s*$",
            rf"^\s*{escaped}\s*[-–—:]\s*",
        )
        for pattern in patterns:
            visual = re.sub(pattern, " ", visual, flags=re.I)
        visual = re.sub(escaped, " ", visual, flags=re.I)

    if notice_type == "vacancy":
        visual = re.sub(
            r"^\s*(?:وظيفة|فرصة\s+عمل|فرصة\s+توظيف|إعلان\s+توظيف|"
            r"offre\s+d['’]?emploi|job\s+opening)\s*[:\-–—]*\s*",
            "",
            visual,
            flags=re.I,
        )
        visual = re.sub(
            r"^\s*(?:توظف|recrute|recrutement|hiring)\s*[:\-–—]*\s*",
            "",
            visual,
            flags=re.I,
        )

    for alias in _visual_location_aliases(article):
        escaped = re.escape(alias)
        visual = re.sub(
            rf"(?:\s+(?:في|بمدينة|à|a|in|at)\s+|\s+ب){escaped}\s*$",
            "",
            visual,
            flags=re.I,
        )
        visual = re.sub(
            rf"\s*[-–—,:،]\s*{escaped}\s*$",
            "",
            visual,
            flags=re.I,
        )

    visual = re.sub(r"\s+", " ", visual).strip(" -–—:،,")
    visual = _compact_visual_role(visual)

    # If cleanup still leaves a very long marketing/SEO sentence, prefer the
    # official role when it is specific and materially shorter.
    if (
        len(visual) > 112
        and raw_role
        and not _is_generic_job_title(raw_role)
        and len(raw_role) <= 105
    ):
        visual = _compact_visual_role(raw_role)

    if not visual or _is_generic_job_title(visual):
        if raw_role and not _is_generic_job_title(raw_role):
            visual = _compact_visual_role(raw_role)
        elif notice_type == "candidate_list":
            visual = "لوائح المدعوين لاجتياز مباراة التوظيف"
        elif notice_type == "final_results":
            visual = "النتائج النهائية لمباراة التوظيف"
        elif notice_type == "results":
            visual = "نتائج مباراة التوظيف"
        else:
            visual = "فرصة عمل جديدة"

    return _clean_caption_line(visual)


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


def _choose_cta(memory):
    recent = set(memory.get("recent_ctas", [])[-4:])
    for cta in random.sample(list(CTA_VARIANTS), k=len(CTA_VARIANTS)):
        if _normalize_memory_text(cta) not in recent:
            return cta
    return random.choice(CTA_VARIANTS)


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


def _keyword_hashtag(token):
    token = re.sub(r"[^\w\u0600-\u06FF]+", "", str(token or ""), flags=re.UNICODE).strip("_")
    if not token or len(token) < 3:
        return ""
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]{1,24}", token):
        for allowed in ALLOWED_ENGLISH_TERMS:
            if token.casefold() == allowed.casefold():
                return f"#{allowed}"
        return ""
    if re.search(r"[\u0600-\u06FF]", token):
        return "#" + token[:28]
    return ""


def _strip_unneeded_latin(text):
    allowed = {item.casefold() for item in ALLOWED_ENGLISH_TERMS}

    def repl(match):
        token = match.group(0)
        if token.casefold() in allowed:
            return token
        return ""

    cleaned = re.sub(r"\b[A-Za-z][A-Za-z0-9+._-]*\b", repl, str(text or ""))
    return re.sub(r"\s+", " ", cleaned).strip()


def _hashtags(article, style=None, text="", memory=None):
    style = style or STYLE_BY_CATEGORY.get(_caption_memory_category(article), "tech_news")
    text = " ".join(
        [
            text,
            _short_title(article),
            _human_summary(article),
            _plain_text_from_html(article.get("final_html") or article.get("blogger_article_html")),
            str(article.get("suggested_category", "")),
            str(article.get("content_preview", "")),
        ]
    )
    lowered = text.casefold()
    tags = []
    seen = set()
    recent_sets = set((memory or {}).get("recent_hashtag_sets", [])[-6:])

    def add(tag):
        normalized = tag.casefold()
        if normalized not in seen and len(tags) < 6:
            seen.add(normalized)
            tags.append(tag)

    for keywords, tag in ARABIC_HASHTAG_MAP:
        if any(keyword in lowered for keyword in keywords):
            add(tag)

    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|\b[\u0600-\u06FF]{3,}\b", text, flags=re.UNICODE)
    stopwords = {
        "هذا", "هذه", "ذلك", "التي", "الذي", "على", "إلى", "الى", "في", "من", "عن",
        "مع", "كما", "لكن", "كان", "كانت", "يكون", "يمكن", "أكثر", "بعد", "قبل",
        "article", "news", "this", "that", "with", "from", "using", "will",
    }
    counts = {}
    for token in tokens:
        key = token.casefold()
        if key in stopwords or len(key) < 3:
            continue
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts, key=lambda key: (-counts[key], len(key)))
    for key in ranked:
        if len(tags) >= 6:
            break
        add(_keyword_hashtag(key))

    fallback_by_style = {
        "ai_tools": ["#ذكاء_اصطناعي", "#تقنية"],
        "cybersecurity": ["#أمن_سيبراني", "#تقنية"],
        "tech_news": ["#أخبار_تقنية", "#تقنية"],
        "apps_programs": ["#تطبيقات", "#برامج"],
    }
    for tag in fallback_by_style.get(style, ["#تقنية"]):
        if len(tags) >= 3:
            break
        add(tag)

    if "|".join(sorted(tag.casefold() for tag in tags)) in recent_sets:
        for tag in DEFAULT_HASHTAGS.get(style, DEFAULT_HASHTAGS["tech_news"]):
            if len(tags) >= 6:
                break
            clean = tag.lstrip("#")
            if re.search(r"[\u0600-\u06FF]", clean) or any(clean.casefold() == allowed.casefold() for allowed in ALLOWED_ENGLISH_TERMS):
                add(tag)

    return tags[:6]


def _render_facebook_post(article, style, structure_id, hook, blogger_url=None, memory=None, cta=None):
    context = _article_context(article)
    lead, sections = _build_style_sections(style, structure_id, context)
    blocks = [_limit_text(hook, limit=120)]
    if lead:
        blocks.append(_limit_text(lead, limit=140))
    for header, body in sections:
        blocks.append(f"{header}\n{_clean_caption_line(body)}")
    hashtags = _hashtags(
        article,
        style=style,
        text=" ".join([context["primary"], context["secondary"], hook]),
        memory=memory,
    )
    cta = cta or _choose_cta(memory or {})
    blocks.append(cta)
    blocks.append(" ".join(hashtags))
    caption = "\n\n".join(block for block in blocks if block)
    if len(caption) > 1200:
        caption = caption[:1197].rstrip() + "..."
    fingerprint = _caption_fingerprint(caption)
    return {
        "caption": caption,
        "hashtags": hashtags,
        "hook": hook,
        "cta": cta,
        "fingerprint": fingerprint,
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
    return _render_facebook_post(article, style, structure_id, hook, memory=memory)


def _build_caption(article, pattern, blogger_url=None):
    blueprint = _build_post_blueprint(article, style=pattern)
    return blueprint["caption"]


def _main_image_url(article):
    if JOBS_MODE:
        if (
            article.get("company_logo_verified")
            and article.get("company_logo_url")
        ):
            return article["company_logo_url"]
        # Jobs image generation expects an employer logo here, never the
        # generated article cover or a source hero image. Empty means render
        # the verified employer-name text fallback instead.
        return ""
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


def _redact_facebook_error(text):
    text = str(text or "")
    for secret in (FACEBOOK_PAGE_ACCESS_TOKEN,):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _post_to_graph(path, payload):
    url = f"{FACEBOOK_GRAPH_API_URL.rstrip('/')}/{path.lstrip('/')}"
    started = time.perf_counter()
    log_event("facebook_graph_start", path=path)
    try:
        response = requests.post(url, data=payload, timeout=60)
    except (requests.Timeout, requests.ConnectionError) as error:
        log_event(
            "facebook_graph_end",
            path=path,
            status="network-uncertain",
            error=error.__class__.__name__,
            elapsed_ms=elapsed_ms(started),
        )
        raise FacebookDeliveryUncertain(
            f"Facebook delivery outcome is uncertain after {error.__class__.__name__}."
        ) from error
    if response.status_code >= 500 or response.status_code == 408:
        error_text = _redact_facebook_error(response.text[:200])
        log_event(
            "facebook_graph_end",
            path=path,
            status=response.status_code,
            error=error_text,
            elapsed_ms=elapsed_ms(started),
        )
        raise FacebookDeliveryUncertain(
            f"Facebook delivery outcome is uncertain after HTTP {response.status_code}."
        )
    if response.status_code >= 400:
        error_text = _redact_facebook_error(response.text[:200])
        log_event(
            "facebook_graph_end",
            path=path,
            status=response.status_code,
            error=error_text,
            elapsed_ms=elapsed_ms(started),
        )
        raise RuntimeError(f"Facebook Graph API error {response.status_code}: {_redact_facebook_error(response.text[:500])}")
    data = response.json()
    if not isinstance(data, dict):
        raise FacebookDeliveryUncertain("Facebook Graph API returned an unexpected response after delivery.")
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
    try:
        with open(image_path, "rb") as handle:
            response = requests.post(
                url,
                data=payload,
                files={"source": handle},
                timeout=60,
            )
    except (requests.Timeout, requests.ConnectionError) as error:
        log_event(
            "facebook_graph_end",
            path=path,
            status="network-uncertain",
            error=error.__class__.__name__,
            elapsed_ms=elapsed_ms(started),
        )
        raise FacebookDeliveryUncertain(
            f"Facebook photo delivery outcome is uncertain after {error.__class__.__name__}."
        ) from error
    if response.status_code >= 500 or response.status_code == 408:
        error_text = _redact_facebook_error(response.text[:200])
        log_event(
            "facebook_graph_end",
            path=path,
            status=response.status_code,
            error=error_text,
            elapsed_ms=elapsed_ms(started),
        )
        raise FacebookDeliveryUncertain(
            f"Facebook photo delivery outcome is uncertain after HTTP {response.status_code}."
        )
    if response.status_code >= 400:
        error_text = _redact_facebook_error(response.text[:200])
        log_event(
            "facebook_graph_end",
            path=path,
            status=response.status_code,
            error=error_text,
            elapsed_ms=elapsed_ms(started),
        )
        raise RuntimeError(f"Facebook Graph API error {response.status_code}: {_redact_facebook_error(response.text[:500])}")
    data = response.json()
    if not isinstance(data, dict):
        raise FacebookDeliveryUncertain("Facebook Graph API returned an unexpected photo response after delivery.")
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


def _facebook_article_image_output_path(article, content_type="", image_url=""):
    article_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(article.get("id") or article.get("url") or "post")).strip("-") or "post"
    parsed_ext = Path(urlparse(str(image_url or "")).path).suffix.lower()
    if parsed_ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        parsed_ext = ".jpg" if "jpeg" in content_type or "jpg" in content_type else ".png"
    return FACEBOOK_IMAGE_OUTPUT_DIR / f"{article_id[:80]}-article-image{parsed_ext}"


def _download_article_image_for_facebook(article):
    image_url = _main_image_url(article)
    if not image_url:
        return {"ok": False, "path": "", "url": "", "error": "No main article image found."}
    if not str(image_url).startswith(("http://", "https://")):
        return {"ok": False, "path": "", "url": image_url, "error": "Main image URL is not public HTTP(S)."}

    try:
        response = requests.get(
            image_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "image/" not in content_type.casefold():
            return {"ok": False, "path": "", "url": image_url, "error": f"Main image returned non-image content type: {content_type or 'unknown'}."}
        if len(response.content or b"") < 4096:
            return {"ok": False, "path": "", "url": image_url, "error": "Main image download was too small."}

        output_path = _facebook_article_image_output_path(article, content_type=content_type, image_url=image_url)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(response.content)

        try:
            from PIL import Image

            with Image.open(output_path) as image:
                image.verify()
        except Exception as verify_error:
            output_path.unlink(missing_ok=True)
            return {"ok": False, "path": "", "url": image_url, "error": f"Downloaded main image is invalid: {verify_error}."}

        return {"ok": True, "path": str(output_path), "url": image_url, "error": ""}
    except Exception as error:
        return {"ok": False, "path": "", "url": image_url, "error": str(error)}


def _split_caption_parts(caption):
    lines = [line for line in str(caption or "").splitlines() if line.strip()]
    hashtags = lines[-1] if lines and lines[-1].startswith("#") else ""
    post_text = "\n".join(lines[:-1]) if hashtags else "\n".join(lines)
    return post_text.strip(), hashtags.strip()


def _jobs_facebook_blueprint(article, blogger_url):
    company = str(article.get("job_company") or article.get("source_name") or "").strip()
    title = _short_title(article) or str(article.get("job_title") or "").strip()
    location = str(article.get("job_location") or "").strip()
    deadline = str(article.get("job_deadline_display") or article.get("job_deadline") or "").strip()
    contract = str(article.get("job_contract_type") or "").strip()
    notice_type = str(article.get("job_notice_type") or "vacancy").strip().lower()
    notice_status = str(article.get("job_notice_status") or "").strip().lower()
    try:
        positions = max(0, int(article.get("job_number_of_positions") or 0))
    except (TypeError, ValueError):
        positions = 0

    if notice_type == "candidate_list":
        status_word = "المؤقتة" if notice_status == "provisional" else ""
        hook = f"صدرت لوائح المدعوين {status_word} لدى {company}".replace("  ", " ").strip() if company else "صدرت لوائح المدعوين للمباراة"
        cta = "التفاصيل واللوائح الرسمية في أول تعليق 👇"
    elif notice_type == "final_results":
        hook = f"صدرت النتائج النهائية لدى {company}" if company else "صدرت النتائج النهائية للمباراة"
        cta = "التفاصيل والنتائج الرسمية في أول تعليق 👇"
    elif notice_type == "results":
        hook = f"صدرت نتائج جديدة لدى {company}" if company else "صدرت نتائج المباراة"
        cta = "التفاصيل والنتائج الرسمية في أول تعليق 👇"
    elif positions >= 100 and company:
        hook = f"فرصة توظيف واسعة لدى {company} تستحق الاطلاع"
        cta = "التفاصيل وطريقة التقديم في أول تعليق 👇"
    elif company:
        hook = f"فرصة توظيف جديدة لدى {company} تستحق الاطلاع"
        cta = "التفاصيل وطريقة التقديم في أول تعليق 👇"
    else:
        hook = "فرصة عمل جديدة تستحق الاطلاع قبل التقديم"
        cta = "التفاصيل وطريقة التقديم في أول تعليق 👇"

    lines = [hook]
    if title:
        label = "📋 الإعلان" if notice_type != "vacancy" else "💼 الوظيفة"
        lines.append(f"{label}: {title}")
    if company:
        lines.append(f"🏢 الجهة: {company}")
    if location:
        lines.append(f"📍 المكان: {location}")
    if positions:
        lines.append(f"👥 عدد المناصب: {positions}")
    if contract and notice_type == "vacancy":
        lines.append(f"📄 نوع العقد: {contract}")
    if deadline and notice_type == "vacancy":
        lines.append(f"⏳ آخر أجل للترشيح: {deadline}")

    if notice_type == "vacancy":
        lines.append("راجع الشروط وآخر أجل قبل إرسال طلبك.")
    elif notice_status == "provisional":
        lines.append("اللائحة مؤقتة وقد يطرأ عليها تحديث قبل الإعلان النهائي.")
    else:
        lines.append("راجع الوثيقة أو اللائحة الرسمية للتأكد من اسمك وباقي التفاصيل.")
    lines.append(f"🔗 {cta}")

    hashtags = ["#وظائف", "#فرص_عمل", "#المغرب"]
    if notice_type in {"candidate_list", "results", "final_results"}:
        hashtags = ["#مباريات", "#نتائج", "#المغرب"]
    if article.get("job_remote") and notice_type == "vacancy":
        hashtags.append("#عمل_عن_بعد")
    if article.get("job_visa_sponsorship") and notice_type == "vacancy":
        hashtags.append("#تأشيرة_عمل")
    hashtags = hashtags[:5]
    caption = "\n\n".join(lines + [" ".join(hashtags)])

    return {
        "caption": caption,
        "hashtags": hashtags,
        "hook": hook,
        "cta": cta,
        "fingerprint": _caption_fingerprint(caption),
        "lead": "",
        "sections": [],
        "style": "jobs",
        "structure": f"jobs_{notice_type}",
        "blogger_url": blogger_url,
    }


def _prepare_facebook_post(article, articles, blogger_url):
    if JOBS_MODE:
        blueprint = _jobs_facebook_blueprint(article, blogger_url)
        _validate_facebook_caption(
            blueprint["caption"],
            blogger_url=blogger_url,
            style="",
            hook=blueprint["hook"],
            structure_id="",
            title=_short_title(article),
            memory=_load_style_memory(),
            allow_simple=True,
        )
        return blueprint

    last_error = None
    memory = _load_style_memory()
    preferred_style = _choose_caption_pattern(article, articles)
    for retry_index in range(2):
        blueprint = _build_post_blueprint(
            article,
            style=preferred_style,
            memory=memory,
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
                memory=memory,
            )
            return blueprint
        except Exception as error:
            last_error = error
    fallback = _build_fallback_blueprint(article, preferred_style, memory)
    try:
        _validate_facebook_caption(
            fallback["caption"],
            blogger_url=blogger_url,
            style="",
            hook=fallback["hook"],
            structure_id="",
            title=_short_title(article),
            memory=memory,
            allow_simple=True,
        )
    except Exception as fallback_error:
        log_event(
            "facebook_caption_emergency_fallback_used",
            article_id=article.get("id"),
            reason=str(fallback_error)[:180],
        )
        fallback = _build_emergency_fallback_blueprint(article, preferred_style, memory)
    log_event(
        "facebook_caption_fallback_used",
        article_id=article.get("id"),
        reason=str(last_error or "quality gate failed")[:180],
    )
    return fallback


def _build_fallback_blueprint(article, style, memory):
    title = _short_title(article)
    summary = _human_summary(article) or _limit_text((_article_sentences(article) or [title])[0], limit=180)
    title = _strip_unneeded_latin(title) or "خبر تقني جديد يستحق الانتباه"
    summary = _strip_unneeded_latin(summary) or "هذا الخبر يسلط الضوء على نقطة مهمة للمستخدمين، مع تفاصيل أوضح في المقال الكامل."
    hook = _fallback_hook(article)
    hook = _strip_unneeded_latin(hook) or "تفصيل تقني صغير قد يكون أهم مما يبدو."
    cta = _choose_cta(memory)
    hashtags = _hashtags(article, style=style, text=f"{title} {summary}", memory=memory)
    blocks = [
        _limit_text(hook, limit=120),
        _clean_caption_line(title),
        _limit_text(summary, limit=220),
        cta,
        " ".join(hashtags),
    ]
    caption = "\n\n".join(block for block in blocks if block)
    return {
        "caption": caption,
        "hashtags": hashtags,
        "hook": hook,
        "cta": cta,
        "fingerprint": _caption_fingerprint(caption),
        "lead": "",
        "sections": [],
        "style": style,
        "structure": "fallback",
        "blogger_url": _blogger_post_url(article),
    }


def _publish_facebook_post(article, blueprint):
    blogger_url = _blogger_post_url(article)
    if not blogger_url:
        raise RuntimeError("Missing live Blogger URL for Facebook post.")

    caption = blueprint["caption"]
    visual_title = _job_visual_title(article) if JOBS_MODE else _short_title(article)
    if JOBS_MODE:
        article["facebook_visual_title"] = visual_title

    image_result = generate_facebook_image(
        visual_title,
        _main_image_url(article),
        _facebook_image_output_path(article),
        hook_text=blueprint.get("hook", ""),
        template_key=article.get("facebook_template_key", ""),
        employer_name=(
            str(article.get("job_company") or article.get("source_name") or "").strip()
            if JOBS_MODE
            else ""
        ),
    )
    if image_result.get("ok"):
        image_result["url"] = _main_image_url(article)
        if JOBS_MODE:
            article["facebook_visual_layout"] = {
                "title": visual_title,
                "font_size": image_result.get("title_font_size"),
                "font_width": image_result.get("title_font_width"),
                "lines": image_result.get("title_lines"),
                "title_bbox": image_result.get("title_bbox"),
                "logo_kind": image_result.get("logo_kind"),
                "logo_bbox": image_result.get("logo_bbox"),
            }
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
        "facebook_generated_image_failed_safe_text_only",
        article_id=article.get("id"),
        error=image_result.get("error", ""),
        image_url=image_result.get("url", ""),
    )

    payload = {
        **base_payload,
        "message": caption,
    }
    data = _post_to_graph(f"{FACEBOOK_PAGE_ID}/feed", payload)
    return data.get("id") or "", "feed", image_result


def _build_emergency_fallback_blueprint(article, style, memory):
    hook = "تفصيل تقني صغير قد يكون أهم مما يبدو."
    summary = "نلخص في المقال أبرز ما يحتاج القارئ معرفته، مع شرح مبسط للسياق وما يعنيه ذلك عمليًا."
    cta = _choose_cta(memory)
    hashtags = _hashtags(article, style=style, text=f"{hook} {summary}", memory=memory)
    if len(hashtags) < 3:
        hashtags = ["#تقنية", "#أخبار_تقنية", "#تطبيقات"]
    caption = "\n\n".join([hook, summary, cta, " ".join(hashtags[:6])])
    return {
        "caption": caption,
        "hashtags": hashtags[:6],
        "hook": hook,
        "cta": cta,
        "fingerprint": _caption_fingerprint(caption),
        "lead": "",
        "sections": [],
        "style": style,
        "structure": "emergency_fallback",
        "blogger_url": _blogger_post_url(article),
    }


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
    lines = [
        "🔗 رابط التفاصيل:",
        blogger_post_url,
    ]
    if JOBS_MODE and WHATSAPP_CHANNEL_URL:
        lines.extend([
            "",
            "📲 تابع قناة واتساب للعروض الجديدة:",
            WHATSAPP_CHANNEL_URL,
        ])
    return "\n".join(lines)


def _validate_facebook_caption(caption, blogger_url="", style="", hook="", structure_id="", title="", memory=None, allow_simple=False):
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
    if not (3 <= len(hashtags) <= 6):
        raise RuntimeError("Facebook caption must contain 3 to 6 hashtags.")
    if blogger_url and "أول تعليق" not in caption:
        raise RuntimeError("Facebook caption must say the link is in the first comment.")
    if not hook or _normalize_memory_text(hook) == _normalize_memory_text(title):
        raise RuntimeError("Facebook caption hook is missing or identical to the title.")
    first_line = next((line.strip() for line in str(caption).splitlines() if line.strip()), "")
    if len(first_line) < 18 or first_line.startswith("#"):
        raise RuntimeError("Facebook caption hook is too weak.")
    arabic_chars = len(re.findall(r"[\u0600-\u06FF]", caption))
    latin_words = re.findall(r"\b[A-Za-z][A-Za-z0-9+._-]*\b", caption)
    allowed_latin = [
        word for word in latin_words
        if any(word.casefold() == allowed.casefold() for allowed in ALLOWED_ENGLISH_TERMS)
        or word.startswith("#")
    ]
    if arabic_chars < 40:
        raise RuntimeError("Facebook caption is not Arabic enough.")
    if (not JOBS_MODE) and latin_words and len(allowed_latin) / max(1, len(latin_words)) < 0.75:
        raise RuntimeError("Facebook caption contains unnecessary mixed-language terms.")
    if len(caption) > 1200 or len(caption) < 120:
        raise RuntimeError("Facebook caption length is outside the expected range.")
    fingerprint = _caption_fingerprint(caption)
    if fingerprint in set((memory or {}).get("recent_fingerprints", [])):
        raise RuntimeError("Facebook caption is too similar to a recent post.")
    if style and structure_id:
        for header in STYLE_HEADERS.get(style, ()):
            if header not in caption:
                raise RuntimeError("Facebook caption is missing its required structure.")
    if not allow_simple and len([line for line in str(caption).splitlines() if line.strip()]) < 5:
        raise RuntimeError("Facebook caption is too thin.")


def _facebook_failure_delay_seconds(error, failure_count):
    text = str(error or "").casefold().replace(" ", "")
    # Authentication/permission failures need configuration changes; hammering
    # Graph every scheduled run cannot fix them.
    if any(token in text for token in (
        "oauthexception",
        '"code":190',
        '"code":10',
        '"code":200',
        "accesstoken",
        "permission",
        "notauthorized",
    )):
        return 6 * 3600
    if any(token in text for token in (
        "ratelimit",
        "toomanyrequests",
        '"code":4',
        '"code":17',
        '"code":32',
        '"code":613',
        "http429",
    )):
        return 60 * 60
    # Definite local/API failures can be retried, but with bounded exponential
    # spacing rather than every 15-minute workflow tick.
    step = max(0, min(int(failure_count or 1) - 1, 4))
    return min(6 * 3600, 30 * 60 * (2 ** step))


def _apply_failure(article, error):
    article["facebook_status"] = "failed"
    article["facebook_error"] = str(error)
    count = int(article.get("facebook_failure_count") or 0) + 1
    article["facebook_failure_count"] = count
    article["facebook_last_failure_at"] = _now_iso()
    delay = _facebook_failure_delay_seconds(error, count)
    article["facebook_retry_after_epoch"] = int(time.time()) + delay
    article["facebook_retry_delay_seconds"] = delay


def _clear_facebook_failure_state(article):
    for key in (
        "facebook_failure_count",
        "facebook_last_failure_at",
        "facebook_retry_after_epoch",
        "facebook_retry_delay_seconds",
    ):
        article.pop(key, None)


def _schedule_comment_retry(article, error):
    count = int(article.get("facebook_comment_failure_count") or 0) + 1
    article["facebook_comment_failure_count"] = count
    delay = _facebook_failure_delay_seconds(error, count)
    article["facebook_comment_retry_after_epoch"] = int(time.time()) + delay
    article["facebook_comment_retry_delay_seconds"] = delay


def _clear_comment_failure_state(article):
    for key in (
        "facebook_comment_failure_count",
        "facebook_comment_retry_after_epoch",
        "facebook_comment_retry_delay_seconds",
    ):
        article.pop(key, None)


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


def _deferred_result(article, reason, extra=None):
    result = {
        "checked": 1 if article else 0,
        "posted": False,
        "deferred": True,
        "article": article,
        "error": str(reason or ""),
    }
    if extra:
        result.update(extra)
    return result


def get_facebook_limits_status(now=None, urgent=False):
    queue = load_article_queue()
    posted_times = []
    for article in queue.get("articles", []):
        status = article.get("facebook_status")
        if status in {"posted", "posted_comment_failed", "posted_comment_uncertain"}:
            value = article.get("facebook_posted_at")
        elif status == "delivery_uncertain":
            # Conservatively count an uncertain upload as if it may have landed.
            value = article.get("facebook_delivery_uncertain_at")
        else:
            value = None
        if value:
            posted_times.append(value)

    if JOBS_MODE:
        local_now = jobs_local_time(now)
        local_posts = []
        for value in posted_times:
            parsed = parse_job_date(value)
            if parsed:
                local_posts.append(jobs_local_time(parsed))
        today_posts = [value for value in local_posts if value.date() == local_now.date()]
        last_post_time = max(local_posts) if local_posts else None

        normal_daily_limit = min(
            max(1, MAX_FACEBOOK_POSTS_PER_DAY),
            FACEBOOK_HARD_MAX_POSTS_PER_DAY,
        )
        effective_daily_limit = min(
            FACEBOOK_HARD_MAX_POSTS_PER_DAY,
            normal_daily_limit + (1 if urgent else 0),
        )
        daily_blocked = len(today_posts) >= effective_daily_limit

        safe_interval = max(
            MIN_MINUTES_BETWEEN_FACEBOOK_POSTS,
            FACEBOOK_SAFETY_MIN_INTERVAL_MINUTES,
        )
        minutes_since_last = (
            max(0, int((local_now - last_post_time).total_seconds() // 60))
            if last_post_time
            else None
        )
        interval_blocked = bool(
            last_post_time
            and (local_now - last_post_time).total_seconds() < safe_interval * 60
        )

        slot = facebook_slot_status(posted_times=posted_times, now=now, urgent=urgent)
        allowed_now = bool(slot.get("allowed_now")) and not daily_blocked and not interval_blocked
        reasons = []
        if daily_blocked:
            reasons.append("Facebook hard daily safety limit reached")
        if interval_blocked:
            reasons.append("Facebook safety interval has not elapsed")
        if not slot.get("allowed_now") and not urgent:
            reasons.append("waiting for Morocco Facebook publishing slot")

        next_allowed = slot.get("next_slot", "")
        if interval_blocked and last_post_time:
            next_allowed = (last_post_time + timedelta(minutes=safe_interval)).isoformat()

        return {
            "facebook_posts_today": len(today_posts),
            "max_facebook_posts_per_day": normal_daily_limit,
            "effective_facebook_posts_per_day": effective_daily_limit,
            "hard_max_facebook_posts_per_day": FACEBOOK_HARD_MAX_POSTS_PER_DAY,
            "last_facebook_post_time": last_post_time.isoformat() if last_post_time else None,
            "minutes_since_last_facebook_post": minutes_since_last,
            "min_minutes_between_facebook_posts": safe_interval,
            "allowed_now": allowed_now,
            "next_allowed_time": next_allowed,
            "reasons": reasons,
            "jobs_slot_mode": slot.get("mode", "scheduled"),
            "jobs_slot": slot.get("slot", ""),
        }

    now = now or datetime.now()
    parsed_times = []
    for value in posted_times:
        parsed = _parse_local_datetime(value)
        if parsed:
            parsed_times.append(parsed)

    today_posts = [posted_at for posted_at in parsed_times if posted_at.date() == now.date()]
    last_post_time = max(parsed_times) if parsed_times else None
    minutes_since_last = (
        max(0, int((now - last_post_time).total_seconds() // 60))
        if last_post_time
        else None
    )

    daily_limit = min(
        max(1, MAX_FACEBOOK_POSTS_PER_DAY),
        FACEBOOK_HARD_MAX_POSTS_PER_DAY,
    )
    safe_interval = max(
        MIN_MINUTES_BETWEEN_FACEBOOK_POSTS,
        FACEBOOK_SAFETY_MIN_INTERVAL_MINUTES,
    )
    daily_blocked = len(today_posts) >= daily_limit
    interval_next_allowed = now
    if last_post_time:
        interval_next_allowed = last_post_time + timedelta(minutes=safe_interval)
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
        reasons.append("Facebook hard daily safety limit reached")
    if interval_blocked:
        reasons.append("Facebook safety interval has not elapsed")

    return {
        "facebook_posts_today": len(today_posts),
        "max_facebook_posts_per_day": daily_limit,
        "hard_max_facebook_posts_per_day": FACEBOOK_HARD_MAX_POSTS_PER_DAY,
        "last_facebook_post_time": last_post_time,
        "minutes_since_last_facebook_post": minutes_since_last,
        "min_minutes_between_facebook_posts": safe_interval,
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

    if article.get("facebook_status") == "failed" and not _facebook_retry_ready(article):
        return _deferred_result(
            article,
            "Facebook retry cooldown has not elapsed.",
            extra={"retry_after_epoch": article.get("facebook_retry_after_epoch")},
        )

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
        limits = get_facebook_limits_status(
            urgent=bool(JOBS_MODE and article.get("job_publish_immediately"))
        )
        if not limits["allowed_now"]:
            return _deferred_result(
                article,
                "; ".join(limits["reasons"]) or "Facebook posting limits blocked this run.",
                extra={"limits": limits},
            )

    if JOBS_MODE:
        selection = choose_job_template(article, JOB_VISUAL_STATE_PATH)
        if not selection.get("pinned"):
            save_article_queue(queue)
        log_event(
            "facebook_job_template_selected",
            article_id=article.get("id"),
            template_key=selection.get("key", ""),
            reason=selection.get("reason", ""),
            pinned=selection.get("pinned", False),
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
        _clear_facebook_failure_state(article)
        article["facebook_post_type"] = post_type
        article["facebook_image_status"] = "posted" if image_result.get("ok") else "failed_text_only"
        article["facebook_image_path"] = image_result.get("path", "")
        article["facebook_image_url"] = image_result.get("url", "")
        article["facebook_image_used_fallback"] = False
        if image_result.get("error"):
            article["facebook_image_error"] = image_result.get("error", "")[:300]
        else:
            article.pop("facebook_image_error", None)
        article["facebook_caption_pattern"] = caption_pattern
        article["facebook_style_used"] = blueprint["style"]
        article["facebook_hook_generated"] = blueprint["hook"]
        article["facebook_structure_used"] = blueprint["structure"]
        article["facebook_hashtags_count"] = len(blueprint["hashtags"])
        article["facebook_cta_used"] = blueprint.get("cta", "")
        article["facebook_caption_fingerprint"] = blueprint.get("fingerprint", "")
        article["facebook_link_mode"] = FACEBOOK_LINK_MODE_ENFORCED
        article["facebook_post_text"] = blueprint["caption"]
        article.pop("facebook_error", None)

        # Persist the acknowledged remote ID before the separate comment call.
        # A comment failure must never cause a second photo post.
        save_article_queue(queue)

        try:
            comment_id = _post_first_comment(facebook_post_id, blogger_url)
            article["facebook_comment_id"] = comment_id
            _clear_comment_failure_state(article)
            log_event(
                "facebook_comment_success",
                article_id=article.get("id"),
                success=True,
                comment_id=comment_id,
            )
        except FacebookDeliveryUncertain as comment_error:
            article["facebook_status"] = "posted_comment_uncertain"
            article["facebook_error"] = f"First comment delivery uncertain: {comment_error}"
            log_event(
                "facebook_comment_success",
                article_id=article.get("id"),
                success=False,
                uncertain=True,
                error=comment_error.__class__.__name__,
            )
        except Exception as comment_error:
            article["facebook_status"] = "posted_comment_failed"
            article["facebook_error"] = f"First comment failed: {comment_error}"
            _schedule_comment_retry(article, comment_error)
            log_event(
                "facebook_comment_success",
                article_id=article.get("id"),
                success=False,
                error=comment_error.__class__.__name__,
            )

        _remember_caption_pattern(
            article,
            caption_pattern,
            posted=bool(article.get("facebook_post_id")),
            structure_id=blueprint["structure"],
            hook=blueprint["hook"],
            cta=blueprint.get("cta", ""),
            hashtags=blueprint.get("hashtags", []),
            fingerprint=blueprint.get("fingerprint", ""),
        )
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": bool(article.get("facebook_post_id")),
            "comment_posted": bool(article.get("facebook_comment_id")),
            "image_posted": article.get("facebook_image_status") == "posted",
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
            success=article.get("facebook_status") in {"posted", "posted_comment_failed", "posted_comment_uncertain"},
            post_id=article.get("facebook_post_id"),
        )
        return result

    except FacebookDeliveryUncertain as error:
        article["facebook_status"] = "delivery_uncertain"
        article["facebook_error"] = str(error)
        article["facebook_delivery_uncertain_at"] = _now_iso()
        save_article_queue(queue)
        log_event(
            "facebook_post_result",
            status="delivery_uncertain",
            article_id=article.get("id"),
            blogger_url=blogger_url,
            error=error.__class__.__name__,
        )
        return {
            "checked": 1,
            "posted": False,
            "delivery_uncertain": True,
            "article": article,
            "error": article.get("facebook_error", ""),
        }

    except Exception as error:
        _apply_failure(article, error)
        if "caption_pattern" in locals():
            _remember_caption_pattern(
                article,
                caption_pattern,
                posted=False,
                structure_id=blueprint.get("structure", "") if "blueprint" in locals() else "",
                hook=blueprint.get("hook", "") if "blueprint" in locals() else "",
                cta=blueprint.get("cta", "") if "blueprint" in locals() else "",
                hashtags=blueprint.get("hashtags", []) if "blueprint" in locals() else [],
                fingerprint=blueprint.get("fingerprint", "") if "blueprint" in locals() else "",
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
        return result


def _facebook_backfill_candidates(articles):
    new_post_candidates = [
        article
        for article in articles
        if _has_blogger_live_publish(article)
        and not article.get("facebook_post_id")
        and article.get("facebook_status") in {None, "", "failed"}
        and _facebook_retry_ready(article)
    ]
    comment_retry_candidates = [
        article
        for article in articles
        if _has_blogger_live_publish(article)
        and article.get("facebook_post_id")
        and not article.get("facebook_comment_id")
        and article.get("facebook_status") == "posted_comment_failed"
        and _facebook_comment_retry_ready(article)
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
        _clear_comment_failure_state(article)
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": True,
            "article": article,
            "error": "",
            "comment_retry": True,
        }
        log_event("facebook_comment_success", article_id=article.get("id"), success=True, comment_id=comment_id)
    except FacebookDeliveryUncertain as error:
        article["facebook_status"] = "posted_comment_uncertain"
        article["facebook_error"] = f"First comment delivery uncertain: {error}"
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": False,
            "delivery_uncertain": True,
            "article": article,
            "error": article.get("facebook_error", ""),
            "comment_retry": True,
        }
        log_event("facebook_comment_success", article_id=article.get("id"), success=False, uncertain=True, error=error.__class__.__name__)
    except Exception as error:
        article["facebook_status"] = "posted_comment_failed"
        article["facebook_error"] = f"First comment failed: {error}"
        _schedule_comment_retry(article, error)
        save_article_queue(queue)
        result = {
            "checked": 1,
            "posted": False,
            "article": article,
            "error": article.get("facebook_error", ""),
            "comment_retry": True,
        }
        log_event("facebook_comment_success", article_id=article.get("id"), success=False, error=error.__class__.__name__)
    return result


def drain_scheduled_facebook():
    """Retry pending social delivery independently of new Blogger generation.

    Never bypass daily/slot limits. Each cycle can create at most one photo and
    retry at most one missing first comment; actual Graph failures are reported.
    """
    stats = {"created": 0, "comments_created": 0, "failed": 0, "skipped": 0}
    if not _is_configured():
        stats["skipped"] = 1
        return stats
    queue = load_article_queue()
    pending, comments = _facebook_backfill_candidates(queue.get("articles", []))
    if comments:
        result = retry_facebook_first_comment(comments[0].get("id") or comments[0].get("url"))
        stats["comments_created"] = int(bool(result.get("posted")))
        stats["failed"] += int(not result.get("posted"))
    for article in pending:
        if JOBS_MODE and classify_urgency(article).get("level") == "expired":
            stats["skipped"] += 1
            continue
        limits = get_facebook_limits_status(urgent=bool(JOBS_MODE and article.get("job_publish_immediately")))
        if not limits["allowed_now"]:
            stats["skipped"] += 1
            continue
        result = post_one_article_to_facebook(article.get("id") or article.get("url"), respect_limits=True)
        stats["created"] += int(bool(result.get("posted")))
        stats["failed"] += int(not result.get("posted"))
        break
    return stats


def backfill_facebook_posts():
    """Safely drain old Facebook work without bypassing Page pacing.

    A manual backfill may create at most one new feed post per invocation. It
    still honors slots, daily limits and the safety interval, and retries at
    most one known-failed first comment.
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

    if comment_retry_candidates:
        article = comment_retry_candidates[0]
        result = retry_facebook_first_comment(article.get("id") or article.get("url"))
        stats["results"].append(result)
        result_article = result.get("article") or {}
        if result_article.get("facebook_comment_id"):
            stats["comments_created"] = 1
            stats["latest_facebook_comment_id"] = result_article.get("facebook_comment_id", "")
        elif not result.get("delivery_uncertain"):
            stats["failed"] += 1

    for article in new_post_candidates:
        if JOBS_MODE and classify_urgency(article).get("level") == "expired":
            stats["skipped"] += 1
            continue
        limits = get_facebook_limits_status(
            urgent=bool(JOBS_MODE and article.get("job_publish_immediately"))
        )
        if not limits.get("allowed_now"):
            stats["skipped"] += 1
            continue
        result = post_one_article_to_facebook(
            target_article_id=article.get("id") or article.get("url"),
            respect_limits=True,
        )
        stats["results"].append(result)
        result_article = result.get("article") or {}
        if result_article.get("facebook_post_id"):
            stats["created"] = 1
            stats["latest_facebook_post_id"] = result_article.get("facebook_post_id", "")
            stats["latest_facebook_comment_id"] = result_article.get("facebook_comment_id", "")
        elif not result.get("deferred") and not result.get("delivery_uncertain"):
            stats["failed"] += 1
        break

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
            "preview_status": "unavailable",
        }

    blogger_url = _blogger_post_url(article) or _valid_public_blogger_url(article.get("blogger_draft_url"))
    if not blogger_url:
        return {
            "available": False,
            "article": article,
            "error": "Selected Blogger article does not have a usable post permalink.",
            "preview_status": "unavailable",
        }
    preview_status = "ok"
    try:
        blueprint = _prepare_facebook_post(article, articles, blogger_url=blogger_url)
    except Exception as error:
        memory = _load_style_memory()
        style = _choose_caption_pattern(article, articles)
        blueprint = _build_emergency_fallback_blueprint(article, style, memory)
        preview_status = "fallback_used"
        log_event(
            "facebook_preview_fallback_used",
            article_id=article.get("id"),
            reason=str(error)[:180],
        )
    caption_pattern = blueprint["style"]
    post_text, hashtags = _split_caption_parts(blueprint["caption"])

    return {
        "available": True,
        "preview_status": preview_status,
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
    delivery_uncertain = [
        article for article in published
        if article.get("facebook_status") == "delivery_uncertain"
    ]
    comment_uncertain = [
        article for article in published
        if article.get("facebook_status") == "posted_comment_uncertain"
    ]

    return {
        "auto_post_enabled": FACEBOOK_AUTO_POST,
        "page_id_configured": bool(FACEBOOK_PAGE_ID),
        "token_configured": bool(FACEBOOK_PAGE_ACCESS_TOKEN),
        "published_without_facebook": len(without_post),
        "posted_to_facebook": len(posted),
        "delivery_uncertain_count": len(delivery_uncertain),
        "comment_uncertain_count": len(comment_uncertain),
        "latest_eligible": _find_latest_eligible_article(articles),
    }
