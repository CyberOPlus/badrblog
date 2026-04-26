# ============================================================
# article_enricher.py - Phase 3 Ready Article Enrichment
# ============================================================

import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from article_queue import load_article_queue, save_article_queue
from config import HEADERS

CONTENT_FETCH_STATUSES = {"success", "failed"}
REQUEST_TIMEOUT_SECONDS = 15
PREVIEW_MIN_CHARS = 800
PREVIEW_MAX_CHARS = 1200
MAX_ARTICLE_IMAGES = 5
MAX_TRUSTED_REFERENCES = 5

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
    if image_url:
        return urljoin(article_url, image_url)

    for selector in ("article img[src]", "main img[src]"):
        img = soup.select_one(selector)
        if img and img.get("src"):
            return urljoin(article_url, img.get("src"))

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

    for paragraph in container.find_all(["p", "li"], limit=30):
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


def _source_url(article):
    explicit = article.get("source_url")
    if explicit:
        return explicit

    parsed = urlparse(article.get("url", ""))
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def enrich_article(article):
    url = article.get("url", "").strip()
    if not url:
        return False, "missing url"

    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.Timeout:
        return False, "timeout"
    except requests.exceptions.RequestException as error:
        return False, str(error)

    if response.status_code >= 400:
        return False, f"http {response.status_code}"

    soup = BeautifulSoup(response.text, "html.parser")
    preview = _extract_content_preview(soup)
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
    article["content_preview"] = preview
    article["source_url"] = _source_url(article)
    article["content_fetched_at"] = _now_iso()
    article["content_fetch_status"] = "success"
    article.pop("content_fetch_error", None)
    return True, ""


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
    already_enriched = 0

    for article in articles:
        allowed_statuses = {"ready"} if not force else {"ready", "selected", "draft_created"}
        if article.get("status") not in allowed_statuses:
            continue

        checked += 1
        if article.get("content_fetch_status") == "success" and not force:
            already_enriched += 1
            continue

        ok, error = enrich_article(article)
        if ok:
            enriched += 1
        else:
            failed += 1
            article["content_fetch_status"] = "failed"
            article["content_fetch_error"] = error
            article["content_fetched_at"] = _now_iso()

    if checked:
        save_article_queue(queue)

    return {
        "checked": checked,
        "enriched": enriched,
        "failed": failed,
        "already_enriched": already_enriched,
        "total_queued": len(articles),
        "force": force,
    }
