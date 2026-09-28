from __future__ import annotations

import argparse
import json
from pathlib import Path

from .article_writer import write_article
from .models import JobCandidate
from .quality import choose_best, score_candidate
from .social_writer import write_social
from .state import already_published, can_publish_today


def _load_candidates(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else raw.get("candidates", [])
    return [JobCandidate(**row) for row in rows]


def preview_candidate(candidate, blogger_url="https://www.cyberoplus.com/search/label/jobs"):
    quality = score_candidate(candidate)
    if not quality["passed"]:
        return {"ok": False, "stage": "quality", "quality": quality}
    article = write_article(candidate)
    social = write_social(article, blogger_url)
    return {
        "ok": True,
        "quality": quality,
        "article": article.to_dict(),
        "social": social.to_dict(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-json", required=True, help="JSON file containing one or more normalized candidates")
    parser.add_argument("--blogger-url", default="https://www.cyberoplus.com/search/label/jobs")
    args = parser.parse_args()

    candidates = [c for c in _load_candidates(args.candidate_json) if not already_published(c)]
    if not can_publish_today():
        print(json.dumps({"ok": False, "reason": "daily publish limit already reached"}, ensure_ascii=False, indent=2))
        return 0

    selected, quality, ranked = choose_best(candidates)
    if not selected:
        print(json.dumps({
            "ok": False,
            "reason": "no candidate passed quality threshold",
            "ranked": [{"score": s, "title": c.title, "reasons": q["reasons"]} for s,q,c in ranked[:10]],
        }, ensure_ascii=False, indent=2))
        return 0

    result = preview_candidate(selected, blogger_url=args.blogger_url)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
