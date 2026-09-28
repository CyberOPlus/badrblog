#!/usr/bin/env python3
"""High-quality image selection for Cybero Plus Jobs.

Priority:
1) A relevant image explicitly supplied with the normalized job candidate.
2) The best usable image from the official/canonical job source.
3) A branded owner-supplied fallback card.

Images are validated before use so broken, tiny, logo/icon/avatar and tracking
assets are not selected accidentally.
"""

from __future__ import annotations

import io
import json
import re
import zlib
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
import arabic_reshaper
from bidi.algorithm import get_display
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageEnhance, ImageFilter


TIMEOUT = 25
MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024
MIN_WIDTH = 640
MIN_HEIGHT = 320
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

BAD_IMAGE_HINTS = (
    "logo", "icon", "avatar", "favicon", "sprite", "emoji", "badge",
    "tracking", "pixel", "spacer", "placeholder", "loader", "adsystem",
    "doubleclick", "gravatar",
)


@dataclass
class ImageAsset:
    url: str
    content: bytes
    mime: str
    width: int
    height: int
    filename: str
    origin: str
    score: float


def _clean_url(url: str, base: str = "") -> str:
    url = (url or "").strip().replace("&amp;", "&")
    if not url:
        return ""
    return urljoin(base, url)


def _bad_url(url: str) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in BAD_IMAGE_HINTS)


def _filename(url: str, mime: str) -> str:
    name = urlparse(url).path.rsplit("/", 1)[-1].split("?", 1)[0].strip()
    if not name or "." not in name:
        ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(mime, ".jpg")
        return "cyberoplus-image" + ext
    return name[-120:]


def _image_score(width: int, height: int, size: int, origin: str) -> float:
    area = width * height
    ratio = width / max(height, 1)

    score = float(area)
    if width >= 1200:
        score *= 1.35
    if width >= 1600:
        score *= 1.15
    if 1.2 <= ratio <= 2.1:
        score *= 1.18
    if size >= 120_000:
        score *= 1.08
    if origin == "telegram":
        score *= 1.25
    return score


def fetch_image(url: str, origin: str, referer: str = "") -> ImageAsset | None:
    url = _clean_url(url)
    if not url or _bad_url(url):
        return None

    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer

    try:
        with requests.get(url, headers=headers, timeout=TIMEOUT, stream=True) as response:
            response.raise_for_status()
            content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
            if content_type and not content_type.startswith("image/"):
                return None

            chunks = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    return None
                chunks.append(chunk)

        content = b"".join(chunks)
        minimum_bytes = 4_000 if origin == "telegram" else 12_000
        if len(content) < minimum_bytes:
            return None

        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            detected = Image.MIME.get(image.format or "", "") or content_type or "image/jpeg"

        minimum_width = 240 if origin == "telegram" else MIN_WIDTH
        minimum_height = 160 if origin == "telegram" else MIN_HEIGHT
        if width < minimum_width or height < minimum_height:
            return None

        ratio = width / max(height, 1)
        if ratio < 0.35 or ratio > 3.5:
            return None

        return ImageAsset(
            url=url,
            content=content,
            mime=detected,
            width=width,
            height=height,
            filename=_filename(url, detected),
            origin=origin,
            score=_image_score(width, height, len(content), origin),
        )
    except Exception:
        return None


def _srcset_best(srcset: str, base: str) -> list[str]:
    candidates: list[tuple[int, str]] = []
    for part in (srcset or "").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = _clean_url(bits[0], base)
        width = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                width = int(bits[1][:-1])
            except ValueError:
                pass
        candidates.append((width, url))
    return [url for _, url in sorted(candidates, reverse=True) if url]


def _jsonld_images(value: Any, base: str) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"image", "thumbnailUrl", "contentUrl"}:
                if isinstance(child, str):
                    found.append(_clean_url(child, base))
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, str):
                            found.append(_clean_url(item, base))
                        elif isinstance(item, dict):
                            found.extend(_jsonld_images(item, base))
                elif isinstance(child, dict):
                    found.extend(_jsonld_images(child, base))
            else:
                found.extend(_jsonld_images(child, base))
    elif isinstance(value, list):
        for item in value:
            found.extend(_jsonld_images(item, base))
    return found


def telegram_image_candidates(post_url: str) -> list[str]:
    """Refresh attached Telegram photo URLs from the stable message URL."""
    if not post_url:
        return []

    try:
        response = requests.get(
            post_url,
            params={"embed": "1", "single": "1"},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    urls: list[str] = []

    for media_node in soup.select(
        ".tgme_widget_message_photo_wrap, .tgme_widget_message_service_photo"
    ):
        style = str(media_node.get("style", ""))
        match = re.search(
            r"background-image\s*:\s*url\(['\"]?(.*?)['\"]?\)",
            style,
        )
        if match:
            url = _clean_url(match.group(1), response.url)
            if url and url not in urls:
                urls.append(url)

        for image in media_node.select("img[src]"):
            url = _clean_url(str(image.get("src", "")), response.url)
            if url and url.startswith(("http://", "https://")) and url not in urls:
                urls.append(url)

    return urls


def source_image_candidates(source_url: str) -> list[str]:
    if not source_url:
        return []

    try:
        response = requests.get(source_url, headers=HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    base = response.url
    candidates: list[str] = []

    def add(url: str) -> None:
        clean = _clean_url(url, base)
        if clean and clean.startswith(("http://", "https://")) and not _bad_url(clean):
            if clean not in candidates:
                candidates.append(clean)

    # Highest-confidence article hero metadata first.
    for selector, attr in (
        ('meta[property="og:image:secure_url"]', "content"),
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('meta[name="twitter:image:src"]', "content"),
        ('link[rel="image_src"]', "href"),
    ):
        for node in soup.select(selector):
            add(str(node.get(attr, "")))

    # Structured data often contains a larger original image than og:image.
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or "")
        except Exception:
            continue
        for url in _jsonld_images(data, base):
            add(url)

    # Then article/body images, preferring the largest srcset variants.
    selectors = (
        "article img", "main img", ".article img", ".post img", ".entry-content img",
        ".content img", "figure img",
    )
    for selector in selectors:
        for image in soup.select(selector):
            for url in _srcset_best(str(image.get("srcset", "")), base):
                add(url)
            add(str(image.get("data-src", "")))
            add(str(image.get("data-lazy-src", "")))
            add(str(image.get("src", "")))

    return candidates[:30]


def resolve_post_images(
    telegram_urls: list[str],
    source_url: str,
    max_images: int = 10,
) -> tuple[list[ImageAsset], dict[str, Any]]:
    """Return validated high-quality images plus diagnostics."""

    diagnostics: dict[str, Any] = {
        "telegram_candidates": len(telegram_urls),
        "source_fallback_used": False,
        "source_candidates": 0,
        "selected": [],
    }

    telegram_assets: list[ImageAsset] = []
    for url in telegram_urls[:max_images * 2]:
        asset = fetch_image(url, "telegram")
        if asset:
            telegram_assets.append(asset)

    # Respect the Telegram post's own visuals when they exist and are usable.
    if telegram_assets:
        telegram_assets.sort(key=lambda asset: asset.score, reverse=True)
        chosen = telegram_assets[:max_images]
        diagnostics["selected"] = [
            {
                "origin": a.origin,
                "width": a.width,
                "height": a.height,
                "url": a.url,
            }
            for a in chosen
        ]
        return chosen, diagnostics

    # No usable Telegram image: inspect the original external source.
    if source_url:
        diagnostics["source_fallback_used"] = True
        candidates = source_image_candidates(source_url)
        diagnostics["source_candidates"] = len(candidates)

        assets: list[ImageAsset] = []
        for url in candidates[:20]:
            asset = fetch_image(url, "source", referer=source_url)
            if asset:
                assets.append(asset)

        if assets:
            # Only one source image: choose the best article hero, not a gallery dump.
            best = max(assets, key=lambda asset: asset.score)
            diagnostics["selected"] = [{
                "origin": best.origin,
                "width": best.width,
                "height": best.height,
                "url": best.url,
            }]
            return [best], diagnostics

    return [], diagnostics


def normalize_for_facebook(asset: ImageAsset) -> ImageAsset:
    """Preserve original quality whenever possible; normalize unsupported/huge images."""

    if asset.mime in {"image/jpeg", "image/png"} and len(asset.content) <= 10 * 1024 * 1024:
        return asset

    with Image.open(io.BytesIO(asset.content)) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode not in {"RGB", "L"}:
            background = Image.new("RGB", image.size, "white")
            if "A" in image.getbands():
                background.paste(image, mask=image.getchannel("A"))
            else:
                background.paste(image)
            image = background
        else:
            image = image.convert("RGB")

        # Improve Facebook presentation without pretending to recover lost detail:
        # small-but-valid images are resized cleanly; large ones are tamed.
        if image.width < 1200 and image.width >= 240:
            scale = min(1200 / image.width, 2.0)
            target_width = round(image.width * scale)
            target_height = round(image.height * scale)
            image = image.resize(
                (target_width, target_height),
                Image.Resampling.LANCZOS,
            )
            image = ImageEnhance.Sharpness(image).enhance(1.08)
        elif image.width > 3000:
            new_height = round(image.height * (3000 / image.width))
            image = image.resize((3000, new_height), Image.Resampling.LANCZOS)

        output = io.BytesIO()
        image.save(output, format="JPEG", quality=96, optimize=True, subsampling=0)
        content = output.getvalue()

    return ImageAsset(
        url=asset.url,
        content=content,
        mime="image/jpeg",
        width=image.width,
        height=image.height,
        filename="cyberoplus-jobs-high-quality.jpg",
        origin=asset.origin,
        score=asset.score,
    )


ROOT = Path(__file__).resolve().parent
CARD_BACKGROUND_FILES = (
    ROOT / "assets" / "job1.png",
    ROOT / "assets" / "job2.png",
    ROOT / "assets" / "job3.png",
    ROOT / "assets" / "job4.png",
)
# Cairo is fetched only when a generated fallback card is needed. If the
# network fetch fails, rendering falls back to DejaVu instead of blocking a post.
CAIRO_FONT_URL = (
    "https://raw.githubusercontent.com/google/fonts/main/ofl/cairo/"
    "Cairo%5Bslnt%2Cwght%5D.ttf"
)
_CAIRO_FONT_BYTES: bytes | None = None
_CAIRO_FONT_DOWNLOAD_FAILED = False

CARD_TEXT_COLORS = (
    (248, 248, 248),  # white
    (255, 213, 74),   # warm yellow
    (88, 196, 255),   # light blue
    (255, 107, 107),  # light red
)

DIRECTION_CONTROLS = "\u2066\u2067\u2068\u2069\u200e\u200f"


def _strip_direction_controls(text: str) -> str:
    value = str(text or "")
    for mark in DIRECTION_CONTROLS:
        value = value.replace(mark, "")
    return value


def _rtl_display(text: str) -> str:
    """Legacy fallback for Pillow builds without libraqm."""
    value = _strip_direction_controls(text).strip()
    if not value:
        return ""
    try:
        return get_display(arabic_reshaper.reshape(value))
    except Exception:
        return value


def _variant_index(title: str, variant_key: int | str | None) -> int:
    if variant_key is not None:
        try:
            return int(variant_key) % 4
        except (TypeError, ValueError):
            pass
    return zlib.crc32(str(title or "").encode("utf-8")) % 4


def _load_card_background(index: int) -> Image.Image:
    """Load one of the four owner-supplied card templates directly."""
    try:
        path = CARD_BACKGROUND_FILES[index % len(CARD_BACKGROUND_FILES)]
        with Image.open(path) as img:
            return img.convert("RGB").resize((1600, 900), Image.Resampling.LANCZOS)
    except Exception:
        colors = ((25, 132, 255), (255, 45, 45), (245, 245, 245), (255, 204, 0))
        image = Image.new("RGB", (1600, 900), (4, 4, 4))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (34, 82, 1566, 825),
            radius=90,
            outline=colors[index % 4],
            width=8,
        )
        return image


def _get_cairo_font_bytes() -> bytes | None:
    global _CAIRO_FONT_BYTES, _CAIRO_FONT_DOWNLOAD_FAILED

    if _CAIRO_FONT_BYTES is not None:
        return _CAIRO_FONT_BYTES
    if _CAIRO_FONT_DOWNLOAD_FAILED:
        return None

    try:
        response = requests.get(
            CAIRO_FONT_URL,
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=15,
        )
        response.raise_for_status()
        content = response.content
        if len(content) < 20_000:
            raise RuntimeError("Cairo font download was unexpectedly small.")
        _CAIRO_FONT_BYTES = content
        return content
    except Exception:
        _CAIRO_FONT_DOWNLOAD_FAILED = True
        return None


def _set_heavy_variation(font: ImageFont.FreeTypeFont) -> None:
    """Prefer an ExtraBold-like Cairo weight when the variable font exposes axes."""
    try:
        axes = font.get_variation_axes()
        values = []
        for axis in axes:
            name = axis.get("name", b"")
            if isinstance(name, bytes):
                name = name.decode("utf-8", "ignore")
            minimum = float(axis.get("minimum", 0))
            maximum = float(axis.get("maximum", 1000))
            default = float(axis.get("default", minimum))
            lowered = str(name).lower()
            if "weight" in lowered:
                values.append(min(max(800.0, minimum), maximum))
            elif "slant" in lowered:
                values.append(min(max(0.0, minimum), maximum))
            else:
                values.append(default)
        if values:
            font.set_variation_by_axes(values)
    except Exception:
        pass


def _load_title_font(size: int) -> tuple[ImageFont.FreeTypeFont, bool]:
    font_bytes = _get_cairo_font_bytes()
    if font_bytes:
        try:
            font = ImageFont.truetype(io.BytesIO(font_bytes), size=size)
            _set_heavy_variation(font)
            return font, True
        except Exception:
            pass

    for fallback in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(fallback, size=size), False
        except OSError:
            continue
    return ImageFont.load_default(), False


def _native_bbox(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
) -> tuple[int, int, int, int] | None:
    try:
        return draw.textbbox(
            (0, 0),
            text,
            font=font,
            direction="rtl",
            language="ar",
        )
    except Exception:
        return None


def _line_metrics(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
) -> tuple[int, int, bool]:
    """Measure exactly the RTL layout that will be drawn."""
    clean = _strip_direction_controls(text).strip()
    bbox = _native_bbox(draw, clean, font)
    if bbox is not None:
        return max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1]), True

    legacy = _rtl_display(clean)
    bbox = draw.textbbox((0, 0), legacy, font=font)
    return max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1]), False


def _split_words(words: list[str], cuts: tuple[int, ...]) -> list[str]:
    points = (0,) + cuts + (len(words),)
    return [
        " ".join(words[points[i] : points[i + 1]])
        for i in range(len(points) - 1)
    ]


def _balanced_lines(
    draw: ImageDraw.ImageDraw,
    words: list[str],
    font: ImageFont.FreeTypeFont,
    max_width: int,
    line_count: int,
) -> tuple[list[str], list[int], list[int], bool] | None:
    if not words or line_count < 1 or line_count > len(words):
        return None

    best = None
    for cuts in combinations(range(1, len(words)), line_count - 1):
        lines = _split_words(words, cuts)
        widths: list[int] = []
        heights: list[int] = []
        native = True
        valid = True

        for line in lines:
            width, height, line_native = _line_metrics(draw, line, font)
            if width > max_width:
                valid = False
                break
            widths.append(width)
            heights.append(height)
            native = native and line_native

        if not valid:
            continue

        average = sum(widths) / len(widths)
        balance = sum(abs(width - average) for width in widths)
        last_line_penalty = max(0.0, average * 0.58 - widths[-1]) * 1.7
        score = balance + last_line_penalty

        if best is None or score < best[0]:
            best = (score, lines, widths, heights, native)

    if best is None:
        return None
    return best[1], best[2], best[3], best[4]


def _fit_title(
    draw: ImageDraw.ImageDraw,
    title: str,
    max_width: int,
    max_height: int,
) -> tuple[ImageFont.FreeTypeFont, list[str], list[int], list[int], int, bool]:
    words = [word for word in title.split() if word]
    if not words:
        words = ["Cybero Plus"]

    # New AI headlines are <= 11 words. The wider cap keeps older queued data safe.
    words = words[:16]

    for size in range(118, 63, -4):
        font, _ = _load_title_font(size)
        line_gap = max(18, round(size * 0.24))

        for line_count in range(1, min(4, len(words)) + 1):
            layout = _balanced_lines(draw, words, font, max_width, line_count)
            if layout is None:
                continue
            lines, widths, heights, native = layout
            total_height = sum(heights) + line_gap * (len(lines) - 1)
            if total_height <= max_height:
                return font, lines, widths, heights, line_gap, native

    font, _ = _load_title_font(62)
    lines = [" ".join(words)]
    width, height, native = _line_metrics(draw, lines[0], font)
    return font, lines, [width], [height], 16, native


def _draw_title_line(
    draw: ImageDraw.ImageDraw,
    *,
    right_x: int,
    y: int,
    line: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    native_rtl: bool,
) -> None:
    clean = _strip_direction_controls(line).strip()

    if native_rtl:
        try:
            draw.text(
                (right_x, y),
                clean,
                font=font,
                fill=fill,
                direction="rtl",
                language="ar",
                anchor="rt",
                stroke_width=1,
                stroke_fill=(0, 0, 0),
            )
            return
        except Exception:
            pass

    legacy = _rtl_display(clean)
    bbox = draw.textbbox((0, 0), legacy, font=font)
    line_width = max(1, bbox[2] - bbox[0])
    draw.text(
        (right_x - line_width, y),
        legacy,
        font=font,
        fill=fill,
        stroke_width=1,
        stroke_fill=(0, 0, 0),
    )


def build_branded_fallback_asset(
    title: str = "",
    variant_key: int | str | None = None,
) -> ImageAsset:
    """Render a large RTL Cairo headline on one of four owner-supplied frames."""
    clean_title = re.sub(
        r"\s+",
        " ",
        _strip_direction_controls(title).strip(),
    )
    if not clean_title:
        clean_title = "فرصة عمل جديدة"

    index = _variant_index(clean_title, variant_key)
    image = _load_card_background(index)
    draw = ImageDraw.Draw(image)

    # Keep the headline fully inside the black reading area and away from the logo.
    left_x = 145
    right_x = 1460
    top_y = 200
    bottom_y = 760
    max_width = right_x - left_x
    max_height = bottom_y - top_y

    font, lines, widths, heights, line_gap, native_rtl = _fit_title(
        draw,
        clean_title,
        max_width,
        max_height,
    )

    total_height = sum(heights) + line_gap * (len(lines) - 1)
    y = top_y + max(0, (max_height - total_height) // 2)
    text_color = CARD_TEXT_COLORS[index]

    for line, height in zip(lines, heights):
        _draw_title_line(
            draw,
            right_x=right_x,
            y=y,
            line=line,
            font=font,
            fill=text_color,
            native_rtl=native_rtl,
        )
        y += height + line_gap

    output = io.BytesIO()
    image.save(output, format="JPEG", quality=96, optimize=True, subsampling=0)
    content = output.getvalue()

    names = ("blue", "red", "white", "yellow")
    return ImageAsset(
        url=f"generated://cyberoplus-jobs-card/{names[index]}",
        content=content,
        mime="image/jpeg",
        width=image.width,
        height=image.height,
        filename=f"cyberoplus-jobs-card-{names[index]}.jpg",
        origin="generated_fallback",
        score=float(image.width * image.height),
    )


def resolve_job_images(source_url: str, provided_urls: list[str] | None = None):
    """Jobs-friendly wrapper around the proven image resolver."""
    return resolve_post_images(provided_urls or [], source_url, max_images=1)
