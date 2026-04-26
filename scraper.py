# ============================================================
# scraper.py - Web Scraping Module
# ============================================================

import re
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from scrapling.fetchers import Fetcher
except ImportError:
    Fetcher = None

from config import HEADERS, MAX_RETRIES, RETRY_DELAY, SCRAPE_DELAY_SECONDS, SOURCE_URLS

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

SCRAPLING_AVAILABLE = Fetcher is not None

if SCRAPLING_AVAILABLE:
    Fetcher.adaptive = True


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
    "telegram.me",
    "t.me",
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


def _is_scrapling_document(document):
    return callable(getattr(document, "css", None)) and getattr(document, "url", None)


def _make_request_with_scrapling(url):
    try:
        print(f"  Fetching with Scrapling: {url}")
        page = Fetcher.get(
            url,
            timeout=30,
            retries=MAX_RETRIES,
            retry_delay=RETRY_DELAY,
            stealthy_headers=True,
            impersonate="chrome",
            headers=HEADERS,
        )
        print(f"  Successfully fetched! (Status: {getattr(page, 'status', 'unknown')})")
        return page
    except Exception as e:
        print(f"  Scrapling request failed: {e}")
        return None


def _make_request_with_requests(url, retries=None):
    if retries is None:
        retries = 0

    try:
        print(f"  Fetching: {url}")
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        print(f"  Successfully fetched! (Status: {response.status_code})")
        return response.text
    except requests.exceptions.Timeout:
        print("  Request timed out after 30 seconds.")
    except requests.exceptions.ConnectionError:
        print("  Could not connect to the server.")
    except requests.exceptions.HTTPError as e:
        print(f"  HTTP Error: {e.response.status_code} - {e.response.reason}")
    except requests.exceptions.RequestException as e:
        print(f"  Request failed: {e}")

    if retries < MAX_RETRIES:
        wait_time = RETRY_DELAY * (retries + 1)
        print(f"  Waiting {wait_time} seconds before retry... (Attempt {retries + 1}/{MAX_RETRIES})")
        time.sleep(wait_time)
        return _make_request_with_requests(url, retries + 1)

    print(f"  All {MAX_RETRIES} retries exhausted. Giving up on: {url}")
    return None


def make_request(url, retries=None):
    """
    Make an HTTP GET request using Scrapling when available,
    otherwise fall back to requests.
    """
    if SCRAPLING_AVAILABLE:
        return _make_request_with_scrapling(url)
    return _make_request_with_requests(url, retries)


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

    for title, url in article_links:
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
        filtered.append((normalized_title, full_url))

    return filtered


def _extract_article_links_with_soup(document, base_url):
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

            if not _is_likely_article_url(title, full_url, base_url):
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

        for child in list(node):
            child_name = _xml_local_name(child.tag)
            if child_name == "title" and not title:
                title = _normalize_text("".join(child.itertext()))
            elif child_name == "link" and not link:
                href = child.attrib.get("href") or _normalize_text("".join(child.itertext()))
                if href:
                    link = urljoin(source_url, href)

        if title and link and link not in seen:
            seen.add(link)
            article_links.append((title, link))

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
        response = requests.get(feed_url, headers=HEADERS, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as error:
        print(f"  Feed failed: {feed_url} ({error})")
        return []
    return _parse_feed_article_links(response.text, source_url, feed_url=feed_url)


def _collect_article_links_for_source(
    source_url,
    per_source_limit=None,
    feed_url=None,
    extractor_type="auto",
):
    print(f"\n--- Discovering links from source: {source_url} ---")

    source_document = make_request(source_url)
    if not source_document:
        print("  Could not fetch source listing page.")
        feed_links = []
        tried_feed_urls = []
        if feed_url:
            tried_feed_urls.append(feed_url)
            feed_links = _collect_links_from_feed(feed_url, source_url)
            print(f"  Feed yielded {len(feed_links)} candidate link(s): {feed_url}")

        if per_source_limit:
            feed_links = feed_links[:per_source_limit]

        return [
            {
                "source_url": source_url,
                "title": title,
                "url": url,
            }
            for title, url in feed_links
        ], "fetch failed", None, {
            "normal_links_found": 0,
            "feed_links_found": len(feed_links),
            "method_used": "feed" if feed_links else "failed",
            "tried_feed_urls": tried_feed_urls,
        }

    status_code = getattr(source_document, "status", None)
    error = ""
    try:
        numeric_status = int(status_code)
    except (TypeError, ValueError):
        numeric_status = None

    if numeric_status and numeric_status >= 400:
        error = f"listing returned HTTP {numeric_status}"

    html_links = get_article_links(source_document, source_url)
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

    should_try_feed = bool(feed_url) or bool(error) or len(html_links) < (per_source_limit or 3)

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
        strict_source_path=False if feed_links else True,
    )
    if per_source_limit:
        combined_links = combined_links[:per_source_limit]

    print(f"  Collected {len(combined_links)} article link(s) from this source.")
    return [
        {
            "source_url": source_url,
            "title": title,
            "url": url,
        }
        for title, url in combined_links
    ], error, numeric_status, {
        "normal_links_found": len(html_links),
        "feed_links_found": len(feed_links),
        "method_used": method_used,
        "tried_feed_urls": tried_feed_urls,
    }


def discover_latest_article_links(sources):
    """
    Phase 1 safe discovery: collect latest article links only.
    This does not fetch article bodies, call AI, or publish anything.
    """
    discovered = []
    checked_sources = 0
    source_results = []

    for source in sources:
        if not source.get("enabled", True):
            continue

        source_name = source.get("name", source.get("base_url", "Unknown source"))
        base_url = source.get("base_url", "").strip()
        if not base_url:
            continue

        checked_sources += 1
        fetch_limit = source.get("fetch_limit_per_run", 3)
        try:
            fetch_limit = int(fetch_limit)
        except (TypeError, ValueError):
            fetch_limit = 3
        fetch_limit = max(1, min(fetch_limit, 3))

        print(f"\n[{checked_sources}] Checking {source_name}")
        category_hint = source.get("category_hint", "")
        try:
            links, error, status_code, details = _collect_article_links_for_source(
                base_url,
                per_source_limit=fetch_limit,
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
            print(f"  Source failed without stopping the fetch run: {error}")

        for link in links[:fetch_limit]:
            discovered.append(
                {
                    "title": link.get("title", ""),
                    "url": link.get("url", ""),
                    "source_name": source_name,
                    "source_url": base_url,
                    "category_hint": category_hint,
                }
            )

        source_results.append(
            {
                "source_name": source_name,
                "base_url": base_url,
                "category_hint": category_hint,
                "fetch_limit_per_run": fetch_limit,
                "links_found": len(links),
                "status": "failed" if error else "success",
                "listing_status_code": status_code,
                "error": error,
                **details,
            }
        )

        if SCRAPE_DELAY_SECONDS > 0:
            time.sleep(SCRAPE_DELAY_SECONDS)

    return {
        "checked_sources": checked_sources,
        "articles": discovered,
        "source_results": source_results,
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


def get_article_links(document, base_url):
    """
    Extract all article links from the listing page.
    """
    if _is_scrapling_document(document):
        raw_links = _get_article_links_with_scrapling(document, base_url)
    else:
        raw_links = _get_article_links_with_bs4(document, base_url)

    supplemental_links = _extract_article_links_with_soup(document, base_url)
    return _filter_article_links(raw_links + supplemental_links, base_url)


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
