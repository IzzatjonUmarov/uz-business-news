"""Картинки к постам: фото из новости или сгенерированная карточка + водяной знак канала."""

from __future__ import annotations

import io
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .fetch import NewsItem

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
FONT_REGULAR = ROOT / "assets" / "fonts" / "DejaVuSans.ttf"
FONT_BOLD = ROOT / "assets" / "fonts" / "DejaVuSans-Bold.ttf"

MAX_SIDE = 1600           # Telegram всё равно ужмёт больше 1280–2560
MIN_WIDTH, MIN_HEIGHT = 400, 220
CARD_SIZE = (1280, 720)
MAX_DOWNLOAD = 15 * 1024 * 1024

# Заглушки сайтов вместо фото новости: логотипы, картинки для соцсетей по умолчанию и т. п.
GENERIC_IMAGE = re.compile(
    r"(logo|favicon|social\.|/social/|og[_-]?default|default[_-]?img|og[_-]?image|share|placeholder|"
    r"no[_-]?image|noimage|map_image|banner[_-]?default|/icons?/)",
    re.I,
)

GROUP_COLORS = {  # (верх градиента, низ градиента, акцент)
    "gov": ((18, 52, 86), (8, 22, 40), (64, 156, 255)),
    "regulator": ((10, 70, 60), (4, 28, 26), (46, 204, 150)),
    "bank": ((28, 40, 92), (10, 14, 38), (120, 140, 255)),
    "market": ((70, 30, 90), (24, 10, 34), (200, 120, 255)),
    "invest": ((90, 56, 14), (34, 20, 6), (255, 176, 59)),
    "media": ((50, 50, 58), (18, 18, 22), (180, 180, 200)),
}


@dataclass
class WatermarkConfig:
    text: str = ""                 # например «@uz_business_news»
    logo: Path | None = None       # PNG с прозрачностью (необязательно)
    opacity: float = 0.85          # непрозрачность плашки в углу
    tiled: bool = True             # дополнительно — бледный диагональный повтор по всей картинке
    tiled_opacity: float = 0.10

    @property
    def enabled(self) -> bool:
        return bool(self.text or self.logo)


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT_REGULAR), size)


def is_generic(url: str | None) -> bool:
    return not url or bool(GENERIC_IMAGE.search(url))


def download_image(client: httpx.Client, url: str) -> Image.Image | None:
    """Скачивает картинку; None, если это не картинка или она слишком маленькая."""
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_DOWNLOAD:
                    return None
        image = Image.open(io.BytesIO(bytes(data)))
        image.load()
    except (httpx.HTTPError, UnidentifiedImageError, OSError, ValueError) as exc:
        log.debug("Картинка %s недоступна: %s", url, exc)
        return None
    image = ImageOps.exif_transpose(image)
    if image.width < MIN_WIDTH or image.height < MIN_HEIGHT:
        return None
    ratio = image.width / image.height
    if ratio > 4 or ratio < 0.3:  # баннеры-полоски и вертикальные обрезки
        return None
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        image = background
    image = image.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    return image


# ── Карточка, если у новости нет фото ──────────────────────────────

def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def make_card(item: NewsItem, headline: str | None = None) -> Image.Image:
    width, height = CARD_SIZE
    top, bottom, accent = GROUP_COLORS.get(item.group, GROUP_COLORS["gov"])
    card = Image.new("RGB", CARD_SIZE)
    draw = ImageDraw.Draw(card)
    for y in range(height):  # вертикальный градиент
        t = y / (height - 1)
        draw.line([(0, y), (width, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))

    pad = 72
    draw.rectangle([0, 0, 14, height], fill=accent)

    source_font = _font(True, 30)
    draw.text((pad, pad - 8), item.source_name.upper(), font=source_font, fill=accent)

    text = headline or item.title
    max_width, max_height = width - 2 * pad, height - 2 * pad - 140
    for size in (64, 58, 52, 46, 42, 38, 34):
        font = _font(True, size)
        lines = _wrap(draw, text, font, max_width)
        line_height = int(size * 1.25)
        if len(lines) * line_height <= max_height:
            break
    else:
        lines = lines[: max_height // line_height]
        lines[-1] = lines[-1].rstrip(" ,.;:") + "…"
    y = pad + 70 + (max_height - len(lines) * line_height) // 2
    for line in lines:
        draw.text((pad, y), line, font=font, fill=(255, 255, 255))
        y += line_height

    if item.published:
        draw.text((pad, height - pad - 28), item.published.strftime("%d.%m.%Y"),
                  font=_font(False, 28), fill=(200, 210, 225))
    return card


# ── Водяной знак ───────────────────────────────────────────────────

def apply_watermark(image: Image.Image, cfg: WatermarkConfig) -> Image.Image:
    if not cfg.enabled:
        return image
    base = image.convert("RGBA")
    width, height = base.size
    scale = max(width, height) / 1280

    if cfg.tiled and cfg.text:
        tile_font = _font(True, max(18, int(34 * scale)))
        layer = Image.new("RGBA", (int(math.hypot(width, height)),) * 2, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        step_x = int(draw.textlength(cfg.text, font=tile_font) + 220 * scale)
        step_y = int(170 * scale)
        alpha = int(255 * cfg.tiled_opacity)
        for row, y in enumerate(range(0, layer.height, step_y)):
            offset = (step_x // 2) * (row % 2)
            for x in range(-step_x, layer.width, step_x):
                draw.text((x + offset, y), cfg.text, font=tile_font, fill=(255, 255, 255, alpha),
                          stroke_width=1, stroke_fill=(0, 0, 0, alpha))
        layer = layer.rotate(30, resample=Image.Resampling.BICUBIC)
        left, top_ = (layer.width - width) // 2, (layer.height - height) // 2
        base.alpha_composite(layer.crop((left, top_, left + width, top_ + height)))

    # плашка в правом нижнем углу: [логотип] @канал
    font = _font(True, max(16, int(30 * scale)))
    pad, gap = int(16 * scale), int(12 * scale)
    logo = None
    if cfg.logo and Path(cfg.logo).exists():
        logo = Image.open(cfg.logo).convert("RGBA")
        logo_h = int(46 * scale)
        logo = logo.resize((max(1, int(logo.width * logo_h / logo.height)), logo_h), Image.Resampling.LANCZOS)
    measure = ImageDraw.Draw(base)
    text_w = int(measure.textlength(cfg.text, font=font)) if cfg.text else 0
    bbox = font.getbbox("Ag@")
    text_h = bbox[3] - bbox[1]
    content_h = max(text_h, logo.height if logo else 0)
    plate_w = pad * 2 + (logo.width if logo else 0) + (gap if logo and text_w else 0) + text_w
    plate_h = pad * 2 + content_h
    margin = int(24 * scale)
    x0, y0 = width - plate_w - margin, height - plate_h - margin

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle([x0, y0, x0 + plate_w, y0 + plate_h], radius=int(14 * scale),
                           fill=(0, 0, 0, int(150 * cfg.opacity)))
    x = x0 + pad
    if logo:
        logo.putalpha(logo.getchannel("A").point(lambda a: int(a * cfg.opacity)))
        overlay.alpha_composite(logo, (x, y0 + (plate_h - logo.height) // 2))
        x += logo.width + gap
    if cfg.text:
        draw.text((x, y0 + (plate_h - text_h) // 2 - bbox[1]), cfg.text, font=font,
                  fill=(255, 255, 255, int(255 * cfg.opacity)))
    base.alpha_composite(overlay)
    return base.convert("RGB")


def to_jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=88, optimize=True, progressive=True)
    return buffer.getvalue()


def build_image(client: httpx.Client, item: NewsItem, cfg: WatermarkConfig,
                headline: str | None = None, generic: set[str] | None = None) -> bytes:
    """Фото новости (первое подходящее) или карточка — всегда с водяным знаком."""
    image = None
    for url in [item.image, *item.extra.get("content_images", [])]:
        if url and not is_generic(url) and url not in (generic or set()):
            image = download_image(client, url)
            if image:
                break
    if image is None:
        image = make_card(item, headline)
    return to_jpeg(apply_watermark(image, cfg))
