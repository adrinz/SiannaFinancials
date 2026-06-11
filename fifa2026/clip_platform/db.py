"""SQLite state for clip platform."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "clip_agent.db"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_videos (
                video_id TEXT PRIMARY KEY,
                title TEXT,
                published_at TEXT,
                source_url TEXT,
                status TEXT DEFAULT 'detected',
                detected_at TEXT,
                processed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS clips (
                clip_id TEXT PRIMARY KEY,
                video_id TEXT,
                start_sec REAL,
                end_sec REAL,
                score REAL,
                title TEXT,
                file_path TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                FOREIGN KEY (video_id) REFERENCES source_videos(video_id)
            );

            CREATE TABLE IF NOT EXISTS posts (
                post_id INTEGER PRIMARY KEY AUTOINCREMENT,
                clip_id TEXT,
                platform TEXT,
                platform_video_id TEXT,
                status TEXT,
                posted_at TEXT,
                FOREIGN KEY (clip_id) REFERENCES clips(clip_id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_clip_platform
                ON posts(clip_id, platform);
            """
        )
        conn.commit()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def video_exists(video_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM source_videos WHERE video_id = ?", (video_id,)
        ).fetchone()
    return row is not None


def video_count() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM source_videos").fetchone()
    return int(row["n"])


def insert_video(
    video_id: str,
    title: str,
    published_at: str,
    source_url: str,
    *,
    status: str = "detected",
) -> bool:
    if video_exists(video_id):
        return False
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO source_videos (video_id, title, published_at, source_url, status, detected_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (video_id, title, published_at, source_url, status, utcnow()),
        )
        conn.commit()
    return True


def list_videos_by_status(status: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM source_videos WHERE status = ? ORDER BY detected_at ASC",
            (status,),
        ).fetchall()


def list_pending_videos() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM source_videos WHERE status = 'detected' ORDER BY published_at DESC"
        ).fetchall()


def mark_video_processed(video_id: str, status: str = "processed") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE source_videos SET status = ?, processed_at = ? WHERE video_id = ?",
            (status, utcnow(), video_id),
        )
        conn.commit()


def is_promo_published(clip_id: str, *, platform: str = "youtube") -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM posts WHERE clip_id = ? AND platform = ?",
            (clip_id, platform),
        ).fetchone()
    return row is not None


def promo_clip_prefix(video_id: str) -> str:
    """Prefix shared by all variant clip_ids for a source video."""
    from promo_builder import promo_clip_id

    return promo_clip_id(video_id, 0).rsplit("_", 1)[0]


def count_published_promos_for_source(video_id: str, *, platform: str = "youtube") -> int:
    prefix = promo_clip_prefix(video_id)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM posts
            WHERE platform = ? AND clip_id LIKE ?
            """,
            (platform, f"{prefix}_%"),
        ).fetchone()
    return int(row["n"])


def source_promo_fully_published(video_id: str, variants: int, *, platform: str = "youtube") -> bool:
    return count_published_promos_for_source(video_id, platform=platform) >= variants


def log_promo_post(clip_id: str, platform: str, platform_video_id: str) -> bool:
    """Record a publish. Returns False if this clip was already logged (duplicate)."""
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO posts (clip_id, platform, platform_video_id, status, posted_at)
            VALUES (?, ?, ?, 'posted', ?)
            """,
            (clip_id, platform, platform_video_id, utcnow()),
        )
        conn.commit()
    return cur.rowcount > 0


def delete_post_by_youtube_id(platform_video_id: str, *, platform: str = "youtube") -> int:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM posts WHERE platform = ? AND platform_video_id = ?",
            (platform, platform_video_id),
        )
        conn.commit()
    return cur.rowcount


def insert_clip(
    clip_id: str,
    video_id: str,
    start_sec: float,
    end_sec: float,
    score: float,
    title: str,
    file_path: str,
    status: str = "pending",
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO clips
            (clip_id, video_id, start_sec, end_sec, score, title, file_path, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (clip_id, video_id, start_sec, end_sec, score, title, file_path, status, utcnow()),
        )
        conn.commit()


def list_clips(status: str | None = None) -> list[sqlite3.Row]:
    with connect() as conn:
        if status:
            return conn.execute(
                "SELECT * FROM clips WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
        return conn.execute("SELECT * FROM clips ORDER BY created_at DESC").fetchall()
