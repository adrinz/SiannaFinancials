"""Match flag cards for copyright-safe promo Shorts."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from visuals import fetch_flag  # noqa: E402


def _load_font(font_path: Path | None, size: int):
    from PIL import ImageFont

    if font_path:
        try:
            return ImageFont.truetype(str(font_path), size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def render_match_card(
    teams: list[tuple[str, str]],
    size: tuple[int, int],
    font_path: Path | None,
) -> np.ndarray:
    """
    Render a VS match card with country flags.
    teams: [(display_name, flag_code), ...] — one or two entries.
    """
    width, height = size
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    card_w = int(width * 0.9)
    card_h = int(height * 0.36)
    card_x = (width - card_w) // 2
    card_y = int(height * 0.34)

    shadow = Image.new("RGBA", size, (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle(
        [card_x + 10, card_y + 14, card_x + card_w + 10, card_y + card_h + 14],
        radius=40,
        fill=(0, 0, 0, 130),
    )
    canvas = Image.alpha_composite(canvas, shadow)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        [card_x, card_y, card_x + card_w, card_y + card_h],
        radius=40,
        fill=(8, 18, 36, 235),
        outline=(255, 215, 80, 255),
        width=5,
    )

    badge_font = _load_font(font_path, 30)
    badge = "MATCH PREVIEW"
    bb = draw.textbbox((0, 0), badge, font=badge_font)
    bw = bb[2] - bb[0]
    draw.rounded_rectangle(
        [card_x + 32, card_y + 24, card_x + 32 + bw + 40, card_y + 72],
        radius=16,
        fill=(200, 32, 32, 255),
    )
    draw.text((card_x + 52, card_y + 32), badge, font=badge_font, fill=(255, 255, 255, 255))

    name_font = _load_font(font_path, 38)
    vs_font = _load_font(font_path, 56)

    if len(teams) == 1:
        name, code = teams[0]
        flag = fetch_flag(code, (int(card_w * 0.45), int(card_w * 0.30)))
        fx = card_x + (card_w - flag.width) // 2
        fy = card_y + 95
        canvas.paste(flag, (fx, fy), flag)
        nb = draw.textbbox((0, 0), name.upper(), font=name_font, stroke_width=2)
        nw = nb[2] - nb[0]
        draw.text(
            (card_x + (card_w - nw) // 2, card_y + card_h - 72),
            name.upper(),
            font=name_font,
            fill=(255, 255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 255),
        )
        return np.array(canvas)

    (name_a, code_a), (name_b, code_b) = teams[0], teams[1]
    flag_w, flag_h = int(card_w * 0.28), int(card_w * 0.19)
    slot_w = (card_w - 120) // 2
    left_x = card_x + 50
    right_x = card_x + card_w - 50 - flag_w
    flag_y = card_y + 100

    flag_a = fetch_flag(code_a, (flag_w, flag_h))
    flag_b = fetch_flag(code_b, (flag_w, flag_h))
    canvas.paste(flag_a, (left_x + (slot_w - flag_w) // 2, flag_y), flag_a)
    canvas.paste(flag_b, (right_x + (slot_w - flag_w) // 2, flag_y), flag_b)

    vs = "VS"
    vb = draw.textbbox((0, 0), vs, font=vs_font, stroke_width=3)
    vw = vb[2] - vb[0]
    draw.text(
        (card_x + (card_w - vw) // 2, flag_y + flag_h // 2 - 20),
        vs,
        font=vs_font,
        fill=(255, 230, 90, 255),
        stroke_width=3,
        stroke_fill=(0, 0, 0, 255),
    )

    for name, slot_x in ((name_a, left_x), (name_b, right_x)):
        label = name.upper()
        if len(label) > 16:
            label = label[:14] + "…"
        nb = draw.textbbox((0, 0), label, font=name_font, stroke_width=1)
        nw = nb[2] - nb[0]
        draw.text(
            (slot_x + (slot_w - nw) // 2, card_y + card_h - 68),
            label,
            font=name_font,
            fill=(230, 240, 255, 255),
            stroke_width=1,
            stroke_fill=(0, 0, 0, 200),
        )

    return np.array(canvas)


def render_promo_hook(
    text: str,
    size: tuple[int, int],
    font_path: Path | None,
    *,
    font_size: int = 44,
) -> np.ndarray:
    """Top-aligned hook banner (leaves room for match card below)."""
    import textwrap

    width, height = size
    font = _load_font(font_path, font_size)
    wrapped = textwrap.fill(text.upper(), width=18)

    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    box_top, box_bottom = 48, 220
    draw.rounded_rectangle(
        [48, box_top, width - 48, box_bottom],
        radius=22,
        fill=(0, 0, 0, 185),
        outline=(255, 230, 90, 255),
        width=4,
    )
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center", stroke_width=2)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.multiline_text(
        ((width - tw) // 2, box_top + (box_bottom - box_top - th) // 2),
        wrapped,
        font=font,
        fill=(255, 229, 102, 255),
        align="center",
        stroke_width=2,
        stroke_fill=(0, 0, 0, 255),
    )
    return np.array(canvas)
