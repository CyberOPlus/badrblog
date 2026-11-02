#!/usr/bin/env python3
"""Back up Blogger posts and pages without any write API methods.

Output is kept only in a temporary GitHub Actions artifact, never committed
to the repository. The backup contains public post/page content, not secrets.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from google.auth.transport.requests import AuthorizedSession
from blogger_preflight import credentials, get, required_secret, EXPECTED_HOST


def fetch_all(session, url, params=None):
    items = []
    token = None
    seen = set()
    for _ in range(100):
        query = {"maxResults": 100, "fetchBodies": "true", "view": "ADMIN"}
        if params:
            query.update(params)
        if token:
            if token in seen:
                raise RuntimeError("Repeated pagination token")
            seen.add(token)
            query["pageToken"] = token
        result = get(session, url, query)
        for item in result.get("items") or []:
            if not all(k in item for k in ("id", "title", "content")):
                raise RuntimeError("Incomplete Blogger resource; refusing backup")
            items.append(item)
        token = result.get("nextPageToken")
        if not token:
            return items
    raise RuntimeError("More than 100 pagination requests unexpectedly")


def main():
    blog_id = required_secret("BLOGGER_BLOG_ID")
    if not blog_id.isdigit():
        raise RuntimeError("Invalid Blogger ID")
    session = AuthorizedSession(credentials())
    blog = get(session, "/blogs/" + blog_id)
    host = (urlsplit(str(blog.get("url") or "")).hostname or "").lower()
    if str(blog.get("id", "")) != blog_id or host not in (EXPECTED_HOST, "www." + EXPECTED_HOST):
        raise RuntimeError("Blog does not match cyberoplus.com")
    myblogs = get(session, "/users/self/blogs")
    ids = {str(i.get("id")) for i in myblogs.get("items") or []}
    token = myblogs.get("nextPageToken")
    while token:
        nxt = get(session, "/users/self/blogs", {"pageToken": token})
        ids.update(str(i.get("id")) for i in nxt.get("items") or [])
        token = nxt.get("nextPageToken")
    if blog_id not in ids:
        raise RuntimeError("OAuth account does not have access to target blog")
    posts = []
    for status in ("live", "draft", "scheduled"):
        posts.extend(fetch_all(session, "/blogs/" + blog_id + "/posts", {"status": status}))
    pages = fetch_all(session, "/blogs/" + blog_id + "/pages")
    ids = [("post", str(x["id"])) for x in posts] + [("page", str(x["id"])) for x in pages]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Duplicate post/page IDs in backup")
    if len(posts) != 259 or len(pages) != 6:
        raise RuntimeError("Unexpected inventory size; found {} posts and {} pages; expected 259 and 6.".format(len(posts), len(pages)))
    payload = {
        "manifest_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "domain": EXPECTED_HOST,
        "blog_id": blog_id,
        "blog_name": blog.get("name"),
        "posts": posts,
        "pages": pages,
    }
    output = Path("migration-output")
    output.mkdir(exist_ok=True)
    out_file = output / "blogger-originals.json"
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    out_file.write_text(serialized, encoding="utf-8")
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    (output / "SHA256SUM.txt").write_text(digest + "  blogger-originals.json\n", encoding="utf-8")
    print("BACKUP_OK: 259 posts and 6 pages saved; SHA256=" + digest)
    print("READ_ONLY: No Blogger modification requests were issued.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("BACKUP_FAIL: " + type(error).__name__ + ": " + str(error), file=sys.stderr)
        raise SystemExit(1)
