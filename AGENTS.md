# Agent start kit: Stream Clipper

You are helping someone run Stream Clipper. It's a local app that watches Twitch, Kick and YouTube streamers, records them when they go live, finds the hype moments, cuts vertical clips, sends each one to Discord and a review queue, and posts the clips the user approves to their social accounts through Post for Me.

Read this file first. Then use these as needed:
- `README.md`: the user's quick start.
- `docs/HOW-IT-WORKS.md`: every stage, file, setting and fix.
- `docs/API.md`: the HTTP API you drive it with.
- `docs/MONETISATION.md`: money routes, sourced and dated.

## Rules you must follow
1. **Never approve or post a clip without the user's explicit OK for that clip.** `POST /api/clips/{id}/approve` publishes to real social accounts. Show the clip (path, streamer, score, transcript, caption, accounts) and wait for a yes. One yes covers one clip.
2. **Never print, log or commit keys.** Keys go in only through the Settings page or `PUT /api/settings`, and the API never returns them. Don't read `data/config.json` aloud. If the user pastes a key in chat, set it with `PUT /api/settings` and don't repeat it.
3. **Ask before deleting anything** in `data/` (clips, the database, recordings), and before `POST /api/test/discord`, which sends a real message.
4. **Rights:**
   - Recording and reposting someone's stream needs their permission or an official clipping programme. If the user hasn't said they have one, say so once, plainly.
   - Don't help bypass platform rules: no fake engagement, no view bots, no hiding where a clip came from.
   - Paid campaigns require clear disclosure beside the clip.
5. **No income promises.** When asked how to make money, use only `docs/MONETISATION.md`. Quote its figures with their sources and dates, and say that rates vary by campaign. Check the date at the top of that file: if it's more than about a month old, tell the user it may be out of date, and re-check the sources if you have web access.

## Set up a new machine (Windows)
1. **Prerequisites:** Python 3.12 (`winget install Python.Python.3.12`) and ffmpeg (`winget install Gyan.FFmpeg`). Open a new terminal afterwards so PATH updates.
2. **First start:** double-click `Start Clipper.cmd`. The first run creates `.venv` and installs `requirements.txt` (a few minutes), then opens http://127.0.0.1:8765.
   - Without the launcher: `python -m venv .venv`, then `.venv\Scripts\python.exe -m pip install -r requirements.txt`, then `.venv\Scripts\python.exe -m clipper`.
3. **Check the machine:** `curl http://127.0.0.1:8765/api/system`. Every tool should be `true`, `disk_free_gb` should be over 20, and `whisper_cached` turns true after the first clip.
4. **Run the smoke checks:** `.venv\Scripts\python.exe scripts\smoke.py`. Every line should say PASS.
5. **Keys:** walk the user through the Setup checklist in the GUI, or the table in `README.md`. Required to post: Post for Me (with their socials connected in the Post for Me dashboard). Recommended: a Discord webhook. Optional: Twitch Confidential app plus "Connect Twitch chat", YouTube Data API key, Kick app.
6. **Check the keys:** `.venv\Scripts\python.exe scripts\check_live.py`. Each key turns its SKIP line into PASS or FAIL. All calls are read-only, except that the Discord check only reads the webhook's name.

**macOS and Linux:** `./start.sh` does the same. It is not tested on those systems yet, so run the tests first (see Testing).

## Everyday tasks (all through the API, header `X-Clipper: 1` on any request that isn't a GET)
| User says | You do |
|---|---|
| "Track xQc on Kick" | `POST /api/streamers {"url":"https://kick.com/xqc"}` |
| "Who's live? Is it recording?" | `GET /api/state`, then report the streamers that have `session_id` (recording), `live`, `check_error` and `chunk_count` |
| "What clips are waiting?" | `GET /api/clips?status=pending`. Summarise each one: streamer, score, length, transcript, and the file at `data/clips/<name>.mp4` |
| "Post clip 12 to TikTok" | `GET /api/accounts` to find the TikTok account ID. Confirm the caption and account with the user, then `POST /api/clips/12/approve`. Poll `GET /api/clips/12` until `post.results` fills in |
| "Too many clips" or "too few" | `PUT /api/settings {"score_threshold": 4.0}` for fewer, or `2.5` for more. Leave a stream running before judging |
| "Clip that now!" | `POST /api/streamers/{id}/clip` (the last 65 s) |
| "Stop recording X" | `POST /api/streamers/{id}/stop`. To stop tracking altogether: `PATCH … {"enabled": false}` |
| "Use campaign tags" | `PUT /api/settings {"default_caption": "{streamer} #ad @brand #campaigntag"}` |
| "Clips must be longer" | Raise `postroll` and/or `preroll`. The total over 60 s keeps TikTok Creator Rewards eligibility |
| "Disk is full" | `PUT /api/settings {"keep_chunks_hours": 1}`. Ask before deleting old clips in `data/clips/` |
| "Nothing is being clipped" | Look at `/api/state` events, check `data/rec/<latest>/recorder.log`, then work through the troubleshooting table in `docs/HOW-IT-WORKS.md` |

## Code map
| File | Job |
|---|---|
| `clipper/pipeline.py` | The orchestrator: monitor, sessions, chunk registration, analysis, clips, posting, results, cleanup |
| `clipper/platforms.py` | Parses links; live checks (Helix, the Kick API, streamlink) |
| `clipper/recorder.py` | streamlink piped into ffmpeg segments; process-tree kill; Windows Job Object |
| `clipper/chat.py`, `twitch_auth.py` | Twitch IRC and YouTube chat; Twitch device-code login |
| `clipper/analysis.py` | Volume, chat and emote scoring; peak picking; whisper laughter and transcript |
| `clipper/cutter.py` | The clip window across chunks; the vertical encode; thumbnails |
| `clipper/notify.py`, `poster.py` | The Discord webhook; the Post for Me client |
| `clipper/app.py` | The FastAPI JSON API and the GUI server, with the Host and CSRF guards |
| `clipper/config.py`, `db.py` | Settings with types and ranges; the SQLite schema |
| `web/` | The GUI (plain HTML, CSS and JS; no build step) |

## Testing (run after any change)
| Command | Layer | Time |
|---|---|---|
| `.venv\Scripts\python.exe -m ruff check .` and `node --check web\app.js` | static | 2 s |
| `run_tests.cmd` (`pytest -q`) | unit, integration (real ffmpeg and SQLite), contract, review | about 2 min |
| `.venv\Scripts\python.exe -m pytest e2e -o python_files=e2e_*.py -q` | browser (Playwright) | about 30 s |
| `.venv\Scripts\python.exe scripts\smoke.py` | the running app | 2 s |
| `.venv\Scripts\python.exe scripts\check_live.py` | real platforms, plus keys if set | 15 s |
| `.venv\Scripts\python.exe stress\s1_monitor_scale.py` (also `s2_concurrent_recording.py`, `s3_api_load.py 8791 30 30`, `s4_failures.py a c e f g h d`, `s5_soak.py`) | stress scenarios, see `stress/STRESS-REPORT.md` | 1 to 3 min each |
| `.venv\Scripts\python.exe scripts\rehearse.py 4` | the real app on 3 real live streams; posts nothing | about 8 min |

The dev tools come from `requirements-dev.txt`. The tests never post or send anything real: Post for Me and Discord are mocked in `tests/mock_services.py`.

## Limits to tell users about
- It only runs while the computer is on and the window is open.
- Kick chat isn't read, so Kick clips are scored on volume and laughter only.
- Whisper and the vertical encode are CPU-heavy. On a laptop, three or more streams at once slow everything down.
- A clip can start up to about 2 s early (keyframes).
- Platform creator funds often treat clips of other people's streams as unoriginal. Paid clipping campaigns are the cleaner route (see `docs/MONETISATION.md`).
