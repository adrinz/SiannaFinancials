#!/usr/bin/env python3
"""Publish promo Shorts that were built but not uploaded; fix DB status."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from db import connect, init_db, mark_video_processed, source_promo_fully_published
from promo_publish import publish_promos_for_video

PLATFORM_DIR = Path(__file__).resolve().parent


def load_config() -> dict:
    for path in (PLATFORM_DIR / "config.yaml", PLATFORM_DIR / "config.example.yaml"):
        if path.exists():
            with path.open(encoding="utf-8") as f:
                return yaml.safe_load(f)
    return {}


def main() -> None:
    init_db()
    config = load_config()
    variants = int(config.get("auto_promo", {}).get("variants", 1))

    with connect() as c:
        pending = c.execute(
            """
            SELECT video_id, title FROM source_videos
            WHERE status IN ('detected', 'promo_built')
            ORDER BY detected_at ASC
            """
        ).fetchall()

    if not pending:
        print("No pending videos to resume.")
        return

    total = 0
    for row in pending:
        video_id = row["video_id"]
        if source_promo_fully_published(video_id, variants):
            mark_video_processed(video_id, status="promo_published")
            print(f"\n▶ {video_id} — already published, marking done")
            continue

        print(f"\n▶ {video_id} — {row['title'][:60]}")
        try:
            total += publish_promos_for_video(video_id, config)
            mark_video_processed(video_id, status="promo_published")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {exc}", file=sys.stderr)
            continue

    print(f"\nResume complete — published {total} missing promo Short(s)")


if __name__ == "__main__":
    main()
