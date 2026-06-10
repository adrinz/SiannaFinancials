#!/usr/bin/env python3
"""Render a vertical YouTube Short from voiceover + CSV metadata."""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

# moviepy 1.x still references Image.ANTIALIAS (removed in Pillow 10+)
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.Resampling.LANCZOS  # type: ignore[attr-defined]

from common import (
    ASSETS_BACKGROUNDS,
    ASSETS_MUSIC,
    DEFAULT_BG,
    OUTPUT_REVIEW,
    audio_path,
    load_config,
    load_shorts_df,
    resolve_font,
    review_video_path,
)
from visuals import (
    players_for_day,
    render_flag_strip,
    render_header,
    render_player_card,
    render_stadium_background,
)

try:
    from moviepy.editor import (
        AudioFileClip,
        CompositeAudioClip,
        CompositeVideoClip,
        ImageClip,
        concatenate_videoclips,
    )
except ImportError:  # moviepy 2.x
    from moviepy import (
        AudioFileClip,
        CompositeAudioClip,
        CompositeVideoClip,
        ImageClip,
        concatenate_videoclips,
    )


VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}
MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac"}


def list_stock_videos() -> list[Path]:
    """Return stock video loops only — never the static default_bg.png fallback."""
    ASSETS_BACKGROUNDS.mkdir(parents=True, exist_ok=True)
    videos = [
        p
        for p in sorted(ASSETS_BACKGROUNDS.iterdir())
        if p.is_file()
        and p.suffix.lower() in VIDEO_EXTENSIONS
        and p.name != DEFAULT_BG.name
    ]
    return videos


def list_stock_music() -> list[Path]:
    ASSETS_MUSIC.mkdir(parents=True, exist_ok=True)
    return [
        p
        for p in sorted(ASSETS_MUSIC.iterdir())
        if p.is_file() and p.suffix.lower() in MUSIC_EXTENSIONS
    ]


def ensure_default_background(width: int, height: int) -> Path:
    """Static pitch graphic — only used when no stock video loops are present."""
    if DEFAULT_BG.exists():
        return DEFAULT_BG

    frame = render_stadium_background((width, height))
    Image.fromarray(frame).save(DEFAULT_BG)
    return DEFAULT_BG


def pick_background(day: int) -> tuple[Path, str]:
    videos = list_stock_videos()
    if videos:
        # Stable pick per day so re-renders stay consistent
        path = videos[(day - 1) % len(videos)]
        return path, "stock_video"
    path = ensure_default_background(1080, 1920)
    return path, "default_pitch"


def pick_music(day: int) -> Path | None:
    tracks = list_stock_music()
    if not tracks:
        return None
    return tracks[(day - 1) % len(tracks)]


def load_font(font_path: Path | None, size: int):
    if font_path:
        try:
            from PIL import ImageFont

            return ImageFont.truetype(str(font_path), size=size)
        except OSError:
            pass
    from PIL import ImageFont

    return ImageFont.load_default()


def render_hook_image(
    text: str,
    size: tuple[int, int],
    font_path: Path | None,
    font_size: int,
) -> np.ndarray:
    width, height = size
    font = load_font(font_path, font_size)
    wrapped = textwrap.fill(text.upper(), width=16)

    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [60, height // 2 - 200, width - 60, height // 2 + 200],
        radius=28,
        fill=(0, 0, 0, 170),
        outline=(255, 230, 90, 255),
        width=4,
    )
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center", stroke_width=3)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.multiline_text(
        ((width - tw) // 2, (height - th) // 2),
        wrapped,
        font=font,
        fill="#FFE566",
        align="center",
        stroke_width=3,
        stroke_fill="#000000",
    )
    return np.array(img)


def _fit_cover(clip, width: int, height: int):
    """Scale clip to fully cover the frame (handles landscape + portrait stock)."""
    scale = max(width / clip.w, height / clip.h)
    clip = clip.resize(scale)
    return clip.crop(
        x_center=clip.w / 2,
        y_center=clip.h / 2,
        width=width,
        height=height,
    )


def build_background_clip(bg_path: Path, width: int, height: int, duration: float):
    suffix = bg_path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        try:
            from moviepy.editor import VideoFileClip
        except ImportError:
            from moviepy import VideoFileClip

        clip = VideoFileClip(str(bg_path)).without_audio()
        clip = _fit_cover(clip, width, height)
        if clip.duration < duration:
            loops = int(duration // clip.duration) + 1
            clip = concatenate_videoclips([clip] * loops)
        return clip.subclip(0, duration)

    frame = Image.open(bg_path).convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
    return ImageClip(np.array(frame)).set_duration(duration)


def build_player_layers(
    day: int,
    duration: float,
    hook_duration: float,
    size: tuple[int, int],
    font_path: Path | None,
    config: dict,
) -> list:
    video_cfg = config.get("video", {})
    if not video_cfg.get("player_spotlight", True):
        return []

    count = int(video_cfg.get("players_per_short", 5))
    players = players_for_day(day, count=count)
    spotlight_start = min(hook_duration, duration * 0.15)
    spotlight_duration = max(duration - spotlight_start, 1.0)
    seg = spotlight_duration / len(players)

    layers = []

    header = render_header(size, font_path)
    layers.append(ImageClip(header).set_duration(duration).set_start(0))

    strip = render_flag_strip(players, size, font_path)
    layers.append(ImageClip(strip).set_duration(duration).set_start(0))

    for i, player in enumerate(players):
        start = spotlight_start + i * seg
        seg_dur = min(seg, duration - start)
        if seg_dur <= 0:
            break
        card = render_player_card(player, size, font_path)
        layers.append(ImageClip(card).set_duration(seg_dur).set_start(start))

    return layers


def build_video(day: int, force: bool = False) -> Path:
    config = load_config()
    video_cfg = config.get("video", {})
    caption_cfg = config.get("captions", {})

    width = int(video_cfg.get("width", 1080))
    height = int(video_cfg.get("height", 1920))
    fps = int(video_cfg.get("fps", 30))
    hook_duration = float(video_cfg.get("hook_duration_sec", 2))
    music_volume = float(video_cfg.get("music_volume", 0.12))
    show_captions = bool(video_cfg.get("show_captions", False))

    df = load_shorts_df()
    row = df.loc[df["Day"] == day].iloc[0]
    hook = str(row["Hook (first line on screen)"]).strip()

    audio_file = audio_path(day)
    if not audio_file.exists():
        raise FileNotFoundError(f"Missing voiceover: {audio_file}. Run generate_voiceover.py first.")

    out = review_video_path(day)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and not force:
        print(f"  skip day {day:02d} (exists): {out.name}")
        return out

    font_path = resolve_font(config)
    caption_size = int(caption_cfg.get("font_size", 72))
    hook_size = int(caption_size * 1.1)

    voice = AudioFileClip(str(audio_file))
    duration = voice.duration

    bg_path, bg_source = pick_background(day)
    music_path = pick_music(day)
    print(f"  background: {bg_path.name} ({bg_source})")
    if music_path:
        print(f"  music:      {music_path.name}")
    else:
        print("  music:      none (add tracks to assets/music/)")

    base = build_background_clip(bg_path, width, height, duration)

    hook_img = render_hook_image(hook, (width, height), font_path, hook_size)
    hook_clip = ImageClip(hook_img).set_duration(min(hook_duration, duration)).set_start(0)

    layers = [base, *build_player_layers(day, duration, hook_duration, (width, height), font_path, config), hook_clip]

    video = CompositeVideoClip(layers, size=(width, height)).set_duration(duration)

    if music_path:
        try:
            from moviepy.editor import concatenate_audioclips
        except ImportError:
            from moviepy import concatenate_audioclips

        music = AudioFileClip(str(music_path)).volumex(music_volume)
        if music.duration < duration:
            loops = int(duration // music.duration) + 1
            music = concatenate_audioclips([music] * loops)
        music = music.subclip(0, duration)
        final_audio = CompositeAudioClip([voice, music])
    else:
        final_audio = voice

    video = video.set_audio(final_audio)

    print(f"  rendering day {day:02d} → {out.name} ({duration:.1f}s, captions=off, players=on) …")
    video.write_videofile(
        str(out),
        fps=fps,
        codec="libx264",
        audio_codec="aac",
        preset="medium",
        threads=4,
        logger=None,
    )

    voice.close()
    video.close()
    print(f"  saved {out}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a FIFA Short MP4 for one day")
    parser.add_argument("--day", type=int, required=True, help="Day number from CSV")
    parser.add_argument("--force", action="store_true", help="Re-render even if MP4 exists")
    args = parser.parse_args()

    try:
        build_video(args.day, force=args.force)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
