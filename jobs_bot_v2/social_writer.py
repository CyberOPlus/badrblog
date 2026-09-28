from __future__ import annotations

import json

from .ai_engine import generate_json
from .config import PROMPTS_DIR, WHATSAPP_CHANNEL_URL
from .models import SocialPackage


PROMPT_PATH = PROMPTS_DIR / "facebook_jobs_ar.txt"


def _validate(data):
    post = str(data.get("facebook_post") or "").strip()
    card = str(data.get("card_title") or "").strip()
    if len(post) < 80 or len(post) > 900:
        raise ValueError("facebook post length")
    if "http://" in post or "https://" in post:
        raise ValueError("Facebook caption must not contain URLs")
    if not card or len(card.split()) > 11:
        raise ValueError("card title must be 1-11 words")


def write_social(article, blogger_url):
    payload = {
        "article": article.to_dict(),
        "blogger_url": blogger_url,
    }
    prompt = PROMPT_PATH.read_text(encoding="utf-8").replace(
        "{{ARTICLE_JSON}}",
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    data = generate_json(prompt, validator=_validate)

    comment_lines = [
        "🔗 التفاصيل والتقديم:",
        blogger_url,
    ]
    if WHATSAPP_CHANNEL_URL:
        comment_lines += [
            "",
            "📲 قناة واتساب للعروض الجديدة:",
            WHATSAPP_CHANNEL_URL,
        ]

    return SocialPackage(
        facebook_post=str(data["facebook_post"]).strip(),
        first_comment="\n".join(comment_lines),
        card_title=str(data["card_title"]).strip(),
    )
