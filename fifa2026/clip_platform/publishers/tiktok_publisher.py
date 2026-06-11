"""TikTok Content Posting API — stub until developer app is configured."""

from __future__ import annotations

from pathlib import Path


def publish_video(file_path: Path, title: str, config: dict) -> str:
    raise NotImplementedError(
        "TikTok publishing not configured. "
        "Set up TikTok Content Posting API at https://developers.tiktok.com/ "
        "and add client_key/client_secret to clip_platform/config.yaml"
    )
