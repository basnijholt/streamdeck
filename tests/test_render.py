import subprocess
from pathlib import Path

import pytest
from PIL import Image

from deck import render
from deck.cli import main
from deck.render import KeyView, SegmentView, compose_deck, draw_key, draw_segment, find_font

EXAMPLE = Path(__file__).parent.parent / "examples" / "config.yaml"


@pytest.fixture
def fresh_fonts():
    """Font lookups are cached; reset around tests that change the environment."""
    caches = (render._fc_match, render._text_font, render._emoji_font)
    for cache in caches:
        cache.cache_clear()
    yield
    for cache in caches:
        cache.cache_clear()


def test_key_size_and_background():
    img = draw_key(KeyView(icon=None, label="Ghostty", sublabel=None, bg="#3a3208", border=None))
    assert img.size == (112, 112)
    assert img.getpixel((56, 2)) == (0x3A, 0x32, 0x08)


def test_border_color_at_edge():
    img = draw_key(KeyView(icon=None, label=None, sublabel=None, bg="#1c1c21", border="#22c55e"))
    assert img.getpixel((1, 56)) == (0x22, 0xC5, 0x5E)


def test_label_region_not_empty():
    img = draw_key(KeyView(icon=None, label="Guest", sublabel=None))
    region = img.crop((0, 70, 112, 100))
    assert len(region.getcolors(region.width * region.height)) > 1


def test_dim_darkens():
    a = draw_key(KeyView(icon=None, label=None, sublabel=None, bg="#3a3208", border=None))
    b = draw_key(KeyView(icon=None, label=None, sublabel=None, bg="#3a3208", border=None, dim=True))
    assert sum(b.getpixel((56, 2))) < sum(a.getpixel((56, 2)))


def test_segment_and_compose_sizes():
    seg = draw_segment(SegmentView(title="☀️ Brightness", value="80%", bar=0.8))
    assert seg.size == (200, 100)
    sheet = compose_deck([None] * 36, [seg] + [None] * 5)
    assert sheet.width >= 1200


def test_find_font_env_override(monkeypatch, tmp_path, fresh_fonts):
    font = tmp_path / "custom.ttf"
    font.touch()
    monkeypatch.setenv("DECK_FONT_TEXT", str(font))
    assert find_font("text") == str(font)


def test_draws_without_any_fonts(monkeypatch, fresh_fonts):
    monkeypatch.delenv("DECK_FONT_TEXT", raising=False)
    monkeypatch.delenv("DECK_FONT_EMOJI", raising=False)
    monkeypatch.setenv("PATH", "")  # fc-match unavailable
    assert find_font("text") is None
    img = draw_key(KeyView(icon="👻", label="Ghostty", sublabel="x", badge="2"))
    assert img.size == (112, 112)
    assert len(img.crop((30, 10, 82, 60)).getcolors(52 * 50)) > 1  # icon fell back to text


def test_cli_render_writes_png(tmp_path):
    out = tmp_path / "deck.png"
    assert main(["render", str(EXAMPLE), "--png", str(out)]) == 0
    with Image.open(out) as sheet:
        assert sheet.width >= 1200


def test_cli_render_unknown_page(tmp_path, capsys):
    out = tmp_path / "deck.png"
    assert main(["render", str(EXAMPLE), "--png", str(out), "--page", "nope"]) == 1
    assert "unknown page 'nope'" in capsys.readouterr().out
    assert not out.exists()


def _stub_fc_match(monkeypatch, families, path):
    """Fake fc-match answering any query with this family/file, honoring the -f format."""

    def run(cmd, **kwargs):
        assert cmd[:2] == ["fc-match", "-f"]
        out = cmd[2].replace("%{family}", families).replace("%{file}", str(path))
        out = out.replace("\\t", "\t")
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(render.subprocess, "run", run)


def test_find_font_rejects_fontconfig_substitute(monkeypatch, tmp_path, fresh_fonts):
    # fc-match exits 0 with its best substitute for an unknown family.
    substitute = tmp_path / "Substitute.ttf"
    substitute.touch()
    monkeypatch.delenv("DECK_FONT_TEXT", raising=False)
    monkeypatch.delenv("DECK_FONT_EMOJI", raising=False)
    monkeypatch.setattr(render, "_MACOS_EMOJI", str(tmp_path / "missing.ttc"))
    _stub_fc_match(monkeypatch, "DejaVu Math TeX Gyre", substitute)

    assert find_font("emoji") is None
    assert find_font("text") is None
    assert render._emoji_font() is None
    img = draw_key(KeyView(icon="👻", label=None, sublabel=None))
    assert len(img.crop((30, 10, 82, 60)).getcolors(52 * 50)) > 1  # drawn via the text path


def test_find_font_next_candidate_after_rejected_substitute(monkeypatch, tmp_path, fresh_fonts):
    substitute = tmp_path / "Substitute.ttf"
    substitute.touch()
    apple = tmp_path / "Apple Color Emoji.ttc"
    apple.touch()
    monkeypatch.delenv("DECK_FONT_EMOJI", raising=False)
    monkeypatch.setattr(render, "_MACOS_EMOJI", str(apple))
    _stub_fc_match(monkeypatch, "DejaVu Math TeX Gyre", substitute)
    assert find_font("emoji") == str(apple)


def test_find_font_accepts_family_in_comma_list(monkeypatch, tmp_path, fresh_fonts):
    font = tmp_path / "DejaVuSans.ttf"
    font.touch()
    monkeypatch.delenv("DECK_FONT_TEXT", raising=False)
    _stub_fc_match(monkeypatch, "DejaVu Sans,DejaVu Sans Condensed", font)
    assert find_font("text") == str(font)


# --- lone emoji (badge, segment title, segment value) -----------------------

needs_emoji_font = pytest.mark.skipif(find_font("emoji") is None, reason="no emoji font")

BADGE_BOX = (80, 5, 108, 31)  # top-right corner of a key
VALUE_BOX = (8, 36, 60, 70)  # start of a segment's value line
TITLE_BOX = (8, 8, 36, 32)  # start of a segment's title line
PILL = (0xEF, 0x44, 0x44)  # BADGE_COLOR


def count_pixels(img: Image.Image, box, predicate) -> int:
    region = img.crop(box)
    colors = region.getcolors(region.width * region.height)
    return sum(n for n, rgb in colors if predicate(rgb))


def is_red(rgb) -> bool:
    return rgb[0] > 180 and rgb[1] < 110 and rgb[2] < 110


@pytest.mark.parametrize("text", ["🚫", "❌", "🔊", "🔴", "⚫", "☀️"])
def test_is_emoji_accepts_lone_emoji(text):
    assert render._is_emoji(text)


@pytest.mark.parametrize("text", ["", "—", "…", "→", "X", "42%", "LIVE", "2", "1️⃣", "🔴 LIVE", "🚫 x"])
def test_is_emoji_rejects_text(text):
    assert not render._is_emoji(text)


@needs_emoji_font
def test_emoji_badge_is_drawn_with_the_emoji_font():
    img = draw_key(KeyView(icon=None, label=None, sublabel=None, badge="🚫"))
    assert count_pixels(img, BADGE_BOX, is_red) > 40  # the red sign itself
    assert count_pixels(img, BADGE_BOX, lambda rgb: rgb == PILL) == 0  # no text-badge pill


def test_text_badge_keeps_its_pill():
    img = draw_key(KeyView(icon=None, label=None, sublabel=None, badge="2"))
    assert count_pixels(img, BADGE_BOX, lambda rgb: rgb == PILL) > 100


@needs_emoji_font
def test_emoji_segment_value_is_drawn_with_the_emoji_font():
    emoji = draw_segment(SegmentView(title="☀️ Brightness", value="❌"))
    assert count_pixels(emoji, VALUE_BOX, is_red) > 100
    text = draw_segment(SegmentView(title="☀️ Brightness", value="X"))
    assert count_pixels(text, VALUE_BOX, is_red) == 0


@needs_emoji_font
def test_segment_title_that_is_only_an_emoji_is_drawn_with_the_emoji_font():
    img = draw_segment(SegmentView(title="🔴", value="x"))
    assert count_pixels(img, TITLE_BOX, is_red) > 50


@needs_emoji_font
def test_dash_value_stays_text():
    img = draw_segment(SegmentView(title="🔊 iLoud", value="—"))
    assert count_pixels(img, VALUE_BOX, lambda rgb: rgb[0] > 200 and rgb[1] > 200) > 20  # white


def test_lone_emoji_falls_back_to_text_without_an_emoji_font(monkeypatch):
    monkeypatch.setattr(render, "_emoji_font", lambda: None)
    key = draw_key(KeyView(icon=None, label=None, sublabel=None, badge="🚫"))
    assert count_pixels(key, BADGE_BOX, lambda rgb: rgb == PILL) > 100  # text badge on its pill
    seg = draw_segment(SegmentView(title="🔴", value="❌"))
    assert seg.size == (200, 100)
