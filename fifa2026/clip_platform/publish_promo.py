#!/usr/bin/env python3
"""Publish copyright-safe promo Shorts (public by default)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from db import init_db, mark_video_processed
from promo_publish import publish_promos_for_video

PLATFORM_DIR = Path(__file__).resolve().parent


def load_config() -> dict:
    for path in (PLATFORM_DIR / "config.yaml", PLATFORM_DIR / "config.example.yaml"):
        if path.exists():
            with path.open(encoding="utf-8") as f:
                return yaml.safe_load(f)
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish promo Shorts to YouTube")
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--variants", type=int, default=None, help="Override config variants")
    parser.add_argument("--public", action="store_true", default=True)
    args = parser.parse_args()

    init_db()
    config = load_config()
    if args.variants is not None:
        config.setdefault("auto_promo", {})["variants"] = args.variants
    if not args.public:
        config.setdefault("publish", {})["privacy"] = config.get("publish", {}).get("privacy", "private")

    count = publish_promos_for_video(args.video_id, config, rebuild_if_missing=False)
    if count:
        mark_video_processed(args.video_id, status="promo_published")

    print(f"\nPublished {count} promo Short(s) to @ScrollandSoull")
    if count == 0:
        print("Nothing new to publish (already uploaded or missing MP4 files).")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
