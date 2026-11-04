#!/usr/bin/env python3
"""Read-only audit of current Blogger HTML, titles and basic editorial issues."""
import collections
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession

BID = "2750143140558868280"
BASE = "https://www.googleapis.com/blogger/v3/blogs/" + BID
token = json.loads(os.environ["BLOGGER_TOKEN_JSON"])
client = json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
cfg = client.get("installed") or client.get("web") or {}
for field in ("client_id", "client_secret", "token_uri"):
    if not token.get(field) and cfg.get(field):
        token[field] = cfg[field]
session = AuthorizedSession(Credentials.from_authorized_user_info(token))
blog_response = session.get(BASE, timeout=40)
blog_response.raise_for_status()
blog = blog_response.json()
assert str(blog.get("id")) == BID
assert (urlsplit(blog.get("url", "")).hostname or "").lower() in (
    "cyberoplus.com", "www.cyberoplus.com"
), "Blog identity mismatch"

posts = []
for status in ("live", "draft", "scheduled"):
    page_token = None
    while True:
        params = {
            "status": status, "view": "ADMIN",
            "maxResults": 100, "fetchBodies": "true"
        }
        if page_token:
            params["pageToken"] = page_token
        response = session.get(BASE + "/posts", params=params, timeout=90)
        response.raise_for_status()
        page = response.json()
        posts.extend(page.get("items", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            break

assert len({str(p["id"]) for p in posts}) == len(posts), "Duplicate post IDs"
summary = collections.Counter()
rows = []
arabic = re.compile(r"[\u0600-\u06ff]")
word_re = re.compile(r"\b[\w'-]+\b")
suspect = re.compile(
    r"what does he really do|solemn loading|shat script|what's nmap's tool|"
    r"what are you doing now|the website does not exist|pivotal",
    re.I,
)
for post in posts:
    html = post.get("content", "") or ""
    soup = BeautifulSoup(html, "html.parser")
    for unwanted in soup(["script", "style"]):
        unwanted.decompose()
    visible = soup.get_text(" ", strip=True)
    words = len(word_re.findall(visible))
    headings = [t.get_text(" ", strip=True) for t in soup.find_all(["h2", "h3"])]
    norm = [re.sub(r"\s+", " ", h).casefold() for h in headings if h]
    duplicates = [h for h, count in collections.Counter(norm).items() if count > 1]
    images = soup.find_all("img")
    issues = []
    if arabic.search(post.get("title", "")):
        issues.append("ARABIC_TITLE")
    ar_chars = len(arabic.findall(visible))
    if ar_chars >= 20:
        issues.append("ARABIC_BODY")
    if words < 250 and post.get("status") == "LIVE":
        issues.append("SHORT_LT250_WORDS")
    if not soup.find("h2") and words >= 400:
        issues.append("LONG_WITHOUT_H2")
    if soup.find("h1"):
        issues.append("INLINE_H1")
    if duplicates:
        issues.append("DUPLICATE_HEADINGS")
    if any(len(h) <= 3 for h in headings):
        issues.append("TINY_HEADING")
    if suspect.search(post.get("title", "") + " " + " ".join(headings)):
        issues.append("SUSPICIOUS_TRANSLATION")
    no_alt = sum(not img.get("alt", "").strip() for img in images)
    if no_alt:
        issues.append("IMAGE_MISSING_ALT")
    dangling = [
        a.get("href", "") for a in soup.find_all("a")
        if a.has_attr("href") and (
            not a["href"].strip() or a["href"].strip().lower().startswith("javascript:")
        )
    ]
    if dangling:
        issues.append("INVALID_LINK_HREF")
    internals = sum(
        "cyberoplus.com" in (a.get("href") or "").lower()
        for a in soup.find_all("a", href=True)
    )
    if words >= 500 and internals == 0:
        issues.append("LONG_WITHOUT_INTERNAL_LINK")
    row = {
        "id": str(post.get("id")), "title": post.get("title", ""),
        "url": post.get("url", ""), "published": post.get("published", ""),
        "updated": post.get("updated", ""), "status": post.get("status", ""),
        "labels": post.get("labels", []), "content_sha256": hashlib.sha256(
            html.encode("utf-8")).hexdigest(),
        "words": words, "arabic_chars": ar_chars, "images": len(images),
        "images_without_alt": no_alt, "internal_links": internals,
        "headings": headings, "duplicate_headings": duplicates,
        "issues": issues, "intro_preview": visible[:500]
    }
    rows.append(row)
    summary["total"] += 1
    summary["status_" + str(post.get("status"))] += 1
    for issue in issues:
        summary[issue] += 1

priority = {
    "ARABIC_TITLE": 30, "ARABIC_BODY": 30, "DUPLICATE_HEADINGS": 15,
    "INVALID_LINK_HREF": 15, "SUSPICIOUS_TRANSLATION": 15,
    "TINY_HEADING": 10, "IMAGE_MISSING_ALT": 5, "SHORT_LT250_WORDS": 5,
    "LONG_WITHOUT_H2": 5, "INLINE_H1": 5,
    "LONG_WITHOUT_INTERNAL_LINK": 3
}
rows.sort(key=lambda row: (
    -sum(priority.get(issue, 0) for issue in row["issues"]),
    row["title"]
))
Path("cyberoplus_deep_qa_20261104.json").write_text(
    json.dumps({"summary": dict(summary), "posts": rows},
               indent=2, ensure_ascii=False), encoding="utf-8"
)
print("CYBEROPLUS_QA_SUMMARY=" + json.dumps(
    dict(sorted(summary.items())), ensure_ascii=False), flush=True)
for row in rows[:75]:
    if not row["issues"]:
        continue
    print("CYBEROPLUS_QA_FLAG=" + json.dumps({
        "id": row["id"], "status": row["status"],
        "title": row["title"], "url": row["url"],
        "sha": row["content_sha256"], "words": row["words"],
        "ar_chars": row["arabic_chars"], "issues": row["issues"],
        "duplicate_headings": row["duplicate_headings"],
        "headings": row["headings"][:15],
        "intro": row["intro_preview"][:280]
    }, ensure_ascii=False), flush=True)
