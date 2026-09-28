from __future__ import annotations

import argparse
import json
from dataclasses import fields
from pathlib import Path

from .article_writer import write_article
from .expiry import expiry_status
from .fact_resolver import reconcile_batch
from .identity_review import review_ambiguous_identity
from .jobposting import build_jobposting
from .memory_store import assign_campaign_id
from .metrics import publication_dimensions, record_event
from .models import JobCandidate
from .quality import choose_best, score_candidate
from .social_writer import write_social
from .state import (
    can_publish_today,
    can_use_urgent_override,
    classify_candidate,
    load_state,
)
from .timing import recommended_facebook_time
from .update_policy import update_actions
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

    state = load_state()
    raw_candidates = _load_candidates(args.candidate_json)
    reconciled_candidates, source_conflicts = reconcile_batch(raw_candidates)
    eligible = []
    identity_meta = {}
    held = []
    duplicates = []

    for candidate in reconciled_candidates:
        decision, existing = classify_candidate(candidate, state=state)
        action = decision.action
        review = None

        if action == "needs_review":
            try:
                review = review_ambiguous_identity(candidate, existing or {})
                action = review.get("action", "hold")
            except Exception as exc:
                action = "hold"
                review = {
                    "action": "hold",
                    "accepted": False,
                    "reason": f"AI identity review unavailable: {exc}",
                }

        if action == "duplicate":
            duplicates.append({
                "title": candidate.title,
                "company": candidate.company,
                "reason": decision.reason,
            })
            continue

        if action == "hold":
            held.append({
                "title": candidate.title,
                "company": candidate.company,
                "reason": decision.reason,
                "review": review,
            })
            continue

        if action == "update" and not decision.material_update:
            duplicates.append({
                "title": candidate.title,
                "company": candidate.company,
                "reason": "no material update",
            })
            continue

        eligible.append(candidate)
        identity_meta[id(candidate)] = {
            "action": action,
            "decision": decision.to_dict(),
            "existing": existing or {},
            "review": review,
        }

    selected, quality, ranked = choose_best(eligible)

    if not selected:
        result = {
            "ok": False,
            "reason": "no new/update candidate passed quality and identity gates",
            "held": (source_conflicts + held)[:10],
            "duplicates": duplicates[:10],
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
        record_event(
            "selection_empty",
            candidates=len(raw_candidates),
            held=len(source_conflicts) + len(held),
            duplicates=len(duplicates),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    identity = identity_meta[id(selected)]
    urgency = classify_urgency(selected)
    is_update = identity["action"] == "update"

    campaign_id = assign_campaign_id(
        selected,
        existing=identity.get("existing") if is_update else None,
    )
    if not isinstance(selected.raw, dict):
        selected.raw = {}
    selected.raw["_campaign_id"] = campaign_id
    identity["campaign_id"] = campaign_id

    # Updating an existing page preserves factual accuracy and does not consume
    # the daily new-article quota.
    normal_slot = True if is_update else can_publish_today(state=state)
    urgent_override = (
        not is_update
        and not normal_slot
        and urgency.get("allow_daily_override")
        and can_use_urgent_override(state=state)
    )

    if not normal_slot and not urgent_override:
        result = {
            "ok": False,
            "reason": "daily new-article limit reached; best candidate kept for later",
            "selected": selected.to_dict(),
            "quality": quality,
            "urgency": urgency,
            "identity": identity,
        }
        record_event(
            "daily_limit_hold",
            **publication_dimensions(selected, quality, urgency),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    result = preview_candidate(selected, blogger_url=args.blogger_url)
    result["identity"] = identity
    if is_update:
        result["update_policy"] = update_actions(
            selected,
            identity.get("existing", {}),
            urgency,
        )
    result["publishing_policy"] = {
        "normal_daily_slot": normal_slot,
        "urgent_override": urgent_override,
        "is_existing_job_update": is_update,
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
