#!/usr/bin/env python3
"""One-shot on-runner, offline Arabic-to-English content conversion for CyberoPlus.

Reads the *existing* GitHub Actions Blogger export, checks every current source
against that snapshot, translates visible prose with a locally installed Argos
model (no remote translation API), preserves article HTML assets/links/code,
adds only demonstrably relevant contextual internal links, and PATCHes Blogger.
Does not write content backups, progress files or external translation data.
"""
import hashlib
import json
import os
import re
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup, NavigableString
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

BLOG_ID = "2750143140558868280"
MODEL_URL = "https://data.argosopentech.com/argospm/v1/translate-ar_en-1_0.argosmodel"
SHARD = int(os.environ["SHARD_ID"])
SHARDS = int(os.environ.get("SHARD_COUNT", "24"))
ARCHIVE_PATH = "/tmp/cyberoplus-live-export.zip"
AR = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")
AR_WORD = re.compile(r"[\u0621-\u064a]{3,}")
NEVER_TRANSLATE = {"script", "style", "pre", "code", "kbd", "samp", "var", "math", "svg", "noscript", "textarea"}
SITE = "cyberoplus.com"
STOP = {"الشرح", "كيفية", "دليل", "طريقة", "أفضل", "للمبتدئين", "بالعربية", "مع", "في", "من", "على", "أو", "الى", "إلى", "كل",
        "التطبيق", "البرنامج", "برامج", "خطوات", "بالخطوات", "الكامل", "جديد", "لـ", "وال", "شرح", "ما", "هو", "هل", "عند", "بدون",
        "التي", "الذي", "هنا", "هذا", "هذه", "ذلك", "بين", "كيف", "لماذا", "أهم", "و", "فيها", "عام", "ل", "لدى", "عالم"}
CANONICAL = {"cybersecurity", "artificial-intelligence", "technology", "guides"}

def require(ok, message):
    if not ok:
        raise RuntimeError(message)

def digest(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def arabic_fraction(s):
    alpha = sum(ch.isalpha() for ch in s)
    return sum(1 for ch in s if AR.match(ch)) / alpha if alpha else 0

def normalized_tokens(s):
    return {w for w in AR_WORD.findall(s.lower()) if w not in STOP and len(w) >= 4}

def api_session():
    t = json.loads(os.environ["BLOGGER_TOKEN_JSON"])
    confroot = json.loads(os.environ["BLOGGER_CLIENT_SECRET_JSON"])
    config = confroot.get("installed") or confroot.get("web") or {}
    for k in ("client_id", "client_secret", "token_uri"):
        if not t.get(k) and config.get(k):
            t[k] = config[k]
    return AuthorizedSession(Credentials.from_authorized_user_info(t))

def load_export():
    with zipfile.ZipFile(ARCHIVE_PATH) as z:
        raw = json.loads(z.read("blogger-originals.json"))
    require(str(raw.get("blog_id")) == BLOG_ID, "Incorrect archive blog ID")
    require(len(raw["posts"]) == 259 and len(raw["pages"]) == 6, "Archived inventory differs from verified export")
    require(all(p.get("content") for p in raw["posts"]), "Incomplete archive post")
    return raw

def init_translation():
    import argostranslate.package as package
    import argostranslate.translate as translate
    from pathlib import Path
    local = Path("/tmp/arabic-english.argosmodel")
    if not local.exists():
        for attempt in range(3):
            try:
                r = requests.get(MODEL_URL, stream=True, timeout=(30, 150))
                r.raise_for_status()
                with local.open("wb") as out:
                    for piece in r.iter_content(chunk_size=1024*1024):
                        if piece: out.write(piece)
                break
            except Exception:
                if attempt == 2: raise
                time.sleep(5 * (attempt + 1))
    require(local.stat().st_size > 50_000_000, "Arabic-English model download incomplete")
    package.install_from_path(local)
    installed = translate.get_installed_languages()
    ar = next((x for x in installed if x.code == "ar"), None)
    en = next((x for x in installed if x.code == "en"), None)
    require(ar is not None and en is not None, "Missing offline language model")
    mt = ar.get_translation(en)
    sample = mt.translate("تعلم أساسيات الشبكات والأمن السيبراني.")
    require(bool(sample.strip()) and arabic_fraction(sample) < .08, "Offline translator failed self test")
    return mt

def chunks(s, maximum=900):
    if len(s) <= maximum:
        return [s]
    sentences = re.split(r"(?<=[\.!؟؛:])\s+", s)
    result, acc = [], ""
    for part in sentences:
        if len(acc) + len(part) + 1 <= maximum:
            acc = (acc + " " + part).strip()
            continue
        if acc:
            result.append(acc)
            acc = ""
        while len(part) > maximum:
            pos = part.rfind(" ", 0, maximum)
            if pos < maximum // 3: pos = maximum
            result.append(part[:pos])
            part = part[pos:].lstrip()
        acc = part
    if acc:result.append(acc)
    return result

def translate_phrase(s, model, cache):
    if not AR.search(s):
        return s
    if s in cache:return cache[s]
    left = len(s) - len(s.lstrip())
    right = len(s) - len(s.rstrip())
    core = s.strip()
    if not core or not AR.search(core):
        return s
    # Preserve standalone technical tokens and URLs rather than sending them to MT.
    if core.startswith(("https://", "http://", "mailto:")):
        return s
    translated = " ".join(model.translate(block).strip() for block in chunks(core))
    require(translated and len(translated) >= max(2, int(len(core) * .18)),
            "Machine translation produced empty or truncated text")
    require(arabic_fraction(translated) < .12,
            "Machine translator left a substantial amount of Arabic text")
    value = s[:left] + translated + (s[len(s)-right:] if right else "")
    cache[s] = value
    return value

def important_media(soup):
    return {
        "img_src": [x.get("src") for x in soup.find_all("img")],
        "iframe_src": [x.get("src") for x in soup.find_all("iframe")],
        "video_src": [x.get("src") for x in soup.find_all("video")],
        "source_src": [x.get("src") for x in soup.find_all("source")],
        "links": [x.get("href") for x in soup.find_all("a")],
        "scripts": [str(x) for x in soup.find_all("script")],
        "styles": [str(x) for x in soup.find_all("style")],
        "pre": [str(x) for x in soup.find_all("pre")],
        "code": [str(x) for x in soup.find_all("code")],
    }

def related_links(current, original_soup, posts_by_id, model, cache):
    own_url = current.get("url") or ""
    own_title = current.get("title") or ""
    old_links = {x.get("href") for x in original_soup.find_all("a") if x.get("href")}
    already_internal = sum(SITE in (u or "") for u in old_links)
    if already_internal >= 3:
        return 0
    terms = normalized_tokens(own_title)
    original_text = original_soup.get_text(" ", strip=True)
    candidates = []
    for other in posts_by_id.values():
        if other["id"] == current["id"] or other["url"] in old_links:
            continue
        if (other.get("labels") or [None])[0] != (current.get("labels") or [None])[0]:
            continue
        other_terms = normalized_tokens(other.get("title", ""))
        overlap = terms & other_terms
        if len(overlap) < 2:
            continue
        # Avoid broad category-word matching; require a substantive title overlap.
        score = len(overlap) / max(2, min(len(other_terms), len(terms)))
        if score < .38:
            continue
        candidates.append((score, other, other_terms))
    candidates.sort(key=lambda entry: (-entry[0], entry[1]["id"]))
    added = 0
    for score, other, other_terms in candidates[:8]:
        if added >= 2 or already_internal + added >= 3:
            break
        matches = []
        for paragraph in original_soup.find_all("p"):
            if paragraph.find(["pre", "code", "script", "style", "iframe", "img"]):
                continue
            if len(normalized_tokens(paragraph.get_text(" ", strip=True)) & other_terms) >= 2:
                matches.append(paragraph)
        if not matches:
            continue
        paragraph = matches[0]
        link = original_soup.new_tag("a", href=other["url"])
        name = other["title"]
        title_translation = translate_phrase(name, model, cache) if AR.search(name) else name
        link.string = title_translation
        paragraph.append(" ")
        paragraph.append("See also: ")
        paragraph.append(link)
        paragraph.append(".")
        added += 1
    return added

def transform(source, posts, model, cache, is_page=False):
    html = source["content"]
    soup = BeautifulSoup(html, "html.parser")
    before = important_media(soup)
    original_visible = len(soup.get_text(" ", strip=True))
    added = 0
    if not is_page:
        added = related_links(source, soup, posts, model, cache)
    # Take a snapshot because replace_with changes DOM structure during iteration.
    all_strings = list(soup.find_all(string=True))
    for text_node in all_strings:
        if type(text_node) is not NavigableString or text_node.parent is None:
            continue
        anc = text_node.parent
        skip = False
        while getattr(anc, "name", None):
            if anc.name in NEVER_TRANSLATE or anc.has_attr("translate") and anc.get("translate") == "no":
                skip = True
                break
            anc = anc.parent
        if skip:continue
        if AR.search(str(text_node)):
            translated = translate_phrase(str(text_node), model, cache)
            text_node.replace_with(translated)
    for img in soup.find_all("img"):
        for attr in ("alt", "title"):
            if img.get(attr) and AR.search(img[attr]):
                img[attr] = translate_phrase(img[attr], model, cache)
    for tag in soup.find_all(attrs={"aria-label": True}):
        val = tag.get("aria-label", "")
        if AR.search(val):
            tag["aria-label"] = translate_phrase(val, model, cache)
    new_html = str(soup)
    check_soup = BeautifulSoup(new_html, "html.parser")
    after = important_media(check_soup)
    for key in ("img_src", "iframe_src", "video_src", "source_src", "scripts", "styles", "pre", "code"):
        require(before[key] == after[key], "Preservation check failed for "+key)
    for old_url in before["links"]:
        require(old_url in after["links"], "Original internal/external link removed")
    new_visible = len(check_soup.get_text(" ", strip=True))
    require(new_visible >= original_visible * .35 and new_visible <= original_visible * 4,
            "Translated content length changed implausibly")
    # Only inspect prose eligible for translation; code samples remain intentionally untouched.
    relevant = " ".join(str(t) for t in check_soup.find_all(string=True)
                        if t.parent and t.parent.name not in NEVER_TRANSLATE)
    require(arabic_fraction(relevant) < .025,
            "Too much Arabic remains after translation")
    title = translate_phrase(source["title"], model, cache) if AR.search(source["title"]) else source["title"]
    require(title and arabic_fraction(title) < .025 and len(title) <= 220,
            "Translated title is invalid")
    return title, new_html, added

def main():
    require(SHARDS == 24 and 0 <= SHARD < SHARDS, "Unexpected workflow shard")
    require(os.environ.get("BLOGGER_BLOG_ID") == BLOG_ID, "Unexpected Blogger secret")
    snapshot = load_export()
    sess = api_session()
    base = "https://www.googleapis.com/blogger/v3/blogs/" + BLOG_ID
    resp = sess.get(base, timeout=35)
    resp.raise_for_status()
    blog = resp.json()
    require(str(blog.get("id")) == BLOG_ID and
            (urlsplit(str(blog.get("url", ""))).hostname or "").lower() in (SITE, "www."+SITE),
            "Authenticated Blogger domain mismatch")
    posts_by_id = {str(x["id"]): x for x in snapshot["posts"]}
    # Sort to make sharding deterministic across independent jobs.
    work = [("posts", x) for x in sorted(snapshot["posts"], key=lambda x: x["id"])]
    work += [("pages", x) for x in sorted(snapshot["pages"], key=lambda x: x["id"])]
    batch = [x for index, x in enumerate(work) if index % SHARDS == SHARD]
    cache = {}
    model = init_translation()
    counters = Counter()
    started = time.monotonic()
    for kind, original in batch:
        pid = str(original["id"])
        endpoint = base + "/" + kind + "/" + pid
        r = sess.get(endpoint, params={"view": "ADMIN", "fetchBody": "true"}, timeout=55)
        r.raise_for_status()
        current = r.json()
        require(str(current.get("id")) == pid and current.get("url") == original.get("url"),
                "Resource identity mismatch for " + pid)
        if (current.get("content") != original.get("content") or
            current.get("title") != original.get("title") or
            current.get("published") != original.get("published")):
            counters["source_changed_skipped"] += 1
            print("SKIP_CHANGED: "+pid, flush=True)
            continue
        if kind == "posts" and (current.get("labels") or []) != (original.get("labels") or []):
            # Taxonomy was normalized AFTER the 15:47 source export; four expected labels are safe.
            require(len(current.get("labels") or []) == 1 and
                    current["labels"][0] in CANONICAL, "Unexpected new post labels "+pid)
        existing_text = BeautifulSoup(current["content"], "html.parser").get_text(" ", strip=True)
        if arabic_fraction(existing_text) < .07 and arabic_fraction(current["title"]) < .07:
            counters["already_english"] += 1
            continue
        try:
            title, new_html, added = transform(original, posts_by_id, model, cache, is_page=(kind == "pages"))
        except Exception as e:
            counters["translation_rejected"] += 1
            print("TRANSLATION_REJECTED "+pid+": "+type(e).__name__+": "+str(e)[:180], flush=True)
            continue
        # Second live read reduces risk of clobbering concurrent Blogger manual edits.
        second = sess.get(endpoint, params={"view":"ADMIN","fetchBody":"true"}, timeout=45)
        second.raise_for_status()
        latest = second.json()
        if (latest.get("content") != current.get("content") or
            latest.get("title") != current.get("title") or
            latest.get("labels") != current.get("labels")):
            counters["changed_during_translation"] += 1
            print("SKIP_CONCURRENT_CHANGE: "+pid, flush=True)
            continue
        patch = {"title":title, "content":new_html}
        result = sess.patch(endpoint, json=patch, timeout=100)
        result.raise_for_status()
        readback = sess.get(endpoint, params={"view":"ADMIN","fetchBody":"true"}, timeout=55)
        readback.raise_for_status()
        saved = readback.json()
        require(saved.get("url") == current.get("url") and
                saved.get("published") == current.get("published") and
                str(saved.get("id")) == pid and
                saved.get("title") == title and
                saved.get("labels") == current.get("labels") and
                digest(saved.get("content") or "") == digest(new_html),
                "CRITICAL: Blogger post verification failed after PATCH for "+pid)
        counters["published_verified"] += 1
        counters["contextual_links_added"] += added
        print("PUBLISHED_AND_VERIFIED: "+kind+" "+pid+" "+current["url"], flush=True)
    print("SHARD_COMPLETE: "+str(SHARD)+" "+json.dumps(counters,sort_keys=True)+
          " elapsed_seconds="+str(int(time.monotonic()-started)), flush=True)

if __name__ == "__main__":
    main()
