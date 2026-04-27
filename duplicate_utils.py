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


def content_hash_from_html(html_content):
    text = re.sub(r"\s+", " ", html_to_text(html_content)).casefold().strip()
    return stable_hash(text, length=24)
