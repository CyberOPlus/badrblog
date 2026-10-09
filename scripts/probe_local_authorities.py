"""Read-only GitHub-runner smoke probe for the approved local-government source.

No job gets published, no queue is mutated. This verifies that the runner can
fetch and extract actual competition detail URLs from the source before enabling.
"""
import json
from pathlib import Path

import requests

from scraper import _parse_emploi_public_links


def main():
    config = json.loads(Path("sources.json").read_text(encoding="utf-8"))
    row = next(
        (s for category in config["categories"] for s in category["sources"]
         if s.get("name") == "Emploi-Public — collectivités territoriales"),
        None,
    )
    if not row:
        raise RuntimeError("Official collectivités source missing from configured allowlist")
    url = row["base_url"]
    response = requests.get(
        url,
        timeout=12,
        headers={"User-Agent": "Mozilla/5.0 (compatible; CyberOPlus-Jobs-Source-Check/1.0)"},
    )
    response.raise_for_status()
    discovered = _parse_emploi_public_links(response.text, url, per_source_limit=8)
    print("listing_status", response.status_code)
    print("verified_official_detail_links", len(discovered))
    for item in discovered[:3]:
        print("sample_competition", item.get("url"), "deadline", item.get("job_deadline", "unverified"))
    if not discovered:
        raise RuntimeError("Official listing returned zero extractable detail links")
    detail = requests.get(discovered[0]["url"], timeout=12, headers={"User-Agent": "Mozilla/5.0"})
    print("sample_detail_status", detail.status_code)
    detail.raise_for_status()
    text = detail.text.lower()
    if not ("concours" in text or "المباراة" in text or "المباريات" in text or "emploi" in text):
        raise RuntimeError("Detail page did not contain recognizable official competition content")
    print("runner_listing_and_detail_smoke", "PASS")
    print("NOTE: This does not certify that a specific competition has an account-free application or Bac+2 requirements.")


if __name__ == "__main__":
    main()
