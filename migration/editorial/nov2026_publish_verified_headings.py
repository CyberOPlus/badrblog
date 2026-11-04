#!/usr/bin/env python3
"""One-time surgical correction of vetted technical headings in two existing posts.

Never changes title, labels, permalink, publication timestamp, body markup outside
plain-text h2/h3 headings, links, images, code, or tables. Requires exact current
SHA256 observed from live Blogger QA and confirms writes by a second read.
"""
import hashlib
import html
import json
import os
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

BLOG_ID = "2750143140558868280"
API = "https://www.googleapis.com/blogger/v3/blogs/" + BLOG_ID

PLANS = [
    {
        "id": "8600317456877269158",
        "title": "Coaxial Cable Explained: Structure and Modern Uses",
        "url": "https://www.cyberoplus.com/2026/03/coaxial-cable-networking-guide.html",
        "expected_sha": "98d39a90fe39572530344a7fd34328c0ffd52f2f22098c70d63450b8d72f8789",
        "headings": {
            "What is Coaxial Cable and why is it called a pivotal cable?":
                "What Is Coaxial Cable, and Why Is It Called Coaxial?",
            "What's the pivotal cable?":
                "What Is Inside a Coaxial Cable?",
            "How do data move inside Coaxial Cable?":
                "How Do Signals Travel Through Coaxial Cable?",
            "What do you mean 50° and 75° in the pivotal cable?":
                "What Do 50 Ω and 75 Ω Mean in Coaxial Cable?",
            "When does he use 50 imams and when to use 75 imams?":
                "When Should You Use 50 Ω or 75 Ω Coaxial Cable?",
            "Why does the line need Termination correct?":
                "Why Does Coaxial Cable Need Proper Termination?",
            "How was Coaxial Cable used in old Ethernet?":
                "How Was Coaxial Cable Used in Legacy Ethernet?",
            "Why was Bus a practical problem?":
                "Why Was Coaxial Bus Ethernet Difficult to Maintain?",
            "Have you finished using Coaxial Cable?":
                "Is Coaxial Cable Still Used Today?",
        },
    },
    {
        "id": "8842376614236276048",
        "title": "Scrapling: Python Web Scraping Framework and Its Features",
        "url": "https://www.cyberoplus.com/2026/04/scrapling-python-web-scraping-framework-adaptive-anti-bot.html",
        "expected_sha": "35dd840f5a5b334b622a585b7c24481b50394c373447a15073ea0c4e6c62c2ae",
        "headings": {
            "Three Fee tchers for three types of sites.":
                "Three Fetchers for Different Website Requirements",
            "Fe tcher for regular requests":
                "Fetcher: HTTP Requests for Static Pages",
            "DynamicF e tcher for pages based on Java Script":
                "DynamicFetcher: JavaScript-Rendered Pages",
            "StealthyF e To higher-protected sites":
                "StealthyFetcher: Browser Fetching with Stealth Features",
            "Pause andResume":
                "Pause and Resume Crawls",
            "Deal with robots. txt":
                "Handling robots.txt",
            "Broxy management in Scrapling":
                "Proxy Management in Scrapling",
            "Extract from page XHR and Fetch responses":
                "Capture XHR and Fetch Responses",
        },
    },
]

PATTERN = re.compile(r"(<h([23])\b[^>]*>)(.*?)(</h\2\s*>)", re.I | re.S)
PROTECTED = ("img", "a", "pre", "code", "table", "script", "iframe", "style", "textarea")

def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def check(condition, reason):
    if not condition:
        raise ValueError(reason)

def edit_headings(old_html, map_):
    counts = {name: 0 for name in map_}
    def rewrite(match):
        start, level, inner, end = match.groups()
        plain = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if plain not in map_:
            return match.group(0)
        check("<" not in inner and ">" not in inner,
              "Heading includes child HTML; requires individual review: " + plain)
        counts[plain] += 1
        return start + html.escape(map_[plain], quote=False) + end
    result = PATTERN.sub(rewrite, old_html)
    check(all(value == 1 for value in counts.values()),
          "Expected exact unique plain heading matches: " + json.dumps(counts))
    old_soup = BeautifulSoup(old_html, "html.parser")
    new_soup = BeautifulSoup(result, "html.parser")
    for tag in PROTECTED:
        check([str(n) for n in old_soup.find_all(tag)] ==
              [str(n) for n in new_soup.find_all(tag)],
              "Protected HTML changed for tag: " + tag)
    check(len(old_soup.find_all(["h2", "h3"])) ==
          len(new_soup.find_all(["h2", "h3"])),
          "Heading elements changed in count")
    check(sha(result) != sha(old_html), "No actual correction produced")
    return result, counts

token = json.loads(os.environ["BLOGGER_TOKEN_JSON"])
oauth = json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
cfg = oauth.get("installed") or oauth.get("web") or {}
for field in ("client_id", "client_secret", "token_uri"):
    if not token.get(field) and cfg.get(field):
        token[field] = cfg[field]
session = AuthorizedSession(Credentials.from_authorized_user_info(token))
response = session.get(API, timeout=40)
response.raise_for_status()
blog = response.json()
check(str(blog.get("id")) == BLOG_ID and
      (urlsplit(blog.get("url", "")).hostname or "").lower()
      in {"cyberoplus.com", "www.cyberoplus.com"}, "Wrong blog")
def get(pid):
    r = session.get(API + "/posts/" + pid,
                    params={"view": "ADMIN", "fetchBody": "true"}, timeout=60)
    r.raise_for_status()
    return r.json()

reports = []
for plan in PLANS:
    pid = plan["id"]
    report = {"id": pid, "url": plan["url"], "status": "NOT_UPDATED"}
    try:
        before = get(pid)
        old = before.get("content", "")
        check(str(before.get("id")) == pid and before.get("status") == "LIVE",
              "Original article identity/status differs")
        check(before.get("url") == plan["url"] and
              before.get("title") == plan["title"], "Original title/URL changed")
        check(sha(old) == plan["expected_sha"],
              "Live HTML changed since reviewed QA; skip to prevent overwrite")
        changed_html, counts = edit_headings(old, plan["headings"])
        current = get(pid)
        check(current.get("content") == old and
              current.get("updated") == before.get("updated"),
              "Concurrent blogger edit; skip")
        patched = session.patch(API + "/posts/" + pid,
                                json={"content": changed_html}, timeout=90)
        patched.raise_for_status()
        verified = get(pid)
        for field in ("id", "url", "published", "title", "status", "labels"):
            check(verified.get(field) == before.get(field),
                  "CRITICAL: post metadata changed: " + field)
        check(sha(verified.get("content", "")) == sha(changed_html),
              "CRITICAL: published body checksum mismatch")
        report["status"] = "PUBLISHED_AND_VERIFIED"
        report["corrected_headings"] = len(counts)
        report["new_sha"] = sha(changed_html)
    except Exception as exc:
        report["status"] = "BLOCKED_NO_CLAIM"
        report["reason"] = type(exc).__name__ + ": " + str(exc)[:250]
    reports.append(report)
    print("TECHNICAL_HEADING_RESULT=" + json.dumps(report, ensure_ascii=False), flush=True)
    if "CRITICAL" in report.get("reason", ""):
        break
    time.sleep(0.2)
print("FINAL_TECHNICAL_CORRECTION_SUMMARY=" + json.dumps({
    "published_verified": sum(r["status"] == "PUBLISHED_AND_VERIFIED" for r in reports),
    "blocked": sum(r["status"] != "PUBLISHED_AND_VERIFIED" for r in reports),
    "reports": reports
}, ensure_ascii=False), flush=True)
