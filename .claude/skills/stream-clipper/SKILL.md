---
name: stream-clipper
description: Run and operate Stream Clipper (local stream-clipping pipeline + GUI). Use when the user asks to set it up, start it, add/track streamers, check what's live or recording, review or post clips, tune how many clips it makes, fix "no clips"/"check failed"/"post failed", or asks how to make money from clipping.
---

# Stream Clipper runbooks

Read `AGENTS.md` first. Its rules beat anything here: never approve or post without the user's OK per clip, never print keys, and ask before deleting. The API is in `docs/API.md`. Every request that isn't a GET needs the header `X-Clipper: 1`.

## A. Set up (new machine)
1. Check that `python --version` is 3.12 and `ffmpeg -version` works. If either fails, have the user run `winget install Python.Python.3.12` or `winget install Gyan.FFmpeg`, then open a new terminal.
2. Start the app: `Start Clipper.cmd`, or `.venv\Scripts\python.exe -m clipper`. The first run installs the requirements.
3. Run `curl http://127.0.0.1:8765/api/system`. Pass: ffmpeg, ffprobe and streamlink are all true, and disk_free_gb is over 20.
4. Run `.venv\Scripts\python.exe scripts\smoke.py`. Pass: every line says PASS.
5. Keys, in this order:
   - Post for Me key (needed to post). The user connects their socials in the Post for Me dashboard.
   - Discord webhook (recommended).
   - Twitch Confidential app ID and secret, then "Connect Twitch chat" (optional).
   - YouTube API key (optional).
   Set each with the Settings page, or `PUT /api/settings`.
6. Run `.venv\Scripts\python.exe scripts\check_live.py`. Pass: no FAIL lines.
7. Add one streamer who is live now. Pass: within about 30 s, `/api/state` shows a `session_id` for them.

## B. Daily check
`GET /api/state` → report:
- Who is recording: streamers with `session_id`, with how long (`now - recording_since`) and how many chunks.
- Who is live but not recording: paused, or stopped by the user.
- Any `check_error`.
- Pending clips: `counts.pending`.
- Clips made and posted in the last 24 h.
- Any `error` or `warn` events.

## C. Review and post
1. `GET /api/clips?status=pending`. They come best first.
2. For each clip, give the user:
   - the streamer, score and length
   - the transcript, if any
   - the file path `data/clips/<file>`, or point them at the GUI's Review tab
3. Get an explicit yes, with the caption and the accounts (`GET /api/accounts` for the IDs).
4. Then `POST /api/clips/{id}/approve`.
5. Poll `GET /api/clips/{id}` every 30 s until every entry in `post.results` has `success` set to true or false.
6. Report each account's result and URL.
7. If `status` is `failed`: show `post.error`, fix the cause, and retry only with the user's OK. The app checks for an existing post before re-sending, so a retry won't double-post.

## D. Tune
- **Too many or weak clips:** raise `score_threshold` by 0.5 at a time.
- **Too few clips:** lower it, or connect Twitch or YouTube chat, which is the strongest signal.
- **Clips feel cut off:** raise `postroll`.
- **The build-up is missing:** raise `preroll`.
- **Clips arrive too slowly:** lower `chunk_seconds`, e.g. 300. This costs more CPU.
- **CPU struggles:** set `whisper_model` to "tiny", or `use_whisper` to false.
- Judge a change only after at least one full chunk on a lively stream.

## E. Troubleshoot
Work through the table in `docs/HOW-IT-WORKS.md` ("Troubleshooting"). Where to look:
- the `/api/state` events
- `data/rec/<newest>/recorder.log` (streamlink and ffmpeg errors)
- `scripts\check_live.py`

## F. Money questions
- Use only `docs/MONETISATION.md`. Note its "Checked" date; if it's more than about a month old, say so and re-check the sources.
- Lead with the proven route: paid clipping campaigns where the rights holder pays per 1,000 views (Whop Content Rewards, Vyro, streamers' own programmes).
- Explain why platform creator funds are a weak fit for clips of other people's streams.
- No income promises. Remind the user to read each campaign's rules (captions, tags, disclosure, minimum length) and set them in `default_caption` and the pre-roll and post-roll settings.
