#!/usr/bin/env python3
"""Render a vertical YouTube Short from voiceover + CSV metadata."""

from __future__ import annotations

import argparse
import random
import sys
import textwrap
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from common import (
    ASSETS_BACKGROUNDS,
    ASSETS_MUSIC,
    DEFAULT_BG,
    OUTPUT_REVIEW,
    PROJECT_ROOT,
    audio_path,
    chunk_durations,
    load_config,
    load_shorts_df,
    resolve_font,
    review_video_path,
    split_caption_chunks,
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


def ensure_default_background(width: int, height: int) -> Path:
    if DEFAULT_BG.exists():
        return DEFAULT_BG

    ASSETS_BACKGROUNDS.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    top = (8, 42, 28)
    bottom = (2, 14, 10)
    for y in range(height):
        t = y / max(height - 1, 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)

    # Subtle pitch lines
    line_color = (20, 90, 55)
    for y in range(height // 4, height, height // 8):
        draw.line([(80, y), (width - 80, y)], fill=line_color, width=2)
    center_x = width // 2
    draw.ellipse(
        [center_x - 120, height // 2 - 180, center_x + 120, height // 2 + 180],
        outline=(25, 110, 65),
        width=4,
    )

    img.save(DEFAULT_BG)
    return DEFAULT_BG


def pick_background(width: int, height: int) -> Path | None:
    videos = sorted(ASSETS_BACKGROUNDS.glob("*.mp4")) + sorted(
        ASSETS_BACKGROUNDS.glob("*.mov")
    )
    if videos:
        return random.choice(videos)
    ensure_default_background(width, height)
    return DEFAULT_BG


def load_font(font_path: Path | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if font_path:
        try:
            return ImageFont.truetype(str(font_path), size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def render_text_image(
    text: str,
    size: tuple[int, int],
    font_path: Path | None,
    font_size: int,
    fill: str,
    stroke_fill: str,
    stroke_width: int,
    max_width_ratio: float = 0.88,
    y_anchor: str = "center",
) -> np.ndarray:
    width, height = size
    font = load_font(font_path, font_size)
    max_px = int(width * max_width_ratio)
    wrapped = textwrap.fill(text, width=max(12, max_px // max(font_size // 2, 1)))

    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center", stroke_width=stroke_width)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (width - tw) // 2
    if y_anchor == "top":
        y = int(height * 0.12)
    elif y_anchor == "bottom":
        y = int(height * 0.68)
    else:
        y = (height - th) // 2

    draw.multiline_text(
        (x, y),
        wrapped,
        font=font,
        fill=fill,
        align="center",
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )
    return np.array(img)


def build_background_clip(bg_path: Path, width: int, height: int, duration: float):
    suffix = bg_path.suffix.lower()
    if suffix in {".mp4", ".mov", ".webm"}:
        try:
            from moviepy.editor import VideoFileClip
        except ImportError:
            from moviepy import VideoFileClip

        clip = VideoFileClip(str(bg_path)).without_audio()
        clip = clip.resize(height=height)
        if clip.w < width:
            clip = clip.resize(width=width)
        clip = clip.crop(x_center=clip.w / 2, width=width, y_center=clip.h / 2, height=height)
        if clip.duration < duration:
            loops = int(duration // clip.duration) + 1
            clip = concatenate_videoclips([clip] * loops)
        return clip.subclip(0, duration)

    frame = Image.open(bg_path).convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
    return ImageClip(np.array(frame)).set_duration(duration)


def build_video(day: int, force: bool = False) -> Path:
    config = load_config()
    video_cfg = config.get("video", {})
    caption_cfg = config.get("captions", {})

    width = int(video_cfg.get("width", 1080))
    height = int(video_cfg.get("height", 1920))
    fps = int(video_cfg.get("fps", 30))
    hook_duration = float(video_cfg.get("hook_duration_sec", 2))
    music_volume = float(video_cfg.get("music_volume", 0.12))

    df = load_shorts_df()
    row = df.loc[df["Day"] == day].iloc[0]
    hook = str(row["Hook (first line on screen)"]).strip()
    script = str(row["Voiceover Script (ready-to-paste)"]).strip()

    audio_file = audio_path(day)
    if not audio_file.exists():
        raise FileNotFoundError(f"Missing voiceover: {audio_file}. Run generate_voiceover.py first.")

    out = review_video_path(day)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and not force:
        print(f"  skip day {day:02d} (exists): {out.name}")
        return out

    font_path = resolve_font(config)
    fill = caption_cfg.get("color", "#FFFFFF")
    stroke = caption_cfg.get("stroke_color", "#000000")
    stroke_w = int(caption_cfg.get("stroke_width", 3))
    caption_size = int(caption_cfg.get("font_size", 72))
    hook_size = int(caption_size * 1.15)

    voice = AudioFileClip(str(audio_file))
    duration = voice.duration

    bg_path = pick_background(width, height)
    base = build_background_clip(bg_path, width, height, duration)

    hook_img = render_text_image(
        hook.upper(),
        (width, height),
        font_path,
        hook_size,
        fill="#FFE566",
        stroke_fill="#000000",
        stroke_width=stroke_w + 1,
        y_anchor="center",
    )
    hook_clip = ImageClip(hook_img).set_duration(min(hook_duration, duration)).set_start(0)

    layers = [base, hook_clip]

    chunks = split_caption_chunks(script)
    durations = chunk_durations(chunks, max(duration - 0.2, 0.5))
    t = 0.15
    for chunk, seg_dur in zip(chunks, durations):
        if t >= duration:
            break
        seg_dur = min(seg_dur, duration - t)
        cap_img = render_text_image(
            chunk,
            (width, height),
            font_path,
            caption_size,
            fill=fill,
            stroke_fill=stroke,
            stroke_width=stroke_w,
            y_anchor=caption_cfg.get("position", "bottom"),
        )
        cap_clip = ImageClip(cap_img).set_duration(seg_dur).set_start(t)
        layers.append(cap_clip)
        t += seg_dur

    video = CompositeVideoClip(layers, size=(width, height)).set_duration(duration)

    music_files = sorted(ASSETS_MUSIC.glob("*.mp3")) + sorted(ASSETS_MUSIC.glob("*.wav"))
    if music_files:
        try:
            from moviepy.editor import concatenate_audioclips
        except ImportError:
            from moviepy import concatenate_audioclips

        music = AudioFileClip(str(random.choice(music_files))).volumex(music_volume)
        if music.duration < duration:
            loops = int(duration // music.duration) + 1
            music = concatenate_audioclips([music] * loops)
        music = music.subclip(0, duration)
        final_audio = CompositeAudioClip([voice, music])
    else:
        final_audio = voice

    video = video.set_audio(final_audio)

    print(f"  rendering day {day:02d} → {out.name} ({duration:.1f}s) …")
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
