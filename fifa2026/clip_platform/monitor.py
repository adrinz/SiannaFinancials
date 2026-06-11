#!/usr/bin/env python3
"""Poll YouTube RSS for new uploads on a source channel (e.g. @fifa)."""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path

import feedparser
import requests
import yaml

from db import init_db, insert_video, list_pending_videos
from tools_cmd import ytdlp_cmd

PLATFORM_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PLATFORM_DIR / "config.yaml"
CONFIG_EXAMPLE = PLATFORM_DIR / "config.example.yaml"


def load_config() -> dict:
    path = CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_EXAMPLE
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def rss_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def fetch_latest_rss(channel_id: str) -> list[dict]:
    feed = feedparser.parse(rss_url(channel_id))
    entries = []
    for entry in feed.entries:
        video_id = entry.yt_videoid if hasattr(entry, "yt_videoid") else entry.id.split(":")[-1]
        entries.append(
            {
                "video_id": video_id,
                "title": entry.title,
                "published_at": entry.published,
                "source_url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return entries


def fetch_latest_api(channel_id: str, max_results: int = 15) -> list[dict]:
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        return []
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "channelId": channel_id,
        "order": "date",
        "type": "video",
        "maxResults": max_results,
        "key": api_key,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    entries = []
    for item in resp.json().get("items", []):
        vid = item["id"]["videoId"]
        snippet = item["snippet"]
        entries.append(
            {
                "video_id": vid,
                "title": snippet["title"],
                "published_at": snippet["publishedAt"],
                "source_url": f"https://www.youtube.com/watch?v={vid}",
            }
        )
    return entries


def fetch_latest_ytdlp(channel_url: str, max_results: int = 15) -> list[dict]:
    """Fallback: list recent uploads via yt-dlp (no API key needed)."""
    playlist_url = channel_url.rstrip("/") + "/videos"
    cmd = [
        *ytdlp_cmd(),
        "--flat-playlist",
        "--print", "%(id)s|||%(title)s|||%(upload_date)s",
        "--playlist-end", str(max_results),
        playlist_url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return []

    entries = []
    for line in proc.stdout.splitlines():
        if "|||" not in line:
            continue
        vid, title, upload_date = line.split("|||", 2)
        published = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}" if upload_date else ""
        entries.append(
            {
                "video_id": vid,
                "title": title,
                "published_at": published,
                "source_url": f"https://www.youtube.com/watch?v={vid}",
            }
        )
    return entries


def fetch_latest(config: dict) -> list[dict]:
    source = config["source"]
    channel_id = source["channel_id"]
    channel_url = source.get("channel_url", f"https://www.youtube.com/channel/{channel_id}")

    for fetcher_name, fetcher in (
        ("YouTube API", lambda: fetch_latest_api(channel_id)),
        ("RSS", lambda: fetch_latest_rss(channel_id)),
        ("yt-dlp", lambda: fetch_latest_ytdlp(channel_url)),
    ):
        try:
            entries = fetcher()
            if entries:
                if fetcher_name != "YouTube API":
                    print(f"  (via {fetcher_name})")
                return entries
        except Exception as exc:  # noqa: BLE001
            print(f"  {fetcher_name} failed: {exc}")
    return []


def check_once(config: dict, verbose: bool = True, *, seed: bool = False) -> list[dict]:
    channel_id = config["source"]["channel_id"]
    handle = config["source"].get("channel_handle", "")
    new_videos = []

    entries = fetch_latest(config)
    if not entries:
        if verbose:
            print(f"Could not fetch uploads for {handle or channel_id}")
        return []

    for entry in entries:
        if insert_video(**entry):
            new_videos.append(entry)
            if verbose:
                print(f"NEW  [{entry['video_id']}] {entry['title']}")
                print(f"     {entry['source_url']}")

    if verbose and not new_videos:
        print(f"No new videos on {handle or channel_id} (checked {len(entries)} recent)")

    pending = list_pending_videos()
    if verbose and pending:
        print(f"\nPending processing: {len(pending)} video(s)")

    return new_videos


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor YouTube channel for new uploads")
    parser.add_argument("--loop", action="store_true", help="Poll continuously")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--seed", action="store_true", help="Import recent uploads into DB (first-time setup)")
    args = parser.parse_args()

    init_db()
    config = load_config()
    interval = int(config["source"].get("poll_interval_sec", 300))
    seed = args.seed or config["source"].get("seed_on_first_run", True)

    if args.loop:
        print(f"Monitoring {config['source'].get('channel_handle', config['source']['channel_id'])} every {interval}s …")
        while True:
            check_once(config, verbose=not args.quiet, seed=seed)
            seed = False
            time.sleep(interval)
    else:
        check_once(config, verbose=not args.quiet, seed=seed)


if __name__ == "__main__":
    main()
