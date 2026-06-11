#!/usr/bin/env python3
"""Orchestrator: detect → download → find clips → render → queue for review."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

import yaml

from clip_finder import analyze_video
from clip_renderer import download_video, render_clip
from db import connect, init_db, insert_clip, list_pending_videos, mark_video_processed, utcnow
from monitor import check_once, load_config

PLATFORM_DIR = Path(__file__).resolve().parent
CLIPS_DIR = PLATFORM_DIR / "data" / "clips"
SOURCES_DIR = PLATFORM_DIR / "data" / "sources"
WORK_DIR = PLATFORM_DIR / "data" / "work"


def process_video(video_id: str, config: dict, *, dry_run: bool = False) -> list[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM source_videos WHERE video_id = ?", (video_id,)
        ).fetchone()
    if not row:
        raise ValueError(f"Unknown video_id {video_id}. Run monitor.py first.")

    source_url = row["source_url"]
    title = row["title"]
    print(f"Processing: {title}")
    print(f"  URL: {source_url}")

    if dry_run:
        print("  [dry-run] would download + analyze + render clips")
        return []

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    candidates = analyze_video(source_url, WORK_DIR, config)
    print(f"  found {len(candidates)} clip candidate(s)")

    source_mp4 = SOURCES_DIR / f"{video_id}.mp4"
    if not source_mp4.exists():
        print("  downloading source video …")
        download_video(source_url, source_mp4)

    created = []
    render_cfg = config.get("render", {})
    for i, cand in enumerate(candidates, start=1):
        clip_id = f"{video_id}_{i:02d}"
        out = CLIPS_DIR / "review" / f"{clip_id}.mp4"
        clip_title = f"{title[:60]} — Clip {i}"
        print(f"  rendering {clip_id} ({cand['start_sec']:.1f}s–{cand['end_sec']:.1f}s, score={cand['score']}) …")
        render_clip(
            source_mp4,
            cand["start_sec"],
            cand["end_sec"],
            out,
            width=int(render_cfg.get("width", 1080)),
            height=int(render_cfg.get("height", 1920)),
            fps=int(render_cfg.get("fps", 30)),
            title=clip_title,
        )
        status = "review" if config.get("publish", {}).get("require_review", True) else "approved"
        insert_clip(
            clip_id=clip_id,
            video_id=video_id,
            start_sec=cand["start_sec"],
            end_sec=cand["end_sec"],
            score=cand["score"],
            title=clip_title,
            file_path=str(out),
            status=status,
        )
        created.append({"clip_id": clip_id, "file": str(out), **cand})

    mark_video_processed(video_id)
    print(f"  done — {len(created)} clip(s) in data/clips/review/")
    return created


def main() -> None:
    parser = argparse.ArgumentParser(description="Run clip agent on new or specific videos")
    parser.add_argument("--video-id", help="Process a specific YouTube video ID")
    parser.add_argument("--latest", action="store_true", help="Process most recent pending video")
    parser.add_argument("--check", action="store_true", help="Check RSS then process all pending")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db()
    config = load_config()

    if args.check:
        check_once(config)

    if args.video_id:
        process_video(args.video_id, config, dry_run=args.dry_run)
        return

    pending = list_pending_videos()
    if not pending:
        print("No pending videos. Run: python monitor.py")
        raise SystemExit(0)

    target = pending[0] if args.latest or not args.video_id else None
    if target is None and args.check:
        for row in pending:
            process_video(row["video_id"], config, dry_run=args.dry_run)
        return

    vid = target["video_id"] if target else pending[0]["video_id"]
    process_video(vid, config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
