#!/usr/bin/env python3
"""Read-only preflight for the CyberoPlus Arabic-to-English migration.

Only GET requests to Blogger v3 are allowed. No API writes, no changes to posts,
no credential material in logs, and no content is saved to this public repository.
"""
import json
import os
import sys
from urllib.parse import urlsplit

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

BLOGGER_API = "https://www.googleapis.com/blogger/v3"
EXPECTED_HOST = "cyberoplus.com"


def required_secret(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError("Missing required GitHub Secret: " + name)
    return value


def credentials():
    token_info = json.loads(required_secret("BLOGGER_TOKEN_JSON"))
    if not isinstance(token_info, dict):
        raise RuntimeError("BLOGGER_TOKEN_JSON must be a JSON object")
    client_info = json.loads(required_secret("BLOGGER_CLIENT_SECRET_JSON"))
    if not isinstance(client_info, dict):
        raise RuntimeError("BLOGGER_CLIENT_SECRET_JSON must be a JSON object")
    client = client_info.get("installed") or client_info.get("web") or {}
    if not isinstance(client, dict):
        raise RuntimeError("Unexpected OAuth client configuration")
    for key in ("client_id", "client_secret", "token_uri"):
        if not token_info.get(key) and client.get(key):
            token_info[key] = client[key]
    creds = Credentials.from_authorized_user_info(token_info)
    if not creds.refresh_token:
        raise RuntimeError("OAuth token does not include a refresh token")
    return creds


def get(session, path, params=None):
    response = session.get(BLOGGER_API + path, params=params or {}, timeout=35)
    if response.status_code != 200:
        raise RuntimeError(
            "Blogger read request failed (HTTP {}): {}".format(
                response.status_code, path.split("?")[0].split("/")[1]
            )
        )
    return response.json()


def count_items(session, path, params=None):
    count = 0
    seen = set()
    token = None
    while True:
        q = {"maxResults": 100, "fetchBodies": "false"}
        if params:
            q.update(params)
        if token:
            if token in seen:
                raise RuntimeError("Repeated pagination token")
            seen.add(token)
            q["pageToken"] = token
        data = get(session, path, q)
        items = data.get("items") or []
        count += len(items)
        token = data.get("nextPageToken")
        if not token:
            return count
        if len(seen) > 100:
            raise RuntimeError("Unexpected pagination volume")


def main():
    blog_id = required_secret("BLOGGER_BLOG_ID")
    if not blog_id.isdigit():
        raise RuntimeError("BLOGGER_BLOG_ID must be numeric")
    session = AuthorizedSession(credentials())
    blog = get(session, "/blogs/" + blog_id)
    actual_id = str(blog.get("id", ""))
    url = str(blog.get("url") or "")
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if actual_id != blog_id:
        raise RuntimeError("Blog ID response mismatch")
    if host not in (EXPECTED_HOST, "www." + EXPECTED_HOST):
        raise RuntimeError("Wrong destination blog: domain mismatch (no writes made)")
    print("TARGET_VERIFIED: Blogger returned the expected CyberoPlus domain.")

    owned = get(session, "/users/self/blogs")
    blog_ids = {str(item.get("id")) for item in (owned.get("items") or [])}
    cursor = owned.get("nextPageToken")
    for _ in range(100):
        if not cursor:
            break
        more = get(session, "/users/self/blogs", {"pageToken": cursor})
        blog_ids.update(str(item.get("id")) for item in (more.get("items") or []))
        cursor = more.get("nextPageToken")
    else:
        raise RuntimeError("Blogger ownership listing exceeded expected pagination")
    if blog_id not in blog_ids:
        raise RuntimeError("OAuth account cannot confirm ownership/access to this blog")
    print("OWNER_VERIFIED: Connected Google account can see target blog.")

    posts_live = count_items(session, "/blogs/" + blog_id + "/posts", {"status": "live"})
    posts_draft = count_items(session, "/blogs/" + blog_id + "/posts", {"status": "draft"})
    posts_scheduled = count_items(session, "/blogs/" + blog_id + "/posts", {"status": "scheduled"})
    pages = count_items(session, "/blogs/" + blog_id + "/pages")
    if posts_live + posts_draft + posts_scheduled < 1:
        raise RuntimeError("No posts returned. Refusing to proceed.")
    print("READ_ONLY_COUNTS: live_posts={}, drafts={}, scheduled={}, pages={}".format(
        posts_live, posts_draft, posts_scheduled, pages
    ))
    print("PREFLIGHT_PASS: Only GET requests sent. No Blogger content modified.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("PREFLIGHT_FAIL: {}: {}".format(type(exc).__name__, str(exc)), file=sys.stderr)
        raise SystemExit(1)
