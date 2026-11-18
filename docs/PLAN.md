# Stream Clipper: what the video describes, and the build plan

Source: https://www.tiktok.com/@kaylumai/video/7708707184489565462 ("GTA clipping system setup", 2 min 22 s). The captions were pulled with yt-dlp on 16 Nov 2026 and are in `docs/transcript.txt`.

## What the video describes

| # | Step in the video | What it means in code |
|---|---|---|
| 1 | "Each of the streaming platforms, Kick, YouTube, Twitch, they've all got an API ... that will expose when a streamer has gone live" | A **monitor** checks a list of streamers every couple of minutes and records who is live |
| 2 | "Give it a list of all of the streamers that you wanna track" | A **streamer list** you edit in the GUI: paste a channel URL |
| 3 | "You'll get notified as soon as the streamer goes live" | A **Discord** message when a stream goes live and recording starts |
| 4 | "Stream Link ... will then start recording each stream in 10 minute chunks" | A **recorder**: streamlink piped into ffmpeg's segment muxer, 600 s chunks |
| 5 | "AI sentiment analysis ... more laughter ... the chat's activity ... certain emojis" | An **analyser** scores every second of each chunk: audio loudness spikes, laughter in the speech transcript, chat message rate, and hype emotes/emojis in chat |
| 6 | "Once it notices something it will start clipping, and 35 to 45 seconds earlier ... in case there's a build up" | A **clipper** cuts from 40 s before the peak to 20 s after it (both settable), across chunk boundaries |
| 7 | "Pushed through ... into a Discord ... you can watch it and then approve it" | A **review queue** in the GUI, with a Discord message for each new clip that links to it |
| 8 | "Once you press approve ... Post for Me ... send it out automatically to any of your socials" | A **poster**: upload to Post for Me, create the post on the chosen accounts, show each platform's result |

## Facts checked on 16 Nov 2026 (before designing)

- streamlink 8.6.3 has Twitch, YouTube and Kick plugins. `streamlink --json <url>` told live from offline correctly on all three: Twitch `gaules`, Kick `classybeef` and YouTube `@LofiGirl/live` live; 20+ others offline. One probe takes about 2 to 4 s and needs no keys.
- Post for Me: base `https://api.postforme.dev`, Bearer API key. `POST /v1/media/create-upload-url` gives `{upload_url, media_url}`, then you `PUT` the file to `upload_url`. `POST /v1/social-posts` takes `{caption, social_accounts[], media[{url}], platform_configurations}`. `GET /v1/social-accounts` lists the connected accounts and `GET /v1/social-post-results?post_id=` reports each platform's result. The spec is saved in `tests/fixtures/postforme/`.
- Twitch chat needs a user token with `chat:read` (dev.twitch.tv/docs/chat/irc). Liveness through Helix `GET /streams` takes an app token (client credentials).
- Kick's official API only delivers chat as **webhooks** to a public URL (docs.kick.com/events). This build does not read Kick chat, so Kick clips use the audio signals only. See "Out of scope".
- YouTube chat comes from the Data API: `videos.list` gives `activeLiveChatId`, then `liveChatMessages.list` is polled. The `chat-downloader` library fails on YouTube today ("Unable to parse initial video data"), so it is not used.
- Auto mode refused calls to private/unofficial endpoints (Twitch's web GraphQL client id, kick.com/api/v2). Only official APIs and streamlink are used.
- No GPU on this laptop. faster-whisper models (tiny/base/small) are cached, and whisper runs only on candidate windows, not whole chunks.

## Architecture

One Python process, started by `Start Clipper.cmd`, which serves the GUI at http://127.0.0.1:8765.

```
streamers (SQLite) --> monitor (every 120 s) --> live? --> recorder: streamlink | ffmpeg segment -> data/rec/<session>/chunk_*.ts
                                         \--> chat collector (Twitch IRC / YouTube API) -> chat_events table
chunk complete --> analyser (audio RMS + chat rate + hype tokens -> score per second -> peaks)
              --> whisper on each candidate window (laughter, transcript) --> clipper (ffmpeg, vertical 9:16) -> data/clips/*.mp4
clip ready --> Discord webhook ("clip ready, review at ...") --> GUI review queue --> Approve --> Post for Me --> results polled into GUI
```

- **Stack:** FastAPI + uvicorn, SQLite (stdlib), asyncio tasks, ffmpeg/ffprobe, streamlink, numpy, faster-whisper. The GUI is one static page (HTML/CSS/JS, no build step) talking to a JSON API.
- **Secrets** live in `data/config.json` (gitignored). The API only ever returns "set" or "not set" for them, and the server binds to 127.0.0.1 only.
- **Each signal is optional.** Without chat credentials the analyser falls back to audio and laughter, and without Discord or Post for Me those steps are skipped and logged. That way the pipeline runs end to end on day one with no keys.

## Build sections and how each is verified

| Section | Files | Verified by |
|---|---|---|
| S1 Base: config, DB, events log | `clipper/config.py`, `db.py` | unit tests; a fresh DB is created from the schema |
| S2 Platforms + monitor | `platforms.py`, `monitor.py` | unit (URL parsing, response parsing from fixtures); live check: probe real live channels |
| S3 Recorder | `recorder.py` | integration: an ffmpeg test stream piped in gives 10 s chunks with parseable start times; a live rehearsal on a real stream |
| S4 Chat collectors | `chat.py` | unit (IRC parse, hype count); contract: YouTube/Twitch replies from dated fixtures through the real adapter |
| S5 Analyser | `analysis.py` | unit (scoring, peak picking, window maths across chunks); integration: a synthetic stream with a loud burst at a known second is found within 2 s |
| S6 Clipper | `clipper.py` | integration: the cut clip is the right length, 1080x1920, and the burst sits at about 40 s in |
| S7 Discord | `notify.py` | contract: payload shape; a fake webhook server receives the file |
| S8 Post for Me | `poster.py` | contract: requests checked against the saved OpenAPI spec; integration: a mock Post for Me server goes through upload, post and results |
| S9 Pipeline orchestration | `pipeline.py` | integration: a fake streamer goes live, gets recorded, analysed and clipped with no network |
| S10 GUI + API | `app.py`, `web/` | Playwright: add a streamer, review, approve (mock poster), reject, settings mask secrets, 390 px no horizontal scroll |

## Test pyramid

- **Unit** (most of the tests, under 5 s): parsers, scoring, peak picking, window maths, payload builders, secret masking.
- **Integration** (real ffmpeg, real SQLite, a synthetic media stream): recorder, analyser, clipper and the whole pipeline with a fake streamlink command.
- **Contract:** Post for Me requests checked against its OpenAPI spec (saved 16 Nov 2026). Twitch, Kick and YouTube replies come from fixtures written from their docs, dated and marked "from docs" until the live check records real ones.
- **Browser E2E:** Playwright with Chromium, about 5 journeys, against the real app with a seeded DB and a mock Post for Me.
- **Smoke:** `scripts/smoke.py <url>` checks pages and health, gets 4xx on bad input, and confirms no secret leaks from `/api/settings`.
- **Live check:** `scripts/check_live.py` probes real live channels with streamlink and, with keys set, makes read-only calls to Helix, Kick, YouTube and the Post for Me accounts list. It is not in the commit gate.
- **Live rehearsal:** record a real live stream with 60 s chunks, force a clip and check it.

Commands: `run_tests.cmd` (unit + integration + contract), `python -m pytest e2e`, `python scripts/smoke.py http://127.0.0.1:8765`, `python scripts/check_live.py`.

## Out of scope for this build

- **Kick chat:** it needs a public webhook URL (a Cloudflare tunnel plus a Kick app webhook). A follow-up if Kick streamers matter.
- **Burned-in captions, titles or AI-written captions.** The whisper transcript is shown to you to help write the caption yourself.
- **Approving from Discord with buttons.** That needs a bot and a public interactions URL; approval happens in the GUI.
- **Running on a server.** It runs on this laptop. Recording needs disk space: about 1.5 GB per hour per streamer at 720p, and chunks older than the retention setting are deleted.

## Credentials (asked for at the end)

| For | What | Where you get it |
|---|---|---|
| Twitch liveness + chat | Client ID + secret, then "Connect Twitch" in Settings (device code) | dev.twitch.tv/console/apps |
| Kick liveness (optional, streamlink works without) | Client ID + secret | kick.com/settings/developer |
| YouTube chat | Data API v3 key | console.cloud.google.com |
| Discord | Webhook URL for your channel | Channel settings > Integrations > Webhooks |
| Post for Me | API key, plus your socials connected in their dashboard | postforme.dev |
