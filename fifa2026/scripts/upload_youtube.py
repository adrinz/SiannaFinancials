#!/usr/bin/env python3
"""Upload approved FIFA Shorts to YouTube with scheduling and affiliate descriptions."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from common import (
    OUTPUT_APPROVED,
    POST_LOG,
    PROJECT_ROOT,
    approved_cover_path,
    approved_video_path,
    day_from_filename,
    load_config,
    load_shorts_df,
    parse_day_range,
    parse_hashtag_tags,
    review_cover_path,
)
from youtube_auth import get_youtube_service

load_dotenv(PROJECT_ROOT / ".env")

LOG_FIELDS = [
    "day",
    "video_id",
    "title",
    "status",
    "publish_at_utc",
    "uploaded_at_utc",
    "video_path",
]

REGION_TZ = {
    "americas": "America/New_York",
    "europe": "Europe/Paris",
    "asia": "Asia/Kolkata",
}


def load_post_log() -> dict[int, dict]:
    if not POST_LOG.exists():
        return {}
    with POST_LOG.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    out: dict[int, dict] = {}
    for row in rows:
        try:
            day = int(row["day"])
        except (KeyError, ValueError):
            continue
        if row.get("status") == "uploaded" and row.get("video_id"):
            out[day] = row
    return out


def append_post_log(row: dict) -> None:
    write_header = not POST_LOG.exists()
    with POST_LOG.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in LOG_FIELDS})


def build_title(short_title: str) -> str:
    title = short_title.strip()
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"
    return title[:100]


def build_description(row, config: dict) -> str:
    caption = str(row["YouTube Description / Caption (ready-to-paste)"]).strip()
    hashtags = str(row["Hashtags"]).strip()
    footer = config.get("upload", {}).get("affiliate_footer", "").strip()

    parts = [caption, "", hashtags, "#Shorts"]
    if footer:
        parts.extend(["", footer])
    return "\n".join(parts).strip()


def next_publish_at(region: str, config: dict) -> datetime:
    tz_name = REGION_TZ.get(region, REGION_TZ["americas"])
    tz = ZoneInfo(tz_name)
    time_key = region if region in config.get("posting_times", {}) else "americas"
    hh, mm = config["posting_times"][time_key].split(":")
    now_local = datetime.now(tz)
    candidate = now_local.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if candidate <= now_local + timedelta(minutes=20):
        candidate += timedelta(days=1)
    return candidate.astimezone(ZoneInfo("UTC"))


def discover_approved_days(explicit_days: list[int] | None) -> list[int]:
    if explicit_days:
        return explicit_days
    days: list[int] = []
    for path in sorted(OUTPUT_APPROVED.glob("day_*.mp4")):
        day = day_from_filename(path.name)
        if day is not None:
            days.append(day)
    return sorted(days)


def resolve_cover(day: int) -> Path | None:
    for path in (approved_cover_path(day), review_cover_path(day)):
        if path.exists():
            return path
    return None


def upload_one(
    day: int,
    *,
    dry_run: bool,
    region: str,
    publish_at: datetime | None,
    paid_promotion: bool,
) -> dict:
    video_path = approved_video_path(day)
    if not video_path.exists():
        raise FileNotFoundError(
            f"Missing approved video: {video_path}\n"
            f"Move output/review/day_{day:02d}.mp4 → output/review/approved/"
        )

    config = load_config()
    upload_cfg = config.get("upload", {})
    df = load_shorts_df()
    row = df.loc[df["Day"] == day]
    if row.empty:
        raise ValueError(f"No CSV metadata for day {day}")
    row = row.iloc[0]

    title = build_title(str(row["Short Title"]))
    description = build_description(row, config)
    tags = parse_hashtag_tags(str(row["Hashtags"]))
    if "Shorts" not in tags:
        tags.insert(0, "Shorts")

    publish_utc = publish_at or next_publish_at(region, config)
    publish_iso = publish_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    cover = resolve_cover(day)

    print(f"\nDay {day:02d}")
    print(f"  file:   {video_path.name}")
    print(f"  title:  {title}")
    print(f"  publish (UTC): {publish_iso}  (region={region})")
    print(f"  cover:  {cover.name if cover else 'none'}")

    if dry_run:
        print("  [dry-run] upload skipped")
        return {
            "day": day,
            "video_id": "",
            "title": title,
            "status": "dry_run",
            "publish_at_utc": publish_iso,
            "uploaded_at_utc": "",
            "video_path": str(video_path),
        }

    youtube = get_youtube_service()
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": str(upload_cfg.get("category_id", "17")),
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": bool(upload_cfg.get("made_for_kids", False)),
            "publishAt": publish_iso,
        },
    }
    if paid_promotion:
        body["status"]["paidPromotion"] = True

    media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True, chunksize=1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    try:
        while response is None:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                print(f"  uploading… {pct}%")
    except HttpError as exc:
        raise RuntimeError(f"YouTube API error: {exc}") from exc

    video_id = response["id"]
    print(f"  uploaded video_id={video_id}")

    if cover:
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(cover), mimetype="image/jpeg"),
            ).execute()
            print(f"  thumbnail set from {cover.name}")
        except HttpError as exc:
            print(f"  WARN: thumbnail upload failed: {exc}", file=sys.stderr)

    uploaded_at = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    log_row = {
        "day": day,
        "video_id": video_id,
        "title": title,
        "status": "uploaded",
        "publish_at_utc": publish_iso,
        "uploaded_at_utc": uploaded_at,
        "video_path": str(video_path),
    }
    append_post_log(log_row)
    print(f"  scheduled → https://studio.youtube.com/video/{video_id}/edit")
    return log_row


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload approved FIFA Shorts to YouTube")
    parser.add_argument("--days", help="Optional day or range, e.g. 1 or 1-3")
    parser.add_argument("--all", action="store_true", help="Upload every day_XX.mp4 in approved/")
    parser.add_argument("--region", choices=list(REGION_TZ), default="americas")
    parser.add_argument(
        "--publish-at",
        help="Override schedule as UTC ISO, e.g. 2026-06-11T16:00:00Z",
    )
    parser.add_argument("--paid-promotion", action="store_true", help="Mark as paid promotion")
    parser.add_argument("--dry-run", action="store_true", help="Print metadata only, no API call")
    parser.add_argument("--force", action="store_true", help="Re-upload even if day is in post_log")
    args = parser.parse_args()

    OUTPUT_APPROVED.mkdir(parents=True, exist_ok=True)

    if args.days:
        days = parse_day_range(args.days)
    elif args.all:
        days = discover_approved_days(None)
    else:
        parser.error("Pass --days N or --all")

    if not days:
        print("No approved videos found in output/review/approved/")
        raise SystemExit(1)

    posted = load_post_log()
    publish_override = None
    if args.publish_at:
        publish_override = datetime.fromisoformat(args.publish_at.replace("Z", "+00:00")).astimezone(
            ZoneInfo("UTC")
        )

    errors = 0
    for day in days:
        if day in posted and not args.force and not args.dry_run:
            print(f"skip day {day:02d} (already uploaded: {posted[day]['video_id']})")
            continue
        try:
            upload_one(
                day,
                dry_run=args.dry_run,
                region=args.region,
                publish_at=publish_override,
                paid_promotion=args.paid_promotion,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR day {day:02d}: {exc}", file=sys.stderr)
            errors += 1

    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
