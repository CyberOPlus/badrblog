#!/usr/bin/env python3
"""Read only published public CyberoPlus posts/pages. Never export drafts or credentials."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from google.auth.transport.requests import AuthorizedSession
from blogger_preflight import credentials, get

BLOG_ID = "2750143140558868280"
HOSTS = {"cyberoplus.com", "www.cyberoplus.com"}
FIELDS = ("id", "title", "url", "content", "labels", "published", "updated", "customMetaData")

def require(value, message):
    if not value:
        raise RuntimeError(message)

def published_resources(session, resource):
    result, seen, cursor = [], set(), None
    for _ in range(20):
        query = {"maxResults": 100, "view": "READER"}
        if resource == "posts":
            query.update(status="live", fetchBodies="true")
        if cursor:
            require(cursor not in seen, "Repeated pagination cursor")
            seen.add(cursor)
            query["pageToken"] = cursor
        data = get(session, "/blogs/" + BLOG_ID + "/" + resource, query)
        for item in data.get("items") or []:
            require(item.get("status", "LIVE") == "LIVE", "Unpublished content returned; aborting")
            require(str(item.get("blog", {}).get("id", BLOG_ID)) == BLOG_ID, "Unexpected blog")
            require(all(item.get(k) for k in ("id", "title", "url", "content")), "Incomplete resource")
            parts = urlsplit(item["url"])
            require(parts.scheme == "https" and parts.hostname in HOSTS, "Unexpected public URL")
            if resource == "pages":
                require(parts.path.startswith("/p/"), "Unexpected page permalink")
            result.append({k: item[k] for k in FIELDS if k in item})
        cursor = data.get("nextPageToken")
        if not cursor:
            require(len(result) == len({x["id"] for x in result}), "Duplicate IDs")
            return result
    raise RuntimeError("Unexpected pagination volume")

def main():
    require(os.environ.get("BLOGGER_BLOG_ID") == BLOG_ID, "Wrong configured blog")
    session = AuthorizedSession(credentials())
    blog = get(session, "/blogs/" + BLOG_ID)
    require(str(blog.get("id")) == BLOG_ID and urlsplit(blog.get("url", "")).hostname in HOSTS, "Wrong destination")
    posts = published_resources(session, "posts")
    pages = published_resources(session, "pages")
    require(len(posts) == 259 and len(pages) == 6, "Inventory changed; review before export")
    snapshot = {
        "manifest_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "site": "https://www.cyberoplus.com/",
        "blog_id": BLOG_ID,
        "source_kind": "blogger_api_published_public_content",
        "fresh_export": True,
        "post_count": len(posts),
        "page_count": len(pages),
        "includes_drafts": False,
        "includes_scheduled_posts": False,
        "posts": posts,
        "pages": pages,
    }
    output = Path("migration-output")
    output.mkdir(exist_ok=True)
    content = json.dumps(snapshot, ensure_ascii=False, indent=2)
    (output / "cyberoplus-published-source.json").write_text(content, encoding="utf-8")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    (output / "SHA256SUM.txt").write_text(digest + "  cyberoplus-published-source.json\n", encoding="utf-8")
    print("PUBLIC_EXPORT_OK: 259 live posts and 6 live pages. No drafts, scheduled posts, account profiles or credentials exported.")
    print("READ_ONLY: no Blogger writes. SHA256=" + digest)

if __name__ == "__main__":
    main()
