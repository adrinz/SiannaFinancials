"""Shared helpers for the FIFA Shorts automation pipeline."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_CSV = PROJECT_ROOT / "data" / "youtube_shorts.csv"
CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"
CONFIG_EXAMPLE = PROJECT_ROOT / "config" / "settings.example.yaml"
OUTPUT_AUDIO = PROJECT_ROOT / "output" / "audio"
OUTPUT_VIDEO = PROJECT_ROOT / "output" / "video"
OUTPUT_REVIEW = PROJECT_ROOT / "output" / "review"
OUTPUT_APPROVED = OUTPUT_REVIEW / "approved"
POST_LOG = PROJECT_ROOT / "post_log.csv"
SECRETS_DIR = PROJECT_ROOT / "secrets"
ASSETS_BACKGROUNDS = PROJECT_ROOT / "assets" / "backgrounds"
ASSETS_MUSIC = PROJECT_ROOT / "assets" / "music"
ASSETS_FONTS = PROJECT_ROOT / "assets" / "fonts"
DEFAULT_BG = ASSETS_BACKGROUNDS / "default_bg.png"


def load_config() -> dict:
    path = CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_EXAMPLE
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_shorts_df() -> pd.DataFrame:
    df = pd.read_csv(DATA_CSV)
    df["Day"] = df["Day"].astype(int)
    return df


def parse_day_range(spec: str) -> list[int]:
    """Parse '3' or '1-7' into a sorted list of day numbers."""
    spec = spec.strip()
    if "-" in spec:
        start_s, end_s = spec.split("-", 1)
        start, end = int(start_s), int(end_s)
        if start > end:
            raise ValueError(f"Invalid day range: {spec}")
        return list(range(start, end + 1))
    return [int(spec)]


def audio_path(day: int) -> Path:
    return OUTPUT_AUDIO / f"day_{day:02d}.mp3"


def review_video_path(day: int) -> Path:
    return OUTPUT_REVIEW / f"day_{day:02d}.mp4"


def review_cover_path(day: int) -> Path:
    return OUTPUT_REVIEW / f"day_{day:02d}_cover.jpg"


def approved_video_path(day: int) -> Path:
    return OUTPUT_APPROVED / f"day_{day:02d}.mp4"


def approved_cover_path(day: int) -> Path:
    return OUTPUT_APPROVED / f"day_{day:02d}_cover.jpg"


def day_from_filename(name: str) -> int | None:
    match = re.match(r"day_(\d+)\.mp4$", name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def parse_hashtag_tags(raw: str, limit: int = 15) -> list[str]:
    tags = [t.lstrip("#").strip() for t in re.split(r"[\s,]+", raw.strip()) if t.strip()]
    return [t[:30] for t in tags if t][:limit]


def resolve_font(config: dict) -> Path | None:
    configured = PROJECT_ROOT / config.get("captions", {}).get("font", "")
    if configured.exists():
        return configured
    for candidate in (
        ASSETS_FONTS / "Inter-Bold.ttf",
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Helvetica.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ):
        if candidate.exists():
            return candidate
    return None


def split_caption_chunks(text: str, max_words: int = 8) -> list[str]:
    """Split narration into short on-screen caption chunks."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks: list[str] = []
    for sentence in sentences:
        words = sentence.split()
        for i in range(0, len(words), max_words):
            chunk = " ".join(words[i : i + max_words]).strip()
            if chunk:
                chunks.append(chunk)
    return chunks or [text.strip()]


def chunk_durations(chunks: Iterable[str], total_seconds: float) -> list[float]:
    parts = list(chunks)
    weights = [max(len(c), 1) for c in parts]
    total_weight = sum(weights)
    return [total_seconds * w / total_weight for w in weights]
