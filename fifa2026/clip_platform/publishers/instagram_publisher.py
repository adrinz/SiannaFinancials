"""Instagram Reels via Meta Graph API — stub until configured."""

from __future__ import annotations

from pathlib import Path


def publish_reel(file_path: Path, caption: str, config: dict) -> str:
    raise NotImplementedError(
        "Instagram Reels publishing not configured. "
        "Requires Meta Graph API with Instagram Business account. "
        "Add page_id and ig_user_id to clip_platform/config.yaml"
    )
