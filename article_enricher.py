# ============================================================
# article_enricher.py - Phase 3 Ready Article Enrichment
# ============================================================

import asyncio
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from article_queue import load_article_queue, save_article_queue
from config import ARTICLE_TIMEOUT_SECONDS, FAST_NEWS_MODE, HEADERS, SOURCE_RETRY_DELAY_SECONDS, MAX_SOURCE_RETRIES
from production_logging import elapsed_ms, log_event

try:
    import aiohttp
except ImportError:
    aiohttp = None

CONTENT_FETCH_STATUSES = {"success", "failed"}
REQUEST_TIMEOUT_SECONDS = ARTICLE_TIMEOUT_SECONDS
PREVIEW_MIN_CHARS = 3500
PREVIEW_MAX_CHARS = 9000
STRONG_ARTICLE_MIN_CHARS = 3500
WEAK_ARTICLE_MIN_CHARS = 1200
MAX_ARTICLE_IMAGES = 5
MAX_TRUSTED_REFERENCES = 5
ASYNC_FETCH_CONCURRENCY = 8
FETCH_RETRIES = MAX_SOURCE_RETRIES
FAST_ENRICH_MIN_CHARS = 600

TRUSTED_REFERENCE_HOSTS = (
    "microsoft.com",
    "google.com",
    "cloud.google.com",
    "mandiant.com",
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


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _normalize_text(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def _meta_content(soup, *selectors):
    for selector in selectors:
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            return _normalize_text(tag.get("content"))
    return ""


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


def _extract_main_image(soup, article_url):
    image_url = _meta_content(
        soup,
        "meta[property='og:image']",
        "meta[name='twitter:image']",
        "meta[name='twitter:image:src']",
    )
    if image_url and _looks_useful_image(urljoin(article_url, image_url), ""):
        return urljoin(article_url, image_url)

    for selector in ("article img[src]", "main img[src]"):
        img = soup.select_one(selector)
        if img and img.get("src"):
            candidate = urljoin(article_url, img.get("src"))
            if _looks_useful_image(candidate, img.get("alt") or "", img=img):
                return candidate

    return ""


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
        if width and height and (width < 120 or height < 80):
            return False

    return True


def _append_image(images, seen, url, alt="", source="article/img", img=None):
    if not _looks_useful_image(url, alt, img=img):
        return
    if url in seen:
        return
    seen.add(url)
    images.append(
        {
            "url": url,
            "alt": _normalize_text(alt),
            "source": source,
        }
    )


def _extract_article_images(soup, article_url):
    images = []
    seen = set()

    og_image = _meta_content(
        soup,
        "meta[property='og:image']",
        "meta[name='twitter:image']",
        "meta[name='twitter:image:src']",
    )
    if og_image:
        _append_image(images, seen, urljoin(article_url, og_image), source="og:image")

    container = _best_article_container(soup)
    for img in container.select("img[src], img[data-src], img[data-lazy-src]"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if not src:
            continue
        url = urljoin(article_url, src)
        alt = img.get("alt") or img.get("title") or ""
        _append_image(images, seen, url, alt=alt, source="article/img", img=img)
        if len(images) >= MAX_ARTICLE_IMAGES:
            break

    return images[:MAX_ARTICLE_IMAGES]


def _trusted_reference_allowed(url, source_url):
    host = urlparse(url).netloc.lower()
    source_host = urlparse(source_url or "").netloc.lower()
    if source_host and host == source_host:
        return any(trusted in host for trusted in TRUSTED_REFERENCE_HOSTS)
    return any(trusted in host for trusted in TRUSTED_REFERENCE_HOSTS)


def _extract_trusted_references(soup, article_url, source_url):
    references = []
    seen = set()
    container = _best_article_container(soup)

    for link in container.select("a[href]"):
        href = urljoin(article_url, (link.get("href") or "").strip())
        if not href.startswith(("http://", "https://")):
            continue
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
    return soup


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
    parts = []

    for paragraph in container.find_all(["p", "li"], limit=100):
        text = _normalize_text(paragraph.get_text(" ", strip=True))
        if len(text) < 40:
            continue
        parts.append(text)
        if len(" ".join(parts)) >= PREVIEW_MAX_CHARS:
            break

    if not parts:
        fallback = _normalize_text(container.get_text(" ", strip=True))
        return _trim_preview(fallback)

    return _trim_preview(" ".join(parts))


def _extract_full_article_text(soup):
    clean = _clean_soup(soup)
    container = _best_article_container(clean)
    parts = []

    for node in container.find_all(["p", "li", "h2", "h3"], limit=180):
        text = _normalize_text(node.get_text(" ", strip=True))
        if len(text) < 30:
            continue
        parts.append(text)

    full_text = _normalize_text(" ".join(parts))
    if len(full_text) >= WEAK_ARTICLE_MIN_CHARS:
        return full_text

    fallback = _normalize_text(container.get_text(" ", strip=True))
    if len(fallback) > len(full_text):
        return fallback
    return full_text


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
    full_text = _extract_full_article_text(soup)
    preview = _trim_preview(full_text) if full_text else _extract_content_preview(soup)
    if not preview:
        return False, "missing article body"

    article["fetched_title"] = _extract_title(soup) or article.get("title", "")
    article["meta_description"] = _extract_meta_description(soup)
    article_images = _extract_article_images(soup, url)
    article["article_images"] = article_images
    article["main_image"] = article_images[0]["url"] if article_images else _extract_main_image(soup, url)
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
    min_success_chars = FAST_ENRICH_MIN_CHARS if FAST_NEWS_MODE else WEAK_ARTICLE_MIN_CHARS
    is_strong = len(article["full_article_text"]) >= STRONG_ARTICLE_MIN_CHARS
    is_weak = len(article["full_article_text"]) >= min_success_chars
    article["enrichment_status"] = "strong" if is_strong else "weak"
    article["content_fetch_status"] = "success" if (is_strong or (FAST_NEWS_MODE and is_weak)) else "weak"
    article.pop("content_fetch_error", None)
    log_event(
        "article_enriched",
        title=article.get("fetched_title") or article.get("title"),
        source=article.get("source_name"),
        source_url=url,
        chars=len(article["full_article_text"]),
        preview_chars=len(preview),
        images=len(article_images),
        references=len(article.get("trusted_references") or []),
        enrichment_status=article["enrichment_status"],
    )
    if not is_weak:
        return False, f"weak article body ({len(article['full_article_text'])} chars)"
    if not is_strong and not FAST_NEWS_MODE:
        return False, f"weak article body ({len(article['full_article_text'])} chars)"
    return True, ""


def _apply_rss_summary_fallback(article):
    summary = _normalize_text(article.get("rss_summary", ""))
    if len(summary) < 300:
        return False, "missing article body"
    article["fetched_title"] = article.get("title", "")
    article["meta_description"] = summary[:240]
    article["article_images"] = []
    article["main_image"] = ""
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
        "article_enriched_from_rss_summary",
        title=article.get("title"),
        source=article.get("source_name"),
        chars=len(summary),
    )
    return True, ""


def enrich_article(article):
    url = article.get("url", "").strip()
    if not url:
        return False, "missing url"

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
    targets = []

    for article in articles:
        allowed_statuses = {"ready"} if not force else {"ready", "selected", "draft_created"}
        if article.get("status") not in allowed_statuses:
            continue

        checked += 1
        if article.get("content_fetch_status") == "success" and not force:
            already_enriched += 1
            continue

        targets.append(article)

    if targets and _can_run_async_fetch():
        log_event("enrich_batch_start", articles=len(targets), mode="aiohttp")
        results = asyncio.run(_enrich_articles_async(targets))
        fallback_articles = []
        for article, ok, error in results:
            if ok:
                enriched += 1
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
                article["content_fetch_status"] = "failed"
                article["content_fetch_error"] = error or original_error
                article["content_fetched_at"] = _now_iso()
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
                article["content_fetch_status"] = "failed"
                article["content_fetch_error"] = error
                article["content_fetched_at"] = _now_iso()
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
        "total_queued": len(articles),
        "force": force,
    }
