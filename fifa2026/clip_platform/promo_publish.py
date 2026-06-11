"""Shared promo publish helpers with deduplication."""

from __future__ import annotations

from pathlib import Path

from db import is_promo_published, log_promo_post, source_promo_fully_published
from post_extras import run_post_publish_extras
from promo_builder import HOOKS, PROMO_DIR, build_promo, fetch_metadata, promo_clip_id, promo_description
from publishers.youtube_publisher import publish_short


def publish_promo_variant(
    video_id: str,
    variant: int,
    config: dict,
    *,
    rebuild_if_missing: bool = True,
) -> str | None:
    """
    Publish one promo Short for a source video variant.
    Returns YouTube video id, or None if already published / skipped.
    """
    clip_id = promo_clip_id(video_id, variant)
    if is_promo_published(clip_id):
        print(f"  skip {clip_id} (already published)")
        return None

    path = PROMO_DIR / f"{clip_id}.mp4"
    if rebuild_if_missing and (not path.exists() or path.stat().st_size < 100_000):
        print(f"  rebuild {clip_id} …")
        path, _, _ = build_promo(video_id, variant=variant)

    meta = fetch_metadata(video_id)
    hook = HOOKS[variant % len(HOOKS)]
    title = f"{meta['title'][:70]} — Fan Preview #Shorts"
    desc = promo_description(meta, hook)
    privacy = config.get("publish", {}).get("privacy", "public")
    category_id = str(config.get("platforms", {}).get("youtube", {}).get("category_id", "17"))

    print(f"  publishing {clip_id} …")
    yt_id = publish_short(path, title, desc, category_id=category_id, privacy_status=privacy)
    if not log_promo_post(clip_id, "youtube", yt_id):
        print(f"  warning: {clip_id} logged by another process — uploaded {yt_id}")
    print(f"    → https://studio.youtube.com/video/{yt_id}/edit")
    run_post_publish_extras(video_id, yt_id, meta, config)
    return yt_id


def publish_promos_for_video(
    video_id: str,
    config: dict,
    *,
    rebuild_if_missing: bool = True,
) -> int:
    """Publish all missing promo variants for a source video. Returns count uploaded."""
    variants = int(config.get("auto_promo", {}).get("variants", 1))
    if source_promo_fully_published(video_id, variants):
        print(f"  skip {video_id} (all {variants} promo variant(s) already published)")
        return 0

    count = 0
    for v in range(variants):
        if publish_promo_variant(video_id, v, config, rebuild_if_missing=rebuild_if_missing):
            count += 1
    return count
