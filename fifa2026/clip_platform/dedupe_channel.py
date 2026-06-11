#!/usr/bin/env python3
"""
Remove duplicate Fan Preview Shorts from @ScrollandSoull.

Keeps the earliest upload per FIFA source title; deletes later copies.
Also cleans matching rows from clip_agent.db.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from db import delete_post_by_youtube_id, init_db
from publishers.youtube_publisher import delete_video
from youtube_auth import get_youtube_service


def _fan_preview_base(title: str) -> str | None:
    if "Fan Preview" not in title:
        return None
    return re.sub(r"\s*—\s*Fan Preview #Shorts\s*$", "", title).strip()


def find_duplicate_groups() -> list[tuple[dict, list[dict]]]:
    yt = get_youtube_service()
    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    uploads_pl = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    videos: list[dict] = []
    token = None
    while True:
        resp = yt.playlistItems().list(
            part="snippet", playlistId=uploads_pl, maxResults=50, pageToken=token
        ).execute()
        for item in resp.get("items", []):
            sn = item["snippet"]
            videos.append(
                {
                    "id": sn["resourceId"]["videoId"],
                    "title": sn["title"],
                    "published": sn["publishedAt"],
                }
            )
        token = resp.get("nextPageToken")
        if not token:
            break

    by_base: dict[str, list[dict]] = {}
    for v in videos:
        base = _fan_preview_base(v["title"])
        if not base:
            continue
        by_base.setdefault(base, []).append(v)

    groups: list[tuple[dict, list[dict]]] = []
    for items in by_base.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: x["published"])
        groups.append((items[0], items[1:]))
    return groups


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete duplicate Fan Preview uploads")
    parser.add_argument("--dry-run", action="store_true", help="List duplicates without deleting")
    args = parser.parse_args()

    init_db()
    groups = find_duplicate_groups()
    if not groups:
        print("No duplicate Fan Preview uploads found.")
        return

    total_remove = sum(len(remove) for _, remove in groups)
    print(f"Found {len(groups)} duplicate group(s) — {total_remove} video(s) to remove\n")

    removed = 0
    for keep, remove in groups:
        print(f"KEEP  {keep['id']}  {keep['title'][:70]}")
        for v in remove:
            print(f"  DEL {v['id']}  {v['published']}")
            if args.dry_run:
                continue
            try:
                delete_video(v["id"])
                delete_post_by_youtube_id(v["id"])
                removed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    ERROR: {exc}", file=sys.stderr)

    if args.dry_run:
        print(f"\nDry run — would delete {total_remove} video(s)")
    else:
        print(f"\nDeleted {removed} duplicate video(s) from YouTube and DB")


if __name__ == "__main__":
    main()
