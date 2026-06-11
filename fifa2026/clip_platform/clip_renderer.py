#!/usr/bin/env python3
"""Render vertical clip files from source video + timestamps."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools_cmd import ffmpeg_cmd, ytdlp_cmd


def download_video(video_url: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        *ytdlp_cmd(),
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(out_path),
        video_url,
    ]
    subprocess.run(cmd, check=True)
    return out_path


def render_clip(
    source_mp4: Path,
    start_sec: float,
    end_sec: float,
    out_path: Path,
    *,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    title: str = "",
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration = end_sec - start_sec

    # Center-crop to 9:16 vertical
    vf = (
        f"crop=ih*9/16:ih:(iw-ih*9/16)/2:0,"
        f"scale={width}:{height},"
        f"fps={fps}"
    )
    cmd = [
        ffmpeg_cmd(), "-y",
        "-ss", str(start_sec),
        "-i", str(source_mp4),
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path
