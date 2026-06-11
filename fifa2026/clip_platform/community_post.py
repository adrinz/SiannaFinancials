"""Queue ready-to-paste YouTube Community posts when @fifa uploads."""

from __future__ import annotations

from pathlib import Path

PLATFORM_DIR = Path(__file__).resolve().parent
QUEUE_DIR = PLATFORM_DIR / "data" / "community_queue"


def community_post_text(meta: dict, *, promo_youtube_id: str | None = None) -> str:
    title = meta.get("title", "FIFA World Cup update")
    source_url = meta.get("source_url", "")
    lines = [
        f"⚽ NEW from @fifa: {title}",
        "",
        f"▶️ Watch the full official video: {source_url}",
    ]
    if promo_youtube_id:
        lines.extend(
            [
                "",
                f"📱 Our Fan Preview Short: https://youtube.com/shorts/{promo_youtube_id}",
            ]
        )
    lines.extend(
        [
            "",
            "Original commentary + royalty-free visuals — we link to FIFA, never re-upload their footage.",
            "",
            "#WorldCup2026 #FIFA #football",
        ]
    )
    return "\n".join(lines)


def queue_community_post(
    source_video_id: str,
    meta: dict,
    *,
    promo_youtube_id: str | None = None,
) -> Path:
    """Write a community post draft for manual paste in YouTube Studio."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    text = community_post_text(meta, promo_youtube_id=promo_youtube_id)
    out = QUEUE_DIR / f"{source_video_id}.txt"
    out.write_text(text, encoding="utf-8")
    return out
