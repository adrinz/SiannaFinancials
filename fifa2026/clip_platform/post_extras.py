"""Post-upload actions: FIFA link comment + community post queue."""

from __future__ import annotations

from community_post import queue_community_post
from db import log_post_action, post_action_done
from promo_builder import fifa_link_comment_text
from publishers.youtube_publisher import post_video_comment


def run_post_publish_extras(
    source_video_id: str,
    youtube_video_id: str,
    meta: dict,
    config: dict,
) -> None:
    """Comment with FIFA link and queue a Community post draft after upload."""
    pub = config.get("publish", {})

    if pub.get("fifa_link_comment", True):
        if not post_action_done(source_video_id, "fifa_comment"):
            template = pub.get("comment_template")
            if template:
                text = template.format(
                    title=meta.get("title", ""),
                    source_url=meta.get("source_url", ""),
                    channel_url=meta.get("channel_url", ""),
                )
            else:
                text = fifa_link_comment_text(meta)
            try:
                thread_id = post_video_comment(youtube_video_id, text)
                log_post_action(
                    source_video_id,
                    "fifa_comment",
                    youtube_video_id=youtube_video_id,
                    detail=thread_id,
                )
                print(f"  comment posted (pin in Studio) → video {youtube_video_id}")
            except Exception as exc:  # noqa: BLE001
                print(f"  comment skipped: {exc}")

    if pub.get("community_post_queue", True):
        if not post_action_done(source_video_id, "community_queued"):
            path = queue_community_post(
                source_video_id,
                meta,
                promo_youtube_id=youtube_video_id,
            )
            log_post_action(
                source_video_id,
                "community_queued",
                youtube_video_id=youtube_video_id,
                detail=str(path),
            )
            print(f"  community draft → {path}")
