from __future__ import annotations

import argparse
import json
from dataclasses import fields
from pathlib import Path

from .article_writer import write_article
from .expiry import expiry_status
from .jobposting import build_jobposting
from .metrics import publication_dimensions, record_event
from .models import JobCandidate
from .quality import choose_best, score_candidate
from .social_writer import write_social
from .state import (
    already_published,
    can_publish_today,
    can_use_urgent_override,
)
from .timing import recommended_facebook_time
from .urgency import classify_urgency


CANDIDATE_FIELDS = {field.name for field in fields(JobCandidate)}


def _candidate_from_row(row):
    clean = {key: value for key, value in dict(row or {}).items() if key in CANDIDATE_FIELDS}
    return JobCandidate(**clean)


def _load_candidates(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else raw.get("candidates", [])
    return [_candidate_from_row(row) for row in rows]


def preview_candidate(candidate, blogger_url=""):
    quality = score_candidate(candidate)
    if not quality["passed"]:
        return {"ok": False, "stage": "quality", "quality": quality}

    urgency = classify_urgency(candidate)
    expiry = expiry_status(candidate)
    if expiry.get("expired"):
        return {
            "ok": False,
            "stage": "expiry",
            "quality": quality,
            "urgency": urgency,
            "expiry": expiry,
        }

    article = write_article(candidate)
    social = write_social(article, blogger_url)
    jobposting = build_jobposting(candidate, article, blogger_url)
    facebook_timing = recommended_facebook_time(urgency)

    return {
        "ok": True,
        "quality": quality,
        "urgency": urgency,
        "expiry": expiry,
        "facebook_timing": facebook_timing,
        "article": article.to_dict(),
        "jobposting": jobposting,
        "social": social.to_dict(),
        "metrics_dimensions": publication_dimensions(candidate, quality, urgency),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--candidate-json",
        required=True,
        help="JSON file containing one or more normalized candidates",
    )
    parser.add_argument(
        "--blogger-url",
        default="",
        help="Final single-job Blogger URL; leave empty in pre-publish preview",
    )
    args = parser.parse_args()

    candidates = [c for c in _load_candidates(args.candidate_json) if not already_published(c)]
    selected, quality, ranked = choose_best(candidates)

    if not selected:
        result = {
            "ok": False,
            "reason": "no candidate passed quality threshold",
            "ranked": [
                {
                    "score": score,
                    "title": candidate.title,
                    "company": candidate.company,
                    "reasons": q["reasons"],
                    "points": q.get("points", {}),
                }
                for score, q, candidate in ranked[:10]
            ],
        }
        record_event("selection_empty", candidates=len(candidates))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    urgency = classify_urgency(selected)
    normal_slot = can_publish_today()
    urgent_override = (
        not normal_slot
        and urgency.get("allow_daily_override")
        and can_use_urgent_override()
    )

    if not normal_slot and not urgent_override:
        result = {
            "ok": False,
            "reason": "daily publish limit reached; best candidate kept for later",
            "selected": selected.to_dict(),
            "quality": quality,
            "urgency": urgency,
        }
        record_event(
            "daily_limit_hold",
            **publication_dimensions(selected, quality, urgency),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    result = preview_candidate(selected, blogger_url=args.blogger_url)
    result["publishing_policy"] = {
        "normal_daily_slot": normal_slot,
        "urgent_override": urgent_override,
        "facebook_action": result.get("facebook_timing", {}).get("mode", "scheduled"),
        "facebook_publish_at": result.get("facebook_timing", {}).get("publish_at", ""),
    }

    record_event(
        "candidate_selected",
        **publication_dimensions(selected, quality, urgency),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
