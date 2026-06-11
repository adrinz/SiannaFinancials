"""Publish clips to YouTube Shorts (reuses fifa2026 OAuth)."""

from __future__ import annotations

import sys
from pathlib import Path

# Reuse existing YouTube auth from parent project
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from googleapiclient.http import MediaFileUpload
from youtube_auth import get_youtube_service


def publish_short(
    file_path: Path,
    title: str,
    description: str,
    *,
    category_id: str = "17",
    tags: list[str] | None = None,
    privacy_status: str = "private",
) -> str:
    youtube = get_youtube_service()
    tags = tags or ["Shorts", "FIFA", "WorldCup2026", "football"]
    if "Shorts" not in tags:
        tags.insert(0, "Shorts")

    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags[:15],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(file_path), mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


def delete_video(video_id: str) -> None:
    """Delete a video from the authenticated YouTube channel."""
    youtube = get_youtube_service()
    youtube.videos().delete(id=video_id).execute()


def post_video_comment(youtube_video_id: str, text: str) -> str:
    """
    Post a top-level comment on one of your channel's videos.
    Returns comment thread id. (YouTube API cannot pin — pin manually in Studio.)
    """
    youtube = get_youtube_service()
    body = {
        "snippet": {
            "videoId": youtube_video_id,
            "topLevelComment": {
                "snippet": {
                    "textOriginal": text[:10000],
                }
            },
        }
    }
    resp = youtube.commentThreads().insert(part="snippet", body=body).execute()
    return resp["id"]
