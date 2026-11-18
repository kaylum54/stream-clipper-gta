# How Stream Clipper works

This is the full reference: every stage, every file it writes, every setting, and what to do when something goes wrong. For a quick start, see `README.md`. For the HTTP API, see `docs/API.md`. For agents, see `AGENTS.md`.

## The pipeline in one picture

```
 Streamers list ──► Monitor (every 2 min) ──► live? ──► Recorder: streamlink ─pipe─► ffmpeg segment ─► data/rec/<session>/chunk_*.ts
                                                  └──► Chat reader (Twitch IRC / YouTube API) ─► chat_events table
 finished chunk ──► Analyser: volume + chat + emotes per second ─► peaks ─► Cutter (ffmpeg, 9:16) ─► data/clips/*.mp4
                                                                                         └─► whisper on 40 s around the peak: laughter + transcript
 new clip ──► Discord webhook (with the file) + Review queue ──► you Approve ──► Post for Me: upload, post, poll results ─► Posted page
```

Everything runs in one Python process on your computer. `Start Clipper.cmd` starts it, and the GUI is at http://127.0.0.1:8765. Closing that window stops everything, including any recordings: the recorder's child processes sit in a Windows Job Object that is closed with the app.

## Stage by stage

### 1. Monitor: who is live (`Pipeline.monitor_once` in `pipeline.py`, checks in `platforms.py`)
- **Frequency:** every `poll_seconds` (default 120), plus straight away when you add a streamer or press "Check now".
- **Twitch:** with a Client ID and secret it batch-checks up to 100 channels per Helix call (`GET /helix/streams`). Without them it uses streamlink.
- **Kick:** with a Client ID and secret it uses Kick's official API (`/public/v1/channels`). Without them it uses streamlink.
- **YouTube:** always uses streamlink (`streamlink --json <channel>/live`), so no API quota is spent. streamlink also returns the video ID that YouTube chat needs.
- **Concurrency:** at most 4 streamlink checks run at once, and each times out after 40 s.
- **Rules:**
  - A failed check (timeout, network) doesn't change a streamer's live state; it shows "Check failed" with the reason.
  - A recording stops only after **two** offline checks in a row, so one flaky check can't cut it.
  - If you press **Stop recording**, it won't restart while that stream stays live. It records again the next time they go live.
  - Overlapping checks are serialised, so a streamer can never get two recorders.
  - A recording whose newest chunk hasn't grown for 180 s counts as stalled. It is ended, and the next check starts a fresh one.

### 2. Recorder (`recorder.py`)
- **Command:** `streamlink --stdout <url> <quality>` piped into `ffmpeg -c copy -f segment -segment_time <chunk_seconds> -strftime 1 chunk_%Y%m%d-%H%M%S.ts`. Nothing is re-encoded, so recording uses very little CPU.
- **Quality:** default `720p60,720p,best`, which means the first that exists.
- **Disk:** roughly 1.5 GB per hour per stream at 720p. Measured figures are in `stress/STRESS-REPORT.md`.
- **Chunk times:** each chunk starts where the previous one ended, using measured durations. The time in the file name is used only for a real gap of more than 3 s, such as a reconnect. This keeps chat and audio lined up and clips the right length.

### 3. Chat (`chat.py`), optional
- **Twitch:** IRC over websocket with your user token (scope `chat:read`). It reconnects with backoff and refreshes the token when it expires (about every 4 hours).
- **YouTube:** Data API `videos.list` gives the chat ID, then `liveChatMessages.list` is polled at most every 20 s. That uses about 900 of your 10,000 free daily units per hour of stream.
- **Kick:** not read. Kick only sends chat to a public webhook URL.
- **What is stored:** only the time of each message and its count of hype emotes, never the text.

### 4. Analyser (`analysis.py`)
A chunk is analysed once the next chunk holds the 25 s of follow-through (`postroll`) after its end, or once the stream has ended.

1. **Volume:** ffmpeg decodes the audio, and the loudness (RMS, in dBFS) is measured for every second.
2. **Chat:** the number of messages per second, and hype emotes per second (`hype_tokens`: KEKW, LUL, OMEGALUL, LMAO, Pog, W, CLIP, 😂, 💀 and others).
3. **Score per second:** each signal is turned into a robust z-score (how unusual it is compared with the median of that chunk), smoothed (volume over 3 s, chat over 10 s), clipped to 0..10, then weighted and added up:
   - With chat (20 or more messages in the chunk): volume ×1.0, chat ×1.0, emotes ×0.7.
   - Without chat: volume ×1.5.
4. **Peaks:** the highest seconds at or above `score_threshold` (default 3.0, the "How big a moment must be" slider) become moments. They must be at least `min_gap_seconds` apart (default 90), with up to `max_clips_per_chunk` (default 3) per chunk.

### 5. Cutter (`cutter.py`)
- **Window:** from `preroll` seconds before the peak (default 40, the build-up) to `postroll` seconds after it (default 25). The default clip is 65 s.
- **Across chunks:** it reads across chunk boundaries with ffmpeg's concat demuxer, including the chunk still being recorded.
- **Vertical:** 1080×1920. The stream sits centred over a blurred, zoomed copy of itself. Encoding is libx264 veryfast at CRF 21 with AAC 128k.
- **Time:** about 20 to 40 s per clip on a laptop CPU. Two media jobs run at once.
- **Clip start:** a clip can begin up to one keyframe (about 2 s) before the planned point.

### 6. Laughter and transcript (whisper)
faster-whisper (default model `base`, CPU, int8) transcribes the 40 s around the peak.
- **Laughter:** if the transcript shows laughter ("haha", "kkkk", "jaja", "lol", "(laughs)"), the clip's score goes up by up to 2 points.
- **Transcript:** shown next to the clip to help you write the caption.
- **Model:** downloads on first use (75 to 500 MB). Turn it off in Settings if the CPU struggles.

### 7. Review, Discord and posting
- **Discord:** each new clip sends a webhook message, with the file attached if it is 20 MB or less. Mentions are disabled and markdown in stream titles is escaped.
- **Approve:**
  1. Your caption and ticked accounts are saved.
  2. The clip is claimed atomically, so a double click can't post it twice.
  3. The upload starts: `POST /v1/media/create-upload-url`, then a PUT of the file to the signed URL (without your key), then `POST /v1/social-posts`, with `external_id` set to `clip-<id>`.
- **Retry:** before any upload, the app looks for an existing post with that `external_id`. A retry after a timeout or crash can't create a second post.
- **Results:** polled every 30 s.
  - Every account worked: **posted**.
  - Some accounts worked: **posted**. The Posted page shows which failed and why, and the clip isn't offered for retry, so no account gets it twice.
  - None worked: **failed**, and you can try again.
  - No result after 24 h: failed.

### 8. Retention
Every 10 minutes, chunks that have been analysed and are older than `keep_chunks_hours` (default 3) are deleted. Chunks not yet analysed are never deleted. Clips are never deleted automatically, so clear `data/clips/` yourself when you need the space.

## Files on disk

| Path | What | Safe to delete? |
|---|---|---|
| `data/config.json` | Settings and keys (plain text; this machine only; gitignored) | Deleting it resets settings and keys |
| `data/clipper.db` | SQLite: streamers, sessions, chunks, chat timings, clips, posts, activity log | Deleting it loses history and the queue |
| `data/rec/<platform>-<channel>-<time>/` | Recording chunks plus `recorder.log` (streamlink and ffmpeg errors) | Yes, once analysed (the app does this itself) |
| `data/clips/*.mp4`, `*.jpg` | Clips and thumbnails | Yes, after posting |

Set `CLIPPER_DATA` to keep data somewhere else, such as a bigger drive.

## Settings reference

| Setting (GUI label) | Key | Default | Range | Notes |
|---|---|---|---|---|
| Check who's live every | `poll_seconds` | 120 | 30 to 3600 | |
| Record in pieces of | `chunk_seconds` | 600 | 10 s to 60 min | Shorter means clips arrive sooner but use more CPU |
| Seconds of build-up | `preroll` | 40 | 1 to 300 | |
| Seconds after the moment | `postroll` | 25 | 1 to 300 | Keep the total over 60 s for TikTok Creator Rewards |
| How big a moment must be | `score_threshold` | 3.0 | 0.5 to 20 | Lower gives more clips |
| Most clips per chunk | `max_clips_per_chunk` | 3 | 1 to 20 | |
| Seconds between two clips | `min_gap_seconds` | 90 | 10 to 3600 | |
| Recording quality | `quality` | `720p60,720p,best` | streamlink syntax | |
| Keep raw recordings for | `keep_chunks_hours` | 3 | 1 to 720 | |
| Starting caption | `default_caption` | `{streamer} #gta6 #gta #clips` | up to 2200 characters | Add campaign tags here |
| Vertical 9:16 | `vertical` | on | | Off keeps the original shape |
| Laughter and transcript | `use_whisper`, `whisper_model` | on, `base` | tiny, base or small | |
| Ticked by default | `default_accounts` | all | Post for Me account IDs | |
| Address in Discord links | `public_url` | `http://127.0.0.1:8765` | | |
| Tool paths | `ffmpeg`, `streamlink` | from PATH or the venv | | |

## Security model
- **Local only:** the server listens on 127.0.0.1 only.
- **Host check:** it refuses requests whose Host isn't `127.0.0.1` or `localhost`, which blocks DNS rebinding.
- **CSRF check:** it refuses any change without the `X-Clipper: 1` header. Other websites can't send that header.
- **Keys:** they never come back out of the API (it returns only "set" or "not set"). The Post for Me key goes only to `api.postforme.dev/v1/`, never to the storage upload URL.
- **Stream text:** titles, transcripts and names are written into the page as text, never as HTML.
- **Media:** `/media/clips/` serves only `.mp4` and `.jpg` files inside `data/clips` (backslash tricks included).

## Troubleshooting

| You see | Why | Do |
|---|---|---|
| Setup says ffmpeg is missing | ffmpeg isn't on PATH | `winget install Gyan.FFmpeg`, then restart the app |
| "Check failed: streamlink timed out" | CPU is busy (whisper or cutting) or the network is slow | Usually clears by itself. Turn whisper off or use the `tiny` model if it keeps happening |
| A streamer is live but nothing is recording | Paused, stopped by you (waits for them to go offline), or the recorder failed | Look in Activity, and in `data/rec/<session>/recorder.log` |
| No clips from a long stream | A flat stream, or the threshold is too high | Move "How big a moment must be" towards "More clips"; connect Twitch or YouTube chat |
| Too many weak clips | The threshold is too low | Move it towards "Fewer, bigger" |
| Discord says "not sent" | Wrong webhook URL, or the webhook was deleted | Paste the URL again, then press "Send a test message" |
| Post failed for one account | That platform refused it (quota, length, account disconnected) | Read the reason in the Posted page (Approved tab); reconnect the account in Post for Me |
| "Twitch token refresh failed" | Token revoked | Settings, then Reconnect Twitch chat |
| Disk filling up | Many streams with long retention | Lower "Keep raw recordings for", or set `CLIPPER_DATA` to a bigger drive |

## Tests and how they're layered
See `docs/testing/PYRAMID-REPORT.md`. In short: unit, integration (real ffmpeg and SQLite), contract (API shapes checked against saved specs), review (one test per review finding), browser (Playwright), smoke, live checks, stress (`stress/`), and a real live rehearsal (`scripts/rehearse.py`).
