#!/usr/bin/env python3
"""Extract a cover frame from a rendered Short with bold title text."""

from __future__ import annotations

import argparse
import re
import sys

from PIL import Image

from build_video import render_text_image
from common import load_shorts_df, resolve_font, load_config, review_cover_path, review_video_path


def strongest_title_words(title: str, count: int = 3) -> str:
    words = [w for w in re.split(r"\s+", title.strip()) if w]
    if len(words) <= count:
        return title.upper()
    # Prefer longer impactful words, keep original order
    ranked = sorted(words, key=len, reverse=True)[:count]
    ordered = [w for w in words if w in ranked]
    return " ".join(ordered[:count]).upper()


def make_thumbnail(day: int, force: bool = False) -> None:
    config = load_config()
    video = review_video_path(day)
    if not video.exists():
        raise FileNotFoundError(f"Missing video: {video}. Run build_video.py first.")

    out = review_cover_path(day)
    if out.exists() and not force:
        print(f"  skip day {day:02d} cover (exists)")
        return

    row = load_shorts_df().loc[load_shorts_df()["Day"] == day].iloc[0]
    title = strongest_title_words(str(row["Short Title"]))

    try:
        from moviepy.editor import VideoFileClip
    except ImportError:
        from moviepy import VideoFileClip

    clip = VideoFileClip(str(video))
    frame = clip.get_frame(min(1.0, clip.duration * 0.15))
    clip.close()

    width, height = frame.shape[1], frame.shape[0]
    font_path = resolve_font(config)
    caption_cfg = config.get("captions", {})
    overlay = render_text_image(
        title,
        (width, height),
        font_path,
        int(caption_cfg.get("font_size", 72) * 1.3),
        fill="#FFE566",
        stroke_fill="#000000",
        stroke_width=4,
        y_anchor="top",
    )

    base = Image.fromarray(frame.astype("uint8"))
    top = Image.fromarray(overlay)
    base.paste(top, (0, 0), top)
    base.save(out, quality=92)
    print(f"  saved {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create YouTube Short cover image")
    parser.add_argument("--day", type=int, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        make_thumbnail(args.day, force=args.force)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
