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
