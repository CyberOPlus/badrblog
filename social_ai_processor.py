# ============================================================
# social_ai_processor.py - Independent Jobs social copy AI
# ============================================================

import re

from article_ai_processor import AIExecutionContext, _generate_ai_article, _parse_complete_ai_json
from production_logging import log_event

SOCIAL_REQUIRED_FIELDS = ("facebook_post_text",)
SOCIAL_MAX_ATTEMPTS = 2


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
    final_html = str(article.get("final_html") or article.get("blogger_article_html") or "")
    article_text = re.sub(r"<[^>]+>", " ", final_html)
    article_text = re.sub(r"\s+", " ", article_text).strip()[:5000]

    repair = ""
    if previous_error:
        repair = f"""
The previous social copy failed this rule:
{previous_error}
Repair that exact issue without changing verified facts.
"""

    return f"""
You are the independent Facebook editor for a Moroccan jobs publication.

The Blogger article has ALREADY passed article AI and publishing quality checks.
Your only task is to create the Facebook copy. Never rewrite, repair, shorten,
or reject the Blogger article.

Return JSON only with exactly one key:
{{"facebook_post_text":"..."}}

STRICT RULES
- Use only facts in the published article context below.
- Write clear Modern Standard Arabic. Arabic must dominate.
- Preserve verified company names, acronyms, official role names and useful French/English terms when necessary.
- Reflect the exact notice stage: vacancy, competition, candidate list, results, final results, or update.
- Never turn lists/results/updates into a fresh vacancy.
- Do not copy the Blogger title as the first line.
- Do not use repetitive database labels such as "الجهة:", "المكان:", "عدد المناصب:" line after line.
- Mention a verified deadline once when useful for an active notice.
- Never invent salary, deadline, count, requirement, location, urgency, application method, or status.
- Do not include any URL. The Blogger URL is posted separately in the first comment.
- End with a natural CTA that explicitly contains "أول تعليق" followed by 👇.
- Finish with 3 to 5 relevant, unique hashtags.
- No HTML, markdown, JSON inside the value, or bidi control characters.
- Keep the complete post between 120 and 1200 visible characters.
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
    if not text:
        raise SocialAIQualityError("Facebook social copy is empty")
    if re.search(r"https?://\S+", text):
        raise SocialAIQualityError("Facebook social copy must not contain a URL")
    if re.search(r"<[^>]+>", text) or (chr(96) * 3) in text:
        raise SocialAIQualityError("Facebook social copy must be plain text")
    if "أول تعليق" not in text:
        raise SocialAIQualityError('Facebook social copy must contain "أول تعليق"')
    hashtags = re.findall(r"#[\w\u0600-\u06FF_]+", text, flags=re.UNICODE)
    if not (3 <= len(hashtags) <= 5):
        raise SocialAIQualityError("Facebook social copy must contain 3 to 5 hashtags")
    if len(set(hashtags)) != len(hashtags):
        raise SocialAIQualityError("Facebook social copy contains duplicate hashtags")
    if len(re.findall(r"[\u0600-\u06FF]", text)) < 40:
        raise SocialAIQualityError("Facebook social copy must be Arabic-first")
    if not (120 <= len(text) <= 1200):
        raise SocialAIQualityError("Facebook social copy length must be 120 to 1200 characters")

    title = str((article or {}).get("seo_title") or (article or {}).get("title") or "").strip()
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if len(first_line) < 18:
        raise SocialAIQualityError("Facebook social copy hook is too weak")
    if title and re.sub(r"\s+", " ", first_line).casefold() == re.sub(r"\s+", " ", title).casefold():
        raise SocialAIQualityError("Facebook social copy hook must not equal the Blogger title")
    return text


_NOTICE_STAGE_LABELS = {
    "vacancy": "فرصة توظيف",
    "competition": "مباراة توظيف",
    "candidate_list": "لائحة مترشحين",
    "results": "نتائج مباراة",
    "final_results": "النتائج النهائية لمباراة",
    "update": "مستجد بخصوص إعلان توظيف",
}

_NOTICE_HASHTAGS = {
    "vacancy": ("#وظائف", "#فرص_عمل", "#توظيف"),
    "competition": ("#وظائف", "#مباريات_التوظيف", "#فرص_عمل"),
    "candidate_list": ("#وظائف", "#مباريات_التوظيف", "#لوائح_المترشحين"),
    "results": ("#وظائف", "#مباريات_التوظيف", "#نتائج"),
    "final_results": ("#وظائف", "#مباريات_التوظيف", "#النتائج_النهائية"),
    "update": ("#وظائف", "#توظيف", "#مستجدات"),
}


def _clean_published_social_fact(value, max_chars=260):
    text = _strip_bidi(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ،؛;.-")
    return text[:max(1, int(max_chars))].strip()


def _deterministic_jobs_facebook_post(article):
    """Build safe social copy only from already-published verified fields.

    This is a last-resort formatting fallback when social AI returns malformed
    JSON or otherwise fails locally. It never runs after a Graph delivery
    attempt, so it cannot create a duplicate remote post.
    """
    article = dict(article or {})
    package = article.get("ai_input_package") or {}
    notice_type = str(
        article.get("job_notice_type")
        or package.get("job_notice_type")
        or "vacancy"
    ).strip().lower()
    stage = _NOTICE_STAGE_LABELS.get(notice_type, "إعلان توظيف")

    company = _clean_published_social_fact(
        article.get("job_company") or package.get("job_company") or "",
        120,
    )
    title = _clean_published_social_fact(
        article.get("seo_title") or article.get("title") or "",
        180,
    )
    description = _clean_published_social_fact(
        article.get("seo_description") or "",
        220,
    )
    location = _clean_published_social_fact(
        article.get("job_location") or package.get("job_location") or "",
        100,
    )
    positions = _clean_published_social_fact(
        article.get("job_number_of_positions")
        or package.get("job_number_of_positions")
        or "",
        20,
    )
    deadline = _clean_published_social_fact(
        article.get("job_deadline_display")
        or article.get("job_deadline")
        or package.get("job_deadline_display")
        or package.get("job_deadline")
        or "",
        80,
    )

    if company:
        hook = f"{stage} لدى {company}: إليك أبرز التفاصيل الموثقة التي تهم المترشحين."
    else:
        hook = f"إليك أبرز التفاصيل الموثقة حول {stage} المتاح ضمن هذا الإعلان."

    lines = [hook]
    if description:
        lines.append(description.rstrip("。.!؟") + ".")
    elif title:
        lines.append(f"يتعلق الإعلان بـ {title}.")

    facts = []
    if location:
        facts.append(f"المكان {location}")
    if positions and positions not in {"0", "0.0"}:
        facts.append(f"عدد المناصب {positions}")
    if deadline and notice_type in {"vacancy", "competition"}:
        facts.append(f"آخر أجل للترشيح {deadline}")
    if facts:
        lines.append("، ".join(facts) + ".")

    lines.append(
        "للاطلاع على التفاصيل الكاملة والوثائق المرتبطة بالإعلان، "
        "تجد الرابط في أول تعليق 👇."
    )
    hashtags = _NOTICE_HASHTAGS.get(
        notice_type,
        ("#وظائف", "#فرص_عمل", "#توظيف"),
    )
    lines.append(" ".join(hashtags))

    text = "\n".join(line for line in lines if line.strip())
    return validate_jobs_facebook_post(text, article=article)


def generate_jobs_facebook_post(article):
    """Generate Facebook copy only after a successful live Blogger publish."""
    if not article or article.get("publish_status") != "published" or not article.get("blogger_post_url"):
        raise RuntimeError("Social AI requires a successful live Blogger publish first.")

    context = AIExecutionContext(article_id=str(article.get("id") or article.get("url") or ""))
    last_error = None
    prompt = _social_prompt(article)

    for attempt in range(1, SOCIAL_MAX_ATTEMPTS + 1):
        try:
            context.current_stage = "facebook_generation"
            raw_text, provider_used = _generate_ai_article(prompt, context=context)
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
            log_event(
                "facebook_social_ai_attempt_failed",
                article_id=article.get("id"),
                attempt=attempt,
                reason=error.__class__.__name__,
            )
            if attempt < SOCIAL_MAX_ATTEMPTS:
                prompt = _social_prompt(article, previous_error=str(error))

    try:
        facebook_post_text = _deterministic_jobs_facebook_post(article)
    except Exception as fallback_error:
        raise RuntimeError(
            f"Facebook social AI failed: {last_error}; "
            f"deterministic fallback failed: {fallback_error}"
        ) from fallback_error

    log_event(
        "facebook_social_ai_fallback_used",
        article_id=article.get("id"),
        attempts=SOCIAL_MAX_ATTEMPTS,
        reason=last_error.__class__.__name__ if last_error else "",
    )
    return {
        "facebook_post_text": facebook_post_text,
        "provider": "deterministic:verified-published-article",
        "attempts": SOCIAL_MAX_ATTEMPTS,
        "fallback": True,
    }
