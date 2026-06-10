# FIFA World Cup 2026 — YouTube Shorts Automation

Automated pipeline for viral football Shorts: predictions, hot takes, and post-match reactions.

## Folder layout

```
fifa2026/
├── docs/                    # Planning & content strategy
│   ├── content_calendar.md  # 30-day hooks + reel scripts
│   ├── monetization_guide.md
│   └── automation_plan.md   # Cursor build phases
├── data/
│   └── youtube_shorts.csv   # Ready-to-paste scripts (30 days)
├── assets/                  # Stock backgrounds, music, fonts
├── scripts/                 # Python automation (to be built)
├── config/                  # Voice, caption, posting settings
├── output/                  # Generated audio/video (gitignored)
└── requirements.txt
```

## Quick start

Requires **Python 3.10+** and **ffmpeg** (`brew install ffmpeg` on macOS).

```bash
cd fifa2026
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/settings.example.yaml config/settings.yaml   # if missing

# Build one Short (voice → video → cover)
python scripts/run_batch.py --days 1

# Build a week
python scripts/run_batch.py --days 1-7

# Preview (generated locally — NOT in git)
open output/review/day_01.mp4

# Approve → move to upload queue
cp output/review/day_01.mp4 output/review/approved/
cp output/review/day_01_cover.jpg output/review/approved/   # optional

# Dry-run upload metadata (no API call)
python scripts/upload_youtube.py --days 1 --dry-run

# Real upload (OAuth setup below)
python scripts/upload_youtube.py --days 1
```

Individual steps:

```bash
python scripts/generate_voiceover.py --days 1
python scripts/build_video.py --day 1
python scripts/make_thumbnail.py --day 1
```

## Docs

| File | Purpose |
|------|---------|
| `docs/content_calendar.md` | Full 30-day content calendar with hooks & scripts |
| `docs/monetization_guide.md` | YPP, affiliates, virality tactics |
| `docs/automation_plan.md` | Step-by-step Cursor prompts to build the pipeline |
| `data/youtube_shorts.csv` | Import into Sheets; voiceover + caption columns |

## Where are my generated videos?

Rendered files live on disk but are **gitignored** (so they won't appear in git or may be hidden in some IDE views):

| File | Path |
|------|------|
| Voiceover | `output/audio/day_01.mp3` |
| Short video | `output/review/day_01.mp4` |
| Cover image | `output/review/day_01_cover.jpg` |
| Approved queue | `output/review/approved/day_01.mp4` |

If missing, regenerate: `python scripts/run_batch.py --days 1`

## YouTube upload (Phase 2)

### One-time OAuth setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → create/select a project.
2. Enable **YouTube Data API v3**.
3. Create **OAuth client ID** → Application type: **Desktop app**.
4. Download JSON → save as `secrets/client_secret.json`.
5. Install upload deps: `pip install -r requirements.txt`
6. First upload opens a browser for consent; token saved to `secrets/youtube_token.json`.

### Upload commands

```bash
# Preview what would upload (safe — no API call)
python scripts/upload_youtube.py --days 1 --dry-run

# Upload day 1 (scheduled per config posting_times.americas)
python scripts/upload_youtube.py --days 1

# Upload all approved Shorts
python scripts/upload_youtube.py --all

# Schedule for Europe evening slot
python scripts/upload_youtube.py --days 1 --region europe

# Custom UTC publish time
python scripts/upload_youtube.py --days 1 --publish-at 2026-06-11T16:00:00Z
```

Upload log: `post_log.csv` (video IDs, publish times).

## Branch

All FIFA 2026 work lives on the `fifa2026` branch.
