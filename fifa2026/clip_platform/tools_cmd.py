"""Resolve yt-dlp / ffmpeg binaries (venv, PATH, Homebrew)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def _venv_bin(name: str) -> Path | None:
    exe = Path(sys.executable).resolve().parent / name
    return exe if exe.exists() else None


def ytdlp_cmd() -> list[str]:
    for candidate in (
        _venv_bin("yt-dlp"),
        shutil.which("yt-dlp"),
        Path("/opt/homebrew/bin/yt-dlp"),
    ):
        if candidate:
            return [str(candidate)]
    return [sys.executable, "-m", "yt_dlp"]


def ffmpeg_cmd() -> str:
    for candidate in (
        shutil.which("ffmpeg"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError("ffmpeg not found. Install: brew install ffmpeg")
