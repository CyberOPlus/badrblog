# ============================================================
# social_ai_processor.py - Independent Jobs social copy AI
# ============================================================

import re

from article_ai_processor import AIExecutionContext, _generate_ai_article, _parse_complete_ai_json
from production_logging import log_event

SOCIAL_REQUIRED_FIELDS = ("facebook_post_text",)
SOCIAL_MAX_ATTEMPTS = 3

_NOTICE_STAGE_HASHTAGS = {
    "vacancy": "#وظائف_المغرب",
    "competition": "#مباريات_التوظيف",
    "candidate_list": "#لوائح_المترشحين",
    "results": "#نتائج_المباريات",
    "final_results": "#النتائج_النهائية",
    "update": "#مستجدات_التوظيف",
}
_ALLOWED_SOCIAL_EMOJIS = ("📢", "💼", "📍", "🎓", "⏳", "📋", "✅", "🔄", "👇")


def jobs_contextual_hashtag(notice_type):
    return _NOTICE_STAGE_HASHTAGS.get(
        str(notice_type or "").strip().lower(),
        "#وظائف_المغرب",
    )


def _copy_memory_key(value):
    return re.sub(
        r"[^\w\u0600-\u06FF]+",
        "",
        _strip_bidi(value).casefold(),
        flags=re.UNICODE,
    )


class SocialAIQualityError(ValueError):
    """Raised when generated social copy is structurally invalid."""


def _strip_bidi(text):
    return re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", str(text or "")).strip()


def _social_prompt(article, previous_error=""):
    package = article.get("ai_input_package") or {}
    title = str(article.get("seo_title") or article.get("title") or package.get("job_title") or "").strip()
    description = str(article.get("seo_description") or "").strip()
    notice_type = str(article.get("job_notice_type") or package.get("job_notice_type") or "").strip()
    deadline = str(
        article.get("job_deadline_display")
        or article.get("job_deadline")
        or package.get("job_deadline_display")
        or package.get("job_deadline")
        or ""
    ).strip()
    company = str(article.get("job_company") or package.get("job_company") or "").strip()
    location = str(article.get("job_location") or package.get("job_location") or "").strip()
    positions = str(article.get("job_number_of_positions") or package.get("job_number_of_positions") or "").strip()
    contextual_hashtag = jobs_contextual_hashtag(notice_type)
    recent_hooks = [
        str(value).strip()
        for value in (article.get("_facebook_recent_hooks") or [])[-8:]
        if str(value).strip()
    ]
    recent_ctas = [
        str(value).strip()
        for value in (article.get("_facebook_recent_ctas") or [])[-6:]
        if str(value).strip()
    ]
    final_html = str(article.get("final_html") or article.get("blogger_article_html") or "")
    article_text = re.sub(r"<[^>]+>", " ", final_html)
    # Facebook must never receive a URL in the generated body. Do not even expose
    # official links to the social model; the Blogger URL is posted separately
    # in the first comment by the publisher.
    article_text = re.sub(r"https?://\S+", " ", article_text, flags=re.IGNORECASE)
    article_text = re.sub(
        r"\b(?:www\.)?[A-Za-z0-9.-]+\.(?:ma|com|org|net|gov|edu)(?:/\S*)?",
        " ",
        article_text,
        flags=re.IGNORECASE,
    )
    article_text = re.sub(r"\s+", " ", article_text).strip()[:5000]

    repair = ""
    if previous_error:
        arabic_only_repair = ""
        previous_error_key = str(previous_error or "").casefold()
        if (
            "entirely in arabic" in previous_error_key
            or "arabic-first" in previous_error_key
            or "latin" in previous_error_key
        ):
            arabic_only_repair = """
ARABIC-ONLY REPAIR:
- The facebook_post_text value must contain ZERO Latin/French letters anywhere
  except the literal final brand hashtag #CyberoPlus.
- Translate foreign-language job titles and descriptive role terms into natural
  Modern Standard Arabic from their verified meaning. Do not copy the Latin form.
- For a foreign proper name with no Arabic form in the verified context, render
  the same proper name with Arabic letters only; do not invent a different entity.
- Before returning, scan the whole value and rewrite every remaining A-Z/a-z or
  accented Latin character into Arabic wording, while preserving numbers and facts.
"""
        url_repair = ""
        if (
            "must not contain a url" in previous_error_key
            or "url" in previous_error_key
            or "http" in previous_error_key
        ):
            url_repair = """
NO-URL REPAIR:
- Remove every URL, domain, protocol, www address, application link, and Blogger link.
- Do not replace a URL with another URL or domain name.
- Keep only a natural Arabic CTA saying that details are in "أول تعليق".
- Before returning, scan facebook_post_text for http, https, www, .com, .ma,
  or another visible web address and remove/rewrite that part.
"""
        repair = f"""
The previous social copy failed this rule:
{previous_error}
Repair that exact issue without changing verified facts.
{arabic_only_repair}
{url_repair}
"""

    return f"""
You are the independent Facebook editor for a Moroccan jobs publication.

The Blogger article has ALREADY passed article AI and publishing quality checks.
Your only task is to create the Facebook copy. Never rewrite, repair, shorten,
or reject the Blogger article.

Return JSON only with exactly one key:
{{"facebook_post_text":"..."}}

EDITORIAL GOAL
Help the right reader quickly judge whether this notice concerns them, then
open the article for useful details. Be engaging through specific verified
information, never by hiding the job, exaggerating, or promising employment.

POST STRUCTURE
- Write 4 to 7 short readable paragraphs/lines, separated by blank lines.
- The first paragraph is a concise hook (18 to 160 characters) chosen from the
  strongest VERIFIED fact for THIS notice. Do not rotate openings randomly:
  * vacancy/competition: lead with the role, qualification, location, positions,
    or another concrete eligibility fact that best helps the reader self-screen.
  * candidate_list: lead with publication of the candidate/admitted list and,
    when verified, the relevant test date/place.
  * results: lead with publication of the results and the exact competition/role.
  * final_results: say clearly that these are FINAL results.
  * update: lead with the actual verified change, such as a new deadline/date/document.
- Use 2 to 6 functional emojis only when they match the fact: 📢 announcement,
  💼 role, 📍 location, 🎓 qualification, ⏳ deadline, 📋 list/results,
  ✅ final result, 🔄 update, 👇 first-comment CTA. Never decorate every line.
- Middle paragraphs: include the useful verified facts a reader needs to decide
  whether the notice concerns them. Do NOT omit a useful verified fact merely to
  hit a target length. Remove repetition and filler instead.
- For an active vacancy/competition, state the verified deadline once. A positions
  value of 0 is unknown and must never be presented as a verified count.
- The paragraph immediately before hashtags is one natural CTA containing
  "أول تعليق". Its wording must match the notice stage: application details for
  active jobs/competitions, list/test details for candidate lists, result details
  for results, and changed details for updates.
- The FINAL line must contain exactly these two hashtags and no others:
  #CyberoPlus {contextual_hashtag}
- Aim for roughly 250 to 800 visible characters when the facts support it, but
  completeness of useful verified information is more important than shortening.

STRICT RULES
- Use only facts in the published article context below.
- Write in clear Modern Standard Arabic. #CyberoPlus is the ONLY Latin text allowed.
  The facebook_post_text value must contain no A-Z/a-z or accented Latin letters
  outside that final brand hashtag. Translate foreign-language role titles and
  descriptive terms into natural Arabic instead of copying their French/English form.
  Use established Arabic names for employers when available; otherwise render the
  same proper name in Arabic letters only without inventing a different entity.
- Reflect the exact notice stage: vacancy, competition, candidate list, results,
  final results, or update. Never turn lists/results/updates into a fresh vacancy.
- Do not copy the Blogger title as the first line.
- Vary the wording and sentence order naturally INSIDE the correct notice type.
  Do not reuse a recent hook/CTA merely for variation, and do not change meaning.
- Do not use repetitive database labels line after line.
- Never invent salary, deadline, count, requirement, location, urgency,
  application method, test date/place, or result status.
- No manufactured urgency, guaranteed acceptance, curiosity bait, or requests
  for likes/shares/comments such as "اكتب مهتم" or "شارك ليصلك العرض".
- Do not include any URL. The Blogger URL is posted separately in the first comment.
- No HTML, markdown, JSON inside the value, or bidi control characters.
- Keep the complete post between 120 and 1200 visible characters.
- Recent hook fingerprints to avoid when possible: {recent_hooks}
- Recent CTA fingerprints to avoid when possible: {recent_ctas}
{repair}
PUBLISHED ARTICLE CONTEXT
Title: {title}
Description: {description}
Notice type: {notice_type}
Company/institution: {company}
Location: {location}
Positions: {positions}
Deadline/status date: {deadline}
Article text: {article_text}
""".strip()


def validate_jobs_facebook_post(text, article=None):
    text = _strip_bidi(text)
    article = dict(article or {})
    if not text:
        raise SocialAIQualityError("Facebook social copy is empty")
    if re.search(r"https?://\S+", text):
        raise SocialAIQualityError("Facebook social copy must not contain a URL")
    if re.search(r"<[^>]+>", text) or (chr(96) * 3) in text:
        raise SocialAIQualityError("Facebook social copy must be plain text")
    if text.count("أول تعليق") != 1:
        raise SocialAIQualityError('Facebook social copy must contain "أول تعليق" once')

    notice_type = str(
        article.get("job_notice_type")
        or (article.get("ai_input_package") or {}).get("job_notice_type")
        or "vacancy"
    ).strip().lower()
    contextual_hashtag = jobs_contextual_hashtag(notice_type)
    hashtags = re.findall(r"#[\w\u0600-\u06FF_]+", text, flags=re.UNICODE)
    if hashtags != ["#CyberoPlus", contextual_hashtag]:
        raise SocialAIQualityError(
            f"Facebook social copy must end with exactly #CyberoPlus and {contextual_hashtag}"
        )

    body_without_hashtags = re.sub(
        r"#[\w\u0600-\u06FF_]+",
        "",
        text,
        flags=re.UNICODE,
    )
    if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", body_without_hashtags):
        raise SocialAIQualityError(
            "Facebook social copy body must be written entirely in Arabic"
        )
    if len(re.findall(r"[\u0600-\u06FF]", body_without_hashtags)) < 40:
        raise SocialAIQualityError("Facebook social copy must be Arabic-first")
    if not (120 <= len(text) <= 1200):
        raise SocialAIQualityError(
            "Facebook social copy length must be 120 to 1200 characters"
        )

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not (4 <= len(lines) <= 8):
        raise SocialAIQualityError(
            "Facebook social copy needs 4 to 8 short readable lines"
        )
    first_line = lines[0]
    if not (18 <= len(first_line) <= 160):
        raise SocialAIQualityError(
            "Facebook social copy hook must be 18 to 160 characters"
        )
    expected_hashtag_line = f"#CyberoPlus {contextual_hashtag}"
    if lines[-1] != expected_hashtag_line:
        raise SocialAIQualityError(
            "Facebook social copy hashtags must be the final line"
        )
    if len(lines) < 2 or "أول تعليق" not in lines[-2]:
        raise SocialAIQualityError(
            "Facebook social copy CTA must be immediately before hashtags"
        )

    emoji_count = sum(text.count(emoji) for emoji in _ALLOWED_SOCIAL_EMOJIS)
    if not (2 <= emoji_count <= 6):
        raise SocialAIQualityError(
            "Facebook social copy must use 2 to 6 functional emojis"
        )

    title = str(article.get("seo_title") or article.get("title") or "").strip()
    hook_for_compare = first_line
    for emoji in _ALLOWED_SOCIAL_EMOJIS:
        hook_for_compare = hook_for_compare.replace(emoji, "")
    if title and re.sub(r"\s+", " ", hook_for_compare).strip().casefold() == re.sub(
        r"\s+", " ", title
    ).casefold():
        raise SocialAIQualityError(
            "Facebook social copy hook must not equal the Blogger title"
        )

    recent_hooks = {
        str(value).strip()
        for value in (article.get("_facebook_recent_hooks") or [])
        if str(value).strip()
    }
    if recent_hooks and _copy_memory_key(first_line) in recent_hooks:
        raise SocialAIQualityError(
            "Facebook hook repeats a recent opening; vary the wording using another verified angle"
        )
    return text


_NOTICE_STAGE_LABELS = {
    "vacancy": "فرصة توظيف",
    "competition": "مباراة توظيف",
    "candidate_list": "لائحة مترشحين",
    "results": "نتائج مباراة",
    "final_results": "النتائج النهائية لمباراة",
    "update": "مستجد بخصوص إعلان توظيف",
}


def _clean_published_social_fact(value, max_chars=260):
    text = _strip_bidi(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ،؛;.-")
    return text[:max(1, int(max_chars))].strip()


def _arabic_published_social_fact(value, max_chars=260):
    """Use supplied Arabic facts; do not guess translations in the fallback."""
    text = _clean_published_social_fact(value, max_chars)
    # An Arabic full name is sufficient without its parenthesized Latin acronym.
    text = re.sub(
        r"\((?=[^)]*[A-Za-zÀ-ÖØ-öø-ÿ])[A-Za-zÀ-ÖØ-öø-ÿ0-9\s._/-]+\)",
        "",
        text,
    )
    if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", text):
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _deterministic_jobs_facebook_post(article):
    """Build stage-aware safe social copy only from published verified fields."""
    article = dict(article or {})
    package = article.get("ai_input_package") or {}
    notice_type = str(
        article.get("job_notice_type")
        or package.get("job_notice_type")
        or "vacancy"
    ).strip().lower()
    stage = _NOTICE_STAGE_LABELS.get(notice_type, "إعلان توظيف")
    contextual_hashtag = jobs_contextual_hashtag(notice_type)

    company = _arabic_published_social_fact(
        article.get("job_company") or package.get("job_company") or "",
        120,
    )
    title = _arabic_published_social_fact(
        article.get("seo_title") or article.get("title") or "",
        180,
    )
    location = _arabic_published_social_fact(
        article.get("job_location") or package.get("job_location") or "",
        45,
    )
    positions = _arabic_published_social_fact(
        article.get("job_number_of_positions")
        or package.get("job_number_of_positions")
        or "",
        20,
    )
    deadline = _arabic_published_social_fact(
        article.get("job_deadline_display")
        or article.get("job_deadline")
        or package.get("job_deadline_display")
        or package.get("job_deadline")
        or "",
        80,
    )

    active = notice_type in {"vacancy", "competition"}
    stage_emoji = {
        "candidate_list": "📋",
        "results": "📋",
        "final_results": "✅",
        "update": "🔄",
    }.get(notice_type, "📢")

    place = f" في {location}" if location else ""
    if active:
        hook = f"{stage_emoji} {stage}{place}: راجع المعطيات الأساسية قبل الترشيح."
    else:
        hook = f"{stage_emoji} {stage}{place}: اطّلع على تفاصيل الإعلان المنشور."

    summary = title
    if company and company not in summary:
        summary = (
            f"{summary}. الجهة المعلنة: {company}"
            if summary
            else f"الجهة المعلنة: {company}"
        )
    if not summary:
        summary = "تفاصيل موثقة من الإعلان المنشور متاحة للاطلاع."
    lines = [hook, f"💼 {summary.rstrip('。.!؟')}."]

    if active and positions and positions not in {"0", "0.0"}:
        lines.append(f"📋 عدد المناصب المؤكد: {positions}.")
    if location and location not in hook:
        lines.append(f"📍 المكان: {location}.")
    if deadline and active and deadline not in summary:
        lines.append(f"⏳ آخر أجل للترشيح: {deadline}.")

    # The fallback does not infer article-body details. Keep its CTA specific to
    # the notice stage without promising documents, test logistics, or another
    # fact that was not read from a verified field above.
    cta_by_type = {
        "vacancy": "👇 تفاصيل الوظيفة والإعلان الكامل في أول تعليق.",
        "competition": "👇 تفاصيل المباراة والإعلان الكامل في أول تعليق.",
        "candidate_list": "👇 تفاصيل لائحة المترشحين والإعلان في أول تعليق.",
        "results": "👇 تفاصيل النتائج والإعلان في أول تعليق.",
        "final_results": "👇 تفاصيل النتائج النهائية والإعلان في أول تعليق.",
        "update": "👇 تفاصيل المستجد والإعلان الكامل في أول تعليق.",
    }
    lines.append(
        cta_by_type.get(
            notice_type,
            "👇 التفاصيل الكاملة لهذا الإعلان في أول تعليق.",
        )
    )
    lines.append(f"#CyberoPlus {contextual_hashtag}")

    text = "\n\n".join(line for line in lines if line.strip())
    return validate_jobs_facebook_post(text, article=article)


def generate_jobs_facebook_post(article):
    """Generate Facebook copy only after a successful live Blogger publish."""
    if not article or article.get("publish_status") != "published" or not article.get("blogger_post_url"):
        raise RuntimeError("Social AI requires a successful live Blogger publish first.")

    context = AIExecutionContext(article_id=str(article.get("id") or article.get("url") or ""))
    last_error = None
    prompt = _social_prompt(article)
    skipped_providers = set()

    for attempt in range(1, SOCIAL_MAX_ATTEMPTS + 1):
        provider_used = ""
        try:
            context.current_stage = "facebook_generation"
            raw_text, provider_used = _generate_ai_article(
                prompt,
                skip_providers=skipped_providers,
                context=context,
            )
            data = _parse_complete_ai_json(raw_text, SOCIAL_REQUIRED_FIELDS, "Facebook social AI response")
            facebook_post_text = validate_jobs_facebook_post(data.get("facebook_post_text"), article=article)
            log_event(
                "facebook_social_ai_success",
                article_id=article.get("id"),
                provider=provider_used,
                attempt=attempt,
            )
            return {
                "facebook_post_text": facebook_post_text,
                "provider": provider_used,
                "attempts": attempt,
            }
        except Exception as error:
            last_error = error
            failed_provider = str(provider_used or "").split(":", 1)[0].strip().lower()
            # Editorial validation failures are prompt/output quality issues, not
            # provider outages. Retry the SAME healthy provider with the precise
            # repair instruction so a single available provider (for example
            # Cloudflare while other free tiers are rate-limited) can recover.
            quality_repair = isinstance(error, SocialAIQualityError)
            if failed_provider and not quality_repair:
                skipped_providers.add(failed_provider)
            log_event(
                "facebook_social_ai_attempt_failed",
                article_id=article.get("id"),
                attempt=attempt,
                provider=failed_provider,
                reason=error.__class__.__name__,
                quality_repair=quality_repair,
            )
            if attempt < SOCIAL_MAX_ATTEMPTS:
                if failed_provider and not quality_repair:
                    log_event(
                        "facebook_social_ai_provider_rotated",
                        article_id=article.get("id"),
                        from_provider=failed_provider,
                        next_attempt=attempt + 1,
                    )
                elif failed_provider:
                    log_event(
                        "facebook_social_ai_same_provider_repair",
                        article_id=article.get("id"),
                        provider=failed_provider,
                        next_attempt=attempt + 1,
                    )
                prompt = _social_prompt(article, previous_error=str(error))

    raise RuntimeError(
        f"Facebook social AI failed after {SOCIAL_MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error
