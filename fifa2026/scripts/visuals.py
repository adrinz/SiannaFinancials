"""Player spotlight cards, flags, and stadium-style backgrounds."""

from __future__ import annotations

import json
import random
from io import BytesIO
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from common import PROJECT_ROOT, resolve_font

FLAGS_DIR = PROJECT_ROOT / "assets" / "flags"
TOP_PLAYERS_JSON = PROJECT_ROOT / "data" / "top_players.json"
FLAG_CDN = "https://flagcdn.com/w320/{code}.png"


def load_top_players() -> list[dict]:
    with TOP_PLAYERS_JSON.open(encoding="utf-8") as f:
        return json.load(f)


def players_for_day(day: int, count: int = 5) -> list[dict]:
    players = load_top_players()
    start = (day - 1) % len(players)
    picked: list[dict] = []
    for i in range(count):
        picked.append(players[(start + i) % len(players)])
    return picked


def fetch_flag(code: str, size: tuple[int, int] = (320, 213)) -> Image.Image:
    FLAGS_DIR.mkdir(parents=True, exist_ok=True)
    cache = FLAGS_DIR / f"{code.lower()}.png"
    if cache.exists():
        img = Image.open(cache).convert("RGBA")
    else:
        url = FLAG_CDN.format(code=code.lower())
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGBA")
        img.save(cache)
    return img.resize(size, Image.Resampling.LANCZOS)


def _load_font(font_path: Path | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if font_path:
        try:
            return ImageFont.truetype(str(font_path), size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def render_stadium_background(size: tuple[int, int]) -> np.ndarray:
    """Dark stadium gradient with subtle pitch lighting."""
    width, height = size
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    top = (6, 18, 48)
    mid = (10, 38, 28)
    bottom = (4, 12, 8)
    for y in range(height):
        t = y / max(height - 1, 1)
        if t < 0.55:
            blend = t / 0.55
            color = tuple(int(top[i] + (mid[i] - top[i]) * blend) for i in range(3))
        else:
            blend = (t - 0.55) / 0.45
            color = tuple(int(mid[i] + (bottom[i] - mid[i]) * blend) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)

    # Stadium lights
    for cx, cy, radius in [
        (width * 0.2, height * 0.18, 220),
        (width * 0.8, height * 0.18, 220),
        (width * 0.5, height * 0.12, 280),
    ]:
        glow = Image.new("RGBA", size, (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow)
        gdraw.ellipse(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            fill=(255, 245, 200, 28),
        )
        img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")

    # Pitch strip at bottom
    pitch_top = int(height * 0.72)
    for y in range(pitch_top, height):
        t = (y - pitch_top) / max(height - pitch_top, 1)
        green = (int(18 + 30 * t), int(95 + 40 * t), int(45 + 20 * t))
        draw.line([(0, y), (width, y)], fill=green)

    draw.line([(60, pitch_top), (width - 60, pitch_top)], fill=(180, 220, 160), width=3)
    cx = width // 2
    draw.ellipse(
        [cx - 90, pitch_top + 40, cx + 90, pitch_top + 220],
        outline=(180, 220, 160),
        width=3,
    )

    # Film grain for texture
    noise = np.random.randint(0, 18, (height, width), dtype=np.uint8)
    arr = np.array(img).astype(np.int16)
    arr[:, :, :] = np.clip(arr[:, :, :] + noise[:, :, None] - 8, 0, 255)
    return arr.astype(np.uint8)


def render_player_card(
    player: dict,
    size: tuple[int, int],
    font_path: Path | None,
) -> np.ndarray:
    width, height = size
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    card_w, card_h = int(width * 0.86), int(height * 0.52)
    card_x = (width - card_w) // 2
    card_y = int(height * 0.28)

    # Card shadow + body
    shadow = Image.new("RGBA", size, (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle(
        [card_x + 8, card_y + 12, card_x + card_w + 8, card_y + card_h + 12],
        radius=36,
        fill=(0, 0, 0, 120),
    )
    canvas = Image.alpha_composite(canvas, shadow)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        [card_x, card_y, card_x + card_w, card_y + card_h],
        radius=36,
        fill=(12, 22, 38, 230),
        outline=(255, 215, 80, 255),
        width=4,
    )

    # Badge
    badge_font = _load_font(font_path, 34)
    badge = "TOP PLAYER TO WATCH"
    bbox = draw.textbbox((0, 0), badge, font=badge_font)
    bw = bbox[2] - bbox[0]
    draw.rounded_rectangle(
        [card_x + 28, card_y + 24, card_x + 28 + bw + 36, card_y + 78],
        radius=18,
        fill=(220, 38, 38, 255),
    )
    draw.text((card_x + 46, card_y + 34), badge, font=badge_font, fill=(255, 255, 255, 255))

    # Flag
    flag = fetch_flag(player["code"], (int(card_w * 0.72), int(card_w * 0.48)))
    flag_x = card_x + (card_w - flag.width) // 2
    flag_y = card_y + 100
    canvas.paste(flag, (flag_x, flag_y), flag)

    # Player name + country
    name_font = _load_font(font_path, 64)
    country_font = _load_font(font_path, 40)
    tag_font = _load_font(font_path, 30)

    name = player["name"].upper()
    country = player["country"]
    tag = player.get("tag", "STAR")

    name_bbox = draw.textbbox((0, 0), name, font=name_font, stroke_width=2)
    nw = name_bbox[2] - name_bbox[0]
    draw.text(
        (card_x + (card_w - nw) // 2, card_y + card_h - 200),
        name,
        font=name_font,
        fill=(255, 255, 255, 255),
        stroke_width=2,
        stroke_fill=(0, 0, 0, 255),
    )

    cbbox = draw.textbbox((0, 0), country, font=country_font)
    cw = cbbox[2] - cbbox[0]
    draw.text(
        (card_x + (card_w - cw) // 2, card_y + card_h - 120),
        country,
        font=country_font,
        fill=(200, 220, 255, 255),
    )

    tbbox = draw.textbbox((0, 0), tag, font=tag_font)
    tw = tbbox[2] - tbbox[0]
    draw.rounded_rectangle(
        [card_x + card_w - tw - 56, card_y + card_h - 58, card_x + card_w - 24, card_y + card_h - 18],
        radius=12,
        fill=(255, 215, 80, 255),
    )
    draw.text(
        (card_x + card_w - tw - 40, card_y + card_h - 52),
        tag,
        font=tag_font,
        fill=(20, 20, 20, 255),
    )

    return np.array(canvas)


def render_flag_strip(players: list[dict], size: tuple[int, int], font_path: Path | None) -> np.ndarray:
    width, height = size
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    strip_h = 120
    y0 = height - strip_h - 40
    draw.rounded_rectangle(
        [40, y0, width - 40, y0 + strip_h],
        radius=20,
        fill=(0, 0, 0, 150),
    )

    codes_seen: list[str] = []
    unique_players: list[dict] = []
    for p in players:
        if p["code"] not in codes_seen:
            codes_seen.append(p["code"])
            unique_players.append(p)

    flag_w, flag_h = 88, 58
    gap = (width - 80 - len(unique_players) * flag_w) // max(len(unique_players) + 1, 1)
    x = 40 + gap
    label_font = _load_font(font_path, 18)

    for p in unique_players[:6]:
        flag = fetch_flag(p["code"], (flag_w, flag_h))
        canvas.paste(flag, (x, y0 + 16), flag)
        code_label = p["code"].upper().replace("GB-ENG", "ENG")
        draw.text((x + 8, y0 + 78), code_label, font=label_font, fill=(255, 255, 255, 220))
        x += flag_w + gap

    return np.array(canvas)


def render_header(size: tuple[int, int], font_path: Path | None) -> np.ndarray:
    width, height = size
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    title_font = _load_font(font_path, 46)
    sub_font = _load_font(font_path, 28)

    draw.rounded_rectangle([50, 36, width - 50, 150], radius=22, fill=(0, 0, 0, 140))
    title = "FIFA WORLD CUP 2026"
    tbbox = draw.textbbox((0, 0), title, font=title_font, stroke_width=2)
    tw = tbbox[2] - tbbox[0]
    draw.text(
        ((width - tw) // 2, 52),
        title,
        font=title_font,
        fill=(255, 230, 90, 255),
        stroke_width=2,
        stroke_fill=(0, 0, 0, 255),
    )
    sub = "PLAYERS TO WATCH"
    sbbox = draw.textbbox((0, 0), sub, font=sub_font)
    sw = sbbox[2] - sbbox[0]
    draw.text(((width - sw) // 2, 108), sub, font=sub_font, fill=(220, 235, 255, 255))

    return np.array(canvas)
