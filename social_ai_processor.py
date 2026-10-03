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


def _social_prompt(article, previous_error="", rejected_copy=""):
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
        previous_error_key = str(previous_error or "").casefold()
        rejected_excerpt = _strip_bidi(rejected_copy)
        rejected_had_url = bool(
            re.search(r"https?://\S+", rejected_excerpt, flags=re.IGNORECASE)
            or re.search(
                r"\b(?:www\.)?[A-Za-z0-9.-]+\.(?:ma|com|org|net|gov|edu)(?:/\S*)?",
                rejected_excerpt,
                flags=re.IGNORECASE,
            )
        )
        if rejected_excerpt:
            rejected_excerpt = re.sub(
                r"https?://\S+",
                " [رابط محذوف] ",
                rejected_excerpt,
                flags=re.IGNORECASE,
            )
            rejected_excerpt = re.sub(
                r"\b(?:www\.)?[A-Za-z0-9.-]+\.(?:ma|com|org|net|gov|edu)(?:/\S*)?",
                " [رابط محذوف] ",
                rejected_excerpt,
                flags=re.IGNORECASE,
            )
            rejected_excerpt = re.sub(r"\s+", " ", rejected_excerpt).strip()[:1400]

        # Validation stops at the first failure. Inspect the sanitized rejected
        # draft too so one repair prompt can fix all visible defects (for example
        # a URL plus a Latin employer name) instead of wasting another attempt.
        rejected_has_latin = bool(
            re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", rejected_excerpt)
        )

        arabic_only_repair = ""
        if (
            "entirely in arabic" in previous_error_key
            or "arabic-first" in previous_error_key
            or "latin" in previous_error_key
            or rejected_has_latin
        ):
            arabic_only_repair = """
ARABIC-ONLY REPAIR:
- The facebook_post_text value must contain ZERO Latin/French letters anywhere.
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
            or rejected_had_url
        ):
            url_repair = """
NO-URL REPAIR:
- Remove every URL, domain, protocol, www address, application link, and Blogger link.
- Do not replace a URL with another URL or domain name.
- Keep only a natural Arabic CTA saying that details are in "أول تعليق".
- Before returning, scan facebook_post_text for http, https, www, .com, .ma,
  or another visible web address and remove/rewrite that part.
"""
        rejected_section = ""
        if rejected_excerpt:
            rejected_section = f"""
REJECTED COPY TO REPAIR
{rejected_excerpt}
"""
        repair = f"""
The previous social copy failed this rule:
{previous_error}
Repair that exact issue without changing verified facts.
{arabic_only_repair}
{url_repair}
{rejected_section}
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
- The FINAL paragraph is one natural CTA containing "أول تعليق". Its wording
  must match the notice stage: application details for active jobs/competitions,
  list/test details for candidate lists, result details for results, and changed
  details for updates.
- Do not use hashtags anywhere in facebook_post_text.
- Aim for roughly 250 to 800 visible characters when the facts support it, but
  completeness of useful verified information is more important than shortening.

STRICT RULES
- Use only facts in the published article context below.
- Write in clear Modern Standard Arabic. The facebook_post_text value must contain
  no A-Z/a-z or accented Latin letters anywhere. Translate foreign-language role titles and
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

    hashtags = re.findall(r"#[\w\u0600-\u06FF_]+", text, flags=re.UNICODE)
    if hashtags:
        raise SocialAIQualityError(
            "Facebook social copy must not contain hashtags"
        )

    if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", text):
        raise SocialAIQualityError(
            "Facebook social copy must be written entirely in Arabic"
        )
    if len(re.findall(r"[\u0600-\u06FF]", text)) < 40:
        raise SocialAIQualityError("Facebook social copy must be Arabic-first")
    if not (120 <= len(text) <= 1200):
        raise SocialAIQualityError(
            "Facebook social copy length must be 120 to 1200 characters"
        )

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not (4 <= len(lines) <= 7):
        raise SocialAIQualityError(
            "Facebook social copy needs 4 to 7 short readable lines"
        )
    first_line = lines[0]
    if not (18 <= len(first_line) <= 160):
        raise SocialAIQualityError(
            "Facebook social copy hook must be 18 to 160 characters"
        )
    if "أول تعليق" not in lines[-1]:
        raise SocialAIQualityError(
            "Facebook social copy CTA must be the final line"
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
            rejected_copy = str(data.get("facebook_post_text") or "").strip()
            facebook_post_text = validate_jobs_facebook_post(rejected_copy, article=article)
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
                prompt = _social_prompt(
                    article,
                    previous_error=str(error),
                    rejected_copy=locals().get("rejected_copy", ""),
                )

    raise RuntimeError(
        f"Facebook social AI failed after {SOCIAL_MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error
