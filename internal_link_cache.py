import json
import re
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from config import INTERNAL_LINK_CACHE_PATH
from production_logging import log_event


INTERNAL_LINK_TTL_MINUTES = 60
INTERNAL_LINK_CACHE_LIMIT = 50
MAX_INSERTED_INTERNAL_LINKS = 3
MAX_INSERTED_TRUSTED_LINKS = 3

TRUSTED_EXTERNAL_HOSTS = (
    "cisa.gov",
    "microsoft.com",
    "google.com",
    "cloud.google.com",
    "github.com",
    "nvd.nist.gov",
    "cve.org",
    "mitre.org",
)

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "into",
    "about",
    "security",
    "update",
    "news",
    "article",
    "تحديث",
    "خبر",
    "مقال",
    "هذا",
    "هذه",
    "على",
    "من",
    "في",
    "عن",
    "إلى",
    "الى",
    "مع",
}


def _now_utc():
    return datetime.now(timezone.utc)


def _parse_dt(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _tokenize(*values):
    text = " ".join(str(value or "") for value in values).lower()
    tokens = re.findall(r"[\w\u0600-\u06ff]{3,}", text)
    return {token for token in tokens if token not in STOPWORDS}


def _canonical_url(url):
    parsed = urlparse(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{parsed.netloc.lower()}{path}"


def _host(url):
    return urlparse(str(url or "")).netloc.lower().removeprefix("www.")


def _same_url(url_a, url_b):
    return bool(_canonical_url(url_a) and _canonical_url(url_a) == _canonical_url(url_b))


def _load_raw_cache(path=INTERNAL_LINK_CACHE_PATH):
    path = Path(path)
    if not path.exists():
        return {"links": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"links": []}
    if isinstance(data, list):
        return {"links": data}
    if not isinstance(data, dict):
        return {"links": []}
    data.setdefault("links", [])
    return data


def _write_cache(data, path=INTERNAL_LINK_CACHE_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prune_internal_link_cache(data, now=None, max_links=INTERNAL_LINK_CACHE_LIMIT):
    now = now or _now_utc()
    cutoff = now - timedelta(minutes=INTERNAL_LINK_TTL_MINUTES)
    kept = []
    expired = 0
    seen = set()

    for entry in data.get("links", []) or []:
        if not isinstance(entry, dict):
            continue
        url = _canonical_url(entry.get("url"))
        published_at = _parse_dt(entry.get("published_at"))
        if not url or not published_at or published_at < cutoff:
            expired += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        entry = dict(entry)
        entry["url"] = entry.get("url", "")
        kept.append(entry)

    kept.sort(key=lambda item: _parse_dt(item.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    data["links"] = kept[:max_links]
    return data, {"expired_removed": expired, "trimmed_removed": max(0, len(kept) - max_links)}


def load_internal_link_cache(path=INTERNAL_LINK_CACHE_PATH, now=None, save=True):
    data = _load_raw_cache(path)
    original_count = len(data.get("links", []) or [])
    data, stats = prune_internal_link_cache(data, now=now)
    stats["loaded"] = len(data.get("links", []) or [])
    log_event("internal_cache_loaded", links=stats["loaded"])
    log_event("internal_cache_expired_removed", expired_internal_links_removed=stats["expired_removed"])
    if save and (stats["expired_removed"] or stats["trimmed_removed"] or original_count != stats["loaded"]):
        _write_cache(data, path)
        log_event("internal_cache_saved", links=stats["loaded"])
        stats["saved"] = True
    else:
        stats["saved"] = False
    return data, stats


def save_internal_link_cache(data, path=INTERNAL_LINK_CACHE_PATH, now=None):
    data, stats = prune_internal_link_cache(data, now=now)
    _write_cache(data, path)
    log_event("internal_cache_saved", links=len(data.get("links", []) or []))
    stats["saved"] = True
    return stats


def _article_keywords(article):
    values = article.get("keywords") or article.get("seo_keywords") or []
    if isinstance(values, str):
        values = [part.strip() for part in re.split(r"[,،;|]", values) if part.strip()]
    package = article.get("ai_input_package") or {}
    tokens = sorted(
        _tokenize(
            article.get("seo_title"),
            article.get("title"),
            article.get("fetched_title"),
            article.get("suggested_category"),
            package.get("title"),
            " ".join(values or []),
        )
    )
    return list(values or [])[:10] or tokens[:10]


def record_published_article(article, post_url, path=INTERNAL_LINK_CACHE_PATH, now=None):
    now = now or _now_utc()
    if not post_url:
        return {"saved": False, "reason": "missing url"}
    data, _stats = load_internal_link_cache(path=path, now=now, save=True)
    links = [
        entry
        for entry in data.get("links", []) or []
        if not _same_url(entry.get("url"), post_url)
    ]
    entry = {
        "title": article.get("seo_title") or article.get("title") or article.get("fetched_title") or "",
        "url": post_url,
        "category": article.get("suggested_category") or article.get("category_label") or "",
        "slug": article.get("seo_slug") or article.get("slug") or "",
        "published_at": _iso(_parse_dt(article.get("published_at")) or now),
        "keywords": _article_keywords(article),
    }
    links.insert(0, entry)
    data["links"] = links
    stats = save_internal_link_cache(data, path=path, now=now)
    stats["saved_entry"] = True
    return stats


def _score_internal_candidate(article, candidate):
    current_url = article.get("blogger_post_url") or article.get("blogger_draft_url") or article.get("url")
    if _same_url(current_url, candidate.get("url")):
        return 0
    title_a = article.get("seo_title") or article.get("title") or article.get("fetched_title") or ""
    title_b = candidate.get("title") or ""
    if title_a and title_b and title_a.strip().casefold() == title_b.strip().casefold():
        return 0
    current_tokens = _tokenize(
        title_a,
        article.get("suggested_category"),
        article.get("category_label"),
        article.get("final_html"),
        " ".join(_article_keywords(article)),
    )
    candidate_tokens = _tokenize(title_b, candidate.get("category"), " ".join(candidate.get("keywords") or []))
    overlap = current_tokens & candidate_tokens
    score = len(overlap)
    if article.get("suggested_category") and article.get("suggested_category") == candidate.get("category"):
        score += 2
    return score


def select_internal_link_candidates(article, links, limit=MAX_INSERTED_INTERNAL_LINKS):
    ranked = []
    for candidate in links or []:
        score = _score_internal_candidate(article, candidate)
        if score > 0:
            ranked.append((score, candidate))
    ranked.sort(key=lambda item: (-item[0], item[1].get("published_at", ""), item[1].get("title", "")))
    selected = []
    seen = set()
    for _score, candidate in ranked:
        url = _canonical_url(candidate.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def _has_prelate(html):
    return "class=\"pRelate\"" in html or "class='pRelate'" in html


def insert_internal_links(html, article, cache_data):
    candidates = select_internal_link_candidates(article, cache_data.get("links", []))
    if not candidates or _has_prelate(html):
        return html, 0
    items = []
    for candidate in candidates[:MAX_INSERTED_INTERNAL_LINKS]:
        if _same_url(candidate.get("url"), article.get("blogger_post_url") or article.get("url")):
            continue
        items.append(
            f"<li><a href='{escape(candidate.get('url', ''), quote=True)}'>"
            f"{escape(candidate.get('title') or candidate.get('url') or '')}</a></li>"
        )
    if len(items) < 2:
        return html, 0
    block = "\n<div class='pRelate'><b>قد يهمك أيضًا:</b><ul>" + "".join(items) + "</ul></div>"
    return html.rstrip() + block, len(items)


def _is_trusted_external(url, source_domain=""):
    host = _host(url)
    source_host = str(source_domain or "").lower().removeprefix("www.")
    if source_host and (host == source_host or host.endswith("." + source_host)):
        return False
    return any(host == trusted or host.endswith("." + trusted) for trusted in TRUSTED_EXTERNAL_HOSTS)


def _reference_anchor_terms(reference):
    title = str(reference.get("title") or "").strip()
    url = str(reference.get("url") or "").strip()
    host = _host(url)
    terms = []
    if "cisa.gov" in host:
        terms.append("CISA")
    if "microsoft.com" in host:
        terms.append("Microsoft")
    if "google.com" in host:
        terms.append("Google")
    if "github.com" in host:
        terms.append("GitHub")
    if "nvd.nist.gov" in host:
        terms.append("NVD")
    if "cve.org" in host:
        terms.append("CVE")
    for token in re.findall(r"\b[A-Z][A-Za-z0-9.+-]{2,}\b", title):
        terms.append(token)
    return list(dict.fromkeys(terms))


def insert_trusted_external_links(html, references, source_domain=""):
    if not references:
        return html, 0
    soup = BeautifulSoup(html or "", "html.parser")
    inserted = 0
    used_urls = set()

    for reference in references:
        if inserted >= MAX_INSERTED_TRUSTED_LINKS:
            break
        url = str(reference.get("url") or "").strip()
        if not url or url in used_urls or not _is_trusted_external(url, source_domain):
            continue
        for term in _reference_anchor_terms(reference):
            linked = False
            pattern = re.compile(rf"(?<![\w])({re.escape(term)})(?![\w])")
            for paragraph in soup.find_all("p"):
                if paragraph.find_parent(["a", "pRef", "pre", "code"]):
                    continue
                if len(paragraph.find_all("a")) >= 2:
                    continue
                for text_node in paragraph.find_all(string=True):
                    if text_node.find_parent("a"):
                        continue
                    text = str(text_node)
                    match = pattern.search(text)
                    if not match:
                        continue
                    before = text[: match.start()]
                    matched = text[match.start() : match.end()]
                    after = text[match.end() :]
                    link_html = (
                        f"{escape(before)}<a class='extL' href='{escape(url, quote=True)}' "
                        f"rel='nofollow noreferrer noopener' target='_blank'>{escape(matched)}</a>{escape(after)}"
                    )
                    fragment = BeautifulSoup(link_html, "html.parser")
                    text_node.replace_with(*fragment.contents)
                    inserted += 1
                    used_urls.add(url)
                    linked = True
                    break
                if linked:
                    break
            if linked:
                break
    return str(soup), inserted


def apply_link_enrichment(html, article, source_domain="", cache_path=INTERNAL_LINK_CACHE_PATH):
    cache_data, cache_stats = load_internal_link_cache(path=cache_path, save=True)
    source_host = str(source_domain or "").lower().removeprefix("www.")
    if source_host:
        cache_data = dict(cache_data)
        cache_data["links"] = [
            entry
            for entry in cache_data.get("links", []) or []
            if _host(entry.get("url")) != source_host
        ]
    html, external_count = insert_trusted_external_links(
        html,
        article.get("trusted_references") or (article.get("ai_input_package") or {}).get("trusted_references") or [],
        source_domain=source_domain,
    )
    html, internal_count = insert_internal_links(html, article, cache_data)
    stats = {
        "internal_cache_loaded": cache_stats.get("loaded", 0),
        "expired_internal_links_removed": cache_stats.get("expired_removed", 0),
        "internal_links_inserted_count": internal_count,
        "external_trusted_links_inserted_count": external_count,
        "internal_cache_saved": bool(cache_stats.get("saved")),
    }
    log_event("internal_links_inserted", internal_links_inserted_count=internal_count)
    log_event("trusted_external_links_inserted", external_trusted_links_inserted_count=external_count)
    return html, stats
