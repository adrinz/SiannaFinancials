#!/usr/bin/env python3
"""
Build copyright-safe promo Shorts from @fifa video METADATA only.

Does NOT use FIFA video/audio — avoids Content ID blocks.
Uses: stock footage + original voiceover + link to official source.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

PLATFORM_DIR = Path(__file__).resolve().parent
ROOT = PLATFORM_DIR.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_video import build_background_clip, pick_background, render_hook_image  # noqa: E402
from common import resolve_font, load_config as load_root_config  # noqa: E402
from tools_cmd import ytdlp_cmd  # noqa: E402

PROMO_DIR = PLATFORM_DIR / "data" / "clips" / "promo"
AUDIO_DIR = PLATFORM_DIR / "data" / "promo" / "audio"

try:
    from moviepy.editor import AudioFileClip, CompositeVideoClip, ImageClip
except ImportError:
    from moviepy import AudioFileClip, CompositeVideoClip, ImageClip

if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.Resampling.LANCZOS  # type: ignore[attr-defined]

HOOKS = [
    "OFFICIAL FIFA PREVIEW — WATCH NOW",
    "WORLD CUP 2026 — MATCH PREVIEW",
    "FULL VIDEO ON @FIFA — LINK BELOW",
]


def promo_clip_id(video_id: str, variant: int) -> str:
    """Filesystem-safe id — YouTube video ids can start with '-' which breaks ffmpeg."""
    base = video_id.lstrip("-")
    if base != video_id:
        base = f"v_{base}"
    return f"{base}_promo_{variant + 1:02d}"


def load_clip_config() -> dict:
    for path in (PLATFORM_DIR / "config.yaml", PLATFORM_DIR / "config.example.yaml"):
        if path.exists():
            with path.open(encoding="utf-8") as f:
                return yaml.safe_load(f)
    return {}


def fetch_metadata(video_id: str) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        *ytdlp_cmd(),
        "--print", "%(title)s",
        "--print", "%(description)s",
        "--print", "%(channel)s",
        "--print", "%(channel_url)s",
        "--no-download",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    title = lines[0] if lines else "FIFA World Cup 2026"
    channel = "FIFA"
    channel_url = "https://www.youtube.com/@fifa"
    description = ""
    if len(lines) > 1:
        # description may be multi-line; channel is near the end
        for i, ln in enumerate(lines[1:], 1):
            if ln.startswith("http") and "youtube" in ln:
                channel_url = ln
                break
        desc_end = next((i for i, ln in enumerate(lines) if "youtube.com" in ln), len(lines))
        description = "\n".join(lines[1:desc_end]).strip()
        for ln in lines:
            if "FIFA" in ln and not ln.startswith("http"):
                channel = ln
    return {
        "video_id": video_id,
        "title": title,
        "description": description[:500],
        "channel": channel,
        "channel_url": channel_url,
        "source_url": url,
    }


def teams_from_title(title: str) -> tuple[str, str]:
    m = re.search(r"([^|]+?)\s+vs\s+([^|]+)", title, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "Team A", "Team B"


def promo_script(meta: dict, variant: int) -> str:
    team_a, team_b = teams_from_title(meta["title"])
    hooks = [
        f"{team_a} versus {team_b}. FIFA just posted the official World Cup preview, and this is your quick breakdown before kickoff. "
        f"Who has the edge, what to watch, and why this match matters. "
        f"The full official preview is on FIFA's YouTube channel. Tap the link in the description to watch it there.",
        f"World Cup 2026 preview time. {team_a} against {team_b}. "
        f"Here is the fast take on form, key battles, and what FIFA highlighted in their official preview. "
        f"Want the full video? It is on the official FIFA channel. Link below.",
        f"Do not miss this one. {team_a} meet {team_b} at the World Cup. "
        f"FIFA's official preview is live, and we are breaking down the headline talking points in under a minute. "
        f"Full source video from FIFA is linked in the description. Go watch the original on their channel.",
    ]
    return hooks[variant % len(hooks)]


async def _tts(text: str, out: Path, voice: str = "en-GB-RyanNeural") -> None:
    import edge_tts

    out.parent.mkdir(parents=True, exist_ok=True)
    await edge_tts.Communicate(text, voice).save(str(out))


def render_credit_overlay(size: tuple[int, int], font_path, source_url: str) -> np.ndarray:
    from PIL import ImageDraw

    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([40, h - 200, w - 40, h - 60], radius=16, fill=(0, 0, 0, 180))
    font = None
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(str(font_path), 32) if font_path else ImageFont.load_default()
    except OSError:
        from PIL import ImageFont
        font = ImageFont.load_default()
    lines = ["Source: @fifa (official)", "Full video linked in description"]
    y = h - 175
    for line in lines:
        draw.text((60, y), line, font=font, fill=(255, 255, 255, 255))
        y += 42
    return np.array(img)


def build_promo(video_id: str, variant: int = 0) -> Path:
    meta = fetch_metadata(video_id)
    script = promo_script(meta, variant)
    hook = HOOKS[variant % len(HOOKS)]

    clip_id = promo_clip_id(video_id, variant)
    audio_path = AUDIO_DIR / f"{clip_id}.mp3"
    out_path = PROMO_DIR / f"{clip_id}.mp4"

    if not audio_path.exists():
        asyncio.run(_tts(script, audio_path))

    root_cfg = load_root_config()
    font_path = resolve_font(root_cfg)
    width, height = 1080, 1920

    voice = AudioFileClip(str(audio_path))
    duration = voice.duration

    bg_result = pick_background(1 + variant)
    bg_path = bg_result[0] if isinstance(bg_result, tuple) else bg_result
    base = build_background_clip(bg_path, width, height, duration)

    hook_img = render_hook_image(hook, (width, height), font_path, 52)
    hook_clip = ImageClip(hook_img).set_duration(min(3.0, duration)).set_start(0)

    credit = render_credit_overlay((width, height), font_path, meta["source_url"])
    credit_clip = ImageClip(credit).set_duration(duration).set_start(0)

    video = CompositeVideoClip([base, credit_clip, hook_clip], size=(width, height))
    video = video.set_duration(duration).set_audio(voice)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    video.write_videofile(str(out_path), fps=30, codec="libx264", audio_codec="aac", logger=None)
    voice.close()
    video.close()
    print(f"  built {out_path.name} ({duration:.1f}s)")
    return out_path, meta, hook


def promo_description(meta: dict, hook: str) -> str:
    return f"""{meta['title']}

{hook}

▶️ FULL OFFICIAL VIDEO (FIFA): {meta['source_url']}
📺 FIFA channel: {meta['channel_url']}

This Short uses original commentary and royalty-free visuals.
It does NOT re-upload FIFA footage. All match media rights belong to FIFA.

#WorldCup2026 #FIFA #football #Shorts #KoreaRepublic #Czechia
"""
