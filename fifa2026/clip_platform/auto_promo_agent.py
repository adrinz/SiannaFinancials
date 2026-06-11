#!/usr/bin/env python3
"""
Auto agent: watch @fifa → build copyright-safe promo Shorts → publish to YouTube.

Run continuously (use PYTHONUNBUFFERED=1 in background):
  python clip_platform/auto_promo_agent.py --loop

One-shot (e.g. cron every 5 min):
  python clip_platform/auto_promo_agent.py --once
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

from db import init_db, insert_video, list_videos_by_status, mark_video_processed, source_promo_fully_published, video_count
from monitor import check_once, fetch_latest, load_config
from promo_publish import publish_promos_for_video

PLATFORM_DIR = Path(__file__).resolve().parent


def baseline_seen_videos(config: dict) -> None:
    """First run: mark recent uploads as seen so we don't promo-blast the backlog."""
    if video_count() > 0:
        return
    entries = fetch_latest(config)
    if not entries:
        print("Baseline skipped: could not fetch channel uploads")
        return
    for entry in entries:
        insert_video(**entry, status="seen")
    print(f"Baseline: marked {len(entries)} recent @fifa uploads as seen (won't auto-promo)")


def process_video(video_id: str, config: dict) -> None:
    auto = config.get("auto_promo", {})
    variants = int(auto.get("variants", 1))
    auto_publish = bool(auto.get("auto_publish", True))

    print(f"\n▶ Processing {video_id}")

    if auto_publish and source_promo_fully_published(video_id, variants):
        mark_video_processed(video_id, status="promo_published")
        print(f"  skip (already published)")
        return

    if auto_publish:
        publish_promos_for_video(video_id, config)
    else:
        from promo_builder import build_promo, promo_clip_id

        for v in range(variants):
            build_promo(video_id, variant=v)
            print(f"  built {promo_clip_id(video_id, v)} (review only — auto_publish=false)")

    status = "promo_published" if auto_publish else "promo_built"
    mark_video_processed(video_id, status=status)
    print(f"  done ({status})")


def run_cycle(config: dict, *, process_backlog: bool = False) -> int:
    """One poll + process cycle. Returns number of videos processed."""
    new_videos = check_once(config, verbose=True)
    to_process = list(new_videos)

    if process_backlog:
        for row in list_videos_by_status("detected"):
            vid = row["video_id"]
            if not any(v["video_id"] == vid for v in to_process):
                to_process.append({"video_id": vid, "title": row["title"]})

    if not to_process:
        return 0

    max_per = int(config.get("auto_promo", {}).get("max_videos_per_cycle", 1))
    to_process = to_process[:max_per]

    for entry in to_process:
        try:
            process_video(entry["video_id"], config)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {entry['video_id']}: {exc}", file=sys.stderr)

    return len(to_process)


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-build & publish promo Shorts when @fifa uploads")
    parser.add_argument("--loop", action="store_true", help="Run forever (poll interval from config)")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit (for cron)")
    parser.add_argument("--backlog", action="store_true", help="Also process detected queue from prior runs")
    parser.add_argument("--rebaseline", action="store_true", help="Reset: mark all current uploads as seen")
    args = parser.parse_args()

    if not args.loop and not args.once:
        parser.error("Use --loop or --once")

    init_db()
    config = load_config()
    auto = config.get("auto_promo", {})
    interval = int(auto.get("poll_interval_sec", config["source"].get("poll_interval_sec", 300)))

    handle = config["source"].get("channel_handle", "@fifa")
    print(f"Auto promo agent — {handle} → @ScrollandSoull")
    print(f"  variants={auto.get('variants', 3)}  auto_publish={auto.get('auto_publish', True)}  interval={interval}s")

    if args.rebaseline:
        from db import connect

        with connect() as conn:
            conn.execute("DELETE FROM source_videos")
            conn.commit()
        print("Cleared video DB for rebaseline")

    baseline_seen_videos(config)

    if args.loop:
        print(f"Watching for new uploads every {interval}s … (Ctrl+C to stop)\n")
        while True:
            run_cycle(config, process_backlog=args.backlog)
            time.sleep(interval)
    else:
        n = run_cycle(config, process_backlog=args.backlog)
        print(f"\nCycle complete — processed {n} video(s)")


if __name__ == "__main__":
    main()
