# Clip Platform — EasySlice-style auto-clipper

Monitor a YouTube channel → build **copyright-safe promo Shorts** → post to YouTube Shorts, TikTok, and Instagram Reels.

## Copyright: why raw clips get BLOCKED

YouTube **Content ID** matches FIFA's video/audio fingerprints. **Credit in the description does not prevent blocks.**

| Method | Content ID risk |
|--------|-----------------|
| Re-upload FIFA footage (`clip_renderer.py`) | **Blocked** (as you saw) |
| **Promo Short** — stock footage + your voiceover + link to @fifa (`build_promo.py`) | **Low** — original content |

**Always use `build_promo.py` for @ScrollandSoull**, not raw clips.

## Important: who owns the source channel?

**EasySlice is built for creators clipping their OWN uploads.** Your connected channel = your content = legal.

| Source | Legal to clip & repost? |
|--------|-------------------------|
| **Your own YouTube channel** | Yes — you own it |
| **@fifa official channel** | No — FIFA/broadcasters own it; re-uploading clips will get copyright strikes |

**Recommended setup:** connect **your** channel for long-form content, OR only use @fifa monitoring for **personal research** and publish clips with **heavy transformation + commentary** (still risky).

This codebase supports any `source_channel_id` in config — default is `@fifa` per your request, but switch to your own channel ID for a monetizable, strike-free workflow.

---

## Architecture

```
YouTube RSS poll (every 5 min)
        ↓
New video detected? → download (yt-dlp)
        ↓
AI clip finder (audio peaks + optional Whisper transcript keywords)
        ↓
Render 9:16 clips (ffmpeg) + burned hook title
        ↓
Human review queue (optional gate)
        ↓
Publish → YouTube Shorts | TikTok | Instagram Reels
```

## Quick start

```bash
cd fifa2026
source .venv/bin/activate
pip install -r clip_platform/requirements.txt
cp clip_platform/config.example.yaml clip_platform/config.yaml

# AUTO MODE (recommended): watch @fifa → build promo Shorts → publish to @ScrollandSoull
python clip_platform/auto_promo_agent.py --loop

# Or one-shot (cron every 5 min):
python clip_platform/auto_promo_agent.py --once
```

### Manual promo workflow

```bash
python clip_platform/build_promo.py --video-id VIDEO_ID --variants 3
python clip_platform/publish_promo.py --video-id VIDEO_ID
```

### Run in background (Mac)

```bash
bash clip_platform/start_agent.sh
tail -f clip_platform/data/agent.log
```

To stop: `pkill -f auto_promo_agent.py`

## Platform API setup

| Platform | API | Notes |
|----------|-----|-------|
| YouTube Shorts | YouTube Data API v3 | Already configured in `secrets/` |
| TikTok | Content Posting API | Requires TikTok developer app + user OAuth |
| Instagram Reels | Meta Graph API | Requires Facebook Page + Instagram Business |

See `docs/clip_platform_plan.md` for full setup steps.

## Folder layout

```
clip_platform/
├── config.example.yaml   # source channel, clip settings, platforms
├── monitor.py            # RSS poll for new uploads
├── downloader.py         # yt-dlp wrapper
├── clip_finder.py        # viral moment detection
├── clip_renderer.py      # vertical clip export
├── run_agent.py          # orchestrator
├── publish.py            # multi-platform upload
├── db.py                 # SQLite state (seen videos, clips, posts)
├── publishers/           # youtube, tiktok, instagram
└── data/                 # SQLite DB + clip queue
```
