#!/usr/bin/env python3
"""CLI: build copyright-safe promo Shorts for a FIFA source video."""

from __future__ import annotations

import argparse
import sys

from promo_builder import build_promo


def main() -> None:
    parser = argparse.ArgumentParser(description="Build safe promo Shorts (no FIFA footage)")
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--variants", type=int, default=3, help="Number of promo Shorts (default 3)")
    args = parser.parse_args()

    print(f"Building {args.variants} copyright-safe promo Short(s) for {args.video_id} …")
    for v in range(args.variants):
        try:
            build_promo(args.video_id, variant=v)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR variant {v + 1}: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
    print("Done. Publish with: python clip_platform/publish_promo.py --video-id", args.video_id)


if __name__ == "__main__":
    main()
