#!/usr/bin/env python3
"""Find viral clip candidates using audio energy peaks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from tools_cmd import ffmpeg_cmd, ytdlp_cmd


def download_audio(video_url: str, out_dir: Path) -> Path:
    """Download audio-only track for analysis."""
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "%(id)s.%(ext)s")
    cmd = [
        *ytdlp_cmd(),
        "-f", "bestaudio/best",
        "-x", "--audio-format", "wav",
        "-o", template,
        video_url,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    wavs = sorted(out_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not wavs:
        raise RuntimeError("yt-dlp did not produce a wav file")
    return wavs[0]


def _load_wav_mono(path: Path, target_sr: int = 8000) -> tuple[np.ndarray, int]:
    try:
        from scipy.io import wavfile
    except ImportError:
        # fallback: use ffmpeg to raw pcm
        cmd = [
            ffmpeg_cmd(), "-y", "-i", str(path),
            "-ac", "1", "-ar", str(target_sr),
            "-f", "s16le", "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, check=True)
        audio = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)
        audio /= np.max(np.abs(audio)) + 1e-9
        return audio, target_sr

    sr, data = wavfile.read(path)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float32)
    data /= np.max(np.abs(data)) + 1e-9
    if sr != target_sr:
        # simple decimation
        factor = max(sr // target_sr, 1)
        data = data[::factor]
        sr = sr // factor
    return data, sr


def find_peaks(
    audio: np.ndarray,
    sr: int,
    *,
    window_sec: float = 2.0,
    min_duration: float = 15.0,
    max_duration: float = 45.0,
    max_clips: int = 5,
    min_gap_sec: float = 30.0,
    threshold: float = 0.65,
) -> list[dict]:
    """Return ranked clip windows based on RMS energy peaks."""
    win = int(window_sec * sr)
    if win < 1:
        win = 1
    rms = np.array([
        np.sqrt(np.mean(audio[i : i + win] ** 2))
        for i in range(0, len(audio) - win, win)
    ])
    if rms.size == 0:
        return []

    rms = rms / (np.max(rms) + 1e-9)
    peak_idx = int(np.argmax(rms))
    candidates: list[dict] = []

    # Always include global peak
    center = peak_idx * window_sec + window_sec / 2
    candidates.append({"center_sec": center, "score": float(rms[peak_idx])})

    # Other high-energy moments
    for i, val in enumerate(rms):
        if val >= threshold:
            center = i * window_sec + window_sec / 2
            if all(abs(center - c["center_sec"]) >= min_gap_sec for c in candidates):
                candidates.append({"center_sec": center, "score": float(val)})

    candidates.sort(key=lambda c: c["score"], reverse=True)
    clips = []
    for cand in candidates[: max_clips * 2]:
        start = max(0.0, cand["center_sec"] - max_duration / 2)
        duration = max_duration
        clip = {
            "start_sec": round(start, 2),
            "end_sec": round(start + duration, 2),
            "score": round(cand["score"], 3),
        }
        if clip["end_sec"] - clip["start_sec"] >= min_duration:
            if all(abs(clip["start_sec"] - c["start_sec"]) >= min_gap_sec for c in clips):
                clips.append(clip)
        if len(clips) >= max_clips:
            break

    return clips


def analyze_video(
    video_url: str,
    work_dir: Path,
    config: dict,
) -> list[dict]:
    clip_cfg = config.get("clips", {})
    wav = download_audio(video_url, work_dir)
    audio, sr = _load_wav_mono(wav)
    return find_peaks(
        audio,
        sr,
        min_duration=float(clip_cfg.get("min_duration_sec", 15)),
        max_duration=float(clip_cfg.get("max_duration_sec", 45)),
        max_clips=int(clip_cfg.get("max_clips_per_video", 5)),
        min_gap_sec=float(clip_cfg.get("min_gap_sec", 30)),
        threshold=float(clip_cfg.get("audio_peak_threshold", 0.65)),
    )
