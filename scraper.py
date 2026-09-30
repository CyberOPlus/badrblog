# ============================================================
# scraper.py - Web Scraping Module
# ============================================================

import asyncio
import html as html_lib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from content_filter import is_promotional_article
from duplicate_utils import canonicalize_url, title_hash, topic_signature
from production_logging import elapsed_ms, log_event

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    from scrapling.fetchers import Fetcher
except ImportError:
    Fetcher = None

from config import (
    ALLOW_UNKNOWN_DATE_IN_FAST_MODE,
    CRAWL_OVERLAP_MINUTES,
    ENABLE_SCRAPLING_FALLBACK,
    FALLBACK_FIRST_RUN_LOOKBACK_HOURS,
    FAST_NEWS_MODE,
    FRESHNESS_SAFETY_MARGIN_MINUTES,
    FIRST_VALID_ARTICLE_MODE,
    HEADERS,
    MAX_AI_ARTICLE_AGE_HOURS,
    MAX_RETRIES,
    MAX_SOURCES_PER_RUN,
    MAX_SOURCE_RETRIES,
    JOBS_MODE,
    JOBS_DISCOVERY_PAGE_SIZE,
    JOBS_DISCOVERY_MAX_PAGES,
    JOBS_DISCOVERY_SEEN_STREAK,
    JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE,
    JOBS_DISCOVERY_SEEN_MEMORY,
    RECENT_NEWS_MAX_AGE_HOURS,
    RECENT_NEWS_ONLY,
    RETRY_DELAY,
    SCRAPE_DELAY_SECONDS,
    SOURCE_RETRY_DELAY_SECONDS,
    SOURCE_TIMEOUT_SECONDS,
    SOURCE_URLS,
    SKIP_ADS_AFFILIATE_SPONSORED,
)
from runtime_state import (
    is_source_cooled_down,
    record_source_cooldown,
    record_source_failure,
    record_source_success,
    source_crawl_record,
    update_source_crawl,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

SCRAPLING_AVAILABLE = Fetcher is not None

if SCRAPLING_AVAILABLE:
    Fetcher.adaptive = True

SMART_FRESHNESS_INITIAL_HOURS = 6
SMART_FRESHNESS_EXPANDED_HOURS = 12
FRESHNESS_HARD_MAX_HOURS = 24 * 7


def _discovery_identity(link):
    link = link or {}
    provider = str(link.get("ats_provider") or "").strip().casefold()
    canonical = canonicalize_url(link.get("url"))

    # Workday bulletFields are not guaranteed to be requisition IDs; they may
    # contain location/category labels. The specific job URL is the stable
    # discovery identity and avoids false seen-streak matches.
    if provider == "workday":
        return f"url:{canonical}" if canonical else ""

    reference = str(
        link.get("ats_reference")
        or link.get("job_external_reference")
        or ""
    ).strip()
    if reference:
        return f"{provider or 'source'}:{reference.casefold()}"
    return f"url:{canonical}" if canonical else ""


def _source_known_discovery_ids(source_url):
    record = source_crawl_record(source_url)
    raw = record.get("job_seen_ids") or []
    if not isinstance(raw, list):
        return set()
    return {
        str(value).strip()
        for value in raw
        if str(value or "").strip()
    }


def _merge_source_seen_ids(source_url, links):
    existing = list(source_crawl_record(source_url).get("job_seen_ids") or [])
    ordered = []
    seen = set()
    for value in [
        *(_discovery_identity(link) for link in links or []),
        *(str(item or "").strip() for item in existing),
    ]:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
        if len(ordered) >= JOBS_DISCOVERY_SEEN_MEMORY:
            break
    return ordered


def _filter_new_discovery_links(
    links,
    known_ids=None,
    *,
    seen_streak_stop=None,
    max_items=None,
    initial_seen_streak=0,
):
    known = known_ids if isinstance(known_ids, set) else set(known_ids or ())
    seen_streak_stop = max(
        1,
        int(seen_streak_stop or JOBS_DISCOVERY_SEEN_STREAK),
    )
    max_items = max(
        1,
        int(max_items or JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE),
    )

    selected = []
    consecutive_seen = max(0, int(initial_seen_streak or 0))
    scanned = 0
    stop_reason = ""

    for link in links or []:
        identity = _discovery_identity(link)
        if not identity:
            continue
        scanned += 1
        if identity in known:
            consecutive_seen += 1
            if consecutive_seen >= seen_streak_stop:
                stop_reason = "seen_streak"
                break
            continue

        consecutive_seen = 0
        selected.append(link)
        known.add(identity)
        if len(selected) >= max_items:
            stop_reason = "max_items"
            break

    return selected, {
        "scanned": scanned,
        "new_links": len(selected),
        "seen_streak": consecutive_seen,
        "stop_reason": stop_reason,
    }


def _pagination_next_url(html_text, current_url):
    soup = BeautifulSoup(html_text or "", "html.parser")
    current = urlparse(current_url)
    candidates = []

    for tag in soup.find_all(["a", "link"], href=True):
        rel = " ".join(tag.get("rel") or []).casefold()
        text = _normalize_text(
            " ".join(
                [
                    tag.get_text(" ", strip=True) if hasattr(tag, "get_text") else "",
                    str(tag.get("aria-label") or ""),
                    str(tag.get("title") or ""),
                ]
            )
        ).casefold()
        href = urljoin(current_url, str(tag.get("href") or "").strip())
        if not href:
            continue
        if "next" in rel:
            candidates.append(href)
            continue
        next_labels = (
            "next", "next page", "suivant", "page suivante",
            "التالي", "الصفحة التالية", "suivante",
        )
        if any(label in text for label in next_labels):
            candidates.append(href)

    for candidate in candidates:
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc.casefold() != current.netloc.casefold():
            continue
        if canonicalize_url(candidate) == canonicalize_url(current_url):
            continue
        return candidate
    return ""


BLOCKED_IMAGE_HINTS = (
    "logo",
    "avatar",
    "icon",
    "favicon",
    "emoji",
    "author",
    "profile",
    "button",
    "badge",
)

BLOCKED_SOURCE_HOST_HINTS = (
    "darkwebinformer.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "reddit.com",
    "substack.com",
    "medium.com",
)

HIGH_TRUST_HOSTS = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "cisa.gov": "cisa",
    "cve.org": "cve",
    "nvd.nist.gov": "nvd",
    "mitre.org": "mitre",
}

IGNORED_LINK_PATTERNS = (
    "/tag/",
    "/tags/",
    "/topic/",
    "/topics/",
    "/category/",
    "/categories/",
    "/author/",
    "/authors/",
    "/search",
    "/page/",
    "/pages/",
    "/about",
    "/contact",
    "/privacy",
    "/terms",
    "/support",
    "/advertis",
    "/sponsor",
    "/account",
    "/login",
    "/signin",
    "/sign-in",
    "/register",
    "/feed",
    "/rss",
    "/feeds/",
    "/wp-json",
    "/cdn-cgi/",
)

IGNORED_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".css",
    ".js",
    ".xml",
    ".rss",
    ".atom",
    ".json",
    ".zip",
    ".pdf",
)

LISTING_SEGMENTS = {
    "tag",
    "tags",
    "topic",
    "topics",
    "category",
    "categories",
    "author",
    "authors",
    "page",
    "pages",
}

ALWAYS_IGNORED_PATH_PARTS = (
    "/search",
    "/about",
    "/contact",
    "/privacy",
    "/terms",
    "/support",
    "/advertis",
    "/sponsor",
    "/account",
    "/login",
    "/signin",
    "/sign-in",
    "/register",
    "/feed",
    "/rss",
    "/feeds/",
    "/wp-json",
    "/cdn-cgi/",
)

COMMON_FEED_SUFFIXES = (
    "/feed",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/atom.xml",
    "/index.xml",
)

ASYNC_SOURCE_FETCH_CONCURRENCY = 8
ASYNC_FETCH_TIMEOUT_SECONDS = SOURCE_TIMEOUT_SECONDS

SOURCE_PRIORITY_HINTS = (
    "the hacker news",
    "bleepingcomputer",
    "securityweek",
    "cyber security news",
    "cybersecurity news",
    "the record",
    "cisa advisories",
    "darkwebinformer",
)


def _source_retries():
    if JOBS_MODE:
        return 0
    return MAX_SOURCE_RETRIES if FAST_NEWS_MODE else MAX_RETRIES


def _source_retry_delay(attempt):
    if JOBS_MODE:
        return 0
    return min(SOURCE_RETRY_DELAY_SECONDS, RETRY_DELAY * (attempt + 1)) if FAST_NEWS_MODE else RETRY_DELAY * (attempt + 1)


def _log(message):
    print(message, flush=True)


def _prioritize_sources(sources):
    def priority(source):
        name = str(source.get("name") or source.get("base_url") or "").casefold()
        for index, hint in enumerate(SOURCE_PRIORITY_HINTS):
            if hint in name:
                return index
        if "ai" in name or "tool" in name:
            return len(SOURCE_PRIORITY_HINTS)
        return len(SOURCE_PRIORITY_HINTS) + 1

    return sorted(sources, key=priority)


def _source_last_crawled(source):
    record = source_crawl_record(source.get("base_url", ""))
    parsed = _parse_datetime_to_utc(record.get("last_crawled_at"))
    return parsed or datetime.min.replace(tzinfo=timezone.utc)


def _order_sources_for_fast_run(sources):
    prioritized = _prioritize_sources(sources)
    priority_index = {id(source): index for index, source in enumerate(prioritized)}
    return sorted(
        prioritized,
        key=lambda source: (
            _source_last_crawled(source),
            priority_index.get(id(source), 9999),
            source.get("base_url", ""),
        ),
    )


def _filter_healthy_sources(sources):
    healthy = []
    skipped = []
    for source in sources:
        base_url = source.get("base_url", "").strip()
        cooled_down, cooldown_until = is_source_cooled_down(base_url)
        if cooled_down:
            skipped.append(
                {
                    "source_name": source.get("name", base_url),
                    "base_url": base_url,
                    "status": "cooldown",
                    "error": f"source cooling down until {cooldown_until}",
                    "links_found": 0,
                    "cooldown_until": cooldown_until,
                }
            )
            continue
        healthy.append(source)
    return healthy, skipped


def _record_source_result(base_url, source_name, error, links_found, empty_ok=False):
    error_text = str(error or "")
    lowered = error_text.casefold()
    if "http 429" in lowered:
        record_source_cooldown(base_url, source_name=source_name, error=error_text, minutes=60)
    elif "http 403" in lowered:
        record_source_cooldown(base_url, source_name=source_name, error=error_text, minutes=180)
    elif "timeout" in lowered or "timed out" in lowered:
        record_source_cooldown(base_url, source_name=source_name, error=error_text, minutes=30)
    elif empty_ok and not error and links_found <= 0:
        record_source_success(base_url, source_name=source_name)
    elif error or links_found <= 0:
        record_source_failure(base_url, source_name=source_name, error=error_text or "zero links")
    else:
        record_source_success(base_url, source_name=source_name)



def _is_scrapling_document(document):
    return callable(getattr(document, "css", None)) and getattr(document, "url", None)


def _make_request_with_scrapling(url):
    try:
        _log(f"  Fetching with Scrapling fallback: {url}")
        page = Fetcher.get(
            url,
            timeout=SOURCE_TIMEOUT_SECONDS,
            retries=_source_retries(),
            retry_delay=SOURCE_RETRY_DELAY_SECONDS if FAST_NEWS_MODE else RETRY_DELAY,
            stealthy_headers=True,
            impersonate="chrome",
            headers=HEADERS,
        )
        _log(f"  Successfully fetched with Scrapling. (Status: {getattr(page, 'status', 'unknown')})")
        return page
    except Exception as e:
        _log(f"  Scrapling request failed: {e}")
        return None


def _make_request_with_requests(url, retries=None):
    if retries is None:
        retries = 0

    try:
        _log(f"  Fetching: {url}")
        response = requests.get(url, headers=HEADERS, timeout=SOURCE_TIMEOUT_SECONDS)
        response.raise_for_status()
        _log(f"  Successfully fetched! (Status: {response.status_code})")
        return response.text
    except requests.exceptions.Timeout:
        _log(f"  Request timed out after {SOURCE_TIMEOUT_SECONDS} seconds.")
    except requests.exceptions.ConnectionError:
        _log("  Could not connect to the server.")
    except requests.exceptions.HTTPError as e:
        _log(f"  HTTP Error: {e.response.status_code} - {e.response.reason}")
    except requests.exceptions.RequestException as e:
        _log(f"  Request failed: {e}")

    max_retries = _source_retries()
    if retries < max_retries:
        wait_time = _source_retry_delay(retries)
        _log(f"  Waiting {wait_time} seconds before retry... (Attempt {retries + 1}/{max_retries})")
        time.sleep(wait_time)
        return _make_request_with_requests(url, retries + 1)

    _log(f"  All {max_retries} retries exhausted. Giving up on: {url}")
    return None


def make_request(url, retries=None):
    """
    Make a fast HTTP GET request first. Scrapling is an optional fallback only,
    because browser-like fetchers are too slow for live fast news mode.
    """
    document = _make_request_with_requests(url, retries)
    if document:
        return document
    if ENABLE_SCRAPLING_FALLBACK and SCRAPLING_AVAILABLE:
        return _make_request_with_scrapling(url)
    return None


def _append_unique_link(articles, seen, title, url):
    if not title or not url or url in seen:
        return
    seen.add(url)
    articles.append((title, url))


def _normalize_hostname(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _is_same_source_site(candidate_url, source_url):
    candidate_host = _normalize_hostname(candidate_url)
    source_host = _normalize_hostname(source_url)
    return candidate_host == source_host or candidate_host.endswith(f".{source_host}")


def _is_likely_article_url(title, article_url, source_url, strict_source_path=True):
    parsed = urlparse(article_url)
    source_parsed = urlparse(source_url)
    normalized_title = re.sub(r"\s+", " ", (title or "").strip())
    path = parsed.path.lower()
    source_path = source_parsed.path.rstrip("/").lower()

    if parsed.scheme not in {"http", "https"}:
        return False
    if not _is_same_source_site(article_url, source_url):
        return False
    if strict_source_path and source_path and source_path != "/":
        if not path.startswith(source_path + "/"):
            return False
    if not normalized_title or len(normalized_title) < 8:
        return False
    if normalized_title.lower() in {"read more", "continue reading", "more", "home", "news", "blog"}:
        return False
    if not path or path == "/":
        return False
    if any(path.endswith(extension) for extension in IGNORED_EXTENSIONS):
        return False

    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return False
    if _is_navigation_or_utility_path(path, segments):
        return False
    if len(segments) == 1 and len(segments[0]) < 6 and len(normalized_title.split()) < 2:
        return False

    return True


def _is_navigation_or_utility_path(path, segments):
    if any(part in path for part in ALWAYS_IGNORED_PATH_PARTS):
        return True

    # Reject short listing pages such as /tag/ai or /blog/topics/security,
    # but allow real article URLs like /blog/topics/security/article-slug.
    for listing_segment in LISTING_SEGMENTS:
        if listing_segment not in segments:
            continue
        index = segments.index(listing_segment)
        trailing_segments = segments[index + 1 :]
        if len(trailing_segments) <= 1:
            return True

    return False


def _filter_article_links(article_links, source_url, strict_source_path=True):
    filtered = []
    seen = set()

    for link in article_links:
        if isinstance(link, dict):
            title = link.get("title", "")
            url = link.get("url", "")
            metadata = {
                key: value
                for key, value in link.items()
                if key not in {"title", "url", "source_url"}
            }
        else:
            title, url = link
            metadata = {}
        normalized_title = _normalize_text(title)
        full_url = urljoin(source_url, url)
        full_url = f"{urlparse(full_url).scheme}://{urlparse(full_url).netloc}{urlparse(full_url).path}".rstrip("/") + (
            "/" if urlparse(full_url).path == "/" else ""
        )

        if not _is_likely_article_url(
            normalized_title,
            full_url,
            source_url,
            strict_source_path=strict_source_path,
        ):
            continue
        if full_url in seen:
            continue

        seen.add(full_url)
        if metadata:
            filtered.append({"title": normalized_title, "url": full_url, **metadata})
        else:
            filtered.append((normalized_title, full_url))

    return filtered


def _link_to_article_dict(link, source_url):
    if isinstance(link, dict):
        return {"source_url": source_url, **link}
    title, url = link
    return {"source_url": source_url, "title": title, "url": url}


def _extract_article_links_with_soup(document, base_url, strict_source_path=True):
    soup = _get_soup_from_document(document, base_url)
    if not soup:
        return []

    best_matches = {}
    selector_weights = (
        ("article h1 a[href]", 10),
        ("article h2 a[href]", 9),
        ("article h3 a[href]", 8),
        ("main h1 a[href]", 8),
        ("main h2 a[href]", 7),
        ("main h3 a[href]", 6),
        ("article a[href]", 4),
        ("main a[href]", 3),
        ("a[href]", 1),
    )

    for selector, base_score in selector_weights:
        for link_tag in soup.select(selector):
            href = link_tag.get("href", "").strip()
            title = _normalize_text(link_tag.get_text(" ", strip=True))
            full_url = urljoin(base_url, href)

            if not _is_likely_article_url(
                title,
                full_url,
                base_url,
                strict_source_path=strict_source_path,
            ):
                continue

            score = base_score
            if link_tag.find_parent("article"):
                score += 2
            if link_tag.find_parent(["h1", "h2", "h3"]):
                score += 2

            current = best_matches.get(full_url)
            if not current or score > current["score"]:
                best_matches[full_url] = {"title": title, "score": score}

    return [
        (entry["title"], url)
        for url, entry in sorted(best_matches.items(), key=lambda item: (-item[1]["score"], item[0]))
    ]


def _extract_feed_urls(document, source_url):
    soup = _get_soup_from_document(document, source_url)
    if not soup:
        return []

    feed_urls = []
    seen = set()

    for tag in soup.select("link[rel~='alternate'][href], a[href]"):
        href = tag.get("href", "").strip()
        if not href:
            continue

        tag_type = (tag.get("type") or "").lower()
        full_url = urljoin(source_url, href)
        lower_href = full_url.lower()

        if (
            "rss" in tag_type
            or "atom" in tag_type
            or any(keyword in lower_href for keyword in ("/feed", "/rss", "alt=rss", "alt=atom"))
        ):
            if full_url not in seen:
                seen.add(full_url)
                feed_urls.append(full_url)

    parsed_source = urlparse(source_url)
    source_root = f"{parsed_source.scheme}://{parsed_source.netloc}"

    if not JOBS_MODE:
        for suffix in COMMON_FEED_SUFFIXES:
            candidate = urljoin(source_root, suffix)
            if candidate not in seen:
                seen.add(candidate)
                feed_urls.append(candidate)

    if "blogspot.com" in parsed_source.netloc.lower():
        for candidate in (
            urljoin(source_root, "/feeds/posts/default?alt=rss"),
            urljoin(source_root, "/feeds/posts/default?alt=atom"),
        ):
            if candidate not in seen:
                seen.add(candidate)
                feed_urls.append(candidate)

    return feed_urls


def _xml_local_name(tag_name):
    return tag_name.split("}", 1)[-1].lower()


def _parse_datetime_to_utc(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _datetime_iso_utc(value):
    parsed = _parse_datetime_to_utc(value)
    if not parsed:
        return ""
    return parsed.isoformat().replace("+00:00", "Z")


def _article_age_hours(published_at, now=None):
    parsed = _parse_datetime_to_utc(published_at)
    if not parsed:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - parsed).total_seconds() / 3600)


def _smart_initial_freshness_hours():
    return min(FRESHNESS_HARD_MAX_HOURS, max(SMART_FRESHNESS_INITIAL_HOURS, RECENT_NEWS_MAX_AGE_HOURS))


def _smart_expanded_freshness_hours(initial_hours=None):
    initial_hours = _smart_initial_freshness_hours() if initial_hours is None else initial_hours
    return min(FRESHNESS_HARD_MAX_HOURS, max(SMART_FRESHNESS_EXPANDED_HOURS, initial_hours))


def _ai_cutoff_for_window(max_age_hours=None):
    max_age_hours = _smart_initial_freshness_hours() if max_age_hours is None else max_age_hours
    return max(0, min(FRESHNESS_HARD_MAX_HOURS, max_age_hours) - (max(0, FRESHNESS_SAFETY_MARGIN_MINUTES) / 60))


def _is_recent_published_at(published_at, now=None, max_age_hours=None):
    age = _article_age_hours(published_at, now=now)
    if age is None:
        return False, None
    max_age_hours = _smart_initial_freshness_hours() if max_age_hours is None else max_age_hours
    return age <= max(0, min(FRESHNESS_HARD_MAX_HOURS, max_age_hours)), age


def _is_safe_for_ai_published_at(published_at, now=None, max_age_hours=None):
    age = _article_age_hours(published_at, now=now)
    if age is None:
        return False, None
    return age <= _ai_cutoff_for_window(max_age_hours=max_age_hours), age


def _freshness_bucket_for_date(published_at, initial_hours=None, expanded_hours=None, now=None):
    age = _article_age_hours(published_at, now=now)
    if age is None:
        return "missing", None
    initial_hours = _smart_initial_freshness_hours() if initial_hours is None else initial_hours
    expanded_hours = _smart_expanded_freshness_hours(initial_hours) if expanded_hours is None else expanded_hours
    if age > FRESHNESS_HARD_MAX_HOURS:
        return "too_old", age
    if age <= initial_hours:
        return "fresh", age
    if age <= expanded_hours:
        return "expanded", age
    return "within_hard_max", age


def _source_crawl_window_start(source_key, now=None):
    now = now or datetime.now(timezone.utc)
    fallback = now - timedelta(hours=max(0, FALLBACK_FIRST_RUN_LOOKBACK_HOURS))
    record = source_crawl_record(source_key)
    last_crawled = _parse_datetime_to_utc(record.get("last_crawled_at"))
    if not last_crawled:
        return fallback
    return last_crawled - timedelta(minutes=max(0, CRAWL_OVERLAP_MINUTES))


def _json_ld_items(value):
    if isinstance(value, list):
        for item in value:
            yield from _json_ld_items(item)
    elif isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if graph:
            yield from _json_ld_items(graph)


def _extract_published_time_from_html(html_text):
    soup = BeautifulSoup(html_text or "", "html.parser")
    meta_keys = {
        "article:published_time",
        "og:published_time",
        "publishdate",
        "pubdate",
        "date",
        "datepublished",
        "dc.date",
        "dc.date.issued",
        "parsely-pub-date",
        "sailthru.date",
    }

    for script in soup.select("script[type='application/ld+json']"):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in _json_ld_items(data):
            for key in ("datePublished", "dateCreated", "uploadDate", "dateModified"):
                published_at = _datetime_iso_utc(item.get(key))
                if published_at:
                    return published_at, f"json-ld:{key}"

    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").strip().casefold()
        if key in meta_keys:
            published_at = _datetime_iso_utc(meta.get("content"))
            if published_at:
                return published_at, f"meta:{key}"

    for time_tag in soup.find_all("time"):
        published_at = _datetime_iso_utc(time_tag.get("datetime") or time_tag.get("content") or time_tag.get_text(" ", strip=True))
        if published_at:
            return published_at, "time"

    return "", ""


def _resolve_article_published_at(article_url, feed_published_at=""):
    if feed_published_at:
        normalized = _datetime_iso_utc(feed_published_at)
        if normalized:
            return normalized, "feed"

    html_text = _make_request_with_requests(article_url, retries=0)
    if not html_text and ENABLE_SCRAPLING_FALLBACK and SCRAPLING_AVAILABLE:
        page = _make_request_with_scrapling(article_url)
        html_text = str(page) if page else ""
    if not html_text:
        return "", ""
    return _extract_published_time_from_html(html_text)


def _parse_feed_article_links(feed_text, source_url, feed_url=None):
    try:
        root = ET.fromstring(feed_text)
    except ET.ParseError:
        return []

    article_links = []
    seen = set()

    for node in root.iter():
        if _xml_local_name(node.tag) not in {"item", "entry"}:
            continue

        title = ""
        link = ""
        published_at = ""
        summary = ""

        for child in list(node):
            child_name = _xml_local_name(child.tag)
            if child_name == "title" and not title:
                title = _normalize_text("".join(child.itertext()))
            elif child_name == "link" and not link:
                href = child.attrib.get("href") or _normalize_text("".join(child.itertext()))
                if href:
                    link = urljoin(source_url, href)
            elif child_name in {"published", "updated", "pubdate", "date"} and not published_at:
                published_at = _datetime_iso_utc("".join(child.itertext()))
            elif child_name in {"description", "summary", "content", "encoded"} and not summary:
                summary = _normalize_text("".join(child.itertext()))

        if title and link and link not in seen:
            seen.add(link)
            article_links.append(
                {
                    "title": title,
                    "url": link,
                    "published_at": published_at,
                    "published_at_source": "feed" if published_at else "",
                    "rss_summary": summary,
                }
            )

    filtered = _filter_article_links(
        article_links,
        source_url,
        strict_source_path=False,
    )
    if not filtered and feed_url:
        filtered = _filter_article_links(
            article_links,
            feed_url,
            strict_source_path=False,
        )
    return filtered


def _collect_links_from_feed(feed_url, source_url):
    try:
        response = requests.get(feed_url, headers=HEADERS, timeout=SOURCE_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.exceptions.RequestException as error:
        print(f"  Feed failed: {feed_url} ({error})")
        return []
    return _parse_feed_article_links(response.text, source_url, feed_url=feed_url)


def _fallback_feed_urls(source_url, feed_url=None):
    parsed_source = urlparse(source_url)
    source_root = f"{parsed_source.scheme}://{parsed_source.netloc}"
    candidates = []
    if feed_url:
        candidates.append(feed_url)
    candidates.extend(urljoin(source_root, suffix) for suffix in COMMON_FEED_SUFFIXES)
    if "blogspot.com" in parsed_source.netloc.lower():
        candidates.extend(
            (
                urljoin(source_root, "/feeds/posts/default?alt=rss"),
                urljoin(source_root, "/feeds/posts/default?alt=atom"),
            )
        )

    seen = set()
    return [
        candidate
        for candidate in candidates
        if candidate and not (candidate in seen or seen.add(candidate))
    ]


async def _fetch_text_async(session, url):
    started = time.perf_counter()
    try:
        log_event("fetch_start", url=url, method="aiohttp-source")
        async with session.get(url, headers=HEADERS, timeout=ASYNC_FETCH_TIMEOUT_SECONDS) as response:
            text = await response.text(errors="ignore")
            if response.status >= 400:
                error = f"http {response.status}"
                log_event(
                    "fetch_end",
                    url=url,
                    method="aiohttp-source",
                    status=response.status,
                    error=error,
                    elapsed_ms=elapsed_ms(started),
                )
                return "", error, response.status
            log_event(
                "fetch_end",
                url=url,
                method="aiohttp-source",
                status=response.status,
                chars=len(text),
                elapsed_ms=elapsed_ms(started),
            )
            return text, "", response.status
    except (asyncio.TimeoutError, aiohttp.ClientError) as error:
        log_event(
            "fetch_end",
            url=url,
            method="aiohttp-source",
            error=error.__class__.__name__,
            elapsed_ms=elapsed_ms(started),
        )
        return "", error.__class__.__name__, None


async def _collect_paginated_html_links_async(
    session,
    source_url,
    parser,
    *,
    known_ids=None,
    page_size=None,
    max_pages=None,
    seen_streak_stop=None,
    max_items=None,
    start_url=None,
):
    page_size = max(5, int(page_size or JOBS_DISCOVERY_PAGE_SIZE))
    max_pages = max(1, int(max_pages or JOBS_DISCOVERY_MAX_PAGES))
    max_items = max(1, int(max_items or JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE))
    seen_streak_stop = max(1, int(seen_streak_stop or JOBS_DISCOVERY_SEEN_STREAK))
    working_known = set(known_ids or ())
    collected = []
    seen_streak = 0
    current_url = str(start_url or source_url).strip() or source_url
    resume_active = bool(
        start_url
        and (canonicalize_url(start_url) or str(start_url))
        != (canonicalize_url(source_url) or str(source_url))
    )
    effective_seen_streak_stop = (
        10**9
        if resume_active
        else seen_streak_stop
    )
    visited = set()
    status_code = None
    last_error = ""
    stop_reason = "end"
    pages_scanned = 0
    resume_url = ""

    for _page_number in range(1, max_pages + 1):
        current_key = canonicalize_url(current_url) or current_url
        if not current_key or current_key in visited:
            stop_reason = "loop_guard"
            break
        visited.add(current_key)
        pages_scanned += 1

        html_text, error, status_code = await _fetch_text_async(session, current_url)
        if error or not html_text:
            last_error = error or "empty paginated listing"
            stop_reason = "partial_error" if collected else "error"
            resume_url = current_url
            if collected:
                break
            return [], last_error, status_code, {
                "stop_reason": stop_reason,
                "pages_scanned": pages_scanned,
                "resume_url": resume_url,
            }

        page_links = parser(
            html_text,
            current_url,
            per_source_limit=max(
                JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE,
                max_items,
            ),
        )
        new_links, meta = _filter_new_discovery_links(
            page_links,
            working_known,
            seen_streak_stop=effective_seen_streak_stop,
            # Never cut a listing page in half; the global cap is checked after
            # this full page so a resume cursor can safely start at next page.
            max_items=max(max_items + len(page_links), len(page_links) + 1),
            initial_seen_streak=seen_streak,
        )
        seen_streak = int(meta.get("seen_streak") or 0)
        collected.extend(new_links)

        next_url = _pagination_next_url(html_text, current_url)

        if meta.get("stop_reason") == "seen_streak":
            stop_reason = "seen_streak"
            resume_url = ""
            break
        if not next_url:
            stop_reason = "end"
            resume_url = ""
            break
        if len(collected) >= max_items:
            stop_reason = "max_items"
            resume_url = next_url
            break

        current_url = next_url
    else:
        stop_reason = "max_pages"
        resume_url = current_url

    return collected, last_error, status_code or 200, {
        "stop_reason": stop_reason,
        "pages_scanned": pages_scanned,
        "resume_url": resume_url,
        "seen_streak": seen_streak,
        "page_size": page_size,
    }


def _collect_paginated_html_links_sync(
    source_url,
    parser,
    *,
    known_ids=None,
    page_size=None,
    max_pages=None,
    seen_streak_stop=None,
    max_items=None,
    start_url=None,
):
    page_size = max(5, int(page_size or JOBS_DISCOVERY_PAGE_SIZE))
    max_pages = max(1, int(max_pages or JOBS_DISCOVERY_MAX_PAGES))
    max_items = max(1, int(max_items or JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE))
    seen_streak_stop = max(1, int(seen_streak_stop or JOBS_DISCOVERY_SEEN_STREAK))
    working_known = set(known_ids or ())
    collected = []
    seen_streak = 0
    current_url = str(start_url or source_url).strip() or source_url
    resume_active = bool(
        start_url
        and (canonicalize_url(start_url) or str(start_url))
        != (canonicalize_url(source_url) or str(source_url))
    )
    effective_seen_streak_stop = (
        10**9
        if resume_active
        else seen_streak_stop
    )
    visited = set()
    last_status = 200
    last_error = ""
    stop_reason = "end"
    pages_scanned = 0
    resume_url = ""

    for _page_number in range(1, max_pages + 1):
        current_key = canonicalize_url(current_url) or current_url
        if not current_key or current_key in visited:
            stop_reason = "loop_guard"
            break
        visited.add(current_key)
        pages_scanned += 1

        document = make_request(current_url)
        if not document:
            last_error = "fetch failed"
            stop_reason = "partial_error" if collected else "error"
            resume_url = current_url
            if collected:
                break
            return [], last_error, None, {
                "stop_reason": stop_reason,
                "pages_scanned": pages_scanned,
                "resume_url": resume_url,
            }
        try:
            last_status = int(getattr(document, "status", 200) or 200)
        except (TypeError, ValueError):
            last_status = 200
        html_text = str(document)

        page_links = parser(
            html_text,
            current_url,
            per_source_limit=max(
                JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE,
                max_items,
            ),
        )
        new_links, meta = _filter_new_discovery_links(
            page_links,
            working_known,
            seen_streak_stop=effective_seen_streak_stop,
            max_items=max(max_items + len(page_links), len(page_links) + 1),
            initial_seen_streak=seen_streak,
        )
        seen_streak = int(meta.get("seen_streak") or 0)
        collected.extend(new_links)

        next_url = _pagination_next_url(html_text, current_url)

        if meta.get("stop_reason") == "seen_streak":
            stop_reason = "seen_streak"
            resume_url = ""
            break
        if not next_url:
            stop_reason = "end"
            resume_url = ""
            break
        if len(collected) >= max_items:
            stop_reason = "max_items"
            resume_url = next_url
            break

        current_url = next_url
    else:
        stop_reason = "max_pages"
        resume_url = current_url

    return collected, last_error, last_status, {
        "stop_reason": stop_reason,
        "pages_scanned": pages_scanned,
        "resume_url": resume_url,
        "seen_streak": seen_streak,
        "page_size": page_size,
    }


def _workday_config(source_url):
    parsed = urlparse(str(source_url or ""))
    if not parsed.scheme.startswith("http") or "myworkdayjobs.com" not in parsed.netloc.casefold():
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    site = parts[-1]
    tenant = parsed.netloc.split(".", 1)[0]
    if not tenant or not site:
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return {
        "origin": origin,
        "tenant": tenant,
        "site": site,
        "listing_url": source_url.rstrip("/"),
        "api_url": f"{origin}/wday/cxs/{tenant}/{site}/jobs",
    }


async def _collect_workday_links_async(
    session,
    source_url,
    per_source_limit=None,
    *,
    known_ids=None,
    max_pages=None,
    seen_streak_stop=None,
    max_items=None,
    start_offset=0,
):
    cfg = _workday_config(source_url)
    if not cfg:
        return [], "invalid workday source URL", None, {
            "stop_reason": "invalid_source",
            "resume_offset": 0,
            "pages_scanned": 0,
        }

    page_size = max(
        1,
        min(
            max(int(per_source_limit or 0), JOBS_DISCOVERY_PAGE_SIZE),
            20,
        ),
    )
    max_pages = max(1, int(max_pages or JOBS_DISCOVERY_MAX_PAGES))
    max_items = max(1, int(max_items or JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE))
    seen_streak_stop = max(1, int(seen_streak_stop or JOBS_DISCOVERY_SEEN_STREAK))
    working_known = set(known_ids or ())
    links = []
    seen_streak = 0
    try:
        offset = max(0, int(start_offset or 0))
    except (TypeError, ValueError):
        offset = 0
    resume_active = offset > 0
    effective_seen_streak_stop = (
        10**9
        if resume_active
        else seen_streak_stop
    )
    started = time.perf_counter()
    status_code = 200
    stop_reason = "end"
    resume_offset = 0
    pages_scanned = 0

    for _page_number in range(1, max_pages + 1):
        pages_scanned += 1
        current_offset = offset
        try:
            async with session.post(
                cfg["api_url"],
                json={
                    "appliedFacets": {},
                    "limit": page_size,
                    "offset": current_offset,
                    "searchText": "",
                },
                headers={
                    **HEADERS,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=ASYNC_FETCH_TIMEOUT_SECONDS,
            ) as response:
                status_code = response.status
                text = await response.text(errors="ignore")
                if response.status >= 400:
                    stop_reason = f"partial_http_{response.status}" if links else f"http_{response.status}"
                    resume_offset = current_offset
                    if links:
                        break
                    return [], f"http {response.status}", response.status, {
                        "stop_reason": stop_reason,
                        "resume_offset": resume_offset,
                        "pages_scanned": pages_scanned,
                    }
                data = json.loads(text or "{}")
        except (asyncio.TimeoutError, aiohttp.ClientError, ValueError, json.JSONDecodeError) as error:
            stop_reason = f"partial_{error.__class__.__name__}" if links else error.__class__.__name__
            resume_offset = current_offset
            if links:
                break
            return [], error.__class__.__name__, None, {
                "stop_reason": stop_reason,
                "resume_offset": resume_offset,
                "pages_scanned": pages_scanned,
            }

        rows = [
            row
            for row in (data.get("jobPostings") or [])
            if isinstance(row, dict)
        ]
        page_links = []
        for row in rows:
            title = _normalize_text(row.get("title") or "")
            external_path = str(row.get("externalPath") or "").strip()
            if not title or not external_path:
                continue
            if external_path.startswith("/"):
                url = cfg["listing_url"] + external_path
            else:
                url = cfg["listing_url"] + "/" + external_path
            page_links.append({
                "title": title,
                "url": url,
                "ats_provider": "workday",
                "ats_reference": (
                    (row.get("bulletFields") or [""])[0]
                    if isinstance(row.get("bulletFields"), list)
                    else ""
                ),
                "source_published_label": str(row.get("postedOn") or "").strip(),
            })

        new_links, meta = _filter_new_discovery_links(
            page_links,
            working_known,
            seen_streak_stop=effective_seen_streak_stop,
            max_items=max(max_items + len(page_links), len(page_links) + 1),
            initial_seen_streak=seen_streak,
        )
        seen_streak = int(meta.get("seen_streak") or 0)
        links.extend(new_links)

        next_offset = current_offset + len(rows)

        if meta.get("stop_reason") == "seen_streak":
            stop_reason = "seen_streak"
            resume_offset = 0
            break
        if not rows or len(rows) < page_size:
            stop_reason = "end"
            resume_offset = 0
            break

        total = data.get("total")
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = 0
        if total and next_offset >= total:
            stop_reason = "end"
            resume_offset = 0
            break

        offset = next_offset
        if len(links) >= max_items:
            stop_reason = "max_items"
            resume_offset = next_offset
            break
    else:
        stop_reason = "max_pages"
        resume_offset = offset

    log_event(
        "workday_source_fetch",
        url=source_url,
        jobs=len(links),
        pages=pages_scanned,
        stop_reason=stop_reason,
        resume_offset=resume_offset,
        elapsed_ms=elapsed_ms(started),
    )
    return links, "", status_code or 200, {
        "stop_reason": stop_reason,
        "resume_offset": resume_offset,
        "pages_scanned": pages_scanned,
        "page_size": page_size,
        "seen_streak": seen_streak,
    }



def _parse_emploi_public_links(document, source_url, per_source_limit=None):
    """Extract only official Emploi-Public competition detail pages."""
    soup = _get_soup_from_document(document, source_url)
    if not soup:
        return []

    limit = max(1, min(int(per_source_limit or JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE), JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE))
    links = []
    seen = set()
    generic_titles = {
        "تفاصيل", "التفاصيل", "عرض التفاصيل", "voir", "voir détails",
        "voir details", "détails", "details",
    }

    for anchor in soup.find_all("a", href=True):
        href = urljoin(source_url, str(anchor.get("href") or "").strip())
        parsed = urlparse(href)
        host = parsed.netloc.casefold().removeprefix("www.")
        if host != "emploi-public.ma":
            continue

        decoded_path = unquote(parsed.path or "")
        match = re.search(
            r"(?:/ar/تفاصيل/المباريات/|/fr/concours/details/)"
            r"([0-9a-fA-F-]{8,}|[^/?#]+)$",
            decoded_path.rstrip("/"),
            flags=re.I,
        )
        if not match:
            continue

        canonical = f"https://www.emploi-public.ma{parsed.path.rstrip('/')}"
        if canonical in seen:
            continue

        candidates = [_normalize_text(anchor.get_text(" ", strip=True))]
        container = anchor.find_parent(["article", "li", "tr", "section", "div"])
        if container:
            for tag_name in ("h1", "h2", "h3", "h4", "h5", "strong"):
                tag = container.find(tag_name)
                if tag:
                    candidates.append(_normalize_text(tag.get_text(" ", strip=True)))

        title = next(
            (
                value for value in candidates
                if value
                and value.casefold() not in generic_titles
                and len(value) >= 8
            ),
            "",
        )
        if not title:
            continue

        seen.add(canonical)
        reference = match.group(1)
        links.append({
            "title": title,
            "url": canonical,
            "ats_provider": "emploi_public",
            "ats_reference": reference,
            "job_external_reference": reference,
        })
        if len(links) >= limit:
            break

    return links


async def _collect_emploi_public_links_async(session, source_url, per_source_limit=None):
    html_text, error, status_code = await _fetch_text_async(session, source_url)
    if error or not html_text:
        return [], error or "empty Emploi-Public listing", status_code
    links = _parse_emploi_public_links(
        html_text,
        source_url,
        per_source_limit=per_source_limit,
    )
    if not links:
        return [], "Emploi-Public listing exposed no competition detail links", status_code or 200
    return links, "", status_code or 200


def _parse_capgemini_job_links(html_text, source_url, per_source_limit=None):
    """Extract only official Capgemini SuccessFactors job-detail URLs."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    limit = max(1, min(int(per_source_limit or JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE), JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE))
    links = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(source_url, str(anchor.get("href") or "").strip())
        parsed = urlparse(href)
        if parsed.netloc.casefold() != "careers.capgemini.com":
            continue
        path = parsed.path.rstrip("/")
        match = re.search(r"/job/[^?#]+/(\d+)$", path, flags=re.I)
        if not match or href in seen:
            continue
        seen.add(href)
        candidates = [_normalize_text(anchor.get_text(" ", strip=True))]
        container = anchor.find_parent(["li", "article", "tr", "div"])
        if container:
            for tag_name in ("h2", "h3", "h4", "span"):
                tag = container.find(tag_name)
                if tag:
                    candidates.append(_normalize_text(tag.get_text(" ", strip=True)))
        generic = {"apply now", "view job", "job details", "more", "details"}
        title = next(
            (
                value for value in candidates
                if value and value.casefold() not in generic and len(value) >= 4
            ),
            "",
        )
        if not title:
            # Job slug is still official and specific; turn separators into a
            # conservative display title without inventing facts.
            slug = path.split("/")[-2] if len(path.split("/")) >= 3 else ""
            title = _normalize_text(slug.replace("-", " "))
        if not title:
            continue
        links.append({
            "title": title,
            "url": href,
            "ats_provider": "capgemini_successfactors",
            "ats_reference": match.group(1),
        })
        if len(links) >= limit:
            break
    return links


async def _collect_capgemini_links_async(session, source_url, per_source_limit=None):
    html_text, error, status_code = await _fetch_text_async(session, source_url)
    if error or not html_text:
        return [], error or "empty Capgemini careers search", status_code
    links = _parse_capgemini_job_links(
        html_text,
        source_url,
        per_source_limit=per_source_limit,
    )
    if not links:
        return [], "Capgemini careers search exposed no job-detail links", status_code or 200
    return links, "", status_code or 200


def _parse_etalent_links(html_text, source_url, per_source_limit=None):
    """Return only real eTalent vacancy detail URLs (/offre/<id>)."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    limit = max(1, min(int(per_source_limit or JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE), JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE))
    links = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(source_url, str(anchor.get("href") or "").strip())
        path = urlparse(href).path.rstrip("/")
        match = re.search(r"/offre/(\d+)$", path, flags=re.I)
        if not match or href in seen:
            continue
        seen.add(href)
        container = anchor.find_parent(["article", "li", "tr", "section", "div"])
        candidates = [
            _normalize_text(anchor.get_text(" ", strip=True)),
        ]
        if container:
            for tag_name in ("h1", "h2", "h3", "h4", "h5", "strong"):
                tag = container.find(tag_name)
                if tag:
                    candidates.append(_normalize_text(tag.get_text(" ", strip=True)))
        generic = {
            "voir", "voir l'offre", "voir loffre", "détails", "details",
            "postuler", "en savoir plus", "offre",
        }
        title = next(
            (
                value for value in candidates
                if value and value.casefold() not in generic and len(value) >= 4
            ),
            f"Offre {match.group(1)}",
        )
        links.append({
            "title": title,
            "url": href,
            "ats_provider": "etalent",
            "ats_reference": match.group(1),
        })
        if len(links) >= limit:
            break
    return links


async def _collect_etalent_links_async(session, source_url, per_source_limit=None):
    html_text, error, status_code = await _fetch_text_async(session, source_url)
    if error or not html_text:
        return [], error or "empty eTalent listing", status_code
    links = _parse_etalent_links(html_text, source_url, per_source_limit=per_source_limit)
    # A healthy eTalent listing can legitimately have zero active offers.
    return links, "", status_code or 200


def _extract_json_object_after_marker(text, marker):
    raw = html_lib.unescape(str(text or ""))
    pos = raw.find(marker)
    if pos < 0:
        return None
    start = pos + len(marker)
    while start < len(raw) and raw[start].isspace():
        start += 1
    if start >= len(raw) or raw[start] != "{":
        return None
    depth = 0
    quote = None
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:index + 1])
                except (TypeError, ValueError, json.JSONDecodeError):
                    return None
    return None


def _phenom_jobs_from_html(html_text, country="MOROCCO"):
    soup = BeautifulSoup(html_text or "", "html.parser")
    ddo = None
    for script in soup.find_all("script"):
        text = script.string or script.get_text("", strip=False)
        if "phApp.ddo" not in str(text or ""):
            continue
        ddo = _extract_json_object_after_marker(text, "phApp.ddo =")
        if isinstance(ddo, dict):
            break
    if not isinstance(ddo, dict):
        return []

    found = []
    seen = set()

    def walk(value):
        if isinstance(value, dict):
            job_id = str(value.get("jobId") or "").strip()
            title = _normalize_text(value.get("title") or "")
            if job_id and title:
                key = str(value.get("jobSeqNo") or job_id)
                if key not in seen:
                    seen.add(key)
                    found.append(dict(value))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(ddo)
    country_folded = str(country or "").casefold()
    if country_folded:
        found = [
            row for row in found
            if str(row.get("country") or "").casefold() == country_folded
            or country_folded in str(row.get("location") or "").casefold()
            or country_folded in str(row.get("cityStateCountry") or "").casefold()
        ]
    return found


def _phenom_page_url(source_url, offset):
    offset = max(0, int(offset or 0))
    if offset <= 0:
        return source_url
    parsed = urlparse(str(source_url or ""))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["from"] = str(offset)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _phenom_rows_for_country(rows, country="MOROCCO"):
    country_folded = str(country or "").casefold()
    if not country_folded:
        return list(rows or [])
    return [
        row
        for row in (rows or [])
        if str(row.get("country") or "").casefold() == country_folded
        or country_folded in str(row.get("location") or "").casefold()
        or country_folded in str(row.get("cityStateCountry") or "").casefold()
    ]


def _phenom_rows_to_links(rows):
    links = []
    for row in rows or []:
        apply_url = str(row.get("applyUrl") or "").strip()
        job_id = str(row.get("jobId") or row.get("reqId") or "").strip()
        title = _normalize_text(row.get("title") or "")
        if not apply_url or not job_id or not title:
            continue
        teaser_candidates = [
            str(row.get("descriptionTeaser") or "").strip(),
            str((row.get("ml_job_parser") or {}).get("descriptionTeaser_ats") or "").strip()
            if isinstance(row.get("ml_job_parser"), dict) else "",
            str((row.get("ml_job_parser") or {}).get("descriptionTeaser_first200") or "").strip()
            if isinstance(row.get("ml_job_parser"), dict) else "",
        ]
        teaser = max(teaser_candidates, key=len, default="")
        links.append({
            "title": title,
            "url": apply_url,
            "ats_provider": "phenom",
            "ats_reference": job_id,
            "ats_description": teaser,
            "job_application_url": apply_url,
            "job_application_link_kind": "direct_apply",
            "job_location": str(row.get("location") or row.get("cityStateCountry") or "").strip(),
            "job_country": "MA",
            "job_contract_type": str(row.get("contractType") or row.get("type") or "").strip(),
            "job_salary": str(row.get("salary") or "").strip(),
            "job_company": str(row.get("company") or "").strip(),
            "job_remote": str(row.get("workModel") or "").strip().casefold() in {
                "remote", "télétravail complet", "teletravail complet",
            },
            "source_published_at": str(row.get("postedDate") or row.get("dateCreated") or "").strip(),
            "phenom_payload": {
                "category": row.get("category"),
                "hiringType": row.get("hiringType"),
                "workModel": row.get("workModel"),
                "company": row.get("company"),
                "city": row.get("city"),
                "country": row.get("country"),
            },
        })
    return links


async def _collect_phenom_links_async(
    session,
    source_url,
    per_source_limit=None,
    *,
    known_ids=None,
    max_pages=None,
    seen_streak_stop=None,
    max_items=None,
    start_offset=0,
):
    page_size_hint = max(5, int(per_source_limit or JOBS_DISCOVERY_PAGE_SIZE))
    max_pages = max(1, int(max_pages or JOBS_DISCOVERY_MAX_PAGES))
    max_items = max(1, int(max_items or JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE))
    seen_streak_stop = max(1, int(seen_streak_stop or JOBS_DISCOVERY_SEEN_STREAK))
    working_known = set(known_ids or ())
    try:
        offset = max(0, int(start_offset or 0))
    except (TypeError, ValueError):
        offset = 0

    collected = []
    seen_streak = 0
    status_code = 200
    pages_scanned = 0
    stop_reason = "end"
    resume_offset = 0
    last_error = ""
    previous_page_signature = None

    for _page_number in range(max_pages):
        pages_scanned += 1
        current_offset = offset
        page_url = _phenom_page_url(source_url, current_offset)
        html_text, error, status_code = await _fetch_text_async(session, page_url)
        if error or not html_text:
            last_error = error or "empty Phenom listing"
            stop_reason = "partial_error" if collected else "error"
            resume_offset = current_offset
            if collected:
                break
            return [], last_error, status_code, {
                "stop_reason": stop_reason,
                "resume_offset": resume_offset,
                "pages_scanned": pages_scanned,
            }

        # Parse the complete embedded page first. The source routes can be wider
        # than Morocco, so raw row count controls pagination while filtering only
        # controls which vacancies enter this Jobs source.
        raw_rows = _phenom_jobs_from_html(html_text, country="")
        if not raw_rows:
            stop_reason = "end"
            resume_offset = 0
            break

        page_signature = tuple(
            sorted(
                str(row.get("jobSeqNo") or row.get("jobId") or row.get("reqId") or "").strip()
                for row in raw_rows
                if str(row.get("jobSeqNo") or row.get("jobId") or row.get("reqId") or "").strip()
            )
        )
        if previous_page_signature is not None and page_signature == previous_page_signature:
            stop_reason = "page_repeat"
            resume_offset = 0
            break
        previous_page_signature = page_signature

        page_rows = _phenom_rows_for_country(raw_rows, country="MOROCCO")
        page_links = _phenom_rows_to_links(page_rows)
        new_links, meta = _filter_new_discovery_links(
            page_links,
            working_known,
            seen_streak_stop=seen_streak_stop,
            max_items=max(max_items + len(page_links), len(page_links) + 1),
            initial_seen_streak=seen_streak,
        )
        seen_streak = int(meta.get("seen_streak") or 0)
        collected.extend(new_links)

        if meta.get("stop_reason") == "seen_streak":
            stop_reason = "seen_streak"
            resume_offset = 0
            break

        # Phenom search pages currently expose offset-style pagination through
        # the "from" query parameter. Use the actual embedded row count when
        # available; keep the source page-size hint only as a safe fallback.
        step = len(raw_rows) or page_size_hint
        next_offset = current_offset + max(1, step)

        # A short raw page is the natural end of the listing once a prior full
        # page established the page size. For the first page, continue once so
        # routes with a small initial payload can still prove whether "from"
        # pagination is supported; page_repeat guards ignored offsets.
        if _page_number > 0 and len(raw_rows) < page_size_hint:
            stop_reason = "end"
            resume_offset = 0
            break

        if len(collected) >= max_items:
            stop_reason = "max_items"
            resume_offset = next_offset
            break

        offset = next_offset
    else:
        stop_reason = "max_pages"
        resume_offset = offset

    return collected, last_error, status_code or 200, {
        "stop_reason": stop_reason,
        "resume_offset": resume_offset,
        "pages_scanned": pages_scanned,
        "page_size": page_size_hint,
        "seen_streak": seen_streak,
    }


def _csod_config(source_url):
    parsed = urlparse(str(source_url or ""))
    host = parsed.netloc.casefold()
    if not (host == "csod.com" or host.endswith(".csod.com")):
        return None
    match = re.search(r"/ux/ats/careersite/(\d+)(?:/|$)", parsed.path, flags=re.I)
    if not match:
        return None
    site_id = int(match.group(1))
    query = parse_qs(parsed.query)
    corp = (query.get("c") or [host.split(".")[0]])[0]
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return {
        "origin": origin,
        "site_id": site_id,
        "corp": corp,
        "home_url": f"{origin}/ux/ats/careersite/{site_id}/home?c={corp}",
        "search_url": f"{origin}/services/x/career-site/v1/search",
    }


def _csod_posted_iso(raw):
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", str(raw or "").strip())
    if not match:
        return ""
    month, day, year = map(int, match.groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc).isoformat()
    except ValueError:
        return ""


async def _collect_csod_links_async(
    session,
    source_url,
    per_source_limit=None,
    *,
    known_ids=None,
    max_pages=None,
    seen_streak_stop=None,
    max_items=None,
    start_page=1,
):
    cfg = _csod_config(source_url)
    if not cfg:
        return [], "invalid csod source URL", None, {
            "stop_reason": "invalid_source",
            "resume_page": 0,
            "pages_scanned": 0,
        }

    page_size = max(
        1,
        min(
            max(int(per_source_limit or 0), JOBS_DISCOVERY_PAGE_SIZE),
            50,
        ),
    )
    max_pages = max(1, int(max_pages or JOBS_DISCOVERY_MAX_PAGES))
    max_items = max(1, int(max_items or JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE))
    seen_streak_stop = max(1, int(seen_streak_stop or JOBS_DISCOVERY_SEEN_STREAK))
    working_known = set(known_ids or ())
    try:
        first_page = max(1, int(start_page or 1))
    except (TypeError, ValueError):
        first_page = 1
    resume_active = first_page > 1
    effective_seen_streak_stop = (
        10**9
        if resume_active
        else seen_streak_stop
    )
    started = time.perf_counter()

    try:
        async with session.get(
            cfg["home_url"],
            headers={**HEADERS, "Accept": "text/html"},
            timeout=ASYNC_FETCH_TIMEOUT_SECONDS,
        ) as bootstrap:
            html = await bootstrap.text(errors="ignore")
            if bootstrap.status >= 400:
                return [], f"http {bootstrap.status}", bootstrap.status, {
                    "stop_reason": f"http_{bootstrap.status}",
                    "resume_page": first_page,
                    "pages_scanned": 0,
                }
        token_match = re.search(r'"token"\s*:\s*"([A-Za-z0-9._-]+)"', html)
        if not token_match:
            return [], "anonymous csod token missing", 200, {
                "stop_reason": "token_missing",
                "resume_page": first_page,
                "pages_scanned": 0,
            }
        token = token_match.group(1)
    except (asyncio.TimeoutError, aiohttp.ClientError) as error:
        return [], error.__class__.__name__, None, {
            "stop_reason": error.__class__.__name__,
            "resume_page": first_page,
            "pages_scanned": 0,
        }

    links = []
    seen_streak = 0
    stop_reason = "end"
    status_code = 200
    resume_page = 0
    pages_scanned = 0

    for relative_page in range(max_pages):
        page_number = first_page + relative_page
        pages_scanned += 1
        payload = {
            "careerSiteId": cfg["site_id"],
            "careerSitePageId": cfg["site_id"],
            "pageNumber": page_number,
            "pageSize": page_size,
            "cultureId": 1,
            "cultureName": "en-US",
            "searchText": "",
            "states": [],
            "countryCodes": [],
            "cities": [],
            "placeID": "",
            "radius": None,
            "postingsWithinDays": None,
            "customFieldCheckboxKeys": [],
            "customFieldDropdowns": [],
            "customFieldRadios": [],
        }
        try:
            async with session.post(
                cfg["search_url"],
                json=payload,
                headers={
                    **HEADERS,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                timeout=ASYNC_FETCH_TIMEOUT_SECONDS,
            ) as response:
                status_code = response.status
                text = await response.text(errors="ignore")
                if response.status >= 400:
                    stop_reason = f"partial_http_{response.status}" if links else f"http_{response.status}"
                    resume_page = page_number
                    if links:
                        break
                    return [], f"http {response.status}", response.status, {
                        "stop_reason": stop_reason,
                        "resume_page": resume_page,
                        "pages_scanned": pages_scanned,
                    }
                data = json.loads(text or "{}")
        except (asyncio.TimeoutError, aiohttp.ClientError, ValueError, json.JSONDecodeError) as error:
            stop_reason = f"partial_{error.__class__.__name__}" if links else error.__class__.__name__
            resume_page = page_number
            if links:
                break
            return [], error.__class__.__name__, None, {
                "stop_reason": stop_reason,
                "resume_page": resume_page,
                "pages_scanned": pages_scanned,
            }

        rows = [
            row
            for row in ((data.get("data") or {}).get("requisitions") or [])
            if isinstance(row, dict)
        ]
        page_links = []
        for row in rows:
            req_id = str(row.get("requisitionId") or "").strip()
            title = _normalize_text(
                re.sub(r"<[^>]+>", " ", str(row.get("displayJobTitle") or ""))
            )
            if not req_id or not title:
                continue
            url = (
                f"{cfg['origin']}/ux/ats/careersite/{cfg['site_id']}/home/"
                f"requisition/{req_id}?c={cfg['corp']}"
            )
            page_links.append({
                "title": title,
                "url": url,
                "ats_provider": "csod",
                "ats_reference": req_id,
                "source_published_at": _csod_posted_iso(row.get("postingEffectiveDate")),
            })

        new_links, meta = _filter_new_discovery_links(
            page_links,
            working_known,
            seen_streak_stop=effective_seen_streak_stop,
            max_items=max(max_items + len(page_links), len(page_links) + 1),
            initial_seen_streak=seen_streak,
        )
        seen_streak = int(meta.get("seen_streak") or 0)
        links.extend(new_links)

        if meta.get("stop_reason") == "seen_streak":
            stop_reason = "seen_streak"
            resume_page = 0
            break
        if not rows or len(rows) < page_size:
            stop_reason = "end"
            resume_page = 0
            break
        if len(links) >= max_items:
            stop_reason = "max_items"
            resume_page = page_number + 1
            break
    else:
        stop_reason = "max_pages"
        resume_page = first_page + max_pages

    log_event(
        "csod_source_fetch",
        url=source_url,
        jobs=len(links),
        pages=pages_scanned,
        stop_reason=stop_reason,
        resume_page=resume_page,
        elapsed_ms=elapsed_ms(started),
    )
    return links, "", status_code or 200, {
        "stop_reason": stop_reason,
        "resume_page": resume_page,
        "pages_scanned": pages_scanned,
        "page_size": page_size,
        "seen_streak": seen_streak,
    }


def _un_careers_title_from_html(html_text, job_id):
    soup = BeautifulSoup(html_text or "", "html.parser")
    candidates = []
    for selector in (
        "h1",
        "[itemprop='title']",
        "meta[property='og:title']",
        "title",
    ):
        if selector.startswith("meta"):
            tag = soup.select_one(selector)
            value = _normalize_text(tag.get("content") if tag else "")
        else:
            tag = soup.select_one(selector)
            value = _normalize_text(tag.get_text(" ", strip=True) if tag else "")
        if value:
            candidates.append(value)

    blocked = {
        "united nations",
        "un careers",
        "job openings",
        "job opening",
    }
    for value in candidates:
        folded = value.casefold()
        if folded in blocked:
            continue
        value = re.sub(r"\s*[-|]\s*united nations.*$", "", value, flags=re.I).strip()
        if value and value.casefold() not in blocked and str(job_id) not in {value}:
            return value
    return f"United Nations Job {job_id}"


async def _collect_un_careers_links_async(session, source_url, per_source_limit=None):
    listing_html, error, status_code = await _fetch_text_async(session, source_url)
    if error or not listing_html:
        return [], error or "empty UN Careers listing", status_code

    visible_text = BeautifulSoup(listing_html, "html.parser").get_text(" ", strip=True)
    job_ids = []
    seen = set()
    for match in re.finditer(r"\bJob\s*ID\s*[:#-]?\s*(\d{5,9})\b", visible_text, flags=re.I):
        job_id = match.group(1)
        if job_id not in seen:
            seen.add(job_id)
            job_ids.append(job_id)

    # Some versions of the UN page serialize the cards in script data rather
    # than visible nodes. Keep a conservative fallback for explicit Job ID keys.
    if not job_ids:
        for match in re.finditer(
            r'(?i)(?:job\s*id|jobId|jobID)["\'\s:=]+(\d{5,9})',
            listing_html,
        ):
            job_id = match.group(1)
            if job_id not in seen:
                seen.add(job_id)
                job_ids.append(job_id)

    limit = max(1, min(int(per_source_limit or 3), 8))
    links = []
    for job_id in job_ids[: max(limit * 3, 12)]:
        detail_url = f"https://careers.un.org/jobSearchDescription/{job_id}?language=en"
        detail_html, detail_error, detail_status = await _fetch_text_async(session, detail_url)
        if detail_error or not detail_html:
            continue
        title = _un_careers_title_from_html(detail_html, job_id)
        links.append({
            "title": title,
            "url": detail_url,
            "ats_provider": "un_careers",
            "ats_reference": job_id,
        })
        if len(links) >= limit:
            break

    if not links:
        return [], "UN Careers listing exposed no usable Job IDs", status_code or 200
    return links, "", 200


async def _collect_links_from_feed_async(session, feed_url, source_url):
    text, error, _status_code = await _fetch_text_async(session, feed_url)
    if error or not text:
        return [], error
    return _parse_feed_article_links(text, source_url, feed_url=feed_url), ""


async def _collect_article_links_for_source_async(
    session,
    source_url,
    per_source_limit=None,
    feed_url=None,
    extractor_type="auto",
    strict_source_path=True,
    *,
    known_ids=None,
    max_pages=None,
    seen_streak_stop=None,
    max_items=None,
    resume_state=None,
):
    print(f"\n--- Discovering links from source: {source_url} ---")

    extractor_mode = str(extractor_type or "").lower()
    resume_state = resume_state if isinstance(resume_state, dict) else {}
    if extractor_mode == "workday_api":
        links, error, status_code, discovery_meta = await _collect_workday_links_async(
            session,
            source_url,
            per_source_limit=per_source_limit,
            known_ids=known_ids,
            max_pages=max_pages,
            seen_streak_stop=seen_streak_stop,
            max_items=max_items,
            start_offset=(
                resume_state.get("offset", 0)
                if resume_state.get("kind") == "workday"
                else 0
            ),
        )
        print(f"  Collected {len(links)} Workday job link(s) from this source.")
        return [
            _link_to_article_dict(link, source_url)
            for link in links
        ], error, status_code, {
            "normal_links_found": len(links),
            "feed_links_found": 0,
            "method_used": "workday_api" if links else ("failed:workday_api" if error else "workday_api"),
            "tried_feed_urls": [],
            "discovery_meta": discovery_meta,
            "discovery_resume": (
                {"kind": "workday", "offset": discovery_meta.get("resume_offset")}
                if int(discovery_meta.get("resume_offset") or 0) > 0
                else {}
            ),
        }

    if extractor_mode == "emploi_public":
        links, error, status_code, discovery_meta = await _collect_paginated_html_links_async(
            session,
            source_url,
            _parse_emploi_public_links,
            known_ids=known_ids,
            page_size=per_source_limit,
            max_pages=max_pages,
            seen_streak_stop=seen_streak_stop,
            max_items=max_items,
            start_url=(
                resume_state.get("url")
                if resume_state.get("kind") == "html"
                else None
            ),
        )
        return [_link_to_article_dict(link, source_url) for link in links], error, status_code, {
            "normal_links_found": len(links),
            "feed_links_found": 0,
            "method_used": "emploi_public" if links else ("failed:emploi_public" if error else "emploi_public"),
            "tried_feed_urls": [],
            "discovery_meta": discovery_meta,
            "discovery_resume": (
                {"kind": "html", "url": discovery_meta.get("resume_url")}
                if discovery_meta.get("resume_url")
                else {}
            ),
        }

    if extractor_mode == "capgemini_jobs":
        links, error, status_code, discovery_meta = await _collect_paginated_html_links_async(
            session,
            source_url,
            _parse_capgemini_job_links,
            known_ids=known_ids,
            page_size=per_source_limit,
            max_pages=max_pages,
            seen_streak_stop=seen_streak_stop,
            max_items=max_items,
            start_url=(
                resume_state.get("url")
                if resume_state.get("kind") == "html"
                else None
            ),
        )
        return [_link_to_article_dict(link, source_url) for link in links], error, status_code, {
            "normal_links_found": len(links),
            "feed_links_found": 0,
            "method_used": "capgemini_jobs" if links else ("failed:capgemini_jobs" if error else "capgemini_jobs"),
            "tried_feed_urls": [],
            "discovery_meta": discovery_meta,
            "discovery_resume": (
                {"kind": "html", "url": discovery_meta.get("resume_url")}
                if discovery_meta.get("resume_url")
                else {}
            ),
        }

    if extractor_mode in {"etalent", "ats_listing"}:
        links, error, status_code, discovery_meta = await _collect_paginated_html_links_async(
            session,
            source_url,
            _parse_etalent_links,
            known_ids=known_ids,
            page_size=per_source_limit,
            max_pages=max_pages,
            seen_streak_stop=seen_streak_stop,
            max_items=max_items,
            start_url=(
                resume_state.get("url")
                if resume_state.get("kind") == "html"
                else None
            ),
        )
        return [_link_to_article_dict(link, source_url) for link in links], error, status_code, {
            "normal_links_found": len(links),
            "feed_links_found": 0,
            "method_used": "etalent",
            "tried_feed_urls": [],
            "discovery_meta": discovery_meta,
            "discovery_resume": (
                {"kind": "html", "url": discovery_meta.get("resume_url")}
                if discovery_meta.get("resume_url")
                else {}
            ),
            "empty_ok": not links and not error and status_code == 200,
        }

    if extractor_mode == "phenom_ddo":
        links, error, status_code, discovery_meta = await _collect_phenom_links_async(
            session,
            source_url,
            per_source_limit=per_source_limit,
            known_ids=known_ids,
            max_pages=max_pages,
            seen_streak_stop=seen_streak_stop,
            max_items=max_items,
            start_offset=(
                resume_state.get("offset", 0)
                if resume_state.get("kind") == "phenom"
                else 0
            ),
        )
        return [_link_to_article_dict(link, source_url) for link in links], error, status_code, {
            "normal_links_found": len(links),
            "feed_links_found": 0,
            "method_used": "phenom_ddo" if links else ("failed:phenom_ddo" if error else "phenom_ddo"),
            "tried_feed_urls": [],
            "discovery_meta": discovery_meta,
            "discovery_resume": (
                {"kind": "phenom", "offset": discovery_meta.get("resume_offset")}
                if int(discovery_meta.get("resume_offset") or 0) > 0
                else {}
            ),
            "empty_ok": not links and not error and status_code == 200,
        }

    if extractor_mode == "csod":
        links, error, status_code, discovery_meta = await _collect_csod_links_async(
            session,
            source_url,
            per_source_limit=per_source_limit,
            known_ids=known_ids,
            max_pages=max_pages,
            seen_streak_stop=seen_streak_stop,
            max_items=max_items,
            start_page=(
                resume_state.get("page", 1)
                if resume_state.get("kind") == "csod"
                else 1
            ),
        )
        print(f"  Collected {len(links)} CSOD job link(s) from this source.")
        return [
            _link_to_article_dict(link, source_url)
            for link in links
        ], error, status_code, {
            "normal_links_found": len(links),
            "feed_links_found": 0,
            "method_used": "csod" if links else ("failed:csod" if error else "csod"),
            "tried_feed_urls": [],
            "discovery_meta": discovery_meta,
            "discovery_resume": (
                {"kind": "csod", "page": discovery_meta.get("resume_page")}
                if int(discovery_meta.get("resume_page") or 0) > 0
                else {}
            ),
        }

    if extractor_mode == "un_careers":
        links, error, status_code = await _collect_un_careers_links_async(
            session,
            source_url,
            per_source_limit=per_source_limit,
        )
        print(f"  Collected {len(links)} UN Careers job link(s) from this source.")
        return [
            _link_to_article_dict(link, source_url)
            for link in links
        ], error, status_code, {
            "normal_links_found": len(links),
            "feed_links_found": 0,
            "method_used": "un_careers" if links else ("failed:un_careers" if error else "un_careers"),
            "tried_feed_urls": [],
        }

    if JOBS_MODE:
        generic_parser = lambda html_text, current_url, per_source_limit=None: get_article_links(
            html_text,
            current_url,
            strict_source_path=strict_source_path,
        )
        html_links, error, status_code, discovery_meta = await _collect_paginated_html_links_async(
            session,
            source_url,
            generic_parser,
            known_ids=known_ids,
            page_size=per_source_limit,
            max_pages=max_pages,
            seen_streak_stop=seen_streak_stop,
            max_items=max_items,
            start_url=(
                resume_state.get("url")
                if resume_state.get("kind") == "html"
                else None
            ),
        )
        combined_links = _filter_article_links(
            html_links,
            source_url,
            strict_source_path=strict_source_path,
        )
        print(f"  Collected {len(combined_links)} paginated Jobs link(s) from this source.")
        return [
            _link_to_article_dict(link, source_url)
            for link in combined_links
        ], error, status_code, {
            "normal_links_found": len(html_links),
            "feed_links_found": 0,
            "method_used": "html-pagination",
            "tried_feed_urls": [],
            "discovery_meta": discovery_meta,
            "discovery_resume": (
                {"kind": "html", "url": discovery_meta.get("resume_url")}
                if discovery_meta.get("resume_url")
                else {}
            ),
        }

    listing_html, error, status_code = await _fetch_text_async(session, source_url)
    html_links = []
    feed_links = []
    tried_feed_urls = []

    if listing_html:
        html_links = get_article_links(
            listing_html,
            source_url,
            strict_source_path=strict_source_path,
        )
        feed_candidates = []
        if feed_url:
            feed_candidates.append(feed_url)
        feed_candidates.extend(_extract_feed_urls(listing_html, source_url))
    else:
        feed_candidates = _fallback_feed_urls(source_url, feed_url=feed_url)

    seen_feed_urls = set()
    feed_candidates = [
        candidate
        for candidate in feed_candidates
        if candidate and not (candidate in seen_feed_urls or seen_feed_urls.add(candidate))
    ]

    if extractor_mode == "feed_fallback":
        direct_feed_links = _parse_feed_article_links(
            listing_html,
            source_url,
            feed_url=source_url,
        ) if listing_html else []
        if direct_feed_links:
            feed_links.extend(direct_feed_links)

    should_try_feed = (
        bool(feed_url)
        or extractor_mode in {"rss", "feed", "xml", "feed_fallback"}
        or (not JOBS_MODE and (bool(error) or len(html_links) < (per_source_limit or 3)))
    )
    if should_try_feed:
        for current_feed_url in feed_candidates[:5]:
            tried_feed_urls.append(current_feed_url)
            current_links, feed_error = await _collect_links_from_feed_async(
                session,
                current_feed_url,
                source_url,
            )
            if feed_error:
                print(f"  Feed failed: {current_feed_url} ({feed_error})")
            print(
                f"  Feed yielded {len(current_links)} candidate link(s): "
                f"{current_feed_url}"
            )
            if current_links:
                feed_links.extend(current_links)
                if len(feed_links) >= (per_source_limit or 3):
                    break

    method_used = "html"
    if feed_links and not html_links:
        method_used = "feed"
    elif feed_links and (error or len(html_links) < (per_source_limit or 3)):
        method_used = "fallback"
    elif feed_links:
        method_used = "html+feed"
    elif error:
        method_used = "failed"

    if extractor_type and extractor_type != "auto":
        method_used = f"{method_used}:{extractor_type}"

    combined_links = _filter_article_links(
        feed_links + html_links,
        source_url,
        strict_source_path=False if feed_links else strict_source_path,
    )
    if per_source_limit:
        combined_links = combined_links[:per_source_limit]

    if not combined_links and not JOBS_MODE:
        sync_links, sync_error, sync_status, sync_details = await asyncio.to_thread(
            _collect_article_links_for_source,
            source_url,
            per_source_limit=per_source_limit,
            feed_url=feed_url,
            extractor_type=extractor_type,
        )
        if sync_links:
            return sync_links, sync_error, sync_status, sync_details

    print(f"  Collected {len(combined_links)} article link(s) from this source.")
    return [
        _link_to_article_dict(link, source_url)
        for link in combined_links
    ], error, status_code, {
        "normal_links_found": len(html_links),
        "feed_links_found": len(feed_links),
        "method_used": method_used,
        "tried_feed_urls": tried_feed_urls,
    }


def _collect_article_links_for_source(
    source_url,
    per_source_limit=None,
    feed_url=None,
    extractor_type="auto",
    strict_source_path=True,
    *,
    known_ids=None,
    max_pages=None,
    seen_streak_stop=None,
    max_items=None,
    resume_state=None,
):
    print(f"\n--- Discovering links from source: {source_url} ---")

    source_document = make_request(source_url)
    if not source_document:
        print("  Could not fetch source listing page.")
        feed_links = []
        tried_feed_urls = []
        fallback_candidates = (
            _fallback_feed_urls(source_url, feed_url=feed_url)
            if not JOBS_MODE or feed_url or str(extractor_type or "").lower() in {"rss", "feed", "xml"}
            else []
        )
        for fallback_feed_url in fallback_candidates[:2 if JOBS_MODE else 5]:
            tried_feed_urls.append(fallback_feed_url)
            current_links = _collect_links_from_feed(fallback_feed_url, source_url)
            print(f"  Feed yielded {len(current_links)} candidate link(s): {fallback_feed_url}")
            if current_links:
                feed_links.extend(current_links)
                if len(feed_links) >= (per_source_limit or 3):
                    break

        if per_source_limit:
            feed_links = feed_links[:per_source_limit]

        return [
            _link_to_article_dict(link, source_url)
            for link in feed_links
        ], "fetch failed", None, {
            "normal_links_found": 0,
            "feed_links_found": len(feed_links),
            "method_used": "feed" if feed_links else "failed",
            "tried_feed_urls": tried_feed_urls,
        }

    extractor_mode = str(extractor_type or "").lower()
    resume_state = resume_state if isinstance(resume_state, dict) else {}
    if JOBS_MODE and extractor_mode in {"emploi_public", "capgemini_jobs", "etalent", "ats_listing"}:
        parser = {
            "emploi_public": _parse_emploi_public_links,
            "capgemini_jobs": _parse_capgemini_job_links,
            "etalent": _parse_etalent_links,
            "ats_listing": _parse_etalent_links,
        }[extractor_mode]
        links, error, status_code, discovery_meta = _collect_paginated_html_links_sync(
            source_url,
            parser,
            known_ids=known_ids,
            page_size=per_source_limit,
            max_pages=max_pages,
            seen_streak_stop=seen_streak_stop,
            max_items=max_items,
            start_url=(
                resume_state.get("url")
                if resume_state.get("kind") == "html"
                else None
            ),
        )
        print(f"  Collected {len(links)} paginated {extractor_mode} link(s) from this source.")
        return [
            _link_to_article_dict(link, source_url)
            for link in links
        ], error, status_code, {
            "normal_links_found": len(links),
            "feed_links_found": 0,
            "method_used": f"{extractor_mode}-pagination",
            "tried_feed_urls": [],
            "discovery_meta": discovery_meta,
            "discovery_resume": (
                {"kind": "html", "url": discovery_meta.get("resume_url")}
                if discovery_meta.get("resume_url")
                else {}
            ),
            "empty_ok": extractor_mode in {"etalent", "ats_listing"} and not links and not error,
        }

    if JOBS_MODE and extractor_mode == "auto":
        parser = lambda html_text, current_url, per_source_limit=None: get_article_links(
            html_text,
            current_url,
            strict_source_path=strict_source_path,
        )
        links, error, status_code, discovery_meta = _collect_paginated_html_links_sync(
            source_url,
            parser,
            known_ids=known_ids,
            page_size=per_source_limit,
            max_pages=max_pages,
            seen_streak_stop=seen_streak_stop,
            max_items=max_items,
            start_url=(
                resume_state.get("url")
                if resume_state.get("kind") == "html"
                else None
            ),
        )
        print(f"  Collected {len(links)} paginated Jobs link(s) from this source.")
        return [
            _link_to_article_dict(link, source_url)
            for link in links
        ], error, status_code, {
            "normal_links_found": len(links),
            "feed_links_found": 0,
            "method_used": "html-pagination",
            "tried_feed_urls": [],
            "discovery_meta": discovery_meta,
            "discovery_resume": (
                {"kind": "html", "url": discovery_meta.get("resume_url")}
                if discovery_meta.get("resume_url")
                else {}
            ),
        }

    status_code = getattr(source_document, "status", None)
    error = ""
    try:
        numeric_status = int(status_code)
    except (TypeError, ValueError):
        numeric_status = None

    if numeric_status and numeric_status >= 400:
        error = f"listing returned HTTP {numeric_status}"

    html_links = get_article_links(
        source_document,
        source_url,
        strict_source_path=strict_source_path,
    )
    feed_links = []
    tried_feed_urls = []

    feed_candidates = []
    if feed_url:
        feed_candidates.append(feed_url)
    feed_candidates.extend(_extract_feed_urls(source_document, source_url))

    seen_feed_urls = set()
    feed_candidates = [
        candidate
        for candidate in feed_candidates
        if candidate and not (candidate in seen_feed_urls or seen_feed_urls.add(candidate))
    ]

    extractor_mode = str(extractor_type or "").lower()
    if extractor_mode == "feed_fallback":
        raw_text = str(source_document or "")
        direct_feed_links = _parse_feed_article_links(
            raw_text,
            source_url,
            feed_url=source_url,
        )
        if direct_feed_links:
            feed_links.extend(direct_feed_links)

    should_try_feed = (
        bool(feed_url)
        or bool(error)
        or extractor_mode in {"rss", "feed", "xml", "feed_fallback"}
        or len(html_links) < (per_source_limit or 3)
    )

    if should_try_feed:
        for current_feed_url in feed_candidates[:5]:
            tried_feed_urls.append(current_feed_url)
            current_links = _collect_links_from_feed(current_feed_url, source_url)
            print(
                f"  Feed yielded {len(current_links)} candidate link(s): "
                f"{current_feed_url}"
            )
            if current_links:
                feed_links.extend(current_links)
                if len(feed_links) >= (per_source_limit or 3):
                    break

    method_used = "html"
    if feed_links and not html_links:
        method_used = "feed"
    elif feed_links and (error or len(html_links) < (per_source_limit or 3)):
        method_used = "fallback"
    elif feed_links:
        method_used = "html+feed"

    if extractor_type and extractor_type != "auto":
        method_used = f"{method_used}:{extractor_type}"

    combined_links = _filter_article_links(
        feed_links + html_links,
        source_url,
        strict_source_path=False if feed_links else strict_source_path,
    )
    if per_source_limit:
        combined_links = combined_links[:per_source_limit]

    print(f"  Collected {len(combined_links)} article link(s) from this source.")
    return [
        _link_to_article_dict(link, source_url)
        for link in combined_links
    ], error, numeric_status, {
        "normal_links_found": len(html_links),
        "feed_links_found": len(feed_links),
        "method_used": method_used,
        "tried_feed_urls": tried_feed_urls,
    }


def _can_run_async_discovery():
    if aiohttp is None:
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


async def _discover_latest_article_links_async(enabled_sources):
    discovered = []
    source_results = []
    timeout = aiohttp.ClientTimeout(total=ASYNC_FETCH_TIMEOUT_SECONDS + 10)
    connector = aiohttp.TCPConnector(limit=ASYNC_SOURCE_FETCH_CONCURRENCY, ttl_dns_cache=300)
    semaphore = asyncio.Semaphore(ASYNC_SOURCE_FETCH_CONCURRENCY)

    async def collect_one(index, source):
        source_name = source.get("name", source.get("base_url", "Unknown source"))
        base_url = source.get("base_url", "").strip()
        category_hint = source.get("category_hint", "")
        category_key = source.get("category_key", "")
        category_name = source.get("category_name", "")
        category_label = source.get("category_label", category_hint)
        try:
            fetch_limit = int(source.get("fetch_limit_per_run", 3))
        except (TypeError, ValueError):
            fetch_limit = 3
        fetch_limit = max(1, min(fetch_limit, 30 if JOBS_MODE else 3))

        if JOBS_MODE:
            try:
                discovery_max_pages = int(
                    source.get("discovery_max_pages", JOBS_DISCOVERY_MAX_PAGES)
                )
            except (TypeError, ValueError):
                discovery_max_pages = JOBS_DISCOVERY_MAX_PAGES
            try:
                discovery_seen_streak = int(
                    source.get("discovery_seen_streak", JOBS_DISCOVERY_SEEN_STREAK)
                )
            except (TypeError, ValueError):
                discovery_seen_streak = JOBS_DISCOVERY_SEEN_STREAK
            try:
                discovery_max_items = int(
                    source.get(
                        "discovery_max_items",
                        JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE,
                    )
                )
            except (TypeError, ValueError):
                discovery_max_items = JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE
            source_crawl = source_crawl_record(base_url)
            known_ids = _source_known_discovery_ids(base_url)
            resume_state = source_crawl.get("job_discovery_resume") or {}
            if not isinstance(resume_state, dict):
                resume_state = {}
        else:
            discovery_max_pages = 1
            discovery_seen_streak = JOBS_DISCOVERY_SEEN_STREAK
            discovery_max_items = fetch_limit
            known_ids = set()
            resume_state = {}

        print(f"\n[{index}] Checking {source_name}")
        async with semaphore:
            try:
                links, error, status_code, details = await _collect_article_links_for_source_async(
                    session,
                    base_url,
                    per_source_limit=fetch_limit,
                    feed_url=source.get("feed_url"),
                    extractor_type=source.get("extractor_type", "auto"),
                    strict_source_path=bool(source.get("strict_source_path", True)),
                    known_ids=known_ids,
                    max_pages=discovery_max_pages,
                    seen_streak_stop=discovery_seen_streak,
                    max_items=discovery_max_items,
                    resume_state=resume_state,
                )
            except Exception as exc:
                links = []
                error = f"{type(exc).__name__}: {exc}"
                status_code = None
                details = {
                    "normal_links_found": 0,
                    "feed_links_found": 0,
                    "method_used": "failed",
                    "tried_feed_urls": [],
                }
                print(f"  Source failed without stopping the fetch run: {error}")

        return {
            "source": source,
            "source_name": source_name,
            "base_url": base_url,
            "category_hint": category_hint,
            "category_key": category_key,
            "category_name": category_name,
            "category_label": category_label,
                    "source_priority": source.get("source_priority", ""),
                    "official_source": bool(source.get("official_source", False)),
                    "source_country": source.get("source_country", ""),
                    "source_eligibility": source.get("source_eligibility", ""),
                    "source_remote": bool(source.get("source_remote", False)),
                    "source_visa_sponsorship": bool(source.get("source_visa_sponsorship", False)),
            "fetch_limit": fetch_limit,
            "discovery_max_pages": discovery_max_pages,
            "discovery_seen_streak": discovery_seen_streak,
            "discovery_max_items": discovery_max_items,
            "known_ids_before": len(known_ids),
            "resume_state_before": resume_state,
            "links": links,
            "error": error,
            "status_code": status_code,
            "details": details,
        }

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tasks = [
            collect_one(index, source)
            for index, source in enumerate(enabled_sources, 1)
            if source.get("base_url", "").strip()
        ]
        results = await asyncio.gather(*tasks)

    for result in results:
        fetch_limit = result["fetch_limit"]
        _record_source_result(
            result["base_url"],
            result["source_name"],
            result["error"],
            len(result["links"]),
            empty_ok=bool(result["details"].get("empty_ok")),
        )
        crawl_fields = {
            "source_name": result["source_name"],
            "last_crawled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "overlap_minutes": CRAWL_OVERLAP_MINUTES,
        }
        if JOBS_MODE:
            crawl_fields.update({
                "job_seen_ids": _merge_source_seen_ids(
                    result["base_url"],
                    result["links"],
                ),
                "job_seen_ids_count": min(
                    JOBS_DISCOVERY_SEEN_MEMORY,
                    result.get("known_ids_before", 0) + len(result["links"]),
                ),
                "discovery_last_new_count": len(result["links"]),
                "discovery_page_size_hint": fetch_limit,
                "discovery_max_pages": result.get("discovery_max_pages"),
                "discovery_seen_streak": result.get("discovery_seen_streak"),
                "discovery_max_items": result.get("discovery_max_items"),
                "job_discovery_resume": (
                    result.get("details", {}).get("discovery_resume") or {}
                ),
                "discovery_stop_reason": (
                    result.get("details", {}).get("discovery_meta", {}).get("stop_reason", "")
                ),
                "discovery_pages_scanned": (
                    result.get("details", {}).get("discovery_meta", {}).get("pages_scanned", 0)
                ),
            })
        update_source_crawl(result["base_url"], **crawl_fields)

        result_links = result["links"] if JOBS_MODE else result["links"][:fetch_limit]
        for link in result_links:
            discovered.append(
                {
                    **{key: value for key, value in link.items() if key not in {"source_name", "source_url"}},
                    "title": link.get("title", ""),
                    "url": link.get("url", ""),
                    "source_name": result["source_name"],
                    "source_url": result["base_url"],
                    "category_hint": result["category_hint"],
                    "category_key": result["category_key"],
                    "category_name": result["category_name"],
                    "category_label": result["category_label"],
                    "source_priority": result.get("source_priority", ""),
                    "official_source": bool(result.get("official_source", False)),
                    "source_country": result.get("source_country", ""),
                    "source_eligibility": result.get("source_eligibility", ""),
                    "source_remote": bool(result.get("source_remote", False)),
                    "source_visa_sponsorship": bool(result.get("source_visa_sponsorship", False)),
                }
            )

        source_results.append(
            {
                "source_name": result["source_name"],
                "base_url": result["base_url"],
                "category_hint": result["category_hint"],
                "category_key": result["category_key"],
                "category_name": result["category_name"],
                "category_label": result["category_label"],
                "fetch_limit_per_run": fetch_limit,
                "discovery_mode": "paginated_seen_ids" if JOBS_MODE else "fixed_limit",
                "discovery_max_pages": result.get("discovery_max_pages"),
                "discovery_seen_streak": result.get("discovery_seen_streak"),
                "discovery_max_items": result.get("discovery_max_items"),
                "known_ids_before": result.get("known_ids_before", 0),
                "resume_before": result.get("resume_state_before") or {},
                "resume_after": result.get("details", {}).get("discovery_resume") or {},
                "stop_reason": result.get("details", {}).get("discovery_meta", {}).get("stop_reason", ""),
                "pages_scanned": result.get("details", {}).get("discovery_meta", {}).get("pages_scanned", 0),
                "links_found": len(result["links"]),
                "status": "failed" if result["error"] else "success",
                "listing_status_code": result["status_code"],
                "error": result["error"],
                **result["details"],
            }
        )

    return {
        "checked_sources": len(results),
        "articles": discovered,
        "source_results": source_results,
    }


def discover_latest_article_links(sources):
    """
    Phase 1 safe discovery: collect latest article links only.
    This does not fetch article bodies, call AI, or publish anything.
    """
    enabled_sources = [source for source in sources if source.get("enabled", True)]
    cooldown_results = []
    if JOBS_MODE:
        enabled_sources, cooldown_results = _filter_healthy_sources(
            _order_sources_for_fast_run(enabled_sources)
        )
    if (
        not JOBS_MODE
        and FAST_NEWS_MODE
        and FIRST_VALID_ARTICLE_MODE
        and MAX_SOURCES_PER_RUN > 0
    ):
        enabled_sources = _prioritize_sources(enabled_sources)
        enabled_sources = enabled_sources[:MAX_SOURCES_PER_RUN]
    if _can_run_async_discovery():
        log_event("source_discovery_start", sources=len(enabled_sources), mode="aiohttp")
        result = asyncio.run(_discover_latest_article_links_async(enabled_sources))
        if cooldown_results:
            result["source_results"] = cooldown_results + list(result.get("source_results") or [])
            result["checked_sources"] = len(result["source_results"])
        log_event(
            "source_discovery_end",
            sources=result.get("checked_sources", 0),
            articles=len(result.get("articles") or []),
            failures=sum(
                1
                for source in result.get("source_results", [])
                if source.get("status") == "failed"
            ),
        )
        return result

    discovered = []
    checked_sources = 0
    source_results = list(cooldown_results)

    log_event("source_discovery_start", sources=len(enabled_sources), mode="requests")
    for source in enabled_sources:
        source_name = source.get("name", source.get("base_url", "Unknown source"))
        base_url = source.get("base_url", "").strip()
        if not base_url:
            continue

        checked_sources += 1
        try:
            fetch_limit = int(source.get("fetch_limit_per_run", 3))
        except (TypeError, ValueError):
            fetch_limit = 3
        fetch_limit = max(1, min(fetch_limit, 30 if JOBS_MODE else 3))

        if JOBS_MODE:
            try:
                discovery_max_pages = int(
                    source.get("discovery_max_pages", JOBS_DISCOVERY_MAX_PAGES)
                )
            except (TypeError, ValueError):
                discovery_max_pages = JOBS_DISCOVERY_MAX_PAGES
            try:
                discovery_seen_streak = int(
                    source.get("discovery_seen_streak", JOBS_DISCOVERY_SEEN_STREAK)
                )
            except (TypeError, ValueError):
                discovery_seen_streak = JOBS_DISCOVERY_SEEN_STREAK
            try:
                discovery_max_items = int(
                    source.get(
                        "discovery_max_items",
                        JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE,
                    )
                )
            except (TypeError, ValueError):
                discovery_max_items = JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE
            source_crawl = source_crawl_record(base_url)
            known_ids = _source_known_discovery_ids(base_url)
            resume_state = source_crawl.get("job_discovery_resume") or {}
            if not isinstance(resume_state, dict):
                resume_state = {}
        else:
            discovery_max_pages = 1
            discovery_seen_streak = JOBS_DISCOVERY_SEEN_STREAK
            discovery_max_items = fetch_limit
            known_ids = set()
            resume_state = {}

        print(f"\n[{checked_sources}] Checking {source_name}")
        category_hint = source.get("category_hint", "")
        category_key = source.get("category_key", "")
        category_name = source.get("category_name", "")
        category_label = source.get("category_label", category_hint)
        try:
            links, error, status_code, details = _collect_article_links_for_source(
                base_url,
                per_source_limit=fetch_limit,
                feed_url=source.get("feed_url"),
                extractor_type=source.get("extractor_type", "auto"),
                strict_source_path=bool(source.get("strict_source_path", True)),
                known_ids=known_ids,
                max_pages=discovery_max_pages,
                seen_streak_stop=discovery_seen_streak,
                max_items=discovery_max_items,
                resume_state=resume_state,
            )
        except Exception as exc:
            links = []
            error = f"{type(exc).__name__}: {exc}"
            status_code = None
            details = {
                "normal_links_found": 0,
                "feed_links_found": 0,
                "method_used": "failed",
                "tried_feed_urls": [],
            }
            print(f"  Source failed without stopping the fetch run: {error}")

        result_links = links if JOBS_MODE else links[:fetch_limit]
        for link in result_links:
            discovered.append(
                {
                    **{key: value for key, value in link.items() if key not in {"source_name", "source_url"}},
                    "title": link.get("title", ""),
                    "url": link.get("url", ""),
                    "source_name": source_name,
                    "source_url": base_url,
                    "category_hint": category_hint,
                    "category_key": category_key,
                    "category_name": category_name,
                    "category_label": category_label,
                    "source_priority": source.get("source_priority", ""),
                    "official_source": bool(source.get("official_source", False)),
                    "source_country": source.get("source_country", ""),
                    "source_eligibility": source.get("source_eligibility", ""),
                    "source_remote": bool(source.get("source_remote", False)),
                    "source_visa_sponsorship": bool(source.get("source_visa_sponsorship", False)),
                }
            )

        if JOBS_MODE:
            update_source_crawl(
                base_url,
                source_name=source_name,
                last_crawled_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                overlap_minutes=CRAWL_OVERLAP_MINUTES,
                job_seen_ids=_merge_source_seen_ids(base_url, links),
                job_seen_ids_count=min(
                    JOBS_DISCOVERY_SEEN_MEMORY,
                    len(known_ids) + len(links),
                ),
                discovery_last_new_count=len(links),
                discovery_page_size_hint=fetch_limit,
                discovery_max_pages=discovery_max_pages,
                discovery_seen_streak=discovery_seen_streak,
                discovery_max_items=discovery_max_items,
                job_discovery_resume=details.get("discovery_resume") or {},
                discovery_stop_reason=details.get("discovery_meta", {}).get("stop_reason", ""),
                discovery_pages_scanned=details.get("discovery_meta", {}).get("pages_scanned", 0),
            )

        source_results.append(
            {
                "source_name": source_name,
                "base_url": base_url,
                "category_hint": category_hint,
                "category_key": category_key,
                "category_name": category_name,
                "category_label": category_label,
                "fetch_limit_per_run": fetch_limit,
                "discovery_mode": "paginated_seen_ids" if JOBS_MODE else "fixed_limit",
                "discovery_max_pages": discovery_max_pages,
                "discovery_seen_streak": discovery_seen_streak,
                "discovery_max_items": discovery_max_items,
                "known_ids_before": len(known_ids),
                "resume_before": resume_state,
                "resume_after": details.get("discovery_resume") or {},
                "stop_reason": details.get("discovery_meta", {}).get("stop_reason", ""),
                "pages_scanned": details.get("discovery_meta", {}).get("pages_scanned", 0),
                "links_found": len(links),
                "status": "failed" if error else "success",
                "listing_status_code": status_code,
                "error": error,
                **details,
            }
        )

        if SCRAPE_DELAY_SECONDS > 0:
            time.sleep(SCRAPE_DELAY_SECONDS)

    log_event(
        "source_discovery_end",
        sources=checked_sources,
        articles=len(discovered),
        failures=sum(1 for source in source_results if source.get("status") == "failed"),
    )
    return {
        "checked_sources": checked_sources,
        "articles": discovered,
        "source_results": source_results,
    }


def discover_first_valid_article_link(sources, existing_articles=None, published_topic_hashes=None):
    """
    Fast path: check sources in order and return as soon as one non-duplicate
    article link is found. This avoids scanning every source before publishing.
    """
    enabled_sources, cooldown_results = _filter_healthy_sources(
        _order_sources_for_fast_run([source for source in sources if source.get("enabled", True)])
    )
    if MAX_SOURCES_PER_RUN > 0:
        enabled_sources = enabled_sources[:MAX_SOURCES_PER_RUN]

    existing_articles = existing_articles or []
    published_topic_hashes = published_topic_hashes or set()
    known_urls = {
        item.get("canonical_url") or canonicalize_url(item.get("url"))
        for item in existing_articles
        if item.get("url") and not item.get("archived")
    }
    known_title_hashes = {
        item.get("title_hash") or title_hash(item.get("title", ""))
        for item in existing_articles
        if item.get("title") and not item.get("archived")
    }

    source_results = list(cooldown_results)
    fallback_expanded_candidates = []
    for index, source in enumerate(enabled_sources, 1):
        source_name = source.get("name", source.get("base_url", "Unknown source"))
        base_url = source.get("base_url", "").strip()
        if not base_url:
            continue
        started = time.perf_counter()
        _log(f"\nChecking source {index}/{len(enabled_sources)}: {source_name}")
        try:
            links, error, status_code, details = _collect_article_links_for_source(
                base_url,
                per_source_limit=source.get("fetch_limit_per_run", 3),
                feed_url=source.get("feed_url"),
                extractor_type=source.get("extractor_type", "auto"),
            )
        except Exception as exc:
            links = []
            error = f"{type(exc).__name__}: {exc}"
            status_code = None
            details = {
                "normal_links_found": 0,
                "feed_links_found": 0,
                "method_used": "failed",
                "tried_feed_urls": [],
            }

        selected = None
        duplicate_count = 0
        old_count = 0
        too_close_count = 0
        promo_count = 0
        missing_date_count = 0
        recent_count = 0
        initial_freshness_hours = _smart_initial_freshness_hours()
        expanded_freshness_hours = _smart_expanded_freshness_hours(initial_freshness_hours)
        expanded_candidates = []
        for link in links:
            if isinstance(link, dict):
                url = link.get("url", "")
                link_title = link.get("title", "")
                feed_published_at = link.get("published_at", "")
                rss_summary = link.get("rss_summary", "")
            else:
                link_title, url = link
                feed_published_at = ""
                rss_summary = ""
            canonical = canonicalize_url(url)
            current_title_hash = title_hash(link_title)
            current_topic_signature = topic_signature(link_title)
            promotional, promo_reason = is_promotional_article({"title": link_title, "url": url, "rss_summary": rss_summary})
            if promotional and SKIP_ADS_AFFILIATE_SPONSORED:
                promo_count += 1
                _log(f"  Skipping promotional/affiliate article: {promo_reason}: {link_title[:80]}")
                continue
            if (
                canonical in known_urls
                or current_title_hash in known_title_hashes
                or current_title_hash in published_topic_hashes
                or current_topic_signature in published_topic_hashes
            ):
                duplicate_count += 1
                _log(f"  Skipping duplicate: {link_title[:80]}")
                continue
            published_at = ""
            published_at_source = ""
            freshness_source = ""
            age_hours = None
            if RECENT_NEWS_ONLY:
                published_at, published_at_source = _resolve_article_published_at(url, feed_published_at)
                if not published_at:
                    missing_date_count += 1
                    freshness_source = "fallback_no_date"
                    log_event(
                        "freshness_not_strict",
                        freshness_source=freshness_source,
                        article_url=url,
                        source_name=source_name,
                        decision="accepted_new_url_without_publish_date",
                    )
                    _log(f"  Article date missing; new URL accepted by freshness fallback: {link_title[:80]}")
                if published_at:
                    freshness_source = "date"
                    bucket, age_hours = _freshness_bucket_for_date(
                        published_at,
                        initial_hours=initial_freshness_hours,
                        expanded_hours=expanded_freshness_hours,
                    )
                    _log(
                        f"  Article date found ({published_at_source or 'unknown'}): "
                        f"{published_at}; age {age_hours:.2f}h"
                    )
                    if bucket == "too_old":
                        old_count += 1
                        log_event(
                            "article_skipped_old",
                            article_url=url,
                            age_hours=round(age_hours, 2),
                            max_age_hours=FRESHNESS_HARD_MAX_HOURS,
                        )
                        _log(
                            f"  Skipping article older than {FRESHNESS_HARD_MAX_HOURS / 24:.0f} days: "
                            f"{link_title[:80]}"
                        )
                        continue
                    log_event(
                        "freshness_not_strict",
                        freshness_source=freshness_source,
                        article_url=url,
                        source_name=source_name,
                        age_hours=round(age_hours, 2),
                        strict_window_hours=initial_freshness_hours,
                        hard_max_hours=FRESHNESS_HARD_MAX_HOURS,
                        decision="accepted_new_url_publish_date_for_sorting_only",
                    )
                    recent_count += 1
            selected = {
                "title": link_title,
                "url": url,
                "source_name": source_name,
                "source_url": base_url,
                "category_hint": source.get("category_hint", ""),
                "category_key": source.get("category_key", ""),
                "category_name": source.get("category_name", ""),
                "category_label": source.get("category_label", source.get("category_hint", "")),
                "source_priority": source.get("source_priority", ""),
                "official_source": bool(source.get("official_source", False)),
                "source_country": source.get("source_country", ""),
                "source_eligibility": source.get("source_eligibility", ""),
                "source_remote": bool(source.get("source_remote", False)),
                "source_visa_sponsorship": bool(source.get("source_visa_sponsorship", False)),
                "published_at_source": published_at_source,
                "source_published_at": published_at,
                "article_age_hours": round(age_hours, 2) if age_hours is not None else None,
                "rss_summary": rss_summary,
                "freshness_source": freshness_source or ("date" if published_at else ""),
                "freshness_window_hours": FRESHNESS_HARD_MAX_HOURS if published_at else "",
                "ats_provider": link.get("ats_provider", ""),
                "ats_reference": link.get("ats_reference", ""),
                "ats_description": link.get("ats_description", ""),
                "job_application_url": link.get("job_application_url", ""),
                "job_application_link_kind": link.get("job_application_link_kind", ""),
                "job_location": link.get("job_location", ""),
                "job_country": link.get("job_country", ""),
                "job_contract_type": link.get("job_contract_type", ""),
                "job_salary": link.get("job_salary", ""),
                "job_company": link.get("job_company", ""),
                "phenom_payload": link.get("phenom_payload", {}),
            }
            break

        if not selected and expanded_candidates:
            fallback_expanded_candidates.extend(expanded_candidates)

        elapsed = elapsed_ms(started)
        if elapsed > 20000:
            _log(f"  Heartbeat: source {source_name} took {elapsed / 1000:.1f}s")
        status_text = "failed" if error else "success"
        _log(
            f"  Source result: {status_text}; links={len(links)}; "
            f"duplicates={duplicate_count}; recent={recent_count}; old={old_count}; "
            f"too_close={too_close_count}; "
            f"promo={promo_count}; missing_date={missing_date_count}; elapsed={elapsed / 1000:.1f}s"
        )
        _record_source_result(
            base_url, source_name, error, len(links),
            empty_ok=bool(details.get("empty_ok")),
        )
        update_source_crawl(
            base_url,
            source_name=source_name,
            last_crawled_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            overlap_minutes=CRAWL_OVERLAP_MINUTES,
        )
        source_results.append(
            {
                "source_name": source_name,
                "base_url": base_url,
                "category_hint": source.get("category_hint", ""),
                "category_key": source.get("category_key", ""),
                "category_name": source.get("category_name", ""),
                "category_label": source.get("category_label", source.get("category_hint", "")),
                "source_priority": source.get("source_priority", ""),
                "official_source": bool(source.get("official_source", False)),
                "source_country": source.get("source_country", ""),
                "source_eligibility": source.get("source_eligibility", ""),
                "source_remote": bool(source.get("source_remote", False)),
                "source_visa_sponsorship": bool(source.get("source_visa_sponsorship", False)),
                "links_found": len(links),
                "duplicates_skipped": duplicate_count,
                "recent_links": recent_count,
                "old_links_skipped": old_count,
                "too_close_links_skipped": too_close_count,
                "promotional_links_skipped": promo_count,
                "missing_date_skipped": missing_date_count,
                "status": "failed" if error else "success",
                "listing_status_code": status_code,
                "error": error,
                "elapsed_ms": elapsed,
                **details,
            }
        )
        if selected:
            _log(f"Selected article: {selected['title'][:80]}")
            return {
                "checked_sources": len(source_results),
                "articles": [selected],
                "source_results": source_results,
                "first_valid": True,
            }

    if fallback_expanded_candidates:
        selected = fallback_expanded_candidates[0]
        log_event(
            "freshness_expanded_range",
            from_hours=_smart_initial_freshness_hours(),
            to_hours=_smart_expanded_freshness_hours(),
            source_name=selected.get("source_name", ""),
        )
        _log(
            "Freshness expanded range: "
            f"{_smart_initial_freshness_hours()}h -> {_smart_expanded_freshness_hours()}h"
        )
        _log(f"Selected expanded-range article: {selected['title'][:80]}")
        return {
            "checked_sources": len(source_results),
            "articles": [selected],
            "source_results": source_results,
            "first_valid": True,
        }

    reason = (
        f"no new publishable article under {FRESHNESS_HARD_MAX_HOURS // 24} days"
        if RECENT_NEWS_ONLY
        else "no valid non-duplicate article"
    )
    _log(f"Finished source scan: {reason}")
    return {
        "checked_sources": len(source_results),
        "articles": [],
        "source_results": source_results,
        "first_valid": False,
        "reason": reason,
    }


def discover_fresh_article_links(
    sources,
    existing_articles=None,
    published_urls=None,
    published_topic_hashes=None,
    process_all_sources=False,
):
    """
    Scan all enabled sources and collect every fresh, non-duplicate article
    within the recent-news window.
    """
    if process_all_sources:
        enabled_sources = [source for source in sources if source.get("enabled", True)]
        cooldown_results = []
    else:
        enabled_sources, cooldown_results = _filter_healthy_sources(
            _order_sources_for_fast_run([source for source in sources if source.get("enabled", True)])
        )
    if MAX_SOURCES_PER_RUN > 0 and not process_all_sources:
        enabled_sources = enabled_sources[:MAX_SOURCES_PER_RUN]
    existing_articles = existing_articles or []
    published_urls = published_urls or set()
    published_topic_hashes = published_topic_hashes or set()
    known_urls = {
        item.get("canonical_url") or canonicalize_url(item.get("url"))
        for item in existing_articles
        if item.get("url") and not item.get("archived")
    }
    known_urls.update(canonicalize_url(url) for url in published_urls if url)
    known_title_hashes = {
        item.get("title_hash") or title_hash(item.get("title", ""))
        for item in existing_articles
        if item.get("title") and not item.get("archived")
    }

    discovered = []
    source_results = list(cooldown_results)

    for index, source in enumerate(enabled_sources, 1):
        source_name = source.get("name", source.get("base_url", "Unknown source"))
        base_url = source.get("base_url", "").strip()
        if not base_url:
            continue
        source_key = base_url
        crawl_now = datetime.now(timezone.utc)
        crawl_window_start = _source_crawl_window_start(source_key, now=crawl_now)

        started = time.perf_counter()
        _log(f"\nChecking source {index}/{len(enabled_sources)}: {source_name}")
        try:
            links, error, status_code, details = _collect_article_links_for_source(
                base_url,
                per_source_limit=source.get("fetch_limit_per_run", 3),
                feed_url=source.get("feed_url"),
                extractor_type=source.get("extractor_type", "auto"),
            )
        except Exception as exc:
            links = []
            error = f"{type(exc).__name__}: {exc}"
            status_code = None
            details = {
                "normal_links_found": 0,
                "feed_links_found": 0,
                "method_used": "failed",
                "tried_feed_urls": [],
            }

        duplicate_count = 0
        old_count = 0
        too_close_count = 0
        promo_count = 0
        missing_date_count = 0
        recent_count = 0
        selected_links = []
        initial_freshness_hours = _smart_initial_freshness_hours()
        expanded_freshness_hours = _smart_expanded_freshness_hours(initial_freshness_hours)
        expanded_candidates = []

        for link in links:
            if isinstance(link, dict):
                url = link.get("url", "")
                link_title = link.get("title", "")
                feed_published_at = link.get("published_at", "")
                rss_summary = link.get("rss_summary", "")
            else:
                link_title, url = link
                feed_published_at = ""
                rss_summary = ""

            canonical = canonicalize_url(url)
            current_title_hash = title_hash(link_title)
            current_topic_signature = topic_signature(link_title)
            promotional, promo_reason = is_promotional_article({"title": link_title, "url": url, "rss_summary": rss_summary})
            if promotional and SKIP_ADS_AFFILIATE_SPONSORED:
                promo_count += 1
                _log(f"  Skipping promotional/affiliate article: {promo_reason}: {link_title[:80]}")
                continue
            if (
                canonical in known_urls
                or current_title_hash in known_title_hashes
                or current_title_hash in published_topic_hashes
                or current_topic_signature in published_topic_hashes
            ):
                duplicate_count += 1
                _log(f"  Skipping duplicate: {link_title[:80]}")
                continue

            published_at = ""
            published_at_source = ""
            freshness_source = ""
            age_hours = None
            if RECENT_NEWS_ONLY:
                published_at, published_at_source = _resolve_article_published_at(url, feed_published_at)
                if not published_at:
                    missing_date_count += 1
                    freshness_source = "fallback_no_date"
                    log_event(
                        "freshness_not_strict",
                        freshness_source=freshness_source,
                        article_url=url,
                        source_name=source_name,
                        decision="accepted_new_url_without_publish_date",
                    )
                    _log(f"  Article date missing; new URL accepted by freshness fallback: {link_title[:80]}")
                if published_at:
                    freshness_source = "date"
                    bucket, age_hours = _freshness_bucket_for_date(
                        published_at,
                        initial_hours=initial_freshness_hours,
                        expanded_hours=expanded_freshness_hours,
                    )
                    _log(
                        f"  Article date found ({published_at_source or 'unknown'}): "
                        f"{published_at}; age {age_hours:.2f}h"
                    )
                    if bucket == "too_old":
                        old_count += 1
                        log_event(
                            "article_skipped_old",
                            article_url=url,
                            age_hours=round(age_hours, 2),
                            max_age_hours=FRESHNESS_HARD_MAX_HOURS,
                        )
                        _log(
                            f"  Skipping article older than {FRESHNESS_HARD_MAX_HOURS / 24:.0f} days: "
                            f"{link_title[:80]}"
                        )
                        continue
                    log_event(
                        "freshness_not_strict",
                        freshness_source=freshness_source,
                        article_url=url,
                        source_name=source_name,
                        age_hours=round(age_hours, 2),
                        strict_window_hours=initial_freshness_hours,
                        hard_max_hours=FRESHNESS_HARD_MAX_HOURS,
                        decision="accepted_new_url_publish_date_for_sorting_only",
                    )
                    recent_count += 1

            selected = {
                "title": link_title,
                "url": url,
                "source_name": source_name,
                "source_url": base_url,
                "category_hint": source.get("category_hint", ""),
                "category_key": source.get("category_key", ""),
                "category_name": source.get("category_name", ""),
                "category_label": source.get("category_label", source.get("category_hint", "")),
                "source_priority": source.get("source_priority", ""),
                "official_source": bool(source.get("official_source", False)),
                "source_country": source.get("source_country", ""),
                "source_eligibility": source.get("source_eligibility", ""),
                "source_remote": bool(source.get("source_remote", False)),
                "source_visa_sponsorship": bool(source.get("source_visa_sponsorship", False)),
                "published_at_source": published_at_source,
                "source_published_at": published_at,
                "article_age_hours": round(age_hours, 2) if age_hours is not None else None,
                "rss_summary": rss_summary,
                "freshness_source": freshness_source or ("date" if published_at else ""),
                "freshness_window_hours": FRESHNESS_HARD_MAX_HOURS if published_at else "",
            }
            selected_links.append(selected)
            known_urls.add(canonical)
            if current_title_hash:
                known_title_hashes.add(current_title_hash)
            if current_topic_signature:
                published_topic_hashes.add(current_topic_signature)

        if not selected_links and not discovered and expanded_candidates:
            log_event(
                "freshness_expanded_range",
                from_hours=initial_freshness_hours,
                to_hours=expanded_freshness_hours,
                source_name=source_name,
            )
            _log(f"  Freshness expanded range: {initial_freshness_hours}h -> {expanded_freshness_hours}h")
            selected_links.extend(expanded_candidates)
            for selected in expanded_candidates:
                known_urls.add(canonicalize_url(selected.get("url", "")))
                selected_title_hash = title_hash(selected.get("title", ""))
                selected_topic_signature = topic_signature(selected.get("title", ""))
                if selected_title_hash:
                    known_title_hashes.add(selected_title_hash)
                if selected_topic_signature:
                    published_topic_hashes.add(selected_topic_signature)

        elapsed = elapsed_ms(started)
        status_text = "failed" if error else "success"
        _log(
            f"  Source result: {status_text}; links={len(links)}; "
            f"duplicates={duplicate_count}; recent={recent_count}; old={old_count}; "
            f"too_close={too_close_count}; "
            f"promo={promo_count}; missing_date={missing_date_count}; queued={len(selected_links)}; "
            f"elapsed={elapsed / 1000:.1f}s"
        )
        _record_source_result(base_url, source_name, error, len(links))
        source_results.append(
            {
                "source_name": source_name,
                "base_url": base_url,
                "category_hint": source.get("category_hint", ""),
                "category_key": source.get("category_key", ""),
                "category_name": source.get("category_name", ""),
                "category_label": source.get("category_label", source.get("category_hint", "")),
                "source_priority": source.get("source_priority", ""),
                "official_source": bool(source.get("official_source", False)),
                "source_country": source.get("source_country", ""),
                "source_eligibility": source.get("source_eligibility", ""),
                "source_remote": bool(source.get("source_remote", False)),
                "source_visa_sponsorship": bool(source.get("source_visa_sponsorship", False)),
                "crawl_window_start": crawl_window_start.isoformat().replace("+00:00", "Z"),
                "links_found": len(links),
                "duplicates_skipped": duplicate_count,
                "recent_links": recent_count,
                "old_links_skipped": old_count,
                "too_close_links_skipped": too_close_count,
                "promotional_links_skipped": promo_count,
                "missing_date_skipped": missing_date_count,
                "queued_links": len(selected_links),
                "status": "failed" if error else "success",
                "listing_status_code": status_code,
                "error": error,
                "elapsed_ms": elapsed,
                **details,
            }
        )
        discovered.extend(selected_links)
        update_source_crawl(
            source_key,
            source_name=source_name,
            last_crawled_at=crawl_now.isoformat().replace("+00:00", "Z"),
            overlap_minutes=CRAWL_OVERLAP_MINUTES,
        )

    reason = (
        f"no new publishable article under {FRESHNESS_HARD_MAX_HOURS // 24} days"
        if RECENT_NEWS_ONLY and not discovered
        else ""
    )
    if reason:
        _log(f"Finished source scan: {reason}")

    discovered.sort(
        key=lambda article: _parse_datetime_to_utc(article.get("source_published_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    return {
        "checked_sources": len(source_results),
        "articles": discovered,
        "source_results": source_results,
        "reason": reason,
    }


def _round_robin_link_groups(link_groups, limit=None):
    merged = []
    seen = set()
    active_groups = [list(group) for group in link_groups if group]

    while active_groups and (not limit or len(merged) < limit):
        next_round = []
        progress = False

        for group in active_groups:
            while group:
                candidate = group.pop(0)
                if candidate["url"] in seen:
                    continue
                seen.add(candidate["url"])
                merged.append(candidate)
                progress = True
                break

            if group:
                next_round.append(group)

            if limit and len(merged) >= limit:
                break

        if not progress:
            break

        active_groups = next_round

    return merged


def _get_article_links_with_scrapling(page, base_url):
    articles = []
    seen = set()

    for article_tag in page.css("article"):
        link_tag = (
            article_tag.css("h2 a").first
            or article_tag.css("h3 a").first
            or article_tag.css("a[href]").first
        )
        if not link_tag:
            continue

        title = link_tag.get_all_text(strip=True)
        url = urljoin(base_url, link_tag.attrib.get("href", ""))
        _append_unique_link(articles, seen, title, url)

    if not articles:
        for selector in ("h2 a", "h3 a", "a[href]"):
            for link_tag in page.css(selector):
                title = link_tag.get_all_text(strip=True)
                url = urljoin(base_url, link_tag.attrib.get("href", ""))
                _append_unique_link(articles, seen, title, url)

    return articles


def _get_article_links_with_bs4(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    articles = []
    seen = set()

    for article_tag in soup.find_all("article"):
        link_tag = article_tag.select_one("h2 a, h3 a") or article_tag.find("a", href=True)
        if not link_tag:
            continue

        title = link_tag.get_text(strip=True)
        url = urljoin(base_url, link_tag.get("href", ""))
        _append_unique_link(articles, seen, title, url)

    if not articles:
        for selector in ("h2 a", "h3 a", "a[href]"):
            for link_tag in soup.select(selector):
                title = link_tag.get_text(strip=True)
                url = urljoin(base_url, link_tag.get("href", ""))
                _append_unique_link(articles, seen, title, url)

    return articles


def get_article_links(document, base_url, strict_source_path=True):
    """
    Extract all article links from the listing page.
    """
    if _is_scrapling_document(document):
        raw_links = _get_article_links_with_scrapling(document, base_url)
    else:
        raw_links = _get_article_links_with_bs4(document, base_url)

    supplemental_links = _extract_article_links_with_soup(
        document,
        base_url,
        strict_source_path=strict_source_path,
    )
    return _filter_article_links(
        raw_links + supplemental_links,
        base_url,
        strict_source_path=strict_source_path,
    )


def _join_paragraphs(paragraphs):
    text_parts = []
    for paragraph in paragraphs:
        if _is_scrapling_document(paragraph):
            text = paragraph.get_all_text(strip=True)
        else:
            text = paragraph.get_text(strip=True)
        if text:
            text_parts.append(text)
    return "\n\n".join(text_parts)


def _extract_article_body_with_scrapling(page):
    best_text = ""
    candidate_selectors = (
        "div.articleBody",
        "div.article_section",
        "[itemprop='articleBody']",
        "div.entry-content",
        "div.post-content",
        "div.article-content",
        "section.article-body",
        "div.c-article-content",
        "div.post__content",
        "div.content-body",
        "div.post-body",
        "article",
        "main",
        "div.article-body",
        "div.content",
    )

    for selector in candidate_selectors:
        content_tag = page.css(selector).first
        if not content_tag:
            continue

        text = _join_paragraphs(content_tag.css("p"))
        if len(text) >= 600:
            return text
        if len(text) > len(best_text):
            best_text = text

    if len(best_text) > 100:
        return best_text

    text = _join_paragraphs(page.css("p"))
    if len(text) > 100:
        return text

    return ""


def _extract_article_body_with_bs4(html):
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    best_text = ""
    candidate_tags = (
        soup.find("div", class_="articleBody"),
        soup.find("div", class_="article_section"),
        soup.find(attrs={"itemprop": "articleBody"}),
        soup.find("div", class_="entry-content"),
        soup.find("div", class_="post-content"),
        soup.find("div", class_="article-content"),
        soup.find("section", class_="article-body"),
        soup.find("div", class_="c-article-content"),
        soup.find("div", class_="post__content"),
        soup.find("div", class_="content-body"),
        soup.find("div", class_="post-body"),
        soup.find("article"),
        soup.find("main"),
        soup.find("div", class_="article-body"),
        soup.find("div", class_="content"),
    )

    for content_tag in candidate_tags:
        if not content_tag:
            continue

        text = _join_paragraphs(content_tag.find_all("p"))
        if len(text) >= 600:
            return text
        if len(text) > len(best_text):
            best_text = text

    if len(best_text) > 100:
        return best_text

    text = _join_paragraphs(soup.find_all("p"))
    if len(text) > 100:
        return text

    return ""


def extract_article_body(document):
    """
    Extract the main article text from a full article page.
    """
    if _is_scrapling_document(document):
        return _extract_article_body_with_scrapling(document)
    return _extract_article_body_with_bs4(document)


def _document_to_html(document):
    if isinstance(document, str):
        return document

    for attr_name in ("html", "html_content", "content", "raw_html"):
        value = getattr(document, attr_name, None)
        if callable(value):
            try:
                value = value()
            except TypeError:
                continue
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", errors="ignore")
            except Exception:
                value = None
        if isinstance(value, str) and "<" in value:
            return value

    return None


def _safe_positive_int(value):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_text(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def _get_soup_from_document(document, article_url):
    html = _document_to_html(document)
    if not html:
        html = _make_request_with_requests(article_url)
    if not html:
        return None
    return BeautifulSoup(html, "html.parser")


def _find_article_container(soup):
    return soup.select_one("article.c-post") or soup.find("article") or soup.find("main") or soup


def _looks_like_article_image(src, classes, alt_text):
    combined = " ".join(classes + [src.lower(), alt_text.lower()])
    if any(hint in combined for hint in BLOCKED_IMAGE_HINTS):
        return False
    if any(class_name in classes for class_name in ("c-logo__img", "avatar", "author-image")):
        return False
    return True


def _extract_feature_image(document, article_url, article_title):
    soup = _get_soup_from_document(document, article_url)
    if not soup:
        return None

    og_width = _safe_positive_int(
        (soup.select_one("meta[property='og:image:width']") or {}).get("content")
        if soup.select_one("meta[property='og:image:width']")
        else None
    )
    og_height = _safe_positive_int(
        (soup.select_one("meta[property='og:image:height']") or {}).get("content")
        if soup.select_one("meta[property='og:image:height']")
        else None
    )

    selectors = (
        "article.c-post img.c-feature-image",
        "article.c-post figure.c-feature-image-figure img",
        "main img.c-feature-image",
        "article img",
    )

    for selector in selectors:
        for img in soup.select(selector):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if not src:
                continue

            classes = [class_name.lower() for class_name in (img.get("class") or [])]
            alt_text = _normalize_text(img.get("alt") or article_title)
            full_src = urljoin(article_url, src)

            if not _looks_like_article_image(full_src, classes, alt_text):
                continue

            return {
                "url": full_src,
                "alt": alt_text or article_title,
                "width": _safe_positive_int(img.get("width")) or og_width,
                "height": _safe_positive_int(img.get("height")) or og_height,
            }

    og_image = soup.select_one("meta[property='og:image']")
    if og_image and og_image.get("content"):
        return {
            "url": urljoin(article_url, og_image.get("content")),
            "alt": article_title,
            "width": og_width,
            "height": og_height,
        }

    return None


def _classify_authoritative_link(link_text, href):
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    text_lower = _normalize_text(link_text).lower()

    if not host:
        return 0, ""

    if any(blocked_host in host for blocked_host in BLOCKED_SOURCE_HOST_HINTS):
        return 0, ""

    for trusted_host, kind in HIGH_TRUST_HOSTS.items():
        if host == trusted_host or host.endswith(f".{trusted_host}"):
            return 120, kind

    if "readthedocs.io" in host or any(keyword in text_lower for keyword in ("documentation", "docs", "doc ")):
        return 95, "docs"

    if any(keyword in text_lower for keyword in ("github", "repository", "repo")):
        return 95, "github"

    if any(keyword in text_lower for keyword in ("advisory", "bulletin", "report", "whitepaper", "release notes")):
        return 90, "report"

    if any(keyword in text_lower for keyword in ("official website", "official site", "project website", "website")):
        return 85, "site"

    host_without_www = host[4:] if host.startswith("www.") else host
    if host_without_www and host_without_www in text_lower:
        return 75, "site"

    if host.endswith((".org", ".io", ".dev", ".com", ".net")) and text_lower:
        return 60, "site"

    return 0, ""


def _extract_trusted_sources(document, article_url):
    soup = _get_soup_from_document(document, article_url)
    if not soup:
        return []

    container = _find_article_container(soup)
    trusted_sources = []
    seen_urls = set()

    for link_tag in container.select("a[href]"):
        href = urljoin(article_url, link_tag.get("href", "").strip())
        if not href.startswith("http") or href in seen_urls:
            continue

        link_text = _normalize_text(link_tag.get_text(" ", strip=True))
        score, kind = _classify_authoritative_link(link_text, href)
        if score < 60:
            continue

        seen_urls.add(href)
        trusted_sources.append(
            {
                "title": link_text or urlparse(href).netloc,
                "url": href,
                "kind": kind,
                "score": score,
            }
        )

    trusted_sources.sort(key=lambda item: (-item["score"], item["title"].lower(), item["url"]))

    return [
        {
            "title": item["title"],
            "url": item["url"],
            "kind": item["kind"],
        }
        for item in trusted_sources[:4]
    ]


def _extract_original_article_links(document, article_url):
    soup = _get_soup_from_document(document, article_url)
    if not soup:
        return []

    container = _find_article_container(soup)
    links = []
    seen_urls = set()

    for link_tag in container.select("a[href]"):
        raw_href = (link_tag.get("href") or "").strip()
        if not raw_href or raw_href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        href = urljoin(article_url, raw_href)
        if not href.startswith(("http://", "https://")) or href in seen_urls:
            continue

        seen_urls.add(href)
        links.append(
            {
                "text": _normalize_text(link_tag.get_text(" ", strip=True)),
                "url": href,
            }
        )

    return links


def get_latest_articles(limit=None):
    """
    Fetch and return the latest articles from configured source websites.
    """
    print("\n" + "=" * 60)
    print("STEP 1: Scraping articles from source websites")
    print("=" * 60)

    if SCRAPLING_AVAILABLE:
        print("  Using Scrapling for adaptive fetching and parsing.")

    print(f"  Configured sources: {len(SOURCE_URLS)}")

    if limit and limit > 0:
        per_source_limit = max(3, ((limit + len(SOURCE_URLS) - 1) // len(SOURCE_URLS)) * 2)
    else:
        per_source_limit = 6

    source_link_groups = []
    for source_url in SOURCE_URLS:
        source_links, _error, _status_code, _details = _collect_article_links_for_source(
            source_url,
            per_source_limit=per_source_limit,
        )
        if source_links:
            source_link_groups.append(source_links)

    article_links = _round_robin_link_groups(source_link_groups, limit=limit)
    if not article_links:
        print("No articles found across the configured sources. The site structures may have changed.")
        return []

    print(f"Found {len(article_links)} article(s) across all configured sources.")

    articles = []

    for i, article_link in enumerate(article_links, 1):
        title = article_link["title"]
        url = article_link["url"]
        source_url = article_link["source_url"]

        print(f"\n--- Article {i}/{len(article_links)} ---")
        print(f"  Title: {title}")
        print(f"  Source: {source_url}")

        article_document = make_request(url)
        if not article_document:
            print("  Skipping article (could not fetch page)")
            continue

        body = extract_article_body(article_document)
        if not body:
            print("  Skipping article (could not extract body text)")
            continue

        image = _extract_feature_image(article_document, url, title)
        original_links = _extract_original_article_links(article_document, url)
        trusted_sources = _extract_trusted_sources(article_document, url)

        print(f"  Extracted {len(body)} characters of content")
        print(
            f"  Feature image: {'yes' if image else 'no'} | "
            f"article links: {len(original_links)} | trusted links: {len(trusted_sources)}"
        )

        articles.append(
            {
                "title": title,
                "url": url,
                "source_url": source_url,
                "body": body,
                "image": image,
                "original_links": original_links,
                "trusted_sources": trusted_sources,
            }
        )

        if i < len(article_links) and SCRAPE_DELAY_SECONDS > 0:
            time.sleep(SCRAPE_DELAY_SECONDS)

    print(f"\nSuccessfully scraped {len(articles)} article(s) in total!")
    return articles
