# Stream Clipper

Watches streamers on Twitch, Kick and YouTube. When one goes live it records the stream in 10-minute chunks and scores every second for loud moments, laughter and chat hype. The best moments become 65-second vertical clips, cut from 40 s before the peak to 25 s after it. Each clip arrives in Discord and in a review queue. Approve one and it posts to your accounts through Post for Me.

This is the method from the TikTok "GTA clipping system setup" (`docs/transcript.txt`). What maps to what is in `docs/PLAN.md`.

## Start it

Double-click **`Start Clipper.cmd`**. The GUI opens at http://127.0.0.1:8765. Keep the black window open while you clip; closing it stops recording.

1. **Streamers:** paste channel links, for example `https://www.twitch.tv/name`, `https://kick.com/name` or `https://www.youtube.com/@handle`.
2. **Settings:** paste your Discord webhook and Post for Me key, and press "Send a test message" and "Test connection".
3. **Review:** clips show up here, best first. The keys are `A` approve, `R` reject, `J`/`K` next/previous and `Space` play. The line under the video shows volume and chat across the clip, with the peak in red; click it to jump.
4. **Clip last minute** (on the Streamers page, while recording) makes a clip of the last 65 s by hand.

Without any keys it still works:
- streamlink checks who is live and records them,
- clips are scored on volume and laughter,
- clips land in the review queue.

Keys add Discord alerts, one-click posting, and chat as a signal for Twitch and YouTube.

## Documents

| File | For |
|---|---|
| `docs/HOW-IT-WORKS.md` | Every stage, file, setting and fix |
| `docs/API.md` | The local HTTP API |
| `docs/MONETISATION.md` | Proven ways clippers get paid today, with sources (checked 18 Nov 2026) |
| `AGENTS.md`, `CLAUDE.md`, `.claude/skills/stream-clipper/` | The agent start kit: rules, setup, everyday tasks and runbooks for your AI agent |
| `docs/testing/PYRAMID-REPORT.md`, `stress/STRESS-REPORT.md` | What is tested and measured, and what isn't |

## What it needs

- Python 3.12 (`winget install Python.Python.3.12`) and ffmpeg (`winget install Gyan.FFmpeg`). Dev tools are in `requirements-dev.txt`. On macOS or Linux, use `./start.sh` (not yet tested there).
- Disk: about 1.5 GB per hour per streamer at 720p. Analysed chunks are deleted after 3 hours (setting: "Keep raw recordings for"). Clips stay in `data/clips/`.
- CPU: no GPU, so whisper runs only on each clip's 40 s around the peak (about 20 s each with the base model). A 60 s vertical cut takes about 20 to 40 s. Two of these run at once.

## Credentials

| For | Where | Notes |
|---|---|---|
| Discord alerts | Channel settings > Integrations > Webhooks > New Webhook > Copy URL | Clips up to 20 MB are attached |
| Posting | postforme.dev > API key. Connect TikTok, YouTube or Instagram in their dashboard first | $10/month plan; posting only happens when you press Approve |
| Twitch (optional) | dev.twitch.tv/console/apps > Register: any name, OAuth redirect `http://localhost`, category Other, client type **Confidential**. Copy the Client ID and a New Secret into Settings, save, then press "Connect Twitch chat" and enter the code at twitch.tv/activate | The ID and secret give batch live checks through Helix. Connecting adds chat speed and emotes as clip signals |
| YouTube chat (optional) | console.cloud.google.com > enable "YouTube Data API v3" > Credentials > API key | About 900 of the 10,000 free daily units per hour of stream |
| Kick (optional) | kick.com/settings/developer | Kick chat isn't read; Kick only sends chat to a public webhook |

Keys are saved in `data/config.json` on your computer (gitignored). The GUI never shows them back.

## Tests

| Command | What it runs | When |
|---|---|---|
| `.venv\Scripts\python.exe -m ruff check .` | static checks | after any change |
| `run_tests.cmd` | unit, integration, contract and review tests (real ffmpeg, SQLite, mock Post for Me/Discord), about 2 min | after any change |
| `.venv\Scripts\python.exe -m pytest e2e -o python_files=e2e_*.py` | Playwright journeys in Chromium, about 20 s | after GUI changes |
| `.venv\Scripts\python.exe scripts\smoke.py http://127.0.0.1:8765` | read-only checks on the running app | after starting it |
| `.venv\Scripts\python.exe scripts\check_live.py` | streamlink on real live channels, plus read-only calls with your keys | before a big stream day |
| `.venv\Scripts\python.exe scripts\rehearse.py 4` | the real app on 3 real live streams for 4 min with 60 s chunks; makes clips, posts nothing | to prove the whole chain |
| `.venv\Scripts\python.exe e2e\demo.py` | the GUI with demo data on port 8770 | to look at the GUI |

## Limits

- **Kick chat:** not read.
- **Captions:** typed by you; the whisper transcript is shown beside the clip to help.
- **Approval:** happens in the GUI, not in Discord.
- **Running time:** it only records while your computer is on and the window is open.
- **Clip start:** a clip can start up to one keyframe (about 2 s on Twitch) before the planned point.
- **Rights:** reposting someone else's stream needs their OK or a clipping programme that allows it. Check each streamer's rules before posting.

## Licence

MIT. See `LICENSE`.
