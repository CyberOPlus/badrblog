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

EDITORIAL GOAL
Help the right reader quickly judge whether this notice concerns them, then
open the article for useful details. Be engaging through specific verified
information, never by hiding the job, exaggerating, or promising employment.

POST STRUCTURE
- Write 3 to 5 short paragraphs, separated by a blank line.
- First paragraph: one concise hook (18 to 140 characters). Lead with the
  strongest verified match: the role, qualification, location, or notice stage.
  A question about a verified qualification is optional, not a fixed template.
  Avoid generic openings such as "هل تبحث عن عمل؟" or "فرصة لا تعوض".
- Middle paragraphs: identify the role and employer, then include only 1 or 2
  useful screening facts, such as qualification, location, or available posts.
  State enough for readers to know what they are opening; do not copy the article.
- For an active vacancy/competition, put the verified application deadline on
  its own line, once. Omit missing deadlines. A positions value of 0 is unknown,
  not a verified count.
- Last paragraph: one natural CTA containing "أول تعليق". Say what the reader
  can check in the article, such as eligibility, required documents or application
  steps, ONLY when those details actually appear in the context. For lists or
  results, invite readers to check that notice, not to apply for a new vacancy.
- Aim for 250 to 650 visible characters. Use plain text and at most one 👇 at
  the end. No decorative emojis, hashtags, or repeated information.

STRICT RULES
- Use only facts in the published article context below.
- Write entirely in clear Modern Standard Arabic, including roles and locations.
  Use established Arabic names for employers. Omit a foreign name if no reliable
  Arabic spelling is available; never invent a name or leave Latin acronyms.
- Reflect the exact notice stage: vacancy, competition, candidate list, results, final results, or update.
- Never turn lists/results/updates into a fresh vacancy.
- Do not copy the Blogger title as the first line.
- Do not use repetitive database labels such as "الجهة:", "المكان:", "عدد المناصب:" line after line.
- Never invent salary, deadline, count, requirement, location, urgency, application method, or status.
- No manufactured urgency, guaranteed acceptance, curiosity bait, or requests
  for likes/shares/comments such as "اكتب مهتم" or "شارك ليصلك العرض".
- Do not include any URL. The Blogger URL is posted separately in the first comment.
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
    if text.count("أول تعليق") != 1:
        raise SocialAIQualityError('Facebook social copy must contain "أول تعليق" once')
    if "#" in text:
        raise SocialAIQualityError("Facebook social copy must not contain hashtags")
    if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", text):
        raise SocialAIQualityError("Facebook social copy must be written entirely in Arabic")
    if len(re.findall(r"[\u0600-\u06FF]", text)) < 40:
        raise SocialAIQualityError("Facebook social copy must be Arabic-first")
    if not (120 <= len(text) <= 1200):
        raise SocialAIQualityError("Facebook social copy length must be 120 to 1200 characters")

    title = str((article or {}).get("seo_title") or (article or {}).get("title") or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not (3 <= len(lines) <= 6):
        raise SocialAIQualityError("Facebook social copy needs 3 to 6 short paragraphs")
    first_line = lines[0]
    if not (18 <= len(first_line) <= 140):
        raise SocialAIQualityError("Facebook social copy hook must be 18 to 140 characters")
    if "أول تعليق" not in lines[-1]:
        raise SocialAIQualityError("Facebook social copy CTA must be the final paragraph")
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
    place = f" في {location}" if location else ""
    hook = f"{stage}{place}: راجع التفاصيل قبل الترشيح." if active else (
        f"{stage}{place}: اطّلع على تفاصيل هذا الإعلان."
    )

    summary = title
    if company and company not in summary:
        summary = f"{summary}. الجهة المعلنة: {company}" if summary else f"الجهة المعلنة: {company}"
    if not summary:
        summary = "تعرّف على تفاصيل هذا الإعلان كما وردت في المقال المنشور."
    lines = [hook, summary.rstrip("。.!؟") + "."]

    facts = []
    if active and positions and positions not in {"0", "0.0"}:
        facts.append(f"عدد المناصب {positions}")
    if facts:
        lines.append("، ".join(facts) + ".")
    if deadline and active and deadline not in summary:
        lines.append(f"آخر أجل للترشيح: {deadline}.")

    lines.append(
        "راجع تفاصيل الإعلان قبل تقديم ترشيحك؛ رابط المقال في أول تعليق 👇."
        if active else
        "للاطلاع على تفاصيل هذا الإعلان، تجد رابط المقال في أول تعليق 👇."
    )

    text = "\n\n".join(line for line in lines if line.strip())
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
