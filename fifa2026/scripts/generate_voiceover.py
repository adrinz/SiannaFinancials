#!/usr/bin/env python3
"""Generate AI voiceovers from youtube_shorts.csv using edge-tts (or ElevenLabs)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from common import (
    OUTPUT_AUDIO,
    audio_path,
    load_config,
    load_shorts_df,
    parse_day_range,
    PROJECT_ROOT,
)

load_dotenv(PROJECT_ROOT / ".env")


async def _edge_tts(text: str, voice: str, out: Path) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out))


def _elevenlabs(text: str, voice_id: str, out: Path) -> None:
    import requests

    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY missing in .env")
    if not voice_id:
        voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    if not voice_id:
        raise RuntimeError("elevenlabs_voice_id missing in config or .env")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.8},
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    out.write_bytes(resp.content)


async def generate_one(day: int, config: dict, force: bool) -> Path:
    df = load_shorts_df()
    row = df.loc[df["Day"] == day]
    if row.empty:
        raise ValueError(f"No CSV row for day {day}")
    row = row.iloc[0]

    text = str(row["Voiceover Script (ready-to-paste)"]).strip()
    if not text:
        raise ValueError(f"Empty voiceover script for day {day}")

    out = audio_path(day)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and not force:
        print(f"  skip day {day:02d} (exists): {out.name}")
        return out

    voice_cfg = config.get("voice", {})
    provider = voice_cfg.get("provider", "edge-tts")

    print(f"  voice day {day:02d} via {provider} …")
    if provider == "elevenlabs":
        _elevenlabs(text, voice_cfg.get("elevenlabs_voice_id", ""), out)
    else:
        await _edge_tts(text, voice_cfg.get("edge_voice", "en-GB-RyanNeural"), out)

    print(f"  saved {out}")
    return out


async def main_async(days: list[int], force: bool) -> int:
    config = load_config()
    errors = 0
    for day in days:
        try:
            await generate_one(day, config, force)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR day {day:02d}: {exc}", file=sys.stderr)
            errors += 1
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate voiceovers for FIFA Shorts")
    parser.add_argument("--days", default="1", help="Day number or range, e.g. 1 or 1-7")
    parser.add_argument("--force", action="store_true", help="Regenerate even if MP3 exists")
    args = parser.parse_args()

    days = parse_day_range(args.days)
    OUTPUT_AUDIO.mkdir(parents=True, exist_ok=True)
    print(f"Generating voiceovers for days: {days}")
    raise SystemExit(asyncio.run(main_async(days, args.force)))


if __name__ == "__main__":
    main()
