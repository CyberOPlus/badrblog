#!/usr/bin/env python3
"""Create or verify exactly one CyberoPlus Blogger DRAFT. Never publish."""
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

BLOG_ID_EXPECTED = "2750143140558868280"
ALLOWED_HOSTS = {"cyberoplus.com", "www.cyberoplus.com"}
PAYLOAD = Path(__file__).resolve().parent / "draft.json"
API = "https://www.googleapis.com/blogger/v3/blogs/"

def ensure(condition, reason):
    if not condition:
        raise RuntimeError(reason)

def response_json(resp):
    resp.raise_for_status()
    return resp.json()

def main():
    content = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    ensure(content.get("state") == "DRAFT_ONLY", "Draft-only payload marker missing")
    ensure(content.get("language") == "fr", "Wrong language")
    ensure(set(content.get("labels", [])) == {"artificial-intelligence","guides","apps"}, "Labels unexpected")
    ensure(all(s in content["content"] for s in ("DFFSDP","NLQPB2","https://muse.ai/join")), "Referral codes missing")
    ensure("__URL_IMAGE_" not in content["content"], "Unresolved image placeholders")
    ensure(content["content"].count('<img ') == 2, "Expected 2 illustrations")
    ensure("raw.githubusercontent.com/CyberOPlus/badrblog/main/assets/muse/" in content["content"], "GitHub illustrations missing")

    blog_id = os.environ.get("BLOGGER_BLOG_ID", "").strip()
    ensure(blog_id == BLOG_ID_EXPECTED, "Unexpected BLOGGER_BLOG_ID; refusing write")
    token = json.loads(os.environ["BLOGGER_TOKEN_JSON"])
    client = json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
    client = client.get("installed") or client.get("web") or {}
    for field in ("client_id", "client_secret", "token_uri"):
        if not token.get(field) and client.get(field):
            token[field] = client[field]
    creds = Credentials.from_authorized_user_info(token)
    sess = AuthorizedSession(creds)
    base = API + blog_id
    blog = response_json(sess.get(base, timeout=35))
    host = (urlsplit(str(blog.get("url",""))).hostname or "").lower()
    ensure(str(blog.get("id")) == blog_id and host in ALLOWED_HOSTS,
           "Blog identity mismatch; refusing write")
    title = content["title"]
    existing = []
    page_token = None
    for _ in range(20):
        params = {"status":"DRAFT","maxResults":100,"fetchBodies":False}
        if page_token:
            params["pageToken"] = page_token
        listing = response_json(sess.get(base + "/posts",params=params,timeout=40))
        existing += [p for p in listing.get("items",[]) if p.get("title")==title]
        page_token = listing.get("nextPageToken")
        if not page_token:
            break
    ensure(len(existing) <= 1, "Multiple matching drafts found; refusing to create another")
    if existing:
        post_id = existing[0]["id"]
        print("EXISTING_DRAFT_FOUND: "+str(post_id))
    else:
        body = {"kind":"blogger#post","title":title,
                "content":content["content"],"labels":content["labels"],
                "customMetaData":"Muse AI referral guide, French, Cybero Plus"}
        resp = sess.post(base + "/posts",params={"isDraft":"true"},
                         json=body,timeout=80)
        post = response_json(resp)
        post_id = str(post["id"])
        print("DRAFT_CREATED: "+post_id)
    post = response_json(sess.get(base + "/posts/" + str(post_id),
                         params={"view":"ADMIN","fetchBody":"true"},timeout=50))
    ensure(str(post.get("id")) == str(post_id), "Post ID verification failed")
    ensure(post.get("title") == title, "Draft title does not match")
    ensure(post.get("status","").upper() == "DRAFT", "CRITICAL: post is not DRAFT")
    ensure(all(code in post.get("content","") for code in ("DFFSDP","NLQPB2")),
           "Referral codes missing from saved Blogger post")
    ensure(len(post.get("content","")) > 15000, "Saved article appears incomplete")
    ensure(not post.get("published"), "CRITICAL: published timestamp on a draft")
    print("BLOGGER_DRAFT_VERIFIED: ID="+str(post_id)+" STATUS=DRAFT")
    print("BLOGGER_DRAFT_URL: "+str(post.get("url","(no public URL for draft)")))
    print("BLOGGER_ADMIN_EDIT: https://www.blogger.com/blog/post/edit/"+blog_id+"/"+str(post_id))

if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, ValueError, KeyError, RuntimeError) as exc:
        print("DRAFT_WORKFLOW_FAILED: "+str(exc), file=sys.stderr)
        sys.exit(1)
