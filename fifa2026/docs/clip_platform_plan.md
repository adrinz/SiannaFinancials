# Clip Platform — Build Plan (EasySlice-style)

## What EasySlice actually does

From their UI: connect **your** YouTube channel → detect **your** new uploads → AI slices viral moments → auto-post to TikTok, IG, YT Shorts.

The monetization hook is **Content Rewards** (TikTok/Instagram creator funds) — paying you for posting **your own** clipped content.

## Your idea vs EasySlice

| | EasySlice | Your idea |
|--|-----------|-----------|
| Source | Your channel | @fifa official |
| Ownership | You | FIFA / broadcast partners |
| Copyright risk | Low | **Very high** |
| Monetization | Content Rewards + YPP | Likely blocked by Content ID |

**Build the tech anyway** — but point `source_channel_id` at **your own channel** when you go live, or use FIFA feeds only for internal clipping practice.

---

## MVP phases (build in Cursor)

### Phase 1 — Monitor (1 day) ✅ scaffolded
- Poll `https://www.youtube.com/feeds/videos.xml?channel_id=UCpcTrCXblq78GZrTUTLWeBw`
- Store seen video IDs in SQLite
- Alert / log when new video appears

### Phase 2 — Download + clip detection (2–3 days)
- `yt-dlp` download best 1080p
- **Audio energy peaks** → candidate timestamps (goals, crowd roars)
- Optional: Whisper transcript → keyword hits ("GOAL", "amazing", "incredible")
- Score segments, pick top 3–5 clips (15–45s each)

### Phase 3 — Render (1 day)
- ffmpeg: extract segment, center-crop to 9:16, add title overlay
- Export to `clip_platform/data/clips/`

### Phase 4 — Publish YouTube Shorts (1 day)
- Reuse existing `upload_youtube.py` OAuth
- Title: source title + " #Shorts"
- Description: "Clip from [source]" + link to original

### Phase 5 — TikTok + Instagram (3–5 days)
- **TikTok**: [Content Posting API](https://developers.tiktok.com/) — OAuth, upload video, publish
- **Instagram**: Meta Graph API `/{ig-user-id}/media` → Reels container → publish
- Both require business/developer accounts and app review

### Phase 6 — Dashboard (optional)
- Simple FastAPI + HTML dashboard like EasySlice screenshots
- Channels, platforms, agent runs, my clips

---

## Viral moment detection (how the AI works)

Simple MVP (no GPU):

1. Extract audio waveform
2. Find top N peaks in 15–60s windows (crowd noise = goals/celebrations)
3. Optional Whisper: boost segments containing football keywords
4. Rank by `audio_score + keyword_score`
5. Deduplicate overlapping windows

Advanced (later):
- Scene change detection (opencv)
- LLM reads transcript, picks "viral" quotes
- Vision model for goal celebrations

---

## Config example

```yaml
source:
  channel_id: UCpcTrCXblq78GZrTUTLWeBw   # @fifa
  channel_url: https://www.youtube.com/@fifa
  poll_interval_sec: 300

clips:
  min_duration_sec: 15
  max_duration_sec: 45
  max_clips_per_video: 5

publish:
  youtube: true
  tiktok: false      # enable after API setup
  instagram: false
  require_review: true   # human gate before post
```

---

## Legal-safe alternatives using same platform

1. **Clip your own long videos** — exact EasySlice model
2. **Clip Creative Commons / royalty-free football content**
3. **Clip videos you have written permission to use**
4. **Reaction format**: 3-sec clip + 25-sec your commentary overlay (transformative; still some risk)

---

## Next build step

Run `python clip_platform/monitor.py` to verify @fifa RSS detection, then `run_agent.py` on a test video.
