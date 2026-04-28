# ============================================================
# Facebook image generator
# ============================================================

import textwrap
from io import BytesIO
from pathlib import Path
import re
import os

import requests

from config import (
    FACEBOOK_FALLBACK_ARTICLE_IMAGE_PATH,
    FACEBOOK_IMAGE_BOX_H,
    FACEBOOK_IMAGE_BOX_W,
    FACEBOOK_IMAGE_BOX_X,
    FACEBOOK_IMAGE_BOX_Y,
    FACEBOOK_IMAGE_OUTPUT_DIR,
    FACEBOOK_IMAGE_SIZE,
    FACEBOOK_IMAGE_TEMPLATE_PATH,
    FACEBOOK_TITLE_BOX_H,
    FACEBOOK_TITLE_BOX_W,
    FACEBOOK_TITLE_BOX_X,
    FACEBOOK_TITLE_BOX_Y,
    FACEBOOK_TITLE_FONT_SIZE,
)
from production_logging import log_event


FONT_PATH = Path("assets/fonts/Cairo-Bold.ttf")
IMAGE_TIMEOUT_SECONDS = 10
MIN_ARTICLE_IMAGE_WIDTH = 360
MIN_ARTICLE_IMAGE_HEIGHT = 220
IMAGE_TITLE_MARGIN = 28
MAX_TITLE_LINES = 2
FACEBOOK_BRAND_TEXT = os.getenv("FACEBOOK_BRAND_TEXT", "CyberOplus").strip() or "CyberOplus"


TECH_TERMS = {
    "ai": "الذكاء الاصطناعي",
    "openai": "OpenAI",
    "gemini": "Gemini",
    "google": "Google",
    "github": "GitHub",
    "microsoft": "Microsoft",
    "windows": "Windows",
    "linux": "Linux",
    "android": "Android",
    "telegram": "Telegram",
    "discord": "Discord",
}


def _load_template():
    from PIL import Image

    if not FACEBOOK_IMAGE_TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"missing template: {FACEBOOK_IMAGE_TEMPLATE_PATH}")
    image = Image.open(FACEBOOK_IMAGE_TEMPLATE_PATH).convert("RGBA")
    if image.size == (FACEBOOK_IMAGE_SIZE, FACEBOOK_IMAGE_SIZE):
        return image
    log_event(
        "facebook_template_resized",
        original_width=image.width,
        original_height=image.height,
        target_size=FACEBOOK_IMAGE_SIZE,
    )
    return _cover(image, (FACEBOOK_IMAGE_SIZE, FACEBOOK_IMAGE_SIZE))


def _load_fallback_image():
    from PIL import Image, ImageDraw

    if FACEBOOK_FALLBACK_ARTICLE_IMAGE_PATH.exists():
        log_event("facebook_image_fallback_used", reason="article image unavailable")
        return Image.open(FACEBOOK_FALLBACK_ARTICLE_IMAGE_PATH).convert("RGBA"), True

    log_event(
        "facebook_image_fallback_generated",
        reason="fallback asset missing",
        path=str(FACEBOOK_FALLBACK_ARTICLE_IMAGE_PATH),
    )
    image = Image.new("RGBA", (FACEBOOK_IMAGE_SIZE, FACEBOOK_IMAGE_SIZE), (18, 28, 44, 255))
    draw = ImageDraw.Draw(image)
    for index in range(0, FACEBOOK_IMAGE_SIZE, 36):
        color = (26 + (index // 36) % 30, 52, 76, 255)
        draw.line((0, index, FACEBOOK_IMAGE_SIZE, index - FACEBOOK_IMAGE_SIZE), fill=color, width=18)
    return image, True


def _load_article_image(image_url):
    from PIL import Image

    if image_url:
        try:
            response = requests.get(
                image_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=IMAGE_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            image = Image.open(BytesIO(response.content)).convert("RGBA")
            if image.width < MIN_ARTICLE_IMAGE_WIDTH or image.height < MIN_ARTICLE_IMAGE_HEIGHT:
                log_event(
                    "facebook_image_article_too_small",
                    width=image.width,
                    height=image.height,
                )
                return _load_fallback_image()
            log_event("facebook_image_article_loaded", width=image.width, height=image.height)
            return image, False
        except Exception as error:
            log_event("facebook_image_article_download_failed", error=error.__class__.__name__)

    return _load_fallback_image()


def _cover(image, size):
    from PIL import Image

    target_w, target_h = size
    scale = max(target_w / image.width, target_h / image.height)
    resized = image.resize((int(image.width * scale), int(image.height * scale)), Image.LANCZOS)
    left = max(0, (resized.width - target_w) // 2)
    top = max(0, (resized.height - target_h) // 2)
    return resized.crop((left, top, left + target_w, top + target_h))


def _font(size):
    from PIL import ImageFont

    absolute_font = Path(__file__).resolve().parents[1] / FONT_PATH
    if absolute_font.exists():
        return ImageFont.truetype(str(absolute_font), size=size)
    log_event("facebook_image_font_missing", path=absolute_font)
    try:
        return ImageFont.truetype("arial.ttf", size=size)
    except OSError:
        return ImageFont.load_default()


def _is_arabic(text):
    return bool(re.search(r"[\u0600-\u06FF]", text or ""))


def _clean_title_text(title):
    cleaned = re.sub(r"https?://\S+", "", str(title or ""))
    cleaned = re.sub(r"#[\w\u0600-\u06FF_]+", "", cleaned)
    cleaned = re.sub(r"[|:؛،,!؟?()\[\]{}<>]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _clean_overlay_text(text):
    cleaned = _clean_title_text(text)
    words = cleaned.split()
    if len(words) > 10:
        cleaned = " ".join(words[:10])
    return cleaned


def _brand_from_title(title):
    lower = str(title or "").casefold()
    for token, display in TECH_TERMS.items():
        if token in lower:
            return display
    match = re.search(r"\b([A-Z][A-Za-z0-9]{2,})\b", str(title or ""))
    return match.group(1) if match else ""


def _generate_image_title(title):
    cleaned = _clean_title_text(title)
    if not cleaned:
        log_event("facebook_image_title_fallback_used", reason="empty title")
        return "خبر تقني جديد يستحق الانتباه الآن"

    lower = cleaned.casefold()
    brand = _brand_from_title(cleaned)

    if any(word in lower for word in ("cve", "malware", "ransomware", "breach", "vulnerability", "security", "phishing")):
        hook = f"تنبيه أمني مهم قد يؤثر على مستخدمي {brand}" if brand else "تنبيه أمني مهم قد يؤثر على المستخدمين"
    elif any(word in lower for word in ("ai", "openai", "gemini", "llm", "model", "chatgpt")):
        hook = f"ميزة جديدة من {brand} تغيّر تجربة الذكاء الاصطناعي" if brand else "تطور جديد يغيّر تجربة الذكاء الاصطناعي"
    elif any(word in lower for word in ("app", "android", "ios", "windows", "software", "tool")):
        hook = f"أداة جديدة من {brand} تجعل الاستخدام اليومي أسهل" if brand else "أداة جديدة تجعل الاستخدام اليومي أسهل"
    elif brand:
        hook = f"خطوة جديدة من {brand} تستحق الانتباه"
    elif _is_arabic(cleaned):
        words = cleaned.split()
        hook = " ".join(words[:10])
    else:
        hook = "خبر تقني جديد يستحق الانتباه الآن"

    words = hook.split()
    if len(words) > 10:
        hook = " ".join(words[:10])
    if len(words) < 6 and brand:
        hook = f"{hook} في عالم التقنية"

    if hook.strip() == cleaned.strip():
        hook = f"ما وراء هذا الخبر التقني المهم"

    log_event("facebook_image_title_generated", image_title=hook)
    return hook


def _wrap_title(title, draw, font, max_width):
    words = str(title or "").strip().split()
    if not words:
        return []
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    if len(lines) > MAX_TITLE_LINES:
        kept = lines[:MAX_TITLE_LINES]
        kept[-1] = kept[-1].rstrip(" .") + "..."
        return kept
    return lines


def _draw_text(draw, position, line, font, fill):
    try:
        draw.text(position, line, font=font, fill=fill, anchor="mm", direction="rtl" if _is_arabic(line) else None)
    except Exception:
        draw.text(position, line, font=font, fill=fill, anchor="mm")


def _draw_title(base, title, prepared=False):
    from PIL import Image, ImageDraw, ImageFilter

    draw = ImageDraw.Draw(base)
    title = _clean_overlay_text(title) if prepared else _generate_image_title(title)

    for size in range(FACEBOOK_TITLE_FONT_SIZE, 27, -3):
        font = _font(size)
        lines = _wrap_title(title, draw, font, FACEBOOK_TITLE_BOX_W)
        line_height = int(size * 1.35)
        if lines and len(lines) <= MAX_TITLE_LINES and len(lines) * line_height <= FACEBOOK_TITLE_BOX_H:
            break
    else:
        font = _font(28)
        lines = textwrap.wrap(title, width=28)[:MAX_TITLE_LINES]
        line_height = 38

    total_h = len(lines) * line_height
    min_title_y = FACEBOOK_IMAGE_BOX_Y + FACEBOOK_IMAGE_BOX_H + IMAGE_TITLE_MARGIN
    title_box_y = max(FACEBOOK_TITLE_BOX_Y, min_title_y)
    y = title_box_y + max(0, (FACEBOOK_TITLE_BOX_H - total_h) // 2)
    center_x = FACEBOOK_TITLE_BOX_X + FACEBOOK_TITLE_BOX_W // 2
    for line in lines:
        shadow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_layer)
        _draw_text(shadow_draw, (center_x + 3, y + line_height // 2 + 3), line, font, (0, 0, 0, 150))
        base.alpha_composite(shadow_layer.filter(ImageFilter.GaussianBlur(2)))
        _draw_text(draw, (center_x, y + line_height // 2), line, font, (255, 255, 255, 255))
        y += line_height


def _draw_brand(base):
    from PIL import ImageDraw

    draw = ImageDraw.Draw(base)
    font = _font(34)
    padding_x = 42
    padding_y = 32
    label = FACEBOOK_BRAND_TEXT
    bbox = draw.textbbox((0, 0), label, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x = FACEBOOK_IMAGE_SIZE - padding_x - width / 2
    y = FACEBOOK_IMAGE_SIZE - padding_y - height / 2
    draw.rounded_rectangle(
        (
            int(x - width / 2 - 18),
            int(y - height / 2 - 12),
            int(x + width / 2 + 18),
            int(y + height / 2 + 12),
        ),
        radius=14,
        fill=(0, 0, 0, 120),
    )
    _draw_text(draw, (x, y), label, font, (255, 255, 255, 245))


def generate_facebook_image(title, image_url, output_path, hook_text=""):
    """
    Generate a Facebook image from the article image, optional template overlay,
    a short Arabic title/hook, and a small brand mark.
    Returns a dict with ok/path/used_fallback/error for logging and tests.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    FACEBOOK_IMAGE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        article_image, used_fallback = _load_article_image(image_url)
        base = _cover(article_image, (FACEBOOK_IMAGE_SIZE, FACEBOOK_IMAGE_SIZE))
        from PIL import Image, ImageFilter

        base = base.filter(ImageFilter.GaussianBlur(1.2))
        shade = Image.new("RGBA", base.size, (0, 0, 0, 92))
        base.alpha_composite(shade)
        try:
            template = _load_template()
            if template.getextrema()[3][0] < 255:
                base.alpha_composite(template)
        except Exception as template_error:
            log_event("facebook_template_overlay_skipped", error=template_error.__class__.__name__)

        article_card = _cover(article_image, (FACEBOOK_IMAGE_BOX_W, FACEBOOK_IMAGE_BOX_H))
        image_x = max(0, (FACEBOOK_IMAGE_SIZE - FACEBOOK_IMAGE_BOX_W) // 2)
        image_y = FACEBOOK_IMAGE_BOX_Y
        base.alpha_composite(article_card, (image_x, image_y))
        overlay_text = _clean_overlay_text(hook_text) or title
        _draw_title(base, overlay_text, prepared=bool(_clean_overlay_text(hook_text)))
        _draw_brand(base)
        base.convert("RGB").save(output_path, "JPEG", quality=90, optimize=True)
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise RuntimeError("empty generated facebook image")
        log_event(
            "facebook_image_generated_successfully",
            path=str(output_path),
            used_fallback=used_fallback,
        )
        return {"ok": True, "path": str(output_path), "used_fallback": used_fallback, "error": ""}
    except Exception as error:
        log_event("facebook_image_generation_failed", error=error.__class__.__name__)
        return {"ok": False, "path": "", "used_fallback": False, "error": str(error)}
