"""Pure image rendering for Stream Deck keys and touch-strip segments.

Nothing here touches the device or the network; the only I/O is font lookup.
"""

import os
import subprocess
import unicodedata
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
KEY_INSET = 12  # the key's bezel hides its outer pixels: text stays this far inside
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
    detail: str | None = None  # small and muted below the value; not drawn next to a bar


# --- fonts ------------------------------------------------------------------


@cache
def _fc_match(family: str) -> str | None:
    """Font file for exactly `family`, or None.

    fc-match always exits 0 with its best substitute for an unknown family, so the
    returned family names are checked against the requested one.
    """
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{family}\\t%{file}", family],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    families, _, path = result.stdout.strip().partition("\t")
    if family.casefold() not in {name.strip().casefold() for name in families.split(",")}:
        return None
    return path if path and Path(path).is_file() else None


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


def _fitting_font(
    draw: ImageDraw.ImageDraw, text: str, sizes: tuple[int, ...], width: int
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """The text font at the largest of `sizes` that fits `text` in `width`, else the smallest."""
    for size in sizes:
        font = _text_font(size)
        if draw.textlength(text, font=font) <= width:
            return font
    return font


def wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int, max_lines: int
) -> list[str]:
    """`text` broken at spaces into at most `max_lines` lines of `width`; the rest is ellipsized."""
    words = text.split()
    lines: list[str] = []
    while words and len(lines) < max_lines:
        line = words.pop(0)
        while words and draw.textlength(f"{line} {words[0]}", font=font) <= width:
            line = f"{line} {words.pop(0)}"
        lines.append(line)
    if words:
        lines[-1] = " ".join([lines[-1], *words])
    return [_ellipsize(draw, line, font, width) for line in lines]


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


def _is_emoji(text: str) -> bool:
    """True for a lone emoji ("🚫", "❌", "☀️"): symbols only, no letters, digits or spaces."""
    symbols = [c for c in text if c not in "\ufe0f\u200d"]  # variation selector, joiner
    return bool(symbols) and all(unicodedata.category(c) in {"So", "Sk"} for c in symbols)


def _emoji_layer(text: str, size: int) -> Image.Image | None:
    """Emoji image for a lone-emoji `text`; None means draw it as plain text instead.

    The text font has no emoji glyphs (they come out as boxes), so every place that
    can show a bare emoji asks here first. Without an emoji font there is nothing
    better than the text fallback, so this also returns None.
    """
    if _emoji_font() is None or not _is_emoji(text):
        return None
    return _icon_image(text, size)


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

    # Text stays clear of the bottom edge too: the key's lens mirrors what is drawn near it.
    max_text = KEY_SIZE - 2 * KEY_INSET
    label = _plain(view.label) if view.label else ""
    if view.icon:
        _paste_centered(img, _icon_image(view.icon, ICON_SIZE), (KEY_SIZE // 2, 34))
        if label:
            font = _fitting_font(draw, label, (16, 14, 12), max_text)
            text = _ellipsize(draw, label, font, max_text)
            y = 74 if view.sublabel else 80
            draw.text((KEY_SIZE // 2, y), text, font=font, fill=_rgb(TEXT_COLOR), anchor="mm")
    elif label:  # no icon (a thread slot): the label gets the room, on up to three lines
        font = _text_font(15)
        lines = wrap(draw, label, font, max_text, 3)
        line_height = 18
        bottom = 80 if view.sublabel else KEY_SIZE - KEY_INSET
        top = KEY_INSET + (bottom - KEY_INSET - line_height * len(lines)) // 2
        for i, line in enumerate(lines):
            y = top + line_height * i + line_height // 2
            draw.text((KEY_SIZE // 2, y), line, font=font, fill=_rgb(TEXT_COLOR), anchor="mm")
    if view.sublabel:
        font = _text_font(12)
        text = _ellipsize(draw, _plain(view.sublabel), font, max_text)
        draw.text((KEY_SIZE // 2, 90), text, font=font, fill=_rgb(MUTED_COLOR), anchor="mm")
    if view.badge:
        _draw_badge(img, draw, view.badge)

    if view.dim:
        img = img.point(lambda v: int(v * DIM_FACTOR))
    return img


BADGE_EMOJI_SIZE = 20


def _draw_badge(img: Image.Image, draw: ImageDraw.ImageDraw, badge: str) -> None:
    right, top = KEY_SIZE - BORDER_WIDTH - 3, BORDER_WIDTH + 3
    emoji = _emoji_layer(badge, BADGE_EMOJI_SIZE)
    if emoji is not None:
        # An emoji is its own badge: a red pill behind it would hide a red 🚫.
        img.paste(emoji, (right - emoji.width, top + (BADGE_EMOJI_SIZE - emoji.height) // 2), emoji)
        return
    font = _text_font(12)
    text = _plain(badge)
    width = max(18, round(draw.textlength(text, font=font)) + 10)
    height = 18
    draw.rounded_rectangle(
        (right - width, top, right, top + height), radius=height // 2, fill=_rgb(BADGE_COLOR)
    )
    draw.text(
        (right - width / 2, top + height / 2), text, font=font, fill=(255, 255, 255), anchor="mm"
    )


# --- segment ----------------------------------------------------------------


VALUE_EMOJI_SIZE = 30
VALUE_SIZES = (30, 24, 20, 16)  # the value takes the largest of these that fits the segment
DETAIL_SIZE = 12
SEGMENT_PAD = 12


def fit_value(draw: ImageDraw.ImageDraw, text: str, width: int) -> tuple[list[str], int]:
    """The lines and the font size at which a segment shows `text` within `width` pixels.

    One line at the largest of `VALUE_SIZES` that fits. A text that does not fit even at the
    smallest size goes onto two lines there, split at ` — ` if it has one, else after as many
    words as fit on the first line. Only what still does not fit on a line is ellipsized.
    """
    text = _plain(text)
    for size in VALUE_SIZES:
        if draw.textlength(text, font=_text_font(size)) <= width:
            return [text], size
    size = VALUE_SIZES[-1]
    font = _text_font(size)
    if " — " in text:
        first, _, second = text.partition(" — ")
    else:
        words = text.split()
        if len(words) < 2:
            return [_ellipsize(draw, text, font, width)], size
        fit = len(words) - 1
        while fit > 1 and draw.textlength(" ".join(words[:fit]), font=font) > width:
            fit -= 1
        first, second = " ".join(words[:fit]), " ".join(words[fit:])
    return [_ellipsize(draw, first, font, width), _ellipsize(draw, second, font, width)], size


def draw_segment(view: SegmentView) -> Image.Image:
    width, height = SEGMENT_SIZE
    img = Image.new("RGB", SEGMENT_SIZE, _rgb(SEGMENT_BG))
    draw = ImageDraw.Draw(img)
    pad = SEGMENT_PAD
    max_text = width - 2 * pad

    title_font = _text_font(16)
    x = pad
    head, _, rest = view.title.partition(" ")
    title_emoji = _emoji_layer(view.title, 20)
    if title_emoji is not None:
        img.paste(title_emoji, (x, 10 + (20 - title_emoji.height) // 2), title_emoji)
        title = ""
    elif rest and not head.isascii():
        icon = _icon_image(head, 20)
        img.paste(icon, (x, 10 + (20 - icon.height) // 2), icon)
        x += icon.width + 6
        title = rest
    else:
        title = view.title
    title = _ellipsize(draw, _plain(title), title_font, width - pad - x)
    draw.text((x, 20), title, font=title_font, fill=_rgb(MUTED_COLOR), anchor="lm")

    value_emoji = _emoji_layer(view.value, VALUE_EMOJI_SIZE)
    if value_emoji is not None:
        img.paste(value_emoji, (pad, 52 - value_emoji.height // 2), value_emoji)
    else:
        lines, size = fit_value(draw, view.value, max_text)
        centers = [52] if len(lines) == 1 else [43, 63]
        for line, y in zip(lines, centers, strict=True):
            draw.text((pad, y), line, font=_text_font(size), fill=_rgb(TEXT_COLOR), anchor="lm")

    if view.detail and view.bar is None:
        font = _text_font(DETAIL_SIZE)
        detail = _ellipsize(draw, _plain(view.detail), font, max_text)
        draw.text((pad, 84), detail, font=font, fill=_rgb(MUTED_COLOR), anchor="lm")

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
