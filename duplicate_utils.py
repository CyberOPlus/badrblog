# ============================================================
# duplicate_utils.py - URL and content duplicate helpers
# ============================================================

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from production_logging import html_to_text


TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_NAMES = {
    "fbclid",
    "gclid",
    "dclid",
    "gbraid",
    "wbraid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
    "ref_src",
    "cmpid",
    "ocid",
    "spm",
}


def canonicalize_url(url):
    parsed = urlparse(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return str(url or "").strip()

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")

    filtered_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower in TRACKING_QUERY_NAMES:
            continue
        if any(key_lower.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        filtered_query.append((key, value))

    query = urlencode(filtered_query, doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def stable_hash(value, length=16):
    return hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()[:length]


def title_hash(title):
    normalized = re.sub(r"\s+", " ", str(title or "").casefold()).strip()
    normalized = re.sub(r"[^\w\u0600-\u06FF ]+", "", normalized)
    return stable_hash(normalized)


TOPIC_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "of",
    "for",
    "in",
    "on",
    "with",
    "by",
    "from",
    "as",
    "is",
    "are",
    "new",
    "news",
    "report",
    "says",
    "how",
    "why",
    "what",
    "this",
    "that",
    "after",
    "over",
    "into",
    "about",
    "best",
    "top",
}


def topic_keywords(title, limit=8):
    text = re.sub(r"[^\w\u0600-\u06FF .-]+", " ", str(title or "").casefold())
    words = []
    for word in re.findall(r"[a-z0-9\u0600-\u06FF][a-z0-9\u0600-\u06FF.-]{2,}", text):
        clean = word.strip(".-")
        if len(clean) < 3 or clean in TOPIC_STOPWORDS:
            continue
        words.append(clean)
    cves = re.findall(r"cve-\d{4}-\d{4,7}", text, flags=re.I)
    important = []
    seen = set()
    for word in cves + words:
        if word not in seen:
            important.append(word)
            seen.add(word)
        if len(important) >= limit:
            break
    return important


def topic_signature(title):
    keywords = topic_keywords(title)
    cves = [word for word in keywords if word.startswith("cve-")]
    if cves:
        return stable_hash(" ".join(sorted(cves)), length=20)
    if not keywords:
        return title_hash(title)
    return stable_hash(" ".join(sorted(keywords)), length=20)


def similar_topic_signature(title_a, title_b, threshold=0.62):
    a = set(topic_keywords(title_a))
    b = set(topic_keywords(title_b))
    if not a or not b:
        return False
    if any(word.startswith("cve-") for word in a | b) and a.intersection(b):
        return True
    return len(a & b) / max(len(a), len(b)) >= threshold


def content_hash_from_html(html_content):
    text = re.sub(r"\s+", " ", html_to_text(html_content)).casefold().strip()
    return stable_hash(text, length=24)
