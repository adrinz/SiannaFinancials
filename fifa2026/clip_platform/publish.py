#!/usr/bin/env python3
"""Publish approved clips to configured platforms."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from db import connect, init_db, utcnow
from publishers.instagram_publisher import publish_reel as ig_publish
from publishers.tiktok_publisher import publish_video as tiktok_publish
from publishers.youtube_publisher import publish_short

PLATFORM_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PLATFORM_DIR / "config.yaml"
CONFIG_EXAMPLE = PLATFORM_DIR / "config.example.yaml"
APPROVED_DIR = PLATFORM_DIR / "data" / "clips" / "approved"


def load_config() -> dict:
    path = CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_EXAMPLE
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def approve_clip(clip_id: str) -> Path:
    review = PLATFORM_DIR / "data" / "clips" / "review" / f"{clip_id}.mp4"
    if not review.exists():
        raise FileNotFoundError(f"Clip not found: {review}")
    APPROVED_DIR.mkdir(parents=True, exist_ok=True)
    dest = APPROVED_DIR / f"{clip_id}.mp4"
    if not dest.exists():
        dest.write_bytes(review.read_bytes())
    with connect() as conn:
        conn.execute("UPDATE clips SET status = 'approved' WHERE clip_id = ?", (clip_id,))
        conn.commit()
    return dest


def publish_clip(clip_id: str, config: dict) -> None:
    with connect() as conn:
        clip = conn.execute("SELECT * FROM clips WHERE clip_id = ?", (clip_id,)).fetchone()
        if not clip:
            raise ValueError(f"Unknown clip_id: {clip_id}")
        video = conn.execute(
            "SELECT * FROM source_videos WHERE video_id = ?", (clip["video_id"],)
        ).fetchone()

    file_path = approve_clip(clip_id)
    pub = config.get("publish", {})
    title = clip["title"] + pub.get("title_suffix", " #Shorts")
    desc = pub.get("description_template", "{title}\n{source_url}").format(
        title=clip["title"],
        source_url=video["source_url"],
        channel_handle=config["source"].get("channel_handle", ""),
    )

    platforms = []
    if pub.get("youtube"):
        vid = publish_short(
            file_path, title, desc,
            category_id=str(config.get("platforms", {}).get("youtube", {}).get("category_id", "17")),
        )
        platforms.append(("youtube", vid))
        print(f"  youtube → {vid}")

    if pub.get("tiktok"):
        tid = tiktok_publish(file_path, title, config)
        platforms.append(("tiktok", tid))
        print(f"  tiktok → {tid}")

    if pub.get("instagram"):
        iid = ig_publish(file_path, desc, config)
        platforms.append(("instagram", iid))
        print(f"  instagram → {iid}")

    with connect() as conn:
        for platform, platform_id in platforms:
            conn.execute(
                """
                INSERT INTO posts (clip_id, platform, platform_video_id, status, posted_at)
                VALUES (?, ?, ?, 'posted', ?)
                """,
                (clip_id, platform, platform_id, utcnow()),
            )
        conn.execute("UPDATE clips SET status = 'posted' WHERE clip_id = ?", (clip_id,))
        conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish approved clips")
    parser.add_argument("--clip-id", required=True)
    args = parser.parse_args()

    init_db()
    config = load_config()
    try:
        publish_clip(args.clip_id, config)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
