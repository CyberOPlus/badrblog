# ============================================================
# Facebook image generator
# ============================================================

import textwrap
import json
import random
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
    JOBS_MODE,
    JOB_VISUAL_STATE_PATH,
)
from production_logging import log_event


FONT_PATH = Path("assets/fonts/Firjar-ExtraBold.ttf")
FIRJAR_FONT_URL = (
    "https://raw.githubusercontent.com/Mestaratype/Firjar/main/fonts/variable/"
    "Firjar%5Bwdth%2Cwght%5D.ttf"
)
_FIRJAR_FONT_BYTES = None
_FIRJAR_FONT_DOWNLOAD_FAILED = False
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

    global _FIRJAR_FONT_BYTES, _FIRJAR_FONT_DOWNLOAD_FAILED

    absolute_font = Path(__file__).resolve().parents[1] / FONT_PATH
    if absolute_font.exists():
        return ImageFont.truetype(str(absolute_font), size=size)

    # Do not commit font binaries to the repository. Fetch Firjar at runtime and
    # keep it in memory for this process. This guarantees the Jobs cards use the
    # requested Firjar family while keeping the repo clean.
    if _FIRJAR_FONT_BYTES is None and not _FIRJAR_FONT_DOWNLOAD_FAILED:
        try:
            response = requests.get(
                FIRJAR_FONT_URL,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            response.raise_for_status()
            if len(response.content) < 20_000:
                raise RuntimeError("Firjar font download was unexpectedly small")
            _FIRJAR_FONT_BYTES = response.content
        except Exception as error:
            _FIRJAR_FONT_DOWNLOAD_FAILED = True
            log_event("firjar_font_download_failed", error=error.__class__.__name__)

    if _FIRJAR_FONT_BYTES:
        try:
            font = ImageFont.truetype(BytesIO(_FIRJAR_FONT_BYTES), size=size)
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
                    values.append(
                        min(max(800.0, minimum), maximum)
                        if "weight" in str(name).lower()
                        else default
                    )
                if values:
                    font.set_variation_by_axes(values)
            except Exception:
                pass
            return font
        except Exception as error:
            log_event("firjar_font_load_failed", error=error.__class__.__name__)

    for fallback in (
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "arial.ttf",
    ):
        try:
            return ImageFont.truetype(fallback, size=size)
        except OSError:
            continue
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
        draw.text(
            position,
            line,
            font=font,
            fill=fill,
            anchor="mm",
            direction="rtl" if _is_arabic(line) else None,
            language="ar" if _is_arabic(line) else None,
        )
        return
    except Exception:
        pass

    fallback_line = line
    if _is_arabic(line):
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display
            fallback_line = get_display(arabic_reshaper.reshape(line))
        except Exception:
            fallback_line = line
    draw.text(position, fallback_line, font=font, fill=fill, anchor="mm")


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


JOB_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "assets" / "facebook"
JOB_TEMPLATE_FILES = (
    JOB_TEMPLATE_DIR / "job-new-orange.png",
    JOB_TEMPLATE_DIR / "job-deadline-yellow.png",
    JOB_TEMPLATE_DIR / "job-alert-blue.png",
    JOB_TEMPLATE_DIR / "job-apply-red.png",
)
JOB_ARTICLE_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "article" / "article img.png"
)
JOB_ARTICLE_TEMPLATE_FALLBACK_PATH = JOB_ARTICLE_TEMPLATE_PATH


def _job_template_index():
    state = {}
    try:
        state = json.loads(JOB_VISUAL_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        state = {}

    try:
        last = int(state.get("last_background_index"))
    except (TypeError, ValueError):
        last = -1

    available = [index for index in range(len(JOB_TEMPLATE_FILES)) if index != last]
    index = random.SystemRandom().choice(available or list(range(len(JOB_TEMPLATE_FILES))))
    JOB_VISUAL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    JOB_VISUAL_STATE_PATH.write_text(
        json.dumps(
            {
                "last_background_index": index,
                "last_background_file": JOB_TEMPLATE_FILES[index].name,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return index


JOB_FACEBOOK_OUTPUT_SIZE = (1080, 1350)
JOB_SQUARE_TEMPLATE_SIZE = (1254, 1254)
JOB_SQUARE_SPLIT_Y = 350


def _normalize_job_template(image):
    """Keep the uploaded square artwork intact while producing the 1080x1350 Facebook card."""
    from PIL import Image

    if image.size == JOB_FACEBOOK_OUTPUT_SIZE:
        return image

    if image.size == JOB_SQUARE_TEMPLATE_SIZE:
        target_w, target_h = JOB_FACEBOOK_OUTPUT_SIZE
        square = image.resize((target_w, target_w), Image.LANCZOS)

        # The templates are mostly white with only the side frame in this band.
        # Insert vertical breathing room here instead of stretching the artwork:
        # the top Cybero+ mark keeps its proportions, the side status icon moves
        # beside the job title area, and the footer stays anchored to the bottom.
        split_y = min(JOB_SQUARE_SPLIT_Y, square.height - 1)
        insert_h = target_h - square.height
        output = Image.new("RGBA", JOB_FACEBOOK_OUTPUT_SIZE, (255, 255, 255, 255))
        output.paste(square.crop((0, 0, target_w, split_y)), (0, 0))

        seam = square.crop((0, split_y - 1, target_w, split_y))
        seam = seam.resize((target_w, insert_h), Image.NEAREST)
        output.paste(seam, (0, split_y))
        output.paste(
            square.crop((0, split_y, target_w, square.height)),
            (0, split_y + insert_h),
        )

        log_event(
            "facebook_job_template_normalized",
            source_width=image.width,
            source_height=image.height,
            output_width=target_w,
            output_height=target_h,
        )
        return output

    raise RuntimeError(
        "unsupported job Facebook template size: "
        f"{image.width}x{image.height}; expected 1254x1254 source or 1080x1350"
    )


def _load_job_template():
    from PIL import Image

    index = _job_template_index()
    path = JOB_TEMPLATE_FILES[index]
    if not path.exists():
        raise FileNotFoundError(f"missing job Facebook template: {path}")
    image = Image.open(path).convert("RGBA")
    return _normalize_job_template(image), index


def _load_job_logo(image_url):
    from PIL import Image

    if not image_url:
        return None
    try:
        response = requests.get(
            image_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=IMAGE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").casefold()
        if "svg" in content_type or str(image_url).casefold().endswith(".svg"):
            return None
        image = Image.open(BytesIO(response.content)).convert("RGBA")
        if image.width < 24 or image.height < 16:
            return None
        return image
    except Exception as error:
        log_event("facebook_job_logo_download_failed", error=error.__class__.__name__)
        return None


def _contain(image, max_size):
    from PIL import Image

    max_w, max_h = max_size
    scale = min(max_w / max(1, image.width), max_h / max(1, image.height), 1.0)
    if image.width < max_w * 0.55 and image.height < max_h * 0.55:
        scale = min(max_w / max(1, image.width), max_h / max(1, image.height), 2.6)
    size = (
        max(1, int(image.width * scale)),
        max(1, int(image.height * scale)),
    )
    return image.resize(size, Image.Resampling.LANCZOS)


def _prepare_article_job_logo(image, max_size):
    """Trim transparent padding and scale employer logos for the wide article cover."""
    from PIL import Image

    if image is None:
        return None

    prepared = image.convert("RGBA")
    alpha = prepared.getchannel("A")
    bbox = alpha.getbbox()
    if bbox:
        prepared = prepared.crop(bbox)

    max_w, max_h = max_size
    scale = min(
        max_w / max(1, prepared.width),
        max_h / max(1, prepared.height),
        5.0,
    )
    size = (
        max(1, int(prepared.width * scale)),
        max(1, int(prepared.height * scale)),
    )
    return prepared.resize(size, Image.Resampling.LANCZOS)


def _job_text_bbox(draw, text, font):
    try:
        return draw.textbbox(
            (0, 0),
            text,
            font=font,
            direction="rtl" if _is_arabic(text) else None,
            language="ar" if _is_arabic(text) else None,
        )
    except Exception:
        fallback_text = text
        if _is_arabic(text):
            try:
                import arabic_reshaper
                from bidi.algorithm import get_display
                fallback_text = get_display(arabic_reshaper.reshape(text))
            except Exception:
                pass
        return draw.textbbox((0, 0), fallback_text, font=font)


def _wrap_job_title(title, draw, font, max_width, max_lines=3):
    words = [word for word in _clean_title_text(title).split() if word]
    if not words:
        return ["فرصة عمل جديدة"]
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = _job_text_bbox(draw, candidate, font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .") + "…"
    return lines


def _draw_job_title(base, title):
    from PIL import ImageDraw

    draw = ImageDraw.Draw(base)
    # Keep the title inside the clean central area of the owner template.
    # Very long public-sector headlines get one extra line instead of an ellipsis.
    clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
    long_headline = len(clean_title) > 88
    max_lines = 5 if long_headline else 4
    max_width = 820
    max_height = 350 if long_headline else 300
    box_top = 590 if long_headline else 625
    min_size = 29 if long_headline else 32
    lines = []
    font = _font(60)
    line_height = 76

    for size in range(68, min_size - 1, -3):
        candidate_font = _font(size)
        candidate_lines = _wrap_job_title(
            clean_title,
            draw,
            candidate_font,
            max_width,
            max_lines=max_lines,
        )
        candidate_height = len(candidate_lines) * int(size * 1.26)
        if candidate_lines and candidate_height <= max_height:
            font = candidate_font
            lines = candidate_lines
            line_height = int(size * 1.26)
            break

    if not lines:
        lines = _wrap_job_title(
            clean_title,
            draw,
            font,
            max_width,
            max_lines=max_lines,
        )

    center_x = 505
    start_y = box_top + max(0, (max_height - len(lines) * line_height) // 2)
    for line in lines:
        _draw_text(
            draw,
            (center_x, start_y + line_height // 2),
            line,
            font,
            (24, 24, 24, 255),
        )
        start_y += line_height


def _draw_job_logo_or_fallback(base, image_url, fallback_text):
    from PIL import ImageDraw

    logo = _load_job_logo(image_url)
    if logo is not None:
        logo = _contain(logo, (510, 270))
        x = 505 - logo.width // 2
        y = 420 - logo.height // 2
        base.alpha_composite(logo, (x, y))
        return True

    fallback = _clean_overlay_text(fallback_text)
    if fallback:
        draw = ImageDraw.Draw(base)
        font = _font(42)
        lines = _wrap_job_title(fallback, draw, font, 600, max_lines=2)
        y = 385
        for line in lines:
            _draw_text(draw, (505, y), line, font, (45, 45, 45, 255))
            y += 58
    return False


def _generate_job_facebook_image(title, image_url, output_path, hook_text=""):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    FACEBOOK_IMAGE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        base, template_index = _load_job_template()
        logo_loaded = _draw_job_logo_or_fallback(base, image_url, hook_text)
        _draw_job_title(base, title)
        base.convert("RGB").save(output_path, "JPEG", quality=95, optimize=True, subsampling=0)
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise RuntimeError("empty generated jobs Facebook image")
        log_event(
            "facebook_job_image_generated",
            path=str(output_path),
            template_index=template_index,
            logo_loaded=logo_loaded,
            width=base.width,
            height=base.height,
        )
        return {
            "ok": True,
            "path": str(output_path),
            "used_fallback": not logo_loaded,
            "error": "",
            "template_index": template_index,
        }
    except Exception as error:
        log_event("facebook_job_image_generation_failed", error=error.__class__.__name__)
        return {"ok": False, "path": "", "used_fallback": False, "error": str(error)}


def generate_job_article_cover(
    title,
    image_url,
    output_path,
    employer_name="",
    template_path=None,
):
    """Render the Blogger/article cover from the owner-supplied template.

    The template may use any sensible landscape size. Logo/title placement is
    relative to the canvas so changing 1200x675 to 1280x720 does not distort it.
    """
    from PIL import Image, ImageDraw

    template_path = Path(template_path or JOB_ARTICLE_TEMPLATE_PATH)
    if not template_path.exists() and JOB_ARTICLE_TEMPLATE_FALLBACK_PATH.exists():
        template_path = JOB_ARTICLE_TEMPLATE_FALLBACK_PATH
    output_path = Path(output_path)
    if not template_path.exists():
        return {
            "ok": False,
            "path": "",
            "error": f"missing article template: {template_path}",
        }

    try:
        base = Image.open(template_path).convert("RGBA")
        width, height = base.size
        if width < 800 or height < 450:
            raise RuntimeError(
                f"article template is too small: {width}x{height}; use at least 800x450"
            )

        # Logo: upper-middle. Trim transparent source padding first so the visible
        # employer mark is large enough without changing the Facebook renderer.
        logo = _load_job_logo(image_url)
        logo_center_x = int(width * 0.50)
        logo_center_y = int(height * 0.32)
        logo_max = (int(width * 0.40), int(height * 0.18))
        if logo is not None:
            logo = _prepare_article_job_logo(logo, logo_max)
            base.alpha_composite(
                logo,
                (
                    logo_center_x - logo.width // 2,
                    logo_center_y - logo.height // 2,
                ),
            )
        elif employer_name:
            draw = ImageDraw.Draw(base)
            font = _font(max(28, int(width * 0.038)))
            lines = _wrap_job_title(
                employer_name,
                draw,
                font,
                int(width * 0.56),
                max_lines=2,
            )
            line_height = max(38, int(width * 0.050))
            y = logo_center_y - ((len(lines) - 1) * line_height) // 2
            for line in lines:
                _draw_text(
                    draw,
                    (logo_center_x, y),
                    line,
                    font,
                    (40, 40, 40, 255),
                )
                y += line_height

        # Title: lower-middle. Dynamic size handles short/medium/long Arabic,
        # French and mixed titles without touching footer/edge branding.
        draw = ImageDraw.Draw(base)
        # Give long Arabic competition/result headlines more breathing room.
        # The box stays clear of the right brand strip and bottom domain line.
        max_width = int(width * 0.82)
        max_height = int(height * 0.28)
        title_center_x = int(width * 0.50)
        title_top = int(height * 0.53)
        lines = []
        font = _font(max(34, int(width * 0.050)))
        line_height = max(44, int(width * 0.060))
        start_size = max(42, int(width * 0.060))
        min_size = max(25, int(width * 0.029))
        for size in range(start_size, min_size - 1, -3):
            candidate_font = _font(size)
            candidate_lines = _wrap_job_title(
                title,
                draw,
                candidate_font,
                max_width,
                max_lines=4,
            )
            candidate_height = len(candidate_lines) * int(size * 1.26)
            if candidate_lines and candidate_height <= max_height:
                font = candidate_font
                lines = candidate_lines
                line_height = int(size * 1.26)
                break

        if not lines:
            lines = _wrap_job_title(title, draw, font, max_width, max_lines=4)

        total_height = len(lines) * line_height
        y = title_top + max(0, (max_height - total_height) // 2)
        for line in lines:
            _draw_text(
                draw,
                (title_center_x, y + line_height // 2),
                line,
                font,
                (24, 24, 24, 255),
            )
            y += line_height

        output_path.parent.mkdir(parents=True, exist_ok=True)
        base.convert("RGB").save(
            output_path,
            "JPEG",
            quality=95,
            optimize=True,
            subsampling=0,
        )
        return {
            "ok": True,
            "path": str(output_path),
            "error": "",
            "width": width,
            "height": height,
            "logo_loaded": logo is not None,
        }
    except Exception as error:
        log_event("job_article_cover_generation_failed", error=error.__class__.__name__)
        return {"ok": False, "path": "", "error": str(error)}


def generate_facebook_image(title, image_url, output_path, hook_text=""):
    if JOBS_MODE:
        return _generate_job_facebook_image(title, image_url, output_path, hook_text=hook_text)

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
