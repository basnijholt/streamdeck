"""Pure image rendering for Stream Deck keys and touch-strip segments.

Nothing here touches the device or the network; the only I/O is font lookup.
"""

import os
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

from PIL import Image, ImageColor, ImageDraw, ImageFont

KEY_SIZE = 112
SEGMENT_SIZE = (200, 100)
GRID_COLS = 9
GRID_ROWS = 4
STRIP_SEGMENTS = 6
GAP = 8
BORDER_WIDTH = 4
BORDER_RADIUS = 14
DIM_FACTOR = 0.4

ICON_SIZE = 44
# Bitmap emoji fonts only load at fixed strike sizes: Noto Color Emoji at 109,
# Apple Color Emoji at 160.
EMOJI_STRIKES = (109, 160)

TEXT_COLOR = "#f4f4f5"
MUTED_COLOR = "#a1a1aa"
BADGE_COLOR = "#ef4444"
BAR_TRACK_COLOR = "#2a2a31"
SEGMENT_BG = "#1c1c21"
SHEET_BG = "#08080a"

_FONT_QUERIES = {"text": "DejaVu Sans", "emoji": "Noto Color Emoji"}
_FONT_ENV = {"text": "DECK_FONT_TEXT", "emoji": "DECK_FONT_EMOJI"}
_MACOS_EMOJI = "/System/Library/Fonts/Apple Color Emoji.ttc"


@dataclass(frozen=True)
class KeyView:
    icon: str | None
    label: str | None
    sublabel: str | None
    bg: str = "#1c1c21"
    border: str | None = "#2a2a31"
    badge: str | None = None
    dim: bool = False


@dataclass(frozen=True)
class SegmentView:
    title: str
    value: str
    bar: float | None = None
    color: str = "#3b82f6"


# --- fonts ------------------------------------------------------------------


@cache
def _fc_match(family: str) -> str | None:
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", family],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = result.stdout.strip()
    return path if result.returncode == 0 and path and Path(path).is_file() else None


def find_font(kind: Literal["text", "emoji"]) -> str | None:
    """Locate a font file: env override, then fontconfig, then the macOS emoji font."""
    override = os.environ.get(_FONT_ENV[kind])
    if override and Path(override).is_file():
        return override
    found = _fc_match(_FONT_QUERIES[kind])
    if found:
        return found
    if kind == "emoji" and Path(_MACOS_EMOJI).is_file():
        return _MACOS_EMOJI
    return None


@cache
def _text_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = find_font("text")
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


@cache
def _emoji_font() -> ImageFont.FreeTypeFont | None:
    path = find_font("emoji")
    if not path:
        return None
    for strike in EMOJI_STRIKES:
        try:
            return ImageFont.truetype(path, strike)
        except OSError:
            continue
    return None


# --- primitives -------------------------------------------------------------


def _rgb(color: str) -> tuple[int, int, int]:
    return ImageColor.getrgb(color)[:3]


def _plain(text: str) -> str:
    """Drop variation selectors, which the text font would draw as boxes."""
    return text.replace("\ufe0f", "")


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def _icon_image(icon: str, size: int) -> Image.Image:
    """Render an icon as an RGBA image fitting `size`x`size`; emoji font if available."""
    emoji = _emoji_font()
    if emoji is not None:
        canvas = Image.new("RGBA", (emoji.size * 2, emoji.size * 2), (0, 0, 0, 0))
        ImageDraw.Draw(canvas).text((0, 0), icon, font=emoji, embedded_color=True)
        box = canvas.getbbox()
        if box:
            glyph = canvas.crop(box)
            scale = size / max(glyph.size)
            new_size = (max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale)))
            return glyph.resize(new_size, Image.Resampling.LANCZOS)
    font = _text_font(size)
    canvas = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((0, 0), _plain(icon), font=font, fill=TEXT_COLOR)
    return canvas.crop(canvas.getbbox() or (0, 0, 1, 1))


def _paste_centered(img: Image.Image, layer: Image.Image, center: tuple[int, int]) -> None:
    img.paste(layer, (center[0] - layer.width // 2, center[1] - layer.height // 2), layer)


# --- key --------------------------------------------------------------------


def draw_key(view: KeyView) -> Image.Image:
    img = Image.new("RGB", (KEY_SIZE, KEY_SIZE), _rgb(view.bg))
    draw = ImageDraw.Draw(img)
    if view.border:
        draw.rounded_rectangle(
            (0, 0, KEY_SIZE - 1, KEY_SIZE - 1),
            radius=BORDER_RADIUS,
            outline=_rgb(view.border),
            width=BORDER_WIDTH,
        )

    max_text = KEY_SIZE - 2 * (BORDER_WIDTH + 4)
    if view.icon:
        _paste_centered(img, _icon_image(view.icon, ICON_SIZE), (KEY_SIZE // 2, 36))
    if view.label:
        font = _text_font(16)
        text = _ellipsize(draw, _plain(view.label), font, max_text)
        y = 80 if view.sublabel else 86
        draw.text((KEY_SIZE // 2, y), text, font=font, fill=_rgb(TEXT_COLOR), anchor="mm")
    if view.sublabel:
        font = _text_font(12)
        text = _ellipsize(draw, _plain(view.sublabel), font, max_text)
        draw.text((KEY_SIZE // 2, 98), text, font=font, fill=_rgb(MUTED_COLOR), anchor="mm")
    if view.badge:
        _draw_badge(draw, view.badge)

    if view.dim:
        img = img.point(lambda v: int(v * DIM_FACTOR))
    return img


def _draw_badge(draw: ImageDraw.ImageDraw, badge: str) -> None:
    font = _text_font(12)
    text = _plain(badge)
    width = max(18, round(draw.textlength(text, font=font)) + 10)
    right, top, height = KEY_SIZE - BORDER_WIDTH - 3, BORDER_WIDTH + 3, 18
    draw.rounded_rectangle(
        (right - width, top, right, top + height), radius=height // 2, fill=_rgb(BADGE_COLOR)
    )
    draw.text(
        (right - width / 2, top + height / 2), text, font=font, fill=(255, 255, 255), anchor="mm"
    )


# --- segment ----------------------------------------------------------------


def draw_segment(view: SegmentView) -> Image.Image:
    width, height = SEGMENT_SIZE
    img = Image.new("RGB", SEGMENT_SIZE, _rgb(SEGMENT_BG))
    draw = ImageDraw.Draw(img)
    pad = 12
    max_text = width - 2 * pad

    title_font = _text_font(16)
    x = pad
    head, _, rest = view.title.partition(" ")
    if rest and not head.isascii():
        icon = _icon_image(head, 20)
        img.paste(icon, (x, 10 + (20 - icon.height) // 2), icon)
        x += icon.width + 6
        title = rest
    else:
        title = view.title
    title = _ellipsize(draw, _plain(title), title_font, width - pad - x)
    draw.text((x, 20), title, font=title_font, fill=_rgb(MUTED_COLOR), anchor="lm")

    value_font = _text_font(30)
    value = _ellipsize(draw, _plain(view.value), value_font, max_text)
    draw.text((pad, 52), value, font=value_font, fill=_rgb(TEXT_COLOR), anchor="lm")

    if view.bar is not None:
        fraction = min(1.0, max(0.0, view.bar))
        top, bottom = height - 22, height - 14
        draw.rounded_rectangle((pad, top, width - pad, bottom), radius=4, fill=_rgb(BAR_TRACK_COLOR))
        fill_right = pad + round(max_text * fraction)
        if fill_right > pad + 1:
            draw.rounded_rectangle(
                (pad, top, fill_right, bottom), radius=4, fill=_rgb(view.color)
            )
    return img


# --- preview sheet ----------------------------------------------------------


def compose_deck(
    keys: list[Image.Image | None], segments: list[Image.Image | None]
) -> Image.Image:
    """Preview sheet: the 9x4 key grid above the 1200x100 touch strip."""
    strip_width = SEGMENT_SIZE[0] * STRIP_SEGMENTS
    strip_height = SEGMENT_SIZE[1]
    grid_width = GRID_COLS * KEY_SIZE + (GRID_COLS - 1) * GAP
    grid_height = GRID_ROWS * KEY_SIZE + (GRID_ROWS - 1) * GAP
    sheet = Image.new(
        "RGB",
        (strip_width + 2 * GAP, grid_height + strip_height + 3 * GAP),
        _rgb(SHEET_BG),
    )

    grid_x = (sheet.width - grid_width) // 2
    for i, key in enumerate(keys[: GRID_COLS * GRID_ROWS]):
        if key is not None:
            row, col = divmod(i, GRID_COLS)
            sheet.paste(key, (grid_x + col * (KEY_SIZE + GAP), GAP + row * (KEY_SIZE + GAP)))

    strip_y = 2 * GAP + grid_height
    for i, segment in enumerate(segments[:STRIP_SEGMENTS]):
        if segment is not None:
            sheet.paste(segment, (GAP + i * SEGMENT_SIZE[0], strip_y))
    return sheet
