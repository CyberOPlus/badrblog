#!/usr/bin/env python3
"""Publish exactly six verified existing CyberoPlus Blogger pages. No templates, no new Blogger pages."""
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

BLOG_ID = "2750143140558868280"
HOSTS = {"cyberoplus.com", "www.cyberoplus.com"}
ROOT = Path("migration/legal-pages")
BASE = "https://www.googleapis.com/blogger/v3/blogs/" + BLOG_ID
PAGE_DATA = [
    ("7048792332168336031", "Privacy Policy", "سياسة الخصوصية", "https://www.cyberoplus.com/p/privacy-policy.html", "2025-03-14T02:42:00Z", "18b00b9585955b044f4e3eb770c5398bd3adf584a3c1988b2b98d04eae828bd3"),
    ("5240089414009174572", "Cookie Policy", "سياسة الكوكيز", "https://www.cyberoplus.com/p/cookie.html", "2025-04-01T14:23:00Z", "582803428ea237082a8aa85d5d35e93db624ee9e633c013f302508fb23f5c75a"),
    ("6820969043412617138", "Terms of Use", "شروط الاستخدام", "https://www.cyberoplus.com/p/terms-of-use.html", "2025-03-14T02:16:00Z", "c7317c9592c1c15b59610f478e37df98a65660435b916c97ffb932b27627b000"),
    ("7151465361445641614", "Disclaimer", "إخلاء المسؤولية", "https://www.cyberoplus.com/p/disclaimer.html", "2025-03-14T02:22:00Z", "004fd198b5628e1e1409d0aec80a4ede4be7ef4364ed63adfd2b3f16130fc1d8"),
    ("5071168766015911433", "About Us", "من نحن ", "https://www.cyberoplus.com/p/about-us.html", "2025-03-14T02:15:00Z", "e2409a943aa10e4d3e42af3707251e9b6603cdb99db11d119b419872cfc48c98"),
    ("2878009689065204652", "Contact Us", "اتصل بنا ", "https://www.cyberoplus.com/p/contact-us.html", "2025-03-14T02:14:00Z", "9b8bf884818fa554b05137103bb43974c1c5e760eb4c28ab2a7342281f363cf0"),
]
VALID_INTERNAL = {
    "/p/privacy-policy.html", "/p/cookie.html", "/p/terms-of-use.html",
    "/p/disclaimer.html", "/p/about-us.html", "/p/contact-us.html",
}
CONTACT_ID = "blogger-contact-form"

def check(value, reason):
    if not value:
        raise RuntimeError(reason)

def sha(data):
    return hashlib.sha256(data.encode("utf-8")).hexdigest()

def authorized_session():
    token = json.loads(os.environ["BLOGGER_TOKEN_JSON"])
    client = json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
    config = client.get("installed") or client.get("web") or {}
    for field in ("client_id", "client_secret", "token_uri"):
        if not token.get(field) and config.get(field):
            token[field] = config[field]
    return AuthorizedSession(Credentials.from_authorized_user_info(token))

def read_page(session, pid):
    r = session.get(BASE + "/pages/" + pid, params={"view": "ADMIN"}, timeout=40)
    r.raise_for_status()
    return r.json()

def validate_html(pid, text):
    doc = BeautifulSoup(text, "html.parser")
    check(1300 <= len(text) <= 30000, pid + ": unexpected content length")
    check(len(doc.select("h2")) >= 3, pid + ": insufficient headings")
    check(not re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", text), pid + ": untranslated Arabic")
    check(not doc.find(["script", "style", "iframe", "form"]), pid + ": embedded active markup not allowed in edited page")
    check("/p/cookie-policy.html" not in text, pid + ": old cookie permalink")
    check("contact@cyberoplus.com" in text, pid + ": missing established contact address")
    for link in doc.find_all("a", href=True):
        dest = link["href"]
        if dest.startswith("/p/"):
            check(dest in VALID_INTERNAL, pid + ": unknown internal page link " + dest)
        if dest.startswith("http"):
            check(urlsplit(dest).scheme == "https", pid + ": insecure external link " + dest)
    if pid == "2878009689065204652":
        nodes = doc.select("#blogger-contact-form")
        check(len(nodes) == 1 and nodes[0].name == "div", pid + ": Blogger contact form mounting element missing")
        check(doc.find("noscript") is not None, pid + ": JS-disabled fallback lost")
    else:
        check(CONTACT_ID not in text, pid + ": unexpected form marker")
    check(len(doc.find_all("h1")) == 0, pid + ": Blogger page title already supplies H1")

def main():
    check(os.environ.get("BLOGGER_BLOG_ID") == BLOG_ID, "Wrong Blogger secret target")
    session = authorized_session()
    r = session.get(BASE, timeout=35)
    r.raise_for_status()
    blog = r.json()
    check(str(blog.get("id")) == BLOG_ID and (urlsplit(blog.get("url", "")).hostname or "").lower() in HOSTS, "Blogger blog mismatch")
    current, targets, planned = {}, {}, {}
    # Preflight ALL six existing pages BEFORE sending any write request.
    for pid, title, oldtitle, url, published, oldhash in PAGE_DATA:
        body = (ROOT / (pid + ".html")).read_text(encoding="utf-8")
        validate_html(pid, body)
        live = read_page(session, pid)
        check(str(live.get("id")) == pid and live.get("url") == url and live.get("published") == published and live.get("status") == "LIVE", pid + ": URL/ID/status/published mismatch")
        old = live.get("title") == oldtitle and sha(live.get("content", "")) == oldhash
        already = live.get("title") == title and sha(live.get("content", "")) == sha(body)
        check(old or already, pid + ": source changed since verified export; refusing to overwrite user's newer edits")
        current[pid] = live
        targets[pid] = body
        planned[pid] = "SKIP_VERIFIED" if already else "UPDATE"
        print("PREFLIGHT: " + pid + " " + planned[pid] + " " + url, flush=True)
    print("PREFLIGHT_ALL_SIX_OK: target blog, exact original content, URLs, publish dates, titles, contact widget and links verified", flush=True)
    updated = 0
    skipped = 0
    for pid, title, oldtitle, url, published, oldhash in PAGE_DATA:
        if planned[pid] == "SKIP_VERIFIED":
            skipped += 1
            print("ALREADY_VERIFIED: " + url, flush=True)
            continue
        # Re-read immediately before updating to avoid overwriting an intervening manual edit.
        latest = read_page(session, pid)
        check(latest.get("title") == oldtitle and latest.get("published") == published and sha(latest.get("content", "")) == oldhash, pid + ": concurrent change detected; stopping to prevent overwrite")
        payload = {"title": title, "content": targets[pid]}
        r = session.patch(BASE + "/pages/" + pid, json=payload, timeout=70)
        r.raise_for_status()
        saved = read_page(session, pid)
        check(str(saved.get("id")) == pid and saved.get("url") == url and saved.get("published") == published and saved.get("status") == "LIVE" and saved.get("title") == title, pid + ": CRITICAL page identity/URL/date/title mismatch")
        check(sha(saved.get("content", "")) == sha(targets[pid]), pid + ": CRITICAL saved HTML differs")
        validate_html(pid, saved["content"])
        updated += 1
        print("PUBLISHED_AND_VERIFIED: " + title + " " + url, flush=True)
    print("LEGAL_PAGES_COMPLETE: published=" + str(updated) + " previously_verified=" + str(skipped) + " total=6", flush=True)

if __name__ == "__main__":
    main()
