#!/usr/bin/env python3
"""Run voiceover → video → thumbnail for a range of days."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow `python scripts/run_batch.py` from project root
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_video import build_video
from common import OUTPUT_REVIEW, parse_day_range, review_cover_path, review_video_path
from generate_voiceover import main_async as voiceover_async
from make_thumbnail import make_thumbnail


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-build FIFA Shorts")
    parser.add_argument("--days", default="1", help="Day number or range, e.g. 1-7")
    parser.add_argument("--force", action="store_true", help="Regenerate existing outputs")
    parser.add_argument("--skip-voice", action="store_true")
    parser.add_argument("--skip-video", action="store_true")
    parser.add_argument("--skip-thumb", action="store_true")
    args = parser.parse_args()

    days = parse_day_range(args.days)
    OUTPUT_REVIEW.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"FIFA Shorts batch — days {days[0]}–{days[-1]} ({len(days)} total)")
    print("=" * 60)

    if not args.skip_voice:
        print("\n[1/3] Voiceovers")
        code = asyncio.run(voiceover_async(days, args.force))
        if code:
            raise SystemExit(code)

    if not args.skip_video:
        print("\n[2/3] Videos")
        for day in days:
            try:
                build_video(day, force=args.force)
            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR day {day:02d}: {exc}", file=sys.stderr)

    if not args.skip_thumb:
        print("\n[3/3] Thumbnails")
        for day in days:
            try:
                make_thumbnail(day, force=args.force)
            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR day {day:02d}: {exc}", file=sys.stderr)

    print("\nDone. Review files in output/review/")
    print("Move approved MP4s to output/review/approved/ before upload.")
    for day in days:
        mp4 = review_video_path(day)
        jpg = review_cover_path(day)
        status = "✓" if mp4.exists() else "✗"
        print(f"  {status} day {day:02d}: {mp4.name}" + (f" + {jpg.name}" if jpg.exists() else ""))


if __name__ == "__main__":
    main()
