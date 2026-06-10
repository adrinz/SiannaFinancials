# FIFA World Cup Shorts — Cursor Automation Plan

Goal: turn `fifa2026/data/youtube_shorts.csv` into finished, uploaded YouTube Shorts with as little manual work as possible — built and run with Cursor.

**Project root:** `fifa2026/` (branch: `fifa2026`)

**Philosophy:** automate the boring 80% (voiceover, captions, rendering, uploading), keep a human in the loop for the 20% that protects monetization (a quick review before publish). Fully hands-off uploads are riskier for YouTube's "inauthentic content" policy, so we build a **"1-click review then publish"** pipeline, not a blind bot.

---

## 1. What the automation actually does (the pipeline)

```
data/youtube_shorts.csv        (your scripts — already done)
        │
        ▼
[1] generate_voiceover.py   → AI voice MP3 per row (ElevenLabs / TTS)
        │
        ▼
[2] build_video.py          → stitch background + captions + voice → vertical 1080x1920 mp4
        │
        ▼
[3] make_thumbnail.py       → cover frame with big text (optional)
        │
        ▼
[4] review/  folder         → YOU watch the 5 queued Shorts (the human gate)
        │
        ▼
[5] upload_youtube.py       → uploads approved Shorts via YouTube Data API (scheduled)
        │
        ▼
[6] post_log.csv            → tracks what was posted, when, and the video ID
```

One command produces a batch; you glance at them; one command schedules them.

---

## 2. Folder structure (ask Cursor to scaffold this)

```
fifa2026/
├── docs/                                 # content calendar, monetization, this plan
├── data/
│   └── youtube_shorts.csv                # source scripts (30 days)
├── assets/
│   ├── backgrounds/                      # licensed stadium/crowd loops (Pexels)
│   ├── music/                            # YT Audio Library tracks only
│   └── fonts/                            # bold caption font
├── output/
│   ├── audio/                            # generated voiceovers
│   ├── video/                            # rendered Shorts
│   └── review/                           # ready-for-you-to-approve queue
│       └── approved/                     # human-approved → upload queue
├── scripts/
│   ├── generate_voiceover.py
│   ├── build_video.py
│   ├── make_thumbnail.py
│   ├── upload_youtube.py
│   └── run_batch.py                      # orchestrates 1→3 for N rows
├── config/
│   └── settings.yaml                     # copy from settings.example.yaml
├── .env                                  # API keys (never commit)
├── post_log.csv
└── requirements.txt
```

---

## 3. Tools & APIs (cheap/free where possible)

| Job | Tool | Cost |
|-----|------|------|
| AI voiceover | ElevenLabs API (or free `edge-tts` / Coqui TTS) | Free tier → ~$5–22/mo |
| Video render | `ffmpeg` + `moviepy` (Python) | Free |
| Captions/subtitles | `whisper` for auto-timing, or burn from script | Free |
| Backgrounds | Pexels/Pixabay API (free stock video) | Free |
| Music | YouTube Audio Library | Free |
| Upload | YouTube Data API v3 | Free (quota-limited) |
| Thumbnails | Pillow (PIL) text-on-image, or an AI image API | Free–cheap |

Total realistic cost: **$0–25/month.**

---

## 4. Build it with Cursor — phase by phase

Do these as separate Cursor tasks. Copy each prompt into Cursor and let it generate + test the script.

### Phase 1 — Voiceover generator
> **Cursor prompt:** "In `fifa2026/`, create `scripts/generate_voiceover.py` that reads `data/youtube_shorts.csv`, and for each row takes the 'Voiceover Script (ready-to-paste)' column and generates an MP3 using edge-tts or ElevenLabs (from `config/settings.yaml`). Save as `output/audio/day_{Day}.mp3`. Skip rows whose audio already exists. Add a `--days 1-7` range flag. Read API keys from `.env`."

### Phase 2 — Video builder
> **Cursor prompt:** "Create `scripts/build_video.py` using moviepy + ffmpeg. For a given day: load `output/audio/day_{Day}.mp3`, pick a random background loop from `assets/backgrounds/`, resize/crop to 1080x1920, overlay the 'Hook' text for the first 2 seconds, then burn word-by-word captions synced to the audio (use whisper for timing), add a low-volume music track from `assets/music/`, and export `output/review/day_{Day}.mp4`. Caption style: bold, white text, black stroke, bottom third."

### Phase 3 — Thumbnail/cover (optional)
> **Cursor prompt:** "Create `scripts/make_thumbnail.py` using Pillow. Take a frame from the video, overlay the 3-strongest words from the title in a huge bold font with high contrast, save to `output/review/day_{Day}_cover.jpg`."

### Phase 4 — Batch orchestrator
> **Cursor prompt:** "Create `scripts/run_batch.py` that runs voiceover → video → thumbnail for a range of days (`--days 1-7`), prints a summary table, and puts finished files in `output/review/` for manual approval."

### Phase 5 — Uploader (with human gate)
> **Cursor prompt:** "Create `scripts/upload_youtube.py` using the YouTube Data API v3 (OAuth). It scans `output/review/`, and for each approved file (only ones I've moved into `output/review/approved/`) uploads it with the title and description from the CSV, sets it as a scheduled private→public publish at the time in `config/settings.yaml`, tags it, marks it 'not made for kids', and logs the video ID + timestamp to `post_log.csv`. Include the paid-promotion disclosure flag if the row is a sponsored one."

### Phase 6 — Scheduler
> **Cursor prompt:** "Add a `cron`/launchd setup (macOS) that runs `run_batch.py` every morning to prep the day's Short, and a note in the README on how to enable it."

---

## 5. The daily workflow once built (≈10 min/day)

1. `python scripts/run_batch.py --days 11` → renders tomorrow's Short into `output/review/`.
2. Watch the 30-second clip (the human gate — catches AI voice glitches, caption typos, policy risks).
3. Drag the good ones into `output/review/approved/`.
4. `python scripts/upload_youtube.py` → schedules them at the optimal timezone slot.
5. Reply to comments in the first hour (this you do manually — it's the biggest reach lever).

Batch a whole week on Sunday → ~1 hour for 7 Shorts.

---

## 6. Where the money comes from (recap, automated-friendly)

1. **YPP ad revenue** once eligible (1k subs + 10M Shorts views/90 days).
2. **Affiliate links** auto-inserted into every description by the uploader script (jerseys, legal fantasy/prediction apps, your AI-tool stack). This is your earliest income — no subscriber minimum.
3. **Cross-post automation:** add a step to also push the same `.mp4` to TikTok + Instagram Reels via their APIs / uploaders → 3 monetizable platforms from one render.
4. **Sponsorships** once you have traction (manual deals; the pipeline just inserts their link + disclosure).
5. **Long-form recaps:** a `compile_weekly.py` script can concatenate 7 Shorts into a 10-min weekly video (much higher RPM).

---

## 7. Guardrails baked into the plan (protect the money)

- **Human review gate** before every upload → avoids YouTube's mass-produced/inauthentic-content demonetization.
- **Original commentary + your chosen AI voice + original graphics** → not just stitched clips.
- **Only YT Audio Library music + licensed/stock backgrounds** → no copyright strikes. Recreate match moments with graphics, never reupload broadcast footage.
- **Disclose paid promotions** (the uploader sets the flag).
- **API keys in `.env`, never committed.**
- **Quota awareness:** YouTube upload API has a daily quota (~6 uploads/day default) — batch accordingly or request more.

---

## 8. Suggested build order (so you ship fast)

| Step | Build | Outcome |
|------|-------|---------|
| 1 | Phase 1 voiceover | Hear your channel's voice |
| 2 | Phase 2 video builder | First finished Short |
| 3 | Phase 4 batch | 7 Shorts in one command |
| 4 | Phase 5 uploader | Hands-off scheduling |
| 5 | Cross-post + weekly compile | Multi-platform + long-form |

Start with Steps 1–2 today — that alone removes most of the manual work.

---

### Next step
Scaffold is ready under `fifa2026/`. Build `scripts/generate_voiceover.py` and `scripts/build_video.py` next (free `edge-tts` recommended to start).
