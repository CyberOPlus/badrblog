# ============================================================
# image_extractor.py - Advanced Image Extraction Pipeline
# ============================================================

import re
import time
from urllib.parse import urljoin, urlparse
from typing import Optional, Dict, List, Tuple
from datetime import datetime

import requests
from bs4 import BeautifulSoup, NavigableString

from config import HEADERS
from production_logging import log_event

# Image extraction configuration
MIN_IMAGE_WIDTH = 360
MIN_IMAGE_HEIGHT = 220
IMAGE_REQUEST_TIMEOUT = 10
IMAGE_DOWNLOAD_TIMEOUT = 5
IMAGE_DOWNLOAD_RETRIES = 3
IMAGE_ASPECT_RATIO_MIN = 0.6  # width/height >= 0.6
IMAGE_ASPECT_RATIO_MAX = 2.0  # width/height <= 2.0

# Blocked image hints (logos, avatars, icons, etc.)
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
    "social",
    "share",
    "comment",
    "advertisement",
    "ad-",
    "banner",
)

BLOCKED_IMAGE_CLASSES = (
    "logo",
    "avatar",
    "icon",
    "favicon",
    "author-image",
    "profile-pic",
    "share-",
    "social-",
    "ad-",
)

# Social media domains to skip
SOCIAL_MEDIA_DOMAINS = {
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "linkedin.com",
    "tiktok.com",
    "pinterest.com",
    "reddit.com",
}


def _get_soup(html_content: str) -> Optional[BeautifulSoup]:
    """Safely parse HTML content."""
    try:
        return BeautifulSoup(html_content, "html.parser")
    except Exception as error:
        log_event("image_extraction_html_parse_error", error=str(error))
        return None


def _is_absolute_url(url: str) -> bool:
    """Check if URL is absolute."""
    return bool(url and urlparse(url).scheme in {"http", "https"})


def _make_absolute_url(url: str, base_url: str) -> Optional[str]:
    """Convert relative URL to absolute."""
    if not url:
        return None
    if _is_absolute_url(url):
        return url
    try:
        return urljoin(base_url, url)
    except Exception:
        return None


def _is_blocked_image(src: str, classes: List[str], alt_text: str = "") -> bool:
    """Check if image should be skipped (logo, avatar, icon, etc.)."""
    combined = " ".join(classes + [src.lower(), alt_text.lower()])
    
    # Check blocked hints
    if any(hint in combined for hint in BLOCKED_IMAGE_HINTS):
        return True
    
    # Check blocked classes
    if any(blocked_class in " ".join(classes).lower() for blocked_class in BLOCKED_IMAGE_CLASSES):
        return True
    
    # Check for social media image sources
    parsed = urlparse(src.lower())
    if any(social_domain in parsed.netloc for social_domain in SOCIAL_MEDIA_DOMAINS):
        return True
    
    return False


def _get_image_dimensions(url: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Fetch image dimensions by making a request.
    Returns (width, height) or (None, None) if unable to determine.
    """
    try:
        response = requests.head(
            url,
            headers=HEADERS,
            timeout=IMAGE_REQUEST_TIMEOUT,
            allow_redirects=True
        )
        response.raise_for_status()
        
        # Try to get dimensions from Content-Length or other headers
        content_length = int(response.headers.get('Content-Length', 0))
        if content_length < 5000:  # Less than 5KB is likely too small
            return None, None
        
        # If we have image dimensions in headers, use them
        # Otherwise, we'll need to download and parse the image
        # For now, return None to indicate unknown dimensions
        return None, None
    except Exception:
        return None, None


def _is_valid_image_dimensions(width: Optional[int], height: Optional[int]) -> bool:
    """Check if image dimensions meet minimum requirements."""
    # If dimensions are unknown, assume valid (will validate later)
    if width is None or height is None:
        return True
    
    # Check minimum size
    if width < MIN_IMAGE_WIDTH or height < MIN_IMAGE_HEIGHT:
        return False
    
    # Check aspect ratio
    aspect_ratio = width / height if height > 0 else 1.0
    if aspect_ratio < IMAGE_ASPECT_RATIO_MIN or aspect_ratio > IMAGE_ASPECT_RATIO_MAX:
        return False
    
    return True


def _safe_positive_int(value) -> Optional[int]:
    """Convert value to positive integer or return None."""
    try:
        if not value:
            return None
        num = int(value)
        return num if num > 0 else None
    except (TypeError, ValueError):
        return None


def _extract_json_ld_image(soup: BeautifulSoup) -> Optional[str]:
    """Extract image from JSON-LD schema."""
    try:
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            if not script.string:
                continue
            try:
                import json
                data = json.loads(script.string)
                
                # Handle both dict and list structures
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict):
                        # Check for image field
                        image = item.get("image")
                        if image:
                            if isinstance(image, str):
                                return image
                            elif isinstance(image, dict):
                                return image.get("url")
                            elif isinstance(image, list) and image:
                                first_image = image[0]
                                if isinstance(first_image, str):
                                    return first_image
                                elif isinstance(first_image, dict):
                                    return first_image.get("url")
            except Exception:
                continue
    except Exception:
        pass
    return None


def _extract_srcset_image(img_tag) -> Optional[str]:
    """Extract image URL from srcset or data-srcset."""
    for attr in ["srcset", "data-srcset"]:
        srcset = img_tag.get(attr)
        if not srcset:
            continue
        
        # Parse srcset: "url1 1x, url2 2x" or "url1 100w, url2 200w"
        sources = srcset.split(",")
        if sources:
            # Take the highest resolution or largest width
            best_url = sources[-1].strip().split()[0]
            if best_url:
                return best_url
    return None


def _extract_meta_image(soup: BeautifulSoup) -> Optional[str]:
    """Extract image from meta tags in priority order."""
    # Priority order for meta tags
    selectors = [
        ("meta[property='og:image']", "content"),
        ("meta[name='twitter:image']", "content"),
        ("meta[property='twitter:image']", "content"),
        ("meta[property='image']", "content"),
        ("meta[name='image']", "content"),
    ]
    
    for selector, attr in selectors:
        element = soup.select_one(selector)
        if element and element.get(attr):
            return element.get(attr)
    
    return None


def _extract_article_images(soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
    """
    Extract images from article content.
    Returns list of dicts with 'url', 'alt', 'method' keys.
    """
    images = []
    seen = set()
    
    # Find article content containers
    article_selectors = [
        "article",
        "main",
        "[class*='content']",
        "[class*='article']",
        "[class*='post']",
    ]
    
    for selector in article_selectors:
        container = soup.select_one(selector)
        if not container:
            continue
        
        # Find images within this container
        for img in container.find_all("img", recursive=True):
            src = img.get("src") or img.get("data-src") or _extract_srcset_image(img)
            if not src:
                continue
            
            # Make URL absolute
            absolute_url = _make_absolute_url(src, base_url)
            if not absolute_url:
                continue
            if absolute_url in seen:
                continue
            
            # Get dimensions
            width = _safe_positive_int(img.get("width"))
            height = _safe_positive_int(img.get("height"))
            
            # Get classes and alt text
            classes = img.get("class") or []
            alt_text = img.get("alt") or ""
            
            # Check if image is blocked
            if _is_blocked_image(absolute_url, classes, alt_text):
                continue
            
            # Check dimensions
            if not _is_valid_image_dimensions(width, height):
                continue
            
            seen.add(absolute_url)
            images.append({
                "url": absolute_url,
                "alt": alt_text,
                "width": width,
                "height": height,
                "method": "article_content",
            })
    
    return images


def extract_images(soup_or_html, article_url: str) -> List[Dict[str, str]]:
    """
    Public backward-compatible image extraction helper.
    Returns dictionaries with url, alt, and source keys.
    """
    if isinstance(soup_or_html, BeautifulSoup):
        soup = soup_or_html
    else:
        soup = _get_soup(str(soup_or_html or ""))
    if not soup or not article_url:
        return []

    images = []
    seen = set()

    def add_image(url, source, alt=""):
        absolute_url = _make_absolute_url(url, article_url)
        if not absolute_url or absolute_url in seen:
            return
        if _is_blocked_image(absolute_url, [], alt):
            return
        seen.add(absolute_url)
        images.append({"url": absolute_url, "alt": alt or "", "source": source})

    for source, selector in (
        ("og", "meta[property='og:image'], meta[property='og:image:url'], meta[property='og:image:secure_url']"),
        ("twitter", "meta[name='twitter:image'], meta[property='twitter:image'], meta[name='twitter:image:src']"),
        ("meta", "meta[name='image'], meta[itemprop='image'], meta[property='image']"),
    ):
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            add_image(tag.get("content"), source)

    json_ld_image = _extract_json_ld_image(soup)
    if json_ld_image:
        add_image(json_ld_image, "jsonld")

    for img_data in _extract_article_images(soup, article_url):
        if img_data["url"] in seen:
            continue
        seen.add(img_data["url"])
        images.append(
            {
                "url": img_data["url"],
                "alt": img_data.get("alt") or "",
                "source": img_data.get("method") or "article_content",
            }
        )

    return images


def download_image_with_retry(url: str, timeout: int = IMAGE_DOWNLOAD_TIMEOUT, retries: int = IMAGE_DOWNLOAD_RETRIES) -> bool:
    """
    Verify an image is downloadable with bounded retries.
    The pipeline keeps the remote URL; this downloads only enough to prove it works.
    """
    if not url or not _is_absolute_url(url):
        log_event("image_download_failed", url_domain="", error="invalid_url")
        return False

    last_error = ""
    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout, stream=True)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and "image" not in content_type:
                raise ValueError("non_image_content_type")

            content_length = int(response.headers.get("Content-Length") or 0)
            if content_length and content_length < 1024:
                raise ValueError("image_too_small")

            first_chunk = next(response.iter_content(chunk_size=1024), b"")
            if not first_chunk and not content_length:
                raise ValueError("empty_image_response")

            log_event(
                "image_download_success",
                url_domain=urlparse(url).netloc,
                attempt=attempt,
            )
            response.close()
            return True
        except Exception as error:
            last_error = error.__class__.__name__
            log_event(
                "image_download_failed",
                url_domain=urlparse(url).netloc,
                attempt=attempt,
                error=last_error,
            )
            try:
                response.close()
            except Exception:
                pass
            if attempt < attempts:
                time.sleep(min(1, attempt * 0.25))

    return False


def extract_main_image(
    html_content: str,
    article_url: str,
    article_title: str = ""
) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract main image from HTML content following priority order:
    
    1. og:image
    2. twitter:image
    3. meta[property="image"] or meta[name="image"]
    4. JSON-LD image
    5. srcset / data-srcset
    6. data-src / lazy images
    7. images inside article/content
    8. largest suitable image
    9. fallback only on complete failure
    
    Returns:
        (image_url, extraction_method) or (None, None) if not found
    """
    if not html_content or not article_url:
        return None, None
    
    soup = _get_soup(html_content)
    if not soup:
        return None, None
    
    # 1. Try meta tags (og:image, twitter:image, etc.)
    meta_image = _extract_meta_image(soup)
    if meta_image:
        absolute_url = _make_absolute_url(meta_image, article_url)
        if absolute_url and not _is_blocked_image(absolute_url, [], article_title):
            log_event(
                "image_extraction_found",
                method="meta_tag",
                url_domain=urlparse(absolute_url).netloc
            )
            return absolute_url, "og:image_or_twitter"
    
    # 2. Try JSON-LD
    json_ld_image = _extract_json_ld_image(soup)
    if json_ld_image:
        absolute_url = _make_absolute_url(json_ld_image, article_url)
        if absolute_url and not _is_blocked_image(absolute_url, [], article_title):
            log_event(
                "image_extraction_found",
                method="json_ld",
                url_domain=urlparse(absolute_url).netloc
            )
            return absolute_url, "json_ld"
    
    # 3-8. Extract images from article content
    article_images = _extract_article_images(soup, article_url)
    
    # Try srcset/data-srcset images first
    for img_data in article_images:
        if img_data["method"] in ["srcset", "data_src"]:
            log_event(
                "image_extraction_found",
                method=img_data["method"],
                url_domain=urlparse(img_data["url"]).netloc
            )
            return img_data["url"], img_data["method"]
    
    # Then try regular article images
    if article_images:
        # Prefer larger images
        best_image = max(
            article_images,
            key=lambda img: (img.get("width") or 0) * (img.get("height") or 0)
        )
        log_event(
            "image_extraction_found",
            method="article_content",
            url_domain=urlparse(best_image["url"]).netloc
        )
        return best_image["url"], "article_content"
    
    # Nothing found
    log_event("image_extraction_failed", article_url=article_url[:100])
    return None, None


def extract_extra_images(
    html_content: str,
    article_url: str,
    main_image_url: Optional[str] = None,
    limit: int = 3
) -> List[Dict[str, str]]:
    """
    Extract additional article images (up to `limit`).
    Excludes main_image_url and blocked images.
    
    Returns list of dicts with 'url', 'alt' keys.
    """
    if not html_content or not article_url or limit <= 0:
        return []
    
    soup = _get_soup(html_content)
    if not soup:
        return []
    
    article_images = _extract_article_images(soup, article_url)
    
    # Filter out main image and keep only up to limit
    extra_images = []
    for img_data in article_images:
        if len(extra_images) >= limit:
            break
        
        # Skip main image
        if main_image_url and img_data["url"].lower() == main_image_url.lower():
            continue
        
        extra_images.append({
            "url": img_data["url"],
            "alt": img_data.get("alt") or "صورة توضيحية",
        })
    
    return extra_images


def validate_image_url(url: str, timeout: int = 10) -> bool:
    """
    Validate that an image URL is accessible and valid.
    Returns True if image is accessible and appropriate size.
    """
    if not url or not _is_absolute_url(url):
        return False
    
    try:
        response = requests.head(
            url,
            headers=HEADERS,
            timeout=timeout,
            allow_redirects=True
        )
        response.raise_for_status()
        
        # Check content type
        content_type = response.headers.get("Content-Type", "").lower()
        if not any(image_type in content_type for image_type in ["image/", "image"]):
            return False
        
        # Check file size (between 5KB and 50MB)
        try:
            content_length = int(response.headers.get("Content-Length", 0))
            if content_length < 5000 or content_length > 50_000_000:
                return False
        except (TypeError, ValueError):
            pass
        
        return True
    except Exception as error:
        log_event(
            "image_validation_failed",
            url_domain=urlparse(url).netloc,
            error=error.__class__.__name__
        )
        return False
