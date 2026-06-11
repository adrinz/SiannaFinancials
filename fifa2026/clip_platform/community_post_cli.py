#!/usr/bin/env python3
"""Generate or list queued YouTube Community post drafts."""

from __future__ import annotations

import argparse
from pathlib import Path

from community_post import QUEUE_DIR, community_post_text, queue_community_post
from promo_builder import fetch_metadata

PLATFORM_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Community post helper for @fifa uploads")
    parser.add_argument("--video-id", help="FIFA source video id")
    parser.add_argument("--promo-id", help="Your promo Short YouTube id (optional)")
    parser.add_argument("--list", action="store_true", help="List queued community drafts")
    args = parser.parse_args()

    if args.list:
        files = sorted(QUEUE_DIR.glob("*.txt")) if QUEUE_DIR.exists() else []
        if not files:
            print("No community drafts queued.")
            return
        for f in files:
            print(f"\n=== {f.name} ===")
            print(f.read_text(encoding="utf-8")[:500])
        return

    if not args.video_id:
        parser.error("Use --video-id or --list")

    meta = fetch_metadata(args.video_id)
    path = queue_community_post(args.video_id, meta, promo_youtube_id=args.promo_id)
    print(community_post_text(meta, promo_youtube_id=args.promo_id))
    print(f"\nSaved → {path}")
    print("Paste in YouTube Studio → Content → Community")


if __name__ == "__main__":
    main()
