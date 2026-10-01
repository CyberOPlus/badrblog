# ============================================================
# article_enricher.py - Phase 3 Ready Article Enrichment
# ============================================================

import asyncio
import html as html_lib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from article_queue import is_candidate_in_recent_failure, load_article_queue, save_article_queue
from config import (
    ARTICLE_TIMEOUT_SECONDS,
    FAST_NEWS_MODE,
    JOBS_MODE,
    HEADERS,
    MIN_EXTRACTED_CHARS,
    PUBLISH_WEAK_ARTICLES,
    SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES,
    SOURCE_RETRY_DELAY_SECONDS,
    MAX_SOURCE_RETRIES,
    JOBS_ENRICH_MAX_TARGETS_PER_CYCLE,
)
from production_logging import elapsed_ms, log_event
from image_extractor import download_image_with_retry, extract_main_image, extract_extra_images
from job_extractor import _deadline_from_text, extract_job_fields
from job_core import invalidate_identity_evidence, job_deadline_time, job_focus_priority
from company_logo_resolver import resolve_company_logo

try:
    import aiohttp
except ImportError:
    aiohttp = None

CONTENT_FETCH_STATUSES = {"success", "failed"}
REQUEST_TIMEOUT_SECONDS = min(ARTICLE_TIMEOUT_SECONDS, 10)
PREVIEW_MIN_CHARS = 3500
PREVIEW_MAX_CHARS = 9000
STRONG_ARTICLE_MIN_CHARS = 3500
WEAK_ARTICLE_MIN_CHARS = 1200
MAX_ARTICLE_IMAGES = 5
MAX_TRUSTED_REFERENCES = 5
ASYNC_FETCH_CONCURRENCY = 8
FETCH_RETRIES = MAX_SOURCE_RETRIES
FAST_ENRICH_MIN_CHARS = max(80, MIN_EXTRACTED_CHARS)
MIN_EXTRACTED_WORDS = 120

TRUSTED_REFERENCE_HOSTS = (
    "microsoft.com",
    "google.com",
    "cloud.google.com",
    "mandiant.com",
    "openai.com",
    "cisa.gov",
    "paloaltonetworks.com",
    "unit42.paloaltonetworks.com",
    "talosintelligence.com",
    "cisco.com",
    "nvd.nist.gov",
    "cve.org",
    "mitre.org",
    "github.com",
    "projectzero.google",
)

BLOCKED_IMAGE_HINTS = (
    "logo",
    "icon",
    "avatar",
    "author",
    "profile",
    "sprite",
    "tracking",
    "pixel",
    "advert",
    "ads",
    "banner",
    "button",
    "badge",
)

NOISE_ATTR_HINTS = (
    "ads",
    "advert",
    "advertisement",
    "affiliate",
    "also-read",
    "author-bio",
    "bio",
    "comments",
    "cookie",
    "deal",
    "deals",
    "newsletter",
    "outbrain",
    "promo",
    "promoted",
    "read-more",
    "recommended",
    "related",
    "share",
    "sidebar",
    "social",
    "sponsor",
    "sponsored",
    "subscribe",
    "taboola",
)

NOISE_TEXT_PATTERNS = (
    "advertisement",
    "also read",
    "comments",
    "continue reading",
    "deal of the day",
    "follow us",
    "more from",
    "newsletter",
    "read more",
    "related posts",
    "share this",
    "sign up",
    "sponsored",
    "subscribe",
    "you may also like",
)

AFFILIATE_HOST_HINTS = (
    "amzn.to",
    "amazon.",
    "awin1.com",
    "click.linksynergy.com",
    "go.redirectingat.com",
    "impact.com",
    "partnerize.com",
    "rstyle.me",
    "shareasale.com",
    "shop-links.co",
    "skimresources.com",
    "tidd.ly",
)

AFFILIATE_PATH_HINTS = (
    "affiliate",
    "affid",
    "deal",
    "deals",
    "partner",
    "promo",
    "redirect",
    "referral",
    "sponsored",
)

TRACKING_QUERY_KEYS = (
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "utm_",
)

REMOVE_SELECTORS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "svg",
    "form",
    "button",
    "nav",
    "header",
    "footer",
    "aside",
    "menu",
    "dialog",
    "[role='navigation']",
    "[role='banner']",
    "[role='contentinfo']",
    ".ad",
    ".ads",
    ".advertisement",
    ".advert",
    ".sponsor",
    ".sponsored",
    ".cookie",
    ".cookies",
    ".cookie-banner",
    ".newsletter",
    ".comments",
    "#comments",
    ".share",
    ".social",
    ".related",
    ".recommended",
    ".sidebar",
    ".author-bio",
    ".bio",
    ".byline",
    ".read-more",
    ".also-read",
    ".more-stories",
    ".outbrain",
    ".taboola",
    ".affiliate",
    ".deal",
    ".deals",
    ".promo",
    ".promoted",
    "[class*='newsletter']",
    "[class*='related']",
    "[class*='read-more']",
    "[class*='also-read']",
    "[class*='social']",
    "[class*='share']",
    "[class*='sponsor']",
    "[class*='affiliate']",
    "[class*='deal']",
    "[id*='newsletter']",
    "[id*='related']",
    "[id*='comments']",
    "[id*='social']",
    "[id*='share']",
    "[id*='sponsor']",
    "[id*='affiliate']",
    "[id*='deal']",
)

ARTICLE_SELECTORS = (
    "article",
    "main article",
    "[itemprop='articleBody']",
    ".article-content",
    ".article-body",
    ".entry-content",
    ".post-content",
    ".c-article-content",
    ".content-body",
    ".post-body",
    "main",
)

BODY_CONTAINER_SELECTORS = (
    "[itemprop='articleBody']",
    ".article-content",
    ".article-body",
    ".entry-content",
    ".post-content",
    ".c-article-content",
    ".content-body",
    ".post-body",
    ".story-body",
    ".storyBody",
    ".article__body",
    ".articleBody",
    ".body-content",
    "[class*='article-body']",
    "[class*='articleBody']",
    "[class*='entry-content']",
    "[class*='post-content']",
    "[class*='story-body']",
)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _retry_after_iso(minutes=None):
    retry_at = datetime.now(timezone.utc) + timedelta(
        minutes=max(1, int(minutes or SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES or 1))
    )
    return retry_at.isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_text(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def _word_count(value):
    return len(re.findall(r"[A-Za-z0-9\u0600-\u06FF][A-Za-z0-9\u0600-\u06FF'’._-]*", value or ""))


def _html_to_text(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "<" in raw and ">" in raw:
        soup = BeautifulSoup(raw, "html.parser")
        _clean_soup(soup)
        return _normalize_text(soup.get_text(" ", strip=True))
    return _normalize_text(raw)


def _meta_content(soup, *selectors):
    for selector in selectors:
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            return _normalize_text(tag.get("content"))
    return ""


def _best_src_from_srcset(value):
    best_url = ""
    best_width = -1
    for item in str(value or "").split(","):
        parts = item.strip().split()
        if not parts:
            continue
        url = parts[0].strip()
        width = 0
        if len(parts) > 1 and parts[1].endswith("w"):
            width = _safe_int(parts[1][:-1]) or 0
        if width >= best_width:
            best_url = url
            best_width = width
    return best_url


def _image_src(img):
    for attr in ("src", "data-src", "data-lazy-src", "data-original", "data-hi-res-src"):
        if img.get(attr):
            return img.get(attr)
    return _best_src_from_srcset(img.get("srcset") or img.get("data-srcset"))


def _extract_title(soup):
    title = _meta_content(
        soup,
        "meta[property='og:title']",
        "meta[name='twitter:title']",
    )
    if title:
        return title

    h1 = soup.find("h1")
    if h1:
        return _normalize_text(h1.get_text(" ", strip=True))

    if soup.title:
        return _normalize_text(soup.title.get_text(" ", strip=True))

    return ""


def _extract_meta_description(soup):
    return _meta_content(
        soup,
        "meta[name='description']",
        "meta[property='og:description']",
        "meta[name='twitter:description']",
    )


def _safe_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _looks_useful_image(url, alt, img=None):
    lower = f"{url} {alt}".lower()
    if not url or url.startswith("data:"):
        return False
    if ".svg" in lower:
        return False
    if any(hint in lower for hint in BLOCKED_IMAGE_HINTS):
        return False

    if img:
        width = _safe_int(img.get("width"))
        height = _safe_int(img.get("height"))
        if width and height and (width < 200 or height < 120):
            return False

    return True


def _append_image(images, seen, url, alt="", source="article/img", img=None):
    if not _looks_useful_image(url, alt, img=img):
        return False
    if url in seen:
        return False
    seen.add(url)
    images.append(
        {
            "url": url,
            "alt": _normalize_text(alt),
            "source": source,
        }
    )
    return True


def _jsonld_image_values(value):
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_jsonld_image_values(item))
        return values
    if isinstance(value, dict):
        return _jsonld_image_values(value.get("url") or value.get("@id"))
    return []


def _extract_jsonld_images(soup):
    images = []
    for script in soup.select("script[type='application/ld+json']"):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if isinstance(node, dict) and "@graph" in node and isinstance(node["@graph"], list):
                nodes.extend(node["@graph"])
            if isinstance(node, dict):
                images.extend(_jsonld_image_values(node.get("image")))
    return images


def _iter_jsonld_nodes(soup):
    for script in soup.select("script[type='application/ld+json']"):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        stack = list(data if isinstance(data, list) else [data])
        while stack:
            node = stack.pop(0)
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            yield node
            for key in ("@graph", "mainEntity", "mainEntityOfPage", "isPartOf"):
                value = node.get(key)
                if isinstance(value, list):
                    stack.extend(value)
                elif isinstance(value, dict):
                    stack.append(value)


def _jsonld_types(node):
    value = node.get("@type")
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, list):
        return {str(item).casefold() for item in value}
    return set()


def _jsonld_is_article(node):
    types = _jsonld_types(node)
    return any("article" in item or item in {"blogposting", "news", "report"} for item in types)


def _jsonld_text_value(value):
    if isinstance(value, str):
        return _html_to_text(value)
    if isinstance(value, dict):
        return _jsonld_text_value(value.get("text") or value.get("value") or value.get("@value"))
    if isinstance(value, list):
        return _normalize_text(" ".join(_jsonld_text_value(item) for item in value))
    return ""


def _extract_jsonld_article_body(soup):
    best = ""
    for node in _iter_jsonld_nodes(soup):
        if not _jsonld_is_article(node):
            continue
        text = _jsonld_text_value(node.get("articleBody"))
        if len(text) > len(best):
            best = text
    return best


def _extract_jsonld_description(soup):
    best = ""
    for node in _iter_jsonld_nodes(soup):
        if not _jsonld_is_article(node):
            continue
        text = _jsonld_text_value(node.get("description"))
        if len(text) > len(best):
            best = text
    return best


def _meta_image_candidates(soup):
    return (
        ("og", _meta_content(soup, "meta[property='og:image']", "meta[property='og:image:url']", "meta[property='og:image:secure_url']")),
        ("twitter", _meta_content(soup, "meta[name='twitter:image']", "meta[property='twitter:image']", "meta[name='twitter:image:src']")),
        ("meta", _meta_content(soup, "meta[name='image']", "meta[itemprop='image']")),
    )


def _extract_article_images(soup, article_url):
    from image_extractor import extract_images
    return extract_images(soup, article_url)


def _existing_image_candidates(article):
    candidates = []
    image = article.get("image")
    if isinstance(image, dict) and image.get("url"):
        candidates.append(
            {
                "url": image.get("url"),
                "alt": image.get("alt") or article.get("title", ""),
                "source": "scraper",
            }
        )
    elif isinstance(image, str) and image.strip():
        candidates.append(
            {
                "url": image.strip(),
                "alt": article.get("title", ""),
                "source": "scraper",
            }
        )
    return candidates


def _select_downloadable_main_image(article, main_image_url, image_extraction_method, extra_images):
    candidates = []
    if main_image_url:
        candidates.append(
            {
                "url": main_image_url,
                "alt": article.get("fetched_title") or article.get("title", ""),
                "source": image_extraction_method or "unknown",
            }
        )
    candidates.extend(_existing_image_candidates(article))
    for extra_image in extra_images or []:
        if extra_image.get("url"):
            candidates.append(
                {
                    "url": extra_image["url"],
                    "alt": extra_image.get("alt") or article.get("title", ""),
                    "source": "article_content",
                }
            )

    seen = set()
    for candidate in candidates:
        image_url = str(candidate.get("url") or "").strip()
        if not image_url or image_url in seen:
            continue
        seen.add(image_url)
        if download_image_with_retry(image_url, timeout=5, retries=3):
            return image_url, candidate.get("source") or "unknown", candidate.get("alt") or ""

    log_event(
        "article_image_missing",
        article_id=article.get("id"),
        url=article.get("url"),
        title=article.get("title"),
    )
    return "", "", ""


def _extract_and_prepare_images(html_content, article_url, article_title=""):
    """
    Extract main image and extra images using the new image_extractor module.
    Returns (main_image_url, extraction_method, extra_images_list)
    """
    soup = BeautifulSoup(html_content or "", "html.parser")
    raw_article_image_count = 0
    if soup:
        raw_article_image_count = len(soup.find_all("img"))

    # Extract main image
    main_image_url, extraction_method = extract_main_image(
        html_content,
        article_url,
        article_title or "Article image"
    )
    
    # Extract extra images (up to 3, excluding main image)
    extra_images = extract_extra_images(
        html_content,
        article_url,
        main_image_url=main_image_url,
        limit=3
    )
    
    # Format as article_images list for backward compatibility
    article_images = []
    seen_article_images = set()
    if main_image_url:
        seen_article_images.add(main_image_url)
        article_images.append({
            "url": main_image_url,
            "alt": article_title or "Article image",
            "source": extraction_method or "unknown",
        })
    
    for extra_image in extra_images:
        image_url = extra_image.get("url")
        if not image_url or image_url in seen_article_images:
            continue
        seen_article_images.add(image_url)
        article_images.append({
            "url": image_url,
            "alt": extra_image.get("alt") or "Article image",
            "source": "article_content",
        })

    log_event(
        "article_images_found",
        article_url=article_url,
        raw_images=str(raw_article_image_count),
        selected_images=str(len(article_images)),
        main_image_found="yes" if main_image_url else "no",
    )
    log_event(
        "article_images_filtered",
        article_url=article_url,
        filtered_count=str(max(0, raw_article_image_count - len(article_images))),
        selected_images=str(len(article_images)),
    )
    
    return main_image_url, extraction_method or "unknown", extra_images


def _host_without_www(value):
    return str(value or "").lower().removeprefix("www.")


def _host_for_url(value):
    parsed = urlparse(str(value or "").strip())
    host = parsed.netloc or parsed.path.split("/")[0]
    return _host_without_www(host)


def _same_site(url_a, url_b):
    host_a = _host_for_url(url_a)
    host_b = _host_for_url(url_b)
    return bool(host_a and host_b and (host_a == host_b or host_a.endswith("." + host_b) or host_b.endswith("." + host_a)))


def _trusted_host(host):
    clean_host = _host_without_www(host)
    return any(clean_host == trusted or clean_host.endswith("." + trusted) for trusted in TRUSTED_REFERENCE_HOSTS)


def _has_tracking_query(url):
    query_keys = [key.casefold() for key, _value in parse_qsl(urlparse(url).query, keep_blank_values=True)]
    return any(any(key.startswith(prefix) for prefix in TRACKING_QUERY_KEYS) for key in query_keys)


def _strip_tracking_query(url):
    parsed = urlparse(url)
    filtered = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(key.casefold().startswith(prefix) for prefix in TRACKING_QUERY_KEYS)
    ]
    return urlunparse(parsed._replace(query=urlencode(filtered, doseq=True), fragment=""))


def _is_affiliate_or_tracking_link(url):
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    path_query = f"{parsed.path} {parsed.query}".casefold()
    if any(hint in host for hint in AFFILIATE_HOST_HINTS):
        return True
    return any(hint in path_query for hint in AFFILIATE_PATH_HINTS)


def _remove_unwanted_links(soup, article_url, source_url):
    source_links_removed = 0
    affiliate_links_removed = 0
    source_base = source_url or article_url

    for link in list(soup.select("a[href]")):
        href = (link.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute_href = urljoin(article_url, href)
        if not absolute_href.startswith(("http://", "https://")):
            link.unwrap()
            continue

        if _is_affiliate_or_tracking_link(absolute_href):
            affiliate_links_removed += 1
            link.unwrap()
            continue

        if _same_site(absolute_href, source_base) and not _trusted_host(_host_for_url(absolute_href)):
            source_links_removed += 1
            link.unwrap()
            continue

        if _has_tracking_query(absolute_href):
            cleaned_href = _strip_tracking_query(absolute_href)
            if cleaned_href != absolute_href:
                link["href"] = cleaned_href

    return source_links_removed, affiliate_links_removed


def _trusted_reference_allowed(url, source_url):
    host = _host_for_url(url)
    source_host = _host_for_url(source_url or "")
    if source_host and host == source_host:
        return _trusted_host(host)
    return _trusted_host(host)


def _extract_trusted_references(soup, article_url, source_url):
    references = []
    seen = set()
    container = _best_article_container(soup)

    for link in container.select("a[href]"):
        href = urljoin(article_url, (link.get("href") or "").strip())
        if not href.startswith(("http://", "https://")):
            continue
        if _is_affiliate_or_tracking_link(href):
            continue
        href = _strip_tracking_query(href)
        if href in seen or not _trusted_reference_allowed(href, source_url):
            continue
        title = _normalize_text(link.get_text(" ", strip=True)) or urlparse(href).netloc
        seen.add(href)
        references.append({"title": title, "url": href})
        if len(references) >= MAX_TRUSTED_REFERENCES:
            break

    return references


def _clean_soup(soup):
    for selector in REMOVE_SELECTORS:
        for tag in soup.select(selector):
            tag.decompose()
    for tag in list(soup.find_all(True)):
        if getattr(tag, "attrs", None) is None:
            continue
        attrs = " ".join(
            str(value)
            for value in (
                tag.get("class", []),
                tag.get("id", ""),
                tag.get("role", ""),
                tag.get("aria-label", ""),
                tag.get("data-testid", ""),
                tag.get("data-component", ""),
            )
        ).casefold()
        if attrs and any(hint in attrs for hint in NOISE_ATTR_HINTS):
            tag.decompose()
    return soup


def _soup_copy(soup):
    return BeautifulSoup(str(soup), "html.parser")


def _best_article_container(soup):
    best = None
    best_length = 0

    for selector in ARTICLE_SELECTORS:
        for candidate in soup.select(selector):
            text = _normalize_text(candidate.get_text(" ", strip=True))
            if len(text) > best_length:
                best = candidate
                best_length = len(text)

    return best or soup.body or soup


def _is_noisy_text(text):
    lower = (text or "").casefold()
    return any(pattern in lower for pattern in NOISE_TEXT_PATTERNS)


def _append_text_part(parts, seen, text, min_chars=30):
    clean = _normalize_text(text)
    if len(clean) < min_chars or _is_noisy_text(clean):
        return False
    fingerprint = re.sub(r"\W+", "", clean.casefold())[:180]
    if fingerprint and fingerprint in seen:
        return False
    seen.add(fingerprint)
    parts.append(clean)
    return True


def _container_text(container, include_tables=True):
    parts = []
    seen = set()
    tags = ["p", "li", "h2", "h3", "blockquote"]
    if include_tables:
        tags.extend(["td", "th"])

    for node in container.find_all(tags, limit=260):
        if node.find_parent(["nav", "header", "footer", "aside", "form"]):
            continue
        text = node.get_text(" ", strip=True)
        min_chars = 18 if node.name in {"h2", "h3", "th"} else 30
        _append_text_part(parts, seen, text, min_chars=min_chars)

    if parts:
        return _normalize_text(" ".join(parts))

    fallback = _normalize_text(container.get_text(" ", strip=True))
    return "" if _is_noisy_text(fallback) else fallback


def _extract_source_tables(
    soup,
    *,
    max_tables=12,
    max_rows_per_table=120,
    max_cells_per_row=16,
    max_total_chars=24000,
):
    """Preserve source table structure as AI evidence instead of flattening every cell into prose."""
    extracted = []
    total_chars = 0
    truncated = False

    for table_index, table in enumerate(soup.find_all("table"), start=1):
        if len(extracted) >= max_tables or total_chars >= max_total_chars:
            truncated = True
            break
        if table.find_parent(["nav", "header", "footer", "aside", "form"]):
            continue

        caption_node = table.find("caption")
        caption = _normalize_text(caption_node.get_text(" ", strip=True) if caption_node else "")
        rows = []

        for row in table.find_all("tr"):
            if len(rows) >= max_rows_per_table or total_chars >= max_total_chars:
                truncated = True
                break
            cells = []
            for cell in row.find_all(["th", "td"], recursive=False)[:max_cells_per_row]:
                text = _normalize_text(cell.get_text(" ", strip=True))
                if text:
                    cells.append(text)
            if not cells:
                continue

            row_chars = sum(len(cell) for cell in cells) + max(0, len(cells) - 1) * 3
            if total_chars + row_chars > max_total_chars:
                truncated = True
                break
            rows.append(cells)
            total_chars += row_chars

        if rows:
            extracted.append(
                {
                    "table_index": table_index,
                    "caption": caption,
                    "rows": rows,
                }
            )

    return extracted, truncated


def _best_text_for_selector(soup, selector, method):
    best = ""
    for candidate in soup.select(selector):
        text = _container_text(candidate)
        if len(text) > len(best):
            best = text
    return method, best


def _paragraph_fallback_text(soup):
    root = soup.body or soup
    parts = []
    seen = set()
    for paragraph in root.find_all(["p", "li"], limit=320):
        if paragraph.find_parent(["nav", "header", "footer", "aside", "form"]):
            continue
        _append_text_part(parts, seen, paragraph.get_text(" ", strip=True), min_chars=40)
    return _normalize_text(" ".join(parts))


def _trim_preview(text):
    text = _normalize_text(text)
    if len(text) <= PREVIEW_MAX_CHARS:
        return text

    preview = text[:PREVIEW_MAX_CHARS]
    for separator in (". ", "! ", "? ", "؟ "):
        cut = preview.rfind(separator)
        if cut >= PREVIEW_MIN_CHARS:
            return preview[: cut + len(separator.strip())].strip()

    cut = preview.rfind(" ")
    if cut >= PREVIEW_MIN_CHARS:
        return preview[:cut].strip()

    return preview.strip()


def _extract_content_preview(soup):
    clean = _clean_soup(soup)
    container = _best_article_container(clean)
    return _trim_preview(_container_text(container))


def _extract_full_article_text(soup):
    clean = _clean_soup(soup)
    container = _best_article_container(clean)
    full_text = _container_text(container)
    fallback = _paragraph_fallback_text(clean)
    return fallback if len(fallback) > len(full_text) else full_text


def _choose_enrichment_text(article, soup, min_success_chars):
    meta_description = _extract_meta_description(soup)
    candidates = [
        ("jsonld_article_body", _extract_jsonld_article_body(_soup_copy(soup))),
        _best_text_for_selector(_soup_copy(soup), "article", "article_tag"),
        _best_text_for_selector(_soup_copy(soup), "main", "main_tag"),
    ]
    for selector in BODY_CONTAINER_SELECTORS:
        method, text = _best_text_for_selector(_soup_copy(soup), selector, "content_container")
        if text:
            candidates.append((method, text))
    candidates.extend(
        [
            ("paragraph_fallback", _paragraph_fallback_text(_clean_soup(_soup_copy(soup)))),
            ("article_body", _extract_full_article_text(_soup_copy(soup))),
            ("rss_summary", _html_to_text(article.get("rss_summary", ""))),
            ("jsonld_description", _extract_jsonld_description(_soup_copy(soup))),
            ("meta_description", meta_description),
            ("page_text", _extract_content_preview(_soup_copy(soup))),
        ]
    )
    best_method = ""
    best_text = ""
    tried = []
    primary_failed_logged = False
    for method, raw_text in candidates:
        text = _normalize_text(raw_text)
        if not text:
            continue
        words = _word_count(text)
        tried.append(f"{method}:{len(text)}chars/{words}words")
        if len(text) > len(best_text):
            best_method = method
            best_text = text
        if method in {"jsonld_article_body", "article_tag", "main_tag"} and words < MIN_EXTRACTED_WORDS and not primary_failed_logged:
            primary_failed_logged = True
            log_event(
                "extraction_primary_failed",
                method=method,
                title=article.get("title"),
                source=article.get("source_name"),
                chars=len(text),
                words=words,
                required_words=MIN_EXTRACTED_WORDS,
            )
        if len(text) >= min_success_chars and words >= MIN_EXTRACTED_WORDS:
            if method == "jsonld_article_body":
                log_event("extraction_jsonld_used", title=article.get("title"), source=article.get("source_name"), words=words)
            elif method == "article_tag":
                log_event("extraction_article_tag_used", title=article.get("title"), source=article.get("source_name"), words=words)
            elif method == "paragraph_fallback":
                log_event("extraction_paragraph_fallback_used", title=article.get("title"), source=article.get("source_name"), words=words)
            return text, method, meta_description, tried
    return best_text, best_method, meta_description, tried


def _source_url(article):
    explicit = article.get("source_url")
    if explicit:
        return explicit

    parsed = urlparse(article.get("url", ""))
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _fetch_html_with_requests(url):
    last_error = ""

    for attempt in range(FETCH_RETRIES + 1):
        started = time.perf_counter()
        try:
            log_event("fetch_start", url=url, method="requests", attempt=attempt + 1)
            response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
            status_code = response.status_code
            if status_code >= 400:
                last_error = f"http {status_code}"
                log_event(
                    "fetch_end",
                    url=url,
                    method="requests",
                    status=status_code,
                    error=last_error,
                    elapsed_ms=elapsed_ms(started),
                )
                if status_code in {401, 403, 404}:
                    break
            else:
                log_event(
                    "fetch_end",
                    url=url,
                    method="requests",
                    status=status_code,
                    chars=len(response.text),
                    elapsed_ms=elapsed_ms(started),
                )
                return response.text, ""
        except requests.exceptions.Timeout:
            last_error = "timeout"
            log_event(
                "fetch_end",
                url=url,
                method="requests",
                error=last_error,
                elapsed_ms=elapsed_ms(started),
            )
        except requests.exceptions.RequestException as error:
            last_error = str(error)
            log_event(
                "fetch_end",
                url=url,
                method="requests",
                error=error.__class__.__name__,
                elapsed_ms=elapsed_ms(started),
            )

        if attempt < FETCH_RETRIES:
            time.sleep(min(SOURCE_RETRY_DELAY_SECONDS, 1 + attempt))

    return "", last_error or "fetch failed"


def _apply_enrichment_from_html(article, html, url):
    soup = BeautifulSoup(html, "html.parser")
    job_source_soup = BeautifulSoup(html, "html.parser") if JOBS_MODE else soup
    source_links_removed, affiliate_links_removed = _remove_unwanted_links(
        soup,
        url,
        article.get("source_url") or _source_url(article),
    )
    log_event(
        "source_links_removed",
        title=article.get("title"),
        source=article.get("source_name"),
        source_url=url,
        count=str(source_links_removed),
    )
    log_event(
        "affiliate_links_removed",
        title=article.get("title"),
        source=article.get("source_name"),
        source_url=url,
        count=str(affiliate_links_removed),
    )
    article["source_links_removed_count"] = source_links_removed
    article["affiliate_links_removed_count"] = affiliate_links_removed
    article["removed_source_links_count"] = source_links_removed
    min_success_chars = (
        max(MIN_EXTRACTED_CHARS, 250)
        if JOBS_MODE
        else (FAST_ENRICH_MIN_CHARS if FAST_NEWS_MODE else WEAK_ARTICLE_MIN_CHARS)
    )
    full_text, text_method, meta_description, tried_text_sources = _choose_enrichment_text(
        article,
        soup,
        min_success_chars,
    )
    preview = _trim_preview(full_text) if full_text else ""
    extracted_words = _word_count(full_text)
    required_words = 40 if JOBS_MODE else MIN_EXTRACTED_WORDS
    log_event(
        "final_extracted_words",
        title=article.get("title"),
        source=article.get("source_name"),
        source_url=url,
        method=text_method or "none",
        chars=len(full_text or ""),
        words=str(extracted_words),
    )
    if not full_text or len(full_text) < min_success_chars or extracted_words < required_words:
        tried = ", ".join(tried_text_sources) if tried_text_sources else "none"
        return (
            False,
            "weak article body after fallbacks "
            f"(best={len(full_text or '')} chars/{extracted_words} words; "
            f"required={min_success_chars} chars/{required_words} words; tried={tried})",
        )
    if text_method and text_method != "article_body":
        article["enrichment_fallback_used"] = text_method
        log_event(
            "enrichment_fallback_used",
            method=text_method,
            title=article.get("title"),
            source=article.get("source_name"),
            source_url=url,
            chars=len(full_text),
        )

    article["fetched_title"] = _extract_title(soup) or article.get("title", "")
    article["meta_description"] = meta_description

    if JOBS_MODE:
        invalidate_identity_evidence(
            article,
            reason="source detail enrichment refreshed identity facts",
        )
        source_tables, source_tables_truncated = _extract_source_tables(job_source_soup)
        article["source_tables"] = source_tables
        article["source_tables_count"] = len(source_tables)
        article["source_tables_truncated"] = bool(source_tables_truncated)
        article.update(extract_job_fields(job_source_soup, article, url, full_text=full_text))
        try:
            article.update(resolve_company_logo(job_source_soup, article, url))
        except Exception as error:
            # Logo resolution must never stop a valid vacancy from publishing.
            # Fail closed: a missing logo is safer than a wrong employer logo.
            article["company_logo_url"] = ""
            article["company_logo_verified"] = False
            article["company_logo_confidence"] = 0
            article["company_logo_source"] = "resolver_error"
            log_event(
                "company_logo_resolver_failed",
                article_id=article.get("id"),
                company=article.get("job_company", ""),
                error=error.__class__.__name__,
            )
        log_event(
            "job_fields_extracted",
            article_id=article.get("id"),
            company=article.get("job_company", ""),
            location=article.get("job_location", ""),
            deadline=article.get("job_deadline", ""),
            positions=article.get("job_number_of_positions", 0),
            eligibility=article.get("job_eligibility", ""),
            logo_verified=bool(article.get("company_logo_verified")),
            logo_confidence=int(article.get("company_logo_confidence") or 0),
            logo_source=article.get("company_logo_source", ""),
        )
    
    if JOBS_MODE:
        # Jobs never reuse hero/content images from the source page.
        # The Blogger publisher creates exactly one branded cover later from
        # the owner-supplied article template + employer logo + job title.
        article["article_images"] = []
        article["main_image"] = ""
        article["main_image_source_type"] = "job_template"
        article["main_image_extraction_method"] = "disabled_for_jobs"
        article["extra_article_images"] = []
        article.pop("image_warning", None)
        article_images = []
        log_event(
            "job_source_image_extraction_disabled",
            article_id=article.get("id"),
            source=article.get("source_name"),
            source_url=url,
        )
    else:
        # Non-job content keeps the existing article-image extraction path.
        main_image_url, image_extraction_method, extra_images = _extract_and_prepare_images(
            str(soup),
            url,
            article.get("title", "") or article.get("fetched_title", "")
        )
        main_image_url, image_extraction_method, _downloaded_alt = _select_downloadable_main_image(
            article,
            main_image_url,
            image_extraction_method,
            extra_images,
        )
        if not main_image_url:
            article["image_warning"] = "missing downloadable article image"
            log_event(
                "enrichment_fallback_used",
                method="no_image_continue",
                title=article.get("title"),
                source=article.get("source_name"),
                source_url=url,
                reason="missing downloadable article image",
            )

        article_images = []
        seen_article_images = set()
        if main_image_url:
            seen_article_images.add(main_image_url)
            article_images.append({
                "url": main_image_url,
                "alt": article.get("fetched_title") or article.get("title", ""),
                "source": image_extraction_method,
            })

        for extra_image in extra_images:
            image_url = extra_image.get("url")
            if not image_url or image_url in seen_article_images:
                continue
            seen_article_images.add(image_url)
            article_images.append({
                "url": image_url,
                "alt": extra_image.get("alt") or "Article image",
                "source": "article_content",
            })

        article["article_images"] = article_images
        article["main_image"] = main_image_url or ""
        article["main_image_source_type"] = image_extraction_method or "fallback"
        article["main_image_extraction_method"] = image_extraction_method
        article["extra_article_images"] = extra_images

        log_event(
            "image_extraction_complete",
            url=url,
            main_image_found="yes" if main_image_url else "no",
            image_extraction_method=image_extraction_method,
            extra_images_count=len(extra_images),
        )
    article["trusted_references"] = _extract_trusted_references(
        soup,
        url,
        article.get("source_url") or _source_url(article),
    )
    article["full_article_text"] = full_text or preview
    article["full_article_text_chars"] = len(article["full_article_text"])
    article["content_preview"] = preview
    article["content_preview_chars"] = len(preview)
    article["source_url"] = _source_url(article)
    article["content_fetched_at"] = _now_iso()
    is_strong = len(article["full_article_text"]) >= STRONG_ARTICLE_MIN_CHARS
    is_weak = len(article["full_article_text"]) >= min_success_chars
    article["enrichment_status"] = "strong" if is_strong else "weak"
    article["content_fetch_status"] = "success" if (
        (JOBS_MODE and is_weak)
        or is_strong
        or (FAST_NEWS_MODE and (is_weak or PUBLISH_WEAK_ARTICLES))
    ) else "weak"
    article.pop("content_fetch_error", None)
    log_event(
        "article_enriched",
        title=article.get("fetched_title") or article.get("title"),
        source=article.get("source_name"),
        source_url=url,
        chars=len(article["full_article_text"]),
        preview_chars=len(preview),
        images=len(article_images),
        main_image_found="yes" if article.get("main_image") else "no",
        image_extraction_method=article.get("main_image_extraction_method"),
        extra_images_count=len(article.get("extra_article_images") or []),
        references=len(article.get("trusted_references") or []),
        enrichment_status=article["enrichment_status"],
    )
    if not is_weak and not (FAST_NEWS_MODE and PUBLISH_WEAK_ARTICLES and article["full_article_text"]):
        return False, f"weak article body ({len(article['full_article_text'])} chars)"
    if not is_strong and not FAST_NEWS_MODE and not JOBS_MODE:
        return False, f"weak article body ({len(article['full_article_text'])} chars)"
    return True, ""


def _apply_rss_summary_fallback(article):
    fallback_image_url = ""
    fallback_image_source = ""
    if not JOBS_MODE:
        fallback_image_url, fallback_image_source, _fallback_image_alt = _select_downloadable_main_image(
            article,
            "",
            "",
            [],
        )
        if not fallback_image_url:
            article["image_warning"] = "missing downloadable article image"

    summary = _html_to_text(article.get("rss_summary", ""))
    if len(summary) < MIN_EXTRACTED_CHARS:
        summary = _normalize_text(
            " ".join(
                value
                for value in (
                    article.get("title", ""),
                    article.get("meta_description", ""),
                    article.get("source_name", ""),
                )
                if value
            )
        )
    summary_words = _word_count(summary)
    required_words = 40 if JOBS_MODE else MIN_EXTRACTED_WORDS
    if len(summary) < MIN_EXTRACTED_CHARS or summary_words < required_words:
        return False, (
            "missing article body after rss fallback "
            f"({len(summary)} chars/{summary_words} words; required {required_words} words)"
        )
    article["fetched_title"] = article.get("title", "")
    article["meta_description"] = summary[:240]
    article["article_images"] = [
        {
            "url": fallback_image_url,
            "alt": article.get("fetched_title") or article.get("title", ""),
            "source": fallback_image_source or "fallback",
        }
    ] if (fallback_image_url and not JOBS_MODE) else []
    article["main_image"] = fallback_image_url if not JOBS_MODE else ""
    article["main_image_source_type"] = (
        fallback_image_source or "fallback"
        if not JOBS_MODE
        else "job_template"
    )
    article["main_image_extraction_method"] = (
        fallback_image_source or "fallback"
        if not JOBS_MODE
        else "disabled_for_jobs"
    )
    article["extra_article_images"] = []
    article.pop("image_warning", None)
    article["trusted_references"] = []
    article["full_article_text"] = summary
    article["full_article_text_chars"] = len(summary)
    article["content_preview"] = _trim_preview(summary)
    article["content_preview_chars"] = len(article["content_preview"])
    article["source_url"] = _source_url(article)
    article["content_fetched_at"] = _now_iso()
    article["enrichment_status"] = "rss_summary"
    article["content_fetch_status"] = "success"
    article.pop("content_fetch_error", None)
    log_event(
        "enrichment_fallback_used",
        method="rss_summary",
        title=article.get("title"),
        source=article.get("source_name"),
        chars=len(summary),
    )
    log_event(
        "article_enriched_from_rss_summary",
        title=article.get("title"),
        source=article.get("source_name"),
        chars=len(summary),
        words=summary_words,
    )
    return True, ""



def _csod_article_config(article):
    url = str(article.get("url") or "").strip()
    parsed = urlparse(url)
    if not (parsed.netloc.casefold() == "csod.com" or parsed.netloc.casefold().endswith(".csod.com")):
        return None
    match = re.search(
        r"/ux/ats/careersite/(\d+)/home/requisition/(\d+)",
        parsed.path,
        flags=re.I,
    )
    if not match:
        return None
    site_id = int(match.group(1))
    req_id = match.group(2)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    corp = query.get("c") or parsed.netloc.split(".")[0]
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return {
        "origin": origin,
        "site_id": site_id,
        "req_id": req_id,
        "corp": corp,
        "home_url": f"{origin}/ux/ats/careersite/{site_id}/home?c={corp}",
        "detail_url": (
            f"{origin}/services/x/job-requisition/v2/requisitions/"
            f"{req_id}/jobDetails?cultureId=1"
        ),
        "ad_url": (
            f"{origin}/Services/API/ATS/CareerSite/{site_id}/"
            f"JobRequisitions/{req_id}?useMobileAd=false&cultureId=1"
        ),
        "posting_url": f"{origin}/services/x/career-site/v1/requisition/{req_id}",
    }


def _csod_token_from_html(html_text):
    match = re.search(r'"token"\s*:\s*"([A-Za-z0-9._-]+)"', str(html_text or ""))
    return match.group(1) if match else ""


def _csod_ad_html(payload):
    try:
        return str(payload["data"][0]["items"][0]["fields"].get("ad") or "")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def _csod_posting(payload):
    try:
        rows = payload.get("data", {}).get("postings", [])
    except AttributeError:
        rows = []
    if not isinstance(rows, list):
        return {}
    return next((row for row in rows if isinstance(row, dict) and row.get("isDefault")), rows[0] if rows else {})


def _apply_csod_payloads(article, detail_payload, posting_payload, ad_payload=None):
    detail = (detail_payload or {}).get("data") or {}
    if not isinstance(detail, dict):
        detail = {}
    posting = _csod_posting(posting_payload or {})
    description = str(detail.get("externalDescription") or "").strip()
    if len(_html_to_text(description)) < 250:
        description = _csod_ad_html(ad_payload or {}) or description
    if not description:
        return False, "CSOD API returned no job description"

    title = _normalize_text(detail.get("displayTitle") or article.get("title") or "")
    primary = detail.get("primaryLocation") if isinstance(detail.get("primaryLocation"), dict) else {}
    location = _normalize_text(
        primary.get("locationDisplayTitle")
        or primary.get("title")
        or article.get("job_location")
        or ""
    )
    country = str(primary.get("country") or article.get("job_country") or "").strip()
    apply_url = str(detail.get("companyApplyUrl") or article.get("url") or "").strip()
    deadline = str(posting.get("endDate") or "").strip()
    published = str(posting.get("startDate") or detail.get("openDate") or "").strip()
    deadline_date = deadline[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", deadline) else ""

    facts = []
    if location:
        facts.append(f"<p><strong>Localisation :</strong> {html_lib.escape(location)}</p>")
    if deadline_date:
        facts.append(f"<p><strong>Date limite de candidature :</strong> {html_lib.escape(deadline_date)}</p>")
    apply_html = (
        f'<p><a href="{html_lib.escape(apply_url, quote=True)}">Postuler sur le site officiel</a></p>'
        if apply_url else ""
    )
    synthetic = (
        "<html><head><title>"
        + html_lib.escape(title or str(article.get("title") or ""))
        + "</title></head><body><h1>"
        + html_lib.escape(title or str(article.get("title") or ""))
        + "</h1>"
        + "".join(facts)
        + description
        + apply_html
        + "</body></html>"
    )
    ok, error = _apply_enrichment_from_html(article, synthetic, article.get("url") or apply_url)
    if not ok:
        return ok, error

    article["ats_provider"] = "csod"
    article["ats_reference"] = str(article.get("ats_reference") or detail.get("ref") or "")
    article["job_external_reference"] = str(detail.get("ref") or article.get("ats_reference") or "")
    if title:
        article["fetched_title"] = title
        article["job_title"] = title
    if location:
        article["job_location"] = location
    if country:
        article["job_country"] = country
    if deadline_date:
        article["job_deadline"] = deadline_date
        article["job_deadline_display"] = deadline_date
    if published:
        article["job_published_at"] = published
        article["source_published_at"] = article.get("source_published_at") or published
    if apply_url:
        article["job_application_url"] = apply_url
        article["job_application_link_kind"] = "official_job_page"
    article["csod_requisition_status_id"] = detail.get("requisitionStatusId")
    article["csod_allow_apply"] = bool(detail.get("allowApply"))
    return True, ""


def _apply_phenom_enrichment(article):
    description = _normalize_text(article.get("ats_description") or "")
    payload = article.get("phenom_payload") if isinstance(article.get("phenom_payload"), dict) else {}
    facts = []
    structured = [
        ("Entreprise", article.get("job_company") or payload.get("company")),
        ("Localisation", article.get("job_location") or payload.get("city")),
        ("Type de contrat", article.get("job_contract_type")),
        ("Catégorie", payload.get("category")),
        ("Mode de travail", payload.get("workModel")),
        ("Temps de travail", payload.get("hiringType")),
        ("Salaire", article.get("job_salary")),
    ]
    for label, value in structured:
        value = _normalize_text(value or "")
        if value:
            facts.append(f"<p><strong>{html_lib.escape(label)} :</strong> {html_lib.escape(value)}</p>")
    if not description and not facts:
        return False, "Phenom payload contains no usable job data"

    apply_url = str(article.get("job_application_url") or article.get("url") or "").strip()
    title = _normalize_text(article.get("title") or article.get("fetched_title") or "")
    synthetic = (
        "<html><head><title>" + html_lib.escape(title) + "</title></head><body>"
        + "<h1>" + html_lib.escape(title) + "</h1>"
        + "".join(facts)
        + ("<p>" + html_lib.escape(description) + "</p>" if description else "")
        + (
            f'<p><a href="{html_lib.escape(apply_url, quote=True)}">Postuler directement</a></p>'
            if apply_url else ""
        )
        + "</body></html>"
    )
    ok, error = _apply_enrichment_from_html(article, synthetic, apply_url or article.get("url"))
    if not ok:
        return ok, error

    article["ats_provider"] = "phenom"
    if apply_url:
        article["job_application_url"] = apply_url
        article["job_application_link_kind"] = "direct_apply"
    article["job_country"] = article.get("job_country") or "MA"
    if article.get("job_location"):
        article["job_location"] = _normalize_text(article["job_location"])
    return True, ""


def _fetch_csod_payloads_sync(article):
    cfg = _csod_article_config(article)
    if not cfg:
        return None, None, None, "invalid CSOD article URL"
    session = requests.Session()
    try:
        bootstrap = session.get(cfg["home_url"], headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        bootstrap.raise_for_status()
        token = _csod_token_from_html(bootstrap.text)
        if not token:
            return None, None, None, "anonymous CSOD token missing"
        headers = {**HEADERS, "Accept": "application/json", "Authorization": f"Bearer {token}"}
        detail_response = session.get(cfg["detail_url"], headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        detail_response.raise_for_status()
        posting_response = session.get(cfg["posting_url"], headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        posting_response.raise_for_status()
        detail_payload = detail_response.json()
        posting_payload = posting_response.json()
        ad_payload = {}
        if len(_html_to_text((detail_payload.get("data") or {}).get("externalDescription") or "")) < 250:
            ad_response = session.get(cfg["ad_url"], headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            if ad_response.ok:
                ad_payload = ad_response.json()
        return detail_payload, posting_payload, ad_payload, ""
    except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
        return None, None, None, f"CSOD API {error.__class__.__name__}"


async def _fetch_csod_payloads_async(article, session):
    cfg = _csod_article_config(article)
    if not cfg:
        return None, None, None, "invalid CSOD article URL"
    try:
        async with session.get(cfg["home_url"], headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            bootstrap = await response.text(errors="ignore")
            if response.status >= 400:
                return None, None, None, f"CSOD bootstrap http {response.status}"
        token = _csod_token_from_html(bootstrap)
        if not token:
            return None, None, None, "anonymous CSOD token missing"
        headers = {**HEADERS, "Accept": "application/json", "Authorization": f"Bearer {token}"}
        async with session.get(cfg["detail_url"], headers=headers, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            detail_text = await response.text(errors="ignore")
            if response.status >= 400:
                return None, None, None, f"CSOD detail http {response.status}"
        async with session.get(cfg["posting_url"], headers=headers, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            posting_text = await response.text(errors="ignore")
            if response.status >= 400:
                return None, None, None, f"CSOD posting http {response.status}"
        detail_payload = json.loads(detail_text or "{}")
        posting_payload = json.loads(posting_text or "{}")
        ad_payload = {}
        if len(_html_to_text((detail_payload.get("data") or {}).get("externalDescription") or "")) < 250:
            async with session.get(cfg["ad_url"], headers=headers, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                ad_text = await response.text(errors="ignore")
                if response.status < 400:
                    ad_payload = json.loads(ad_text or "{}")
        return detail_payload, posting_payload, ad_payload, ""
    except (asyncio.TimeoutError, aiohttp.ClientError, ValueError, json.JSONDecodeError) as error:
        return None, None, None, f"CSOD API {error.__class__.__name__}"


def enrich_article(article):
    url = article.get("url", "").strip()
    if not url:
        return False, "missing url"

    provider = str(article.get("ats_provider") or "").strip().lower()
    if provider == "phenom":
        return _apply_phenom_enrichment(article)
    if provider == "csod" or _csod_article_config(article):
        detail, posting, ad, error = _fetch_csod_payloads_sync(article)
        if error:
            return False, error
        return _apply_csod_payloads(article, detail, posting, ad)

    html, error = _fetch_html_with_requests(url)
    if not html:
        return _apply_rss_summary_fallback(article) if article.get("rss_summary") else (False, error)
    return _apply_enrichment_from_html(article, html, url)


async def _fetch_html_with_aiohttp(session, url):
    last_error = ""

    for attempt in range(FETCH_RETRIES + 1):
        started = time.perf_counter()
        try:
            log_event("fetch_start", url=url, method="aiohttp", attempt=attempt + 1)
            async with session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                status_code = response.status
                text = await response.text(errors="ignore")
                if status_code >= 400:
                    last_error = f"http {status_code}"
                    log_event(
                        "fetch_end",
                        url=url,
                        method="aiohttp",
                        status=status_code,
                        error=last_error,
                        elapsed_ms=elapsed_ms(started),
                    )
                    if status_code in {401, 403, 404}:
                        break
                else:
                    log_event(
                        "fetch_end",
                        url=url,
                        method="aiohttp",
                        status=status_code,
                        chars=len(text),
                        elapsed_ms=elapsed_ms(started),
                    )
                    return text, ""
        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            last_error = error.__class__.__name__
            log_event(
                "fetch_end",
                url=url,
                method="aiohttp",
                error=last_error,
                elapsed_ms=elapsed_ms(started),
            )

        if attempt < FETCH_RETRIES:
            await asyncio.sleep(min(SOURCE_RETRY_DELAY_SECONDS, 1 + attempt))

    return "", last_error or "fetch failed"


async def _enrich_article_async(article, session, semaphore):
    url = article.get("url", "").strip()
    if not url:
        return article, False, "missing url"

    provider = str(article.get("ats_provider") or "").strip().lower()
    if provider == "phenom":
        ok, error = _apply_phenom_enrichment(article)
        return article, ok, error

    if provider == "csod" or _csod_article_config(article):
        async with semaphore:
            detail, posting, ad, error = await _fetch_csod_payloads_async(article, session)
        if error:
            return article, False, error
        ok, parse_error = _apply_csod_payloads(article, detail, posting, ad)
        return article, ok, parse_error

    async with semaphore:
        html, error = await _fetch_html_with_aiohttp(session, url)

    if not html:
        if article.get("rss_summary"):
            ok, fallback_error = _apply_rss_summary_fallback(article)
            return article, ok, fallback_error or error
        return article, False, error

    ok, parse_error = _apply_enrichment_from_html(article, html, url)
    return article, ok, parse_error


async def _enrich_articles_async(articles):
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS * (FETCH_RETRIES + 1) + 5)
    connector = aiohttp.TCPConnector(limit=ASYNC_FETCH_CONCURRENCY, ttl_dns_cache=300)
    semaphore = asyncio.Semaphore(ASYNC_FETCH_CONCURRENCY)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tasks = [_enrich_article_async(article, session, semaphore) for article in articles]
        return await asyncio.gather(*tasks)


def _can_run_async_fetch():
    if aiohttp is None:
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


def _candidate_failure_backoff_minutes(failure_count):
    base = max(1, int(SOURCE_CANDIDATE_FAILURE_COOLDOWN_MINUTES or 1))
    exponent = min(max(0, int(failure_count or 1) - 1), 7)
    return min(24 * 60, base * (2 ** exponent))


def _job_cached_enrichment_is_sufficient(article, existing_words):
    if not JOBS_MODE or article.get("content_fetch_status") != "success":
        return False
    if existing_words >= MIN_EXTRACTED_WORDS:
        return True
    if existing_words < 40:
        return False
    provider = str(article.get("ats_provider") or "").strip().casefold()
    if provider not in {"phenom", "csod", "workday", "etalent"}:
        return False
    return bool(
        article.get("job_application_url")
        and (article.get("job_title") or article.get("title"))
        and (article.get("job_company") or article.get("source_name"))
    )


def _record_enrichment_failure(article, error):
    reason = str(error or "unknown enrichment failure")
    failure_count = int(article.get("candidate_failure_count") or 0) + 1
    retry_minutes = _candidate_failure_backoff_minutes(failure_count)
    article["content_fetch_status"] = "failed"
    article["content_fetch_error"] = reason
    article["content_fetched_at"] = _now_iso()
    article["candidate_retry_after"] = _retry_after_iso(retry_minutes)
    article["candidate_failure_stage"] = "enrichment"
    article["candidate_failure_reason"] = reason[:300]
    article["candidate_failed_at"] = _now_iso()
    article["candidate_failure_count"] = failure_count
    article["candidate_retry_backoff_minutes"] = retry_minutes
    log_event(
        "enrichment_failed_reason",
        title=article.get("title"),
        source=article.get("source_name"),
        source_url=article.get("url"),
        reason=reason,
        retry_after=article.get("candidate_retry_after"),
    )


def _jobs_enrichment_priority(article, queue_index=0, now=None):
    """Prioritize urgent/publishable Jobs while aging prevents starvation."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    deadline_rank = 0
    deadline = job_deadline_time(article)
    if (
        deadline is None
        and str(article.get("ats_provider") or "").strip().lower() == "emploi_public"
    ):
        # Emploi-Public listing cards include an explicit "آخر أجل" in the
        # discovered title/card text before the detail page is enriched. Use it
        # only as a queue-order hint so expired historical notices do not consume
        # the small fresh-enrichment batch ahead of still-open opportunities.
        # The verified detail-page extraction remains authoritative and persists
        # the real job_deadline later.
        listing_deadline = _deadline_from_text(article.get("title") or "")
        if listing_deadline:
            deadline = job_deadline_time({"job_deadline": listing_deadline})
    if deadline:
        hours_remaining = (deadline - now).total_seconds() / 3600.0
        if hours_remaining < 0:
            deadline_rank = -1
        elif hours_remaining <= 24:
            deadline_rank = 6
        elif hours_remaining <= 72:
            deadline_rank = 5
        elif hours_remaining <= 7 * 24:
            deadline_rank = 4
        elif hours_remaining <= 14 * 24:
            deadline_rank = 3
        elif hours_remaining <= 30 * 24:
            deadline_rank = 2
        else:
            deadline_rank = 1

    status_rank = {
        "selected": 4,
        "ready": 3,
        "identity_pending": 2,
        "draft_created": 1,
    }.get(str(article.get("status") or "").strip(), 0)

    source_rank = {
        "S+": 6,
        "S": 5,
        "A+": 4,
        "A": 3,
        "B+": 2,
        "B": 1,
    }.get(str(article.get("source_priority") or "").strip().upper(), 0)
    if article.get("official_source") or article.get("job_official_source"):
        source_rank += 2

    try:
        job_score = int(article.get("job_score") or 0)
    except (TypeError, ValueError):
        job_score = 0
    try:
        queue_score = int(article.get("score") or 0) * 10
    except (TypeError, ValueError):
        queue_score = 0
    score = max(job_score, queue_score)
    focus_rank = job_focus_priority(article)

    published_raw = str(
        article.get("job_published_at")
        or article.get("source_published_at")
        or article.get("discovered_at")
        or ""
    ).strip()
    published_epoch = 0.0
    if published_raw:
        try:
            parsed = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            published_epoch = parsed.astimezone(timezone.utc).timestamp()
        except ValueError:
            published_epoch = 0.0

    # Deadline urgency stays first. Then prioritize cyber/IT/developer/student
    # roles, then verified publication freshness. Score/source only break ties.
    return (
        -deadline_rank,
        -focus_rank,
        -published_epoch,
        -status_rank,
        -score,
        -source_rank,
        int(queue_index or 0),
    )


def enrich_ready_articles(force=False):
    """
    Enrich ready articles only. Existing successful enrichments are skipped
    unless force=True.
    """
    queue = load_article_queue()
    articles = queue.get("articles", [])

    checked = 0
    enriched = 0
    failed = 0
    weak = 0
    already_enriched = 0
    recent_failure_skipped = 0
    failed_articles = []
    successful_articles = []
    targets = []
    deferred_targets = 0

    for queue_index, article in enumerate(articles):
        allowed_statuses = {"ready", "identity_pending"} if not force else {"ready", "identity_pending", "selected", "draft_created"}
        if article.get("status") not in allowed_statuses:
            continue
        if JOBS_MODE and not str(
            article.get("category_label") or article.get("category_hint") or ""
        ).strip().casefold().startswith(("jobs-", "remote-jobs")):
            continue

        checked += 1
        if article.get("content_fetch_status") == "success" and not force:
            existing_text = article.get("full_article_text") or article.get("content_full") or article.get("content_preview", "")
            existing_words = _word_count(existing_text)
            if (
                existing_words >= MIN_EXTRACTED_WORDS
                or _job_cached_enrichment_is_sufficient(article, existing_words)
            ):
                already_enriched += 1
                continue
            log_event(
                "extraction_primary_failed",
                article_id=article.get("id"),
                title=article.get("title"),
                source=article.get("source_name"),
                method="cached_success_too_short",
                words=str(existing_words),
                required_words=MIN_EXTRACTED_WORDS,
            )
        if not force and is_candidate_in_recent_failure(article):
            recent_failure_skipped += 1
            log_event(
                "candidate_skipped_recent_failure",
                article_id=article.get("id"),
                source=article.get("source_name"),
                stage=article.get("candidate_failure_stage"),
                retry_after=article.get("candidate_retry_after"),
            )
            continue

        # Do not terminally skip an active vacancy merely because its official
        # publication date is outside the fresh-priority window. Expiry and
        # long-lived no-deadline lifecycle cleanup are handled separately.
        targets.append((queue_index, article))

    if JOBS_MODE and not force and len(targets) > JOBS_ENRICH_MAX_TARGETS_PER_CYCLE:
        targets.sort(key=lambda row: _jobs_enrichment_priority(row[1], row[0]))
        deferred_targets = len(targets) - JOBS_ENRICH_MAX_TARGETS_PER_CYCLE
        targets = targets[:JOBS_ENRICH_MAX_TARGETS_PER_CYCLE]
        log_event(
            "jobs_enrichment_batch_limited",
            selected=len(targets),
            deferred=deferred_targets,
            total_candidates=len(targets) + deferred_targets,
            max_targets=JOBS_ENRICH_MAX_TARGETS_PER_CYCLE,
        )

    targets = [article for _index, article in targets]

    if targets and _can_run_async_fetch():
        log_event("enrich_batch_start", articles=len(targets), mode="aiohttp")
        results = asyncio.run(_enrich_articles_async(targets))
        fallback_articles = []
        for article, ok, error in results:
            if ok:
                enriched += 1
                successful_articles.append(
                    {
                        "id": article.get("id"),
                        "url": article.get("url"),
                        "source_name": article.get("source_name"),
                        "source_url": article.get("source_url"),
                    }
                )
                continue
            if article.get("content_fetch_status") == "weak":
                weak += 1
                log_event(
                    "article_enrich_weak",
                    title=article.get("title"),
                    source=article.get("source_name"),
                    source_url=article.get("url"),
                    chars=article.get("full_article_text_chars"),
                    error=error,
                )
                continue
            fallback_articles.append((article, error))

        if fallback_articles:
            log_event("enrich_fallback_start", articles=len(fallback_articles), mode="requests")

        for article, original_error in fallback_articles:
            ok, error = enrich_article(article)
            if ok:
                enriched += 1
                successful_articles.append(
                    {
                        "id": article.get("id"),
                        "url": article.get("url"),
                        "source_name": article.get("source_name"),
                        "source_url": article.get("source_url"),
                    }
                )
            elif article.get("content_fetch_status") == "weak":
                weak += 1
                log_event(
                    "article_enrich_weak",
                    title=article.get("title"),
                    source=article.get("source_name"),
                    source_url=article.get("url"),
                    chars=article.get("full_article_text_chars"),
                    error=error or original_error,
                )
            else:
                failed += 1
                _record_enrichment_failure(article, error or original_error)
                failed_articles.append(
                    {
                        "id": article.get("id"),
                        "url": article.get("url"),
                        "title": article.get("title"),
                        "source_name": article.get("source_name"),
                        "source_url": article.get("source_url"),
                        "reason": article.get("content_fetch_error"),
                    }
                )
                log_event(
                    "article_enrich_failed",
                    title=article.get("title"),
                    source=article.get("source_name"),
                    error=article.get("content_fetch_error"),
                )
    else:
        if targets:
            log_event("enrich_batch_start", articles=len(targets), mode="requests")
        for article in targets:
            ok, error = enrich_article(article)
            if ok:
                enriched += 1
                successful_articles.append(
                    {
                        "id": article.get("id"),
                        "url": article.get("url"),
                        "source_name": article.get("source_name"),
                        "source_url": article.get("source_url"),
                    }
                )
            elif article.get("content_fetch_status") == "weak":
                weak += 1
                log_event(
                    "article_enrich_weak",
                    title=article.get("title"),
                    source=article.get("source_name"),
                    source_url=article.get("url"),
                    chars=article.get("full_article_text_chars"),
                    error=error,
                )
            else:
                failed += 1
                _record_enrichment_failure(article, error)
                failed_articles.append(
                    {
                        "id": article.get("id"),
                        "url": article.get("url"),
                        "title": article.get("title"),
                        "source_name": article.get("source_name"),
                        "source_url": article.get("source_url"),
                        "reason": article.get("content_fetch_error"),
                    }
                )
                log_event(
                    "article_enrich_failed",
                    title=article.get("title"),
                    source=article.get("source_name"),
                    error=error,
                )

    if checked:
        save_article_queue(queue)

    return {
        "checked": checked,
        "enriched": enriched,
        "failed": failed,
        "weak": weak,
        "already_enriched": already_enriched,
        "recent_failure_skipped": recent_failure_skipped,
        "deferred_targets": deferred_targets,
        "batch_limit": JOBS_ENRICH_MAX_TARGETS_PER_CYCLE if JOBS_MODE and not force else 0,
        "failed_articles": failed_articles,
        "successful_articles": successful_articles,
        "total_queued": len(articles),
        "force": force,
    }
