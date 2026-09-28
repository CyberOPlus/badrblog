from __future__ import annotations

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from .ai_engine import generate_json
from .config import BLOG_LABEL, PROMPTS_DIR
from .models import ArticlePackage


PROMPT_PATH = PROMPTS_DIR / "job_article_ar.txt"


def _word_count(html):
    text = BeautifulSoup(str(html or ""), "html.parser").get_text(" ", strip=True)
    return len(re.findall(r"\S+", text))


def _validate(data):
    required = ["title","seo_title","meta_description","slug","html","card_title","labels"]
    missing = [k for k in required if not str(data.get(k, "")).strip() and k != "labels"]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    if not isinstance(data.get("labels"), list):
        raise ValueError("labels must be an array")
    html = str(data["html"])
    if "<script" in html.lower() or "<style" in html.lower():
        raise ValueError("unsafe HTML")
    if _word_count(html) < 220:
        raise ValueError("article too short")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+){2,8}", str(data["slug"])):
        raise ValueError("invalid slug")
    if not (35 <= len(str(data["seo_title"])) <= 75):
        raise ValueError("SEO title length")
    if not (90 <= len(str(data["meta_description"])) <= 180):
        raise ValueError("meta description length")


def write_article(candidate):
    template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = template.replace(
        "{{JOB_JSON}}",
        json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2),
    )
    data = generate_json(prompt, validator=_validate)
    labels = [BLOG_LABEL] + [str(x).strip() for x in data.get("labels", []) if str(x).strip()]
    labels = list(dict.fromkeys(labels))[:6]
    return ArticlePackage(
        title=str(data["title"]).strip(),
        seo_title=str(data["seo_title"]).strip(),
        meta_description=str(data["meta_description"]).strip(),
        slug=str(data["slug"]).strip(),
        html=str(data["html"]).strip(),
        card_title=str(data["card_title"]).strip(),
        labels=labels,
        facts=candidate.to_dict(),
        source_url=candidate.canonical_url or candidate.source_url,
        application_url=candidate.application_url,
    )
