# ============================================================
# Facebook image generator
# ============================================================

from functools import lru_cache
from io import BytesIO
from pathlib import Path
import re

import requests

from config import (
    FACEBOOK_IMAGE_OUTPUT_DIR,
    JOBS_MODE,
)
from production_logging import log_event
from job_visual_policy import (
    DEFAULT_JOB_TEMPLATE_KEY,
    JOB_TEMPLATE_FILES_BY_KEY,
    JOB_TEMPLATE_KEYS,
    template_filename,
)


FONT_PATH = Path("assets/fonts/Firjar-ExtraBold.ttf")
FIRJAR_FONT_URL = (
    "https://raw.githubusercontent.com/Mestaratype/Firjar/main/fonts/variable/"
    "Firjar%5Bwdth%2Cwght%5D.ttf"
)
_FIRJAR_FONT_BYTES = None
_FIRJAR_FONT_DOWNLOAD_FAILED = False
IMAGE_TIMEOUT_SECONDS = 10
MAX_JOB_LOGO_BYTES = 3_000_000


def _apply_font_variations(font, weight=800, width=100):
    try:
        axes = font.get_variation_axes()
        values = []
        for axis in axes:
            name = axis.get("name", b"")
            if isinstance(name, bytes):
                name = name.decode("utf-8", "ignore")
            name = str(name).casefold()
            minimum = float(axis.get("minimum", 0))
            maximum = float(axis.get("maximum", 1000))
            default = float(axis.get("default", minimum))
            target = default
            if "weight" in name:
                target = float(weight)
            elif "width" in name:
                target = float(width)
            values.append(min(max(target, minimum), maximum))
        if values:
            font.set_variation_by_axes(values)
    except Exception:
        pass
    return font


@lru_cache(maxsize=96)
def _font(size, width=100, weight=800):
    from PIL import ImageFont

    global _FIRJAR_FONT_BYTES, _FIRJAR_FONT_DOWNLOAD_FAILED

    size = max(8, int(size))
    width = max(75, min(125, int(width)))
    weight = max(100, min(900, int(weight)))

    absolute_font = Path(__file__).resolve().parents[1] / FONT_PATH
    if absolute_font.exists():
        try:
            return _apply_font_variations(
                ImageFont.truetype(str(absolute_font), size=size),
                weight=weight,
                width=width,
            )
        except Exception:
            pass

    # Do not commit font binaries to the repository. Fetch Firjar at runtime and
    # keep it in memory for this process. Jobs cards use the same bilingual family
    # for Arabic and Latin/French text.
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
            return _apply_font_variations(
                ImageFont.truetype(BytesIO(_FIRJAR_FONT_BYTES), size=size),
                weight=weight,
                width=width,
            )
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


JOB_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "assets" / "facebook"
JOB_TEMPLATE_FILES = tuple(JOB_TEMPLATE_DIR / JOB_TEMPLATE_FILES_BY_KEY[key] for key in JOB_TEMPLATE_KEYS)
JOB_ARTICLE_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "article" / "article img.png"
)
JOB_ARTICLE_TEMPLATE_FALLBACK_PATH = JOB_ARTICLE_TEMPLATE_PATH


JOB_FACEBOOK_OUTPUT_SIZE = (1080, 1350)
JOB_SQUARE_TEMPLATE_SIZE = (1254, 1254)
JOB_SQUARE_SPLIT_Y = 350

# Safe geometry for all four owner-supplied templates. The right-side semantic
# icon occupies the outer strip, so readable content stays inside this column.
JOB_CONTENT_CENTER_X = 505
JOB_CONTENT_LEFT = 92
JOB_CONTENT_RIGHT = 910
JOB_LOGO_TOP = 175
JOB_LOGO_BOTTOM = 545
JOB_TITLE_TOP = 610
JOB_TITLE_BOTTOM = 1105
JOB_LOGO_TITLE_GAP = 58
JOB_TITLE_MIN_SIZE = 48
JOB_TITLE_MAX_SIZE = 88
JOB_TITLE_MAX_LINES = 4
JOB_TITLE_WIDTH_AXES = (100, 96, 92, 90)


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


def _load_job_template(template_key=""):
    from PIL import Image

    key = str(template_key or DEFAULT_JOB_TEMPLATE_KEY).strip().lower()
    if key not in JOB_TEMPLATE_FILES_BY_KEY:
        key = DEFAULT_JOB_TEMPLATE_KEY
    path = JOB_TEMPLATE_DIR / template_filename(key)
    if not path.exists():
        raise FileNotFoundError(f"missing job Facebook template: {path}")
    image = Image.open(path).convert("RGBA")
    return _normalize_job_template(image), key


def _logo_visible_on_white(image):
    """Reject effectively invisible white/transparent marks on white templates."""
    sample = image.copy().convert("RGBA")
    sample.thumbnail((256, 256))
    visible = 0
    distinguishable = 0
    for red, green, blue, alpha in sample.getdata():
        if alpha < 40:
            continue
        visible += 1
        brightness = (red + green + blue) / 3
        chroma = max(red, green, blue) - min(red, green, blue)
        if brightness < 242 or chroma > 10:
            distinguishable += 1
    if visible < 20:
        return False
    return (distinguishable / visible) >= 0.03


def _safe_svg_to_rgba(content):
    """Rasterize a self-contained SVG without allowing remote/file references."""
    from PIL import Image

    lowered = content[:200_000].lower()
    if b"<script" in lowered or re.search(rb"\bon\w+\s*=", lowered):
        raise ValueError("unsafe SVG scripting")
    if re.search(
        rb"(?:href|xlink:href)\s*=\s*[\"']\s*(?:https?:|//|file:)",
        lowered,
        flags=re.I,
    ):
        raise ValueError("external SVG reference")
    if re.search(
        rb"url\(\s*[\"']?(?:https?:|//|file:)",
        lowered,
        flags=re.I,
    ):
        raise ValueError("external SVG CSS reference")

    import cairosvg

    png = cairosvg.svg2png(
        bytestring=content,
        output_width=1200,
        unsafe=False,
    )
    return Image.open(BytesIO(png)).convert("RGBA")


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
        length_header = response.headers.get("content-length")
        try:
            if length_header and int(length_header) > MAX_JOB_LOGO_BYTES:
                raise ValueError("employer logo is too large")
        except ValueError:
            if length_header and str(length_header).isdigit():
                raise
        content = response.content
        if not content or len(content) > MAX_JOB_LOGO_BYTES:
            raise ValueError("employer logo is empty or too large")

        content_type = (response.headers.get("content-type") or "").casefold()
        sample = content[:4096].lstrip().lower()
        is_svg = (
            "svg" in content_type
            or str(image_url).casefold().split("?", 1)[0].endswith(".svg")
            or b"<svg" in sample
        )
        if is_svg:
            image = _safe_svg_to_rgba(content)
        else:
            image = Image.open(BytesIO(content)).convert("RGBA")

        if image.width < 24 or image.height < 16:
            raise ValueError("employer logo is too small")
        if image.width > 12000 or image.height > 12000:
            raise ValueError("employer logo dimensions are too large")
        if not _logo_visible_on_white(image):
            log_event("facebook_job_logo_invisible_on_white")
            return None
        return image
    except Exception as error:
        log_event("facebook_job_logo_download_failed", error=error.__class__.__name__)
        return None


def _trim_job_logo_padding(image):
    """Crop transparent or obvious near-white canvas padding around a logo."""
    from PIL import Image, ImageChops

    if image is None:
        return None
    prepared = image.convert("RGBA")
    alpha = prepared.getchannel("A")
    bbox = alpha.getbbox()
    if bbox:
        prepared = prepared.crop(bbox)

    # Many official sites publish a logo centered on a large opaque white PNG.
    # Trim only when the canvas is essentially opaque and the content clearly
    # differs from white; never guess against coloured backgrounds.
    try:
        alpha = prepared.getchannel("A")
        alpha_min, _alpha_max = alpha.getextrema()
        if alpha_min >= 250 and prepared.width > 40 and prepared.height > 30:
            rgb = prepared.convert("RGB")
            white = Image.new("RGB", rgb.size, (255, 255, 255))
            diff = ImageChops.difference(rgb, white).convert("L")
            mask = diff.point(lambda value: 255 if value > 18 else 0)
            content_bbox = mask.getbbox()
            if content_bbox:
                left, top, right, bottom = content_bbox
                margin = max(4, int(min(prepared.size) * 0.02))
                left = max(0, left - margin)
                top = max(0, top - margin)
                right = min(prepared.width, right + margin)
                bottom = min(prepared.height, bottom + margin)
                if right - left >= 12 and bottom - top >= 12:
                    prepared = prepared.crop((left, top, right, bottom))
    except Exception:
        pass
    return prepared


def _prepare_article_job_logo(image, max_size):
    """Trim transparent padding and scale employer logos for the wide article cover."""
    from PIL import Image

    prepared = _trim_job_logo_padding(image)
    if prepared is None:
        return None

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


def _prepare_facebook_job_logo(image):
    """Scale the visible employer mark by shape, not by source-canvas size."""
    from PIL import Image

    prepared = _trim_job_logo_padding(image)
    if prepared is None or prepared.width <= 0 or prepared.height <= 0:
        return None

    aspect = prepared.width / max(1, prepared.height)
    if aspect >= 2.6:       # long wordmark
        max_w, max_h = 610, 205
    elif aspect >= 1.45:    # ordinary horizontal brand
        max_w, max_h = 565, 235
    elif aspect >= 0.78:    # square / crest / public institution
        max_w, max_h = 315, 305
    else:                   # vertical mark
        max_w, max_h = 245, 315

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


def _job_text_width(draw, text, font):
    bbox = _job_text_bbox(draw, text, font)
    return max(0, bbox[2] - bbox[0])


def _wrap_job_title(title, draw, font, max_width, max_lines=3, truncate=False):
    words = [word for word in _clean_title_text(title).split() if word]
    if not words:
        return ["فرصة عمل جديدة"]
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _job_text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    if truncate and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .") + "…"
    return lines


def _fit_job_text_layout(
    draw,
    text,
    *,
    max_width,
    max_height,
    max_lines,
    min_size,
    max_size,
    width_axes=JOB_TITLE_WIDTH_AXES,
    line_ratio=1.15,
):
    clean_text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean_text:
        clean_text = "فرصة عمل جديدة"

    for size in range(int(max_size), int(min_size) - 1, -2):
        for width_axis in width_axes:
            font = _font(size, width=width_axis, weight=800)
            lines = _wrap_job_title(
                clean_text,
                draw,
                font,
                max_width,
                max_lines=max_lines,
                truncate=False,
            )
            if not lines or len(lines) > max_lines:
                continue
            widths = [_job_text_width(draw, line, font) for line in lines]
            if not widths or max(widths) > max_width:
                continue
            line_height = max(size + 7, int(round(size * line_ratio)))
            total_height = len(lines) * line_height
            if total_height > max_height:
                continue
            return {
                "font": font,
                "font_size": size,
                "font_width": width_axis,
                "lines": lines,
                "line_height": line_height,
                "total_height": total_height,
                "max_line_width": max(widths),
            }
    return None


def _draw_job_title(base, title, min_top=JOB_TITLE_TOP):
    from PIL import ImageDraw

    draw = ImageDraw.Draw(base)
    title_top = max(JOB_TITLE_TOP, int(min_top))
    title_bottom = JOB_TITLE_BOTTOM
    max_width = JOB_CONTENT_RIGHT - JOB_CONTENT_LEFT
    max_height = max(0, title_bottom - title_top)

    layout = _fit_job_text_layout(
        draw,
        title,
        max_width=max_width,
        max_height=max_height,
        max_lines=JOB_TITLE_MAX_LINES,
        min_size=JOB_TITLE_MIN_SIZE,
        max_size=JOB_TITLE_MAX_SIZE,
        width_axes=JOB_TITLE_WIDTH_AXES,
        line_ratio=1.15,
    )
    if not layout:
        raise RuntimeError(
            "job visual title cannot fit the safe area at a readable font size"
        )

    start_y = title_top + max(0, (max_height - layout["total_height"]) // 2)
    line_boxes = []
    for line in layout["lines"]:
        center_y = start_y + layout["line_height"] // 2
        _draw_text(
            draw,
            (JOB_CONTENT_CENTER_X, center_y),
            line,
            layout["font"],
            (22, 22, 22, 255),
        )
        width = _job_text_width(draw, line, layout["font"])
        line_boxes.append(
            (
                int(JOB_CONTENT_CENTER_X - width / 2),
                int(start_y),
                int(JOB_CONTENT_CENTER_X + width / 2),
                int(start_y + layout["line_height"]),
            )
        )
        start_y += layout["line_height"]

    bbox = (
        min(box[0] for box in line_boxes),
        min(box[1] for box in line_boxes),
        max(box[2] for box in line_boxes),
        max(box[3] for box in line_boxes),
    )
    layout.update({"bbox": bbox, "top": bbox[1], "bottom": bbox[3]})
    return layout


def _draw_job_logo_or_fallback(base, image_url, fallback_text):
    from PIL import ImageDraw

    logo = _load_job_logo(image_url)
    if logo is not None:
        logo = _prepare_facebook_job_logo(logo)
    if logo is not None:
        center_y = (JOB_LOGO_TOP + JOB_LOGO_BOTTOM) // 2
        x = JOB_CONTENT_CENTER_X - logo.width // 2
        y = center_y - logo.height // 2
        base.alpha_composite(logo, (x, y))
        return {
            "loaded": True,
            "kind": "logo",
            "bbox": (x, y, x + logo.width, y + logo.height),
            "bottom": y + logo.height,
            "width": logo.width,
            "height": logo.height,
        }

    fallback = _clean_title_text(fallback_text)
    if fallback:
        draw = ImageDraw.Draw(base)
        layout = _fit_job_text_layout(
            draw,
            fallback,
            max_width=640,
            max_height=155,
            max_lines=2,
            min_size=44,
            max_size=62,
            width_axes=(100, 96, 92),
            line_ratio=1.14,
        )
        if layout:
            top = JOB_LOGO_TOP + max(
                0,
                ((JOB_LOGO_BOTTOM - JOB_LOGO_TOP) - layout["total_height"]) // 2,
            )
            boxes = []
            y = top
            for line in layout["lines"]:
                center_y = y + layout["line_height"] // 2
                _draw_text(
                    draw,
                    (JOB_CONTENT_CENTER_X, center_y),
                    line,
                    layout["font"],
                    (42, 42, 42, 255),
                )
                width = _job_text_width(draw, line, layout["font"])
                boxes.append(
                    (
                        int(JOB_CONTENT_CENTER_X - width / 2),
                        int(y),
                        int(JOB_CONTENT_CENTER_X + width / 2),
                        int(y + layout["line_height"]),
                    )
                )
                y += layout["line_height"]
            bbox = (
                min(box[0] for box in boxes),
                min(box[1] for box in boxes),
                max(box[2] for box in boxes),
                max(box[3] for box in boxes),
            )
            return {
                "loaded": False,
                "kind": "employer_text",
                "bbox": bbox,
                "bottom": bbox[3],
                "width": bbox[2] - bbox[0],
                "height": bbox[3] - bbox[1],
            }

    return {
        "loaded": False,
        "kind": "none",
        "bbox": None,
        "bottom": JOB_LOGO_TOP,
        "width": 0,
        "height": 0,
    }


def _validate_job_visual_layout(logo_layout, title_layout):
    title_bbox = title_layout.get("bbox")
    if not title_bbox:
        raise RuntimeError("missing title layout bounds")
    if title_bbox[0] < JOB_CONTENT_LEFT or title_bbox[2] > JOB_CONTENT_RIGHT:
        raise RuntimeError("job title escaped the horizontal safe zone")
    if title_bbox[1] < JOB_TITLE_TOP or title_bbox[3] > JOB_TITLE_BOTTOM:
        raise RuntimeError("job title escaped the vertical safe zone")
    if int(title_layout.get("font_size") or 0) < JOB_TITLE_MIN_SIZE:
        raise RuntimeError("job title became too small for mobile readability")
    if len(title_layout.get("lines") or []) > JOB_TITLE_MAX_LINES:
        raise RuntimeError("job title has too many lines")

    logo_bbox = logo_layout.get("bbox") if logo_layout else None
    if logo_bbox:
        if logo_bbox[0] < JOB_CONTENT_LEFT or logo_bbox[2] > JOB_CONTENT_RIGHT:
            raise RuntimeError("employer mark escaped the horizontal safe zone")
        if logo_bbox[1] < JOB_LOGO_TOP - 2 or logo_bbox[3] > JOB_LOGO_BOTTOM + 2:
            raise RuntimeError("employer mark escaped the logo safe zone")
        if logo_bbox[3] + JOB_LOGO_TITLE_GAP > title_bbox[1]:
            raise RuntimeError("employer mark collides with the job title")
    return True


def _generate_job_facebook_image(
    title,
    image_url,
    output_path,
    hook_text="",
    template_key="",
    employer_name="",
):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    FACEBOOK_IMAGE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        if not str(image_url or "").strip():
            raise RuntimeError("verified employer logo is required for Jobs Facebook images")
        base, selected_template_key = _load_job_template(template_key=template_key)
        logo_layout = _draw_job_logo_or_fallback(
            base,
            image_url,
            "",
        )
        if not logo_layout.get("loaded"):
            raise RuntimeError("verified employer logo could not be rendered for Facebook")
        title_layout = _draw_job_title(
            base,
            title,
            min_top=max(
                JOB_TITLE_TOP,
                int(logo_layout.get("bottom") or JOB_LOGO_TOP) + JOB_LOGO_TITLE_GAP,
            ),
        )
        _validate_job_visual_layout(logo_layout, title_layout)

        base.convert("RGB").save(
            output_path,
            "JPEG",
            quality=95,
            optimize=True,
            subsampling=0,
        )
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise RuntimeError("empty generated jobs Facebook image")

        log_event(
            "facebook_job_image_generated",
            path=str(output_path),
            template_key=selected_template_key,
            logo_loaded=bool(logo_layout.get("loaded")),
            logo_kind=logo_layout.get("kind"),
            logo_width=logo_layout.get("width"),
            logo_height=logo_layout.get("height"),
            title_font_size=title_layout.get("font_size"),
            title_font_width=title_layout.get("font_width"),
            title_lines=len(title_layout.get("lines") or []),
            width=base.width,
            height=base.height,
        )
        return {
            "ok": True,
            "path": str(output_path),
            "used_fallback": not bool(logo_layout.get("loaded")),
            "error": "",
            "template_key": selected_template_key,
            "title_font_size": title_layout.get("font_size"),
            "title_font_width": title_layout.get("font_width"),
            "title_lines": len(title_layout.get("lines") or []),
            "title_bbox": list(title_layout.get("bbox") or ()),
            "logo_kind": logo_layout.get("kind"),
            "logo_bbox": list(logo_layout.get("bbox") or ()),
            "logo_width": logo_layout.get("width"),
            "logo_height": logo_layout.get("height"),
            "layout_valid": True,
        }
    except Exception as error:
        log_event("facebook_job_image_generation_failed", error=error.__class__.__name__)
        return {
            "ok": False,
            "path": "",
            "used_fallback": False,
            "error": str(error),
            "layout_valid": False,
        }


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

        # Logo: upper-middle. Jobs article covers require the same verified
        # employer-logo asset used by Facebook; plain employer text is not a
        # visual substitute.
        if not str(image_url or "").strip():
            raise RuntimeError("verified employer logo is required for Jobs article covers")
        logo = _load_job_logo(image_url)
        if logo is None:
            raise RuntimeError("verified employer logo could not be loaded for article cover")
        logo_center_x = int(width * 0.50)
        logo_center_y = int(height * 0.32)
        logo_max = (int(width * 0.40), int(height * 0.18))
        logo = _prepare_article_job_logo(logo, logo_max)
        if logo is None:
            raise RuntimeError("verified employer logo could not be prepared for article cover")
        base.alpha_composite(
            logo,
            (
                logo_center_x - logo.width // 2,
                logo_center_y - logo.height // 2,
            ),
        )

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
        rgb = base.convert("RGB")
        # Article covers live in Git so keep future repository growth bounded.
        # Start at high visual quality and step down only when the file would
        # otherwise be unnecessarily large. Text/logo cards compress well with
        # 4:2:0 chroma subsampling while remaining clear at Blogger sizes.
        for quality in (86, 82, 78, 74, 70):
            rgb.save(
                output_path,
                "JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
                subsampling=2,
            )
            if output_path.stat().st_size <= 120_000:
                break
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


def generate_facebook_image(
    title,
    image_url,
    output_path,
    hook_text="",
    template_key="",
    employer_name="",
):
    return _generate_job_facebook_image(
        title,
        image_url,
        output_path,
        hook_text=hook_text,
        template_key=template_key,
        employer_name=employer_name,
    )
