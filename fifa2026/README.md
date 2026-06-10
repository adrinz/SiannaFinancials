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

# Review output/review/day_01.mp4 → move approved to output/review/approved/
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

## Branch

All FIFA 2026 work lives on the `fifa2026` branch.
