# UI redesign notes (18 Nov 2026)

Brief: redesign `web/` so a first-time, non-technical user can set Stream Clipper up and review clips, while keeping every existing behaviour. Contract: `clipper/app.py`. Files I may change: `web/*`, `e2e/e2e_journeys.py`, `e2e/seed.py` (demo data), this file.

## Read first
- README.md, docs/PLAN.md, clipper/app.py, clipper/config.py (RANGES, SECRET_KEYS), clipper/db.py (schema), clipper/pipeline.py (signals shape, statuses), clipper/analysis.py (how the score is summed), tests/mock_services.py (two mock accounts), the old web/ and e2e/.
- Other agents worked in this folder at the same time (AGENTS.md, docs/API.md, docs/HOW-IT-WORKS.md, config.py postroll 20 -> 25, db.py indexes, stress/). None of it changed the API I build on. docs/HOW-IT-WORKS.md has a "Settings reference" table with GUI labels: see "Docs to update" below.
- Repo has no commits yet, so there is no git diff. The old GUI and e2e files are copied to the scratchpad (`sc-before/`) for comparison.
- Ports: demo on 8770 (as briefed), a bare first-run app on 8773 for screenshots. 8788 belongs to something else. Not using 8765, 8771, 8772.

## Thesis
The product cuts a moment out of a stream: in point, peak, out point. The interface uses that material and nothing decorative:
- The trim-bracket mark stays. It closes on hover, scrubs while recording, and snaps shut once each time you approve a clip.
- The selected clip in the queue sits between the same in/out brackets; approving or rejecting closes them over it as the row folds away.
- Settings draws the clip itself: build-up, the red moment, the after-part, between brackets, and it redraws as you type the seconds.
- Red means one thing: live/recording/the peak. Errors also use red but always with a word ("Failed", "Missing").
- Liveness is real numbers: the recording timer ticks every second, "checked 2 min ago" ages in place, the pending count is in the tab title.

## Decisions
- **Theme:** tokens use CSS `light-dark()`, so each colour is written once. `html[data-theme]` pins light or dark; the top-bar button flips it and stores the choice in localStorage (try/catch). Flipping back to the system's own scheme clears the stored value, so "follow the system" is never lost.
- **Fonts:** Geist + Geist Mono from Google Fonts, loaded non-blocking (`media=print` swap), falling back to Segoe UI Variable / Cascadia Mono offline. Mono only for timecodes and commands: in mono "7.4" read as "7 . 4" and "2,200" as "2 , 200" (first screenshots), so scores and counts use the sans face's tabular figures.
- **Score:** the hype score is literally loudness + chat + emotes + 2 x laughter (analysis.score_series + pipeline.make_clip), so the panel shows those four parts as bars that add up to the number. Kick clips say "no chat data" instead of a 0.
- **Signal strip:** two lanes (loudness line on top, chat bars below) instead of one overlapping band; red peak line with "Peak at 0:40" in the legend; an editor-style playhead with a handle, moved by requestAnimationFrame while playing; hover shows "Jump to 0:23"; drag to scrub; it's a `role=slider` with arrow keys, Home/End.
- **Decide:** Approve (primary) and Reject side by side; Download moved to a text link. After a decision a status line stays at the top of the next clip ("Sent ... Results show in Posted", or "Rejected ... Undo"). Not a toast: it doesn't float or time out. Undo calls `/restore`.
- **Phones:** the queue becomes a sideways strip, the player is sized from the screen height, and Approve/Reject sit in a fixed bar at the bottom.
- **Setup:** a page of five steps. Default route goes there when no streamer exists or a tool is missing; the top bar shows "Setup n of 5" until the three needed steps are done, and Settings always links back. Missing tools show the exact install command with a Copy button (winget on Windows, brew/apt elsewhere, from `/api/system.platform`). Disk under 20 GB shows a warning with the 1.5 GB/hour figure from the README.
- **Settings:** plain labels ("Seconds of build-up before the moment", "Record in pieces of N minutes", "How big a moment must be"); range checks in the form with the server's ranges and a message under the field; only changed fields are sent, so an odd stored value (say a threshold of 3.13) is never rewritten by an untouched slider; a key pasted and tested without pressing Save is saved first; unsaved edits survive switching tabs.
- **Big lists:** keyed rendering (`keyed()` in app.js) reuses rows by id, so a poll never swaps an element under the pointer, the focus or a playing video. The queue renders 50 at a time ("Show 50 more"; J/K extend it), streamers 100 at a time with a filter box from 8 streamers up.
- **Ids that matched route hashes** (`#streamers`, `#posted`, `#setup`) made the browser scroll the page to that element on every visit. Renamed to `#streamer-list`, `#posted-list`; the setup page wrapper has no id.
- **Add form:** `type=text` + `inputmode=url`. The old `type=url` made the browser refuse "twitch.tv/name", which the help text says works.
- **Decide box:** on desktop the Approve/Reject box sticks to the bottom of the panel. At 1024x768 it sat below the fold behind the transcript and accounts.
- **Next check:** while something records and nothing waits, Review says when the next piece is checked, from the pipeline's own rule (piece k is checked once piece k+1 holds the post-roll, plus CHUNK_GRACE 5 s), recomputed every second. The first version computed "about 11 minutes" once and kept saying it for up to a whole piece. The formula is checked against a brute-force loop (scratchpad `sc_nextcheck.mjs`).
- **Phone top bar:** below 480 px the pulse drops "N clips today · N posted" and keeps who's recording; it was being cut to "1 p…". Install commands on the setup page wrap instead of hiding their end in a sideways scroll.

## API requests
- `GET /api/clips` caps `limit` at 500 and has no offset. With 600 waiting, the queue loads the best 500 and says "The best 500 of 600 are loaded; the rest move up as you decide." An `offset` (or `before_score`) parameter would let "Show more" reach the rest: `GET /api/clips?status=pending&limit=200&offset=500`.
- Nice to have: `GET /api/state` could include the next monitor pass as server time in seconds, `"next_check_at": 1795000000.0`, so the GUI could count down to the next live check instead of saying "checked 2 min ago, every 2 min".

## Docs to update (not mine to edit)
- docs/HOW-IT-WORKS.md "Settings reference" uses the old labels ("Check for live every (s)", "Chunk length (min)", "Min gap between clips", "Keep recordings (hours)"). The GUI now says "Check who's live every", "Record in pieces of", "Seconds between two clips", "Keep raw recordings for". "How big a moment must be", "More clips" / "Fewer, bigger", "Send a test message", "Reconnect Twitch chat", "Starting caption" are unchanged.
- The Posted page's tabs are now "Approved" and "Rejected".

## Screenshots
`docs/screenshots/`, all PNG, each at 1440x900 and 390x844 in light and dark (`<view>-<width>-<scheme>.png`):
- Demo data (`e2e/demo.py`, with a recording session added after start, because the app ends seeded sessions on start): `review`, `streamers`, `posted`, `rejected`, `activity`, `settings`, `setup`.
- A bare data dir with ffmpeg and streamlink pointed at missing files: `first-run-setup`, `first-run-review` (no streamers), `recording-empty-review` (recording, nothing to review yet).

## Checks run
- `node --check web/app.js`: OK after every edit. `ruff check e2e/`: clean.
- Width sweep (scratchpad `sc_sweep.py`): 7 views x 17 widths (320 to 1920, every breakpoint edge): page scroll width, any box past the viewport, text spilling out of its box. Found and fixed: the streamer table squeezing its "Now" column to 3 px from 900 to 1099 px (now stacks below 1100), and the setup page's install command pushing the page to 528 px on phones (grid item min-width). Final run: 0 problems on the demo and on the bare first-run app, no console errors.
- Empty review state was rendered in the 300 px queue column (grid auto-placement once the queue was hidden). The first fix, `.review.empty`, clashed with the empty-state box's own `.empty` class (max-width 500 px), so the modifier is `.review.no-clips`.
- Contrast (same script, `--contrast`): every text node's computed colour against the first opaque background behind it, 4.5:1 (3:1 for large text), all views at 1440 and 390, light and dark. Fixed light-mode red on the stage grey (`--rec` is now #c22b1a). Final run: 0 failures. A planted low-contrast rule was caught (13 hits), so the check works.
- Reduced motion (scratchpad `sc_motion.py`): with `prefers-reduced-motion: reduce`, `document.getAnimations()` is empty on every view and the queue and mark transitions are 0 s; without it the recording scrub and the mark's hover run. The shortcut dialog opens with `?`, closes with Escape and starts on Close.
- e2e: 15 journeys. Planted two mistakes in a copy of app.js: a full list rebuild (caught by the 600-clip test) and no auto-repeat guard (first slipped through: the busy flag blocked the second key, so the test now sends a `repeat: true` keydown after the first reject settles; caught after that). Final three runs in a row after the last code change: 15 passed each (57.4 s, 55.1 s, 45.3 s). Big lists: the 600-clip queue renders in 3.5 ms (195 ms after navigation), 20 presses of J take 56 ms, 121 streamers render in 5 ms.
- Unit and integration suite (`pytest -q`, which includes `tests/review_misc.py` reading `web/app.js` for the auto-repeat guard): 218 passed. `scripts/smoke.py` not run: it needs the app on 8765, which I was told not to use.
- Servers: the demo (8770) and the bare app (8773) are stopped; nothing listens on either port and no server process is left.

## Not done, limits
- On phones the signal strip sits just under the fixed Approve/Reject bar until you scroll (at 390x844 its legend shows, the graph doesn't). Fitting it above the bar meant a video about 20% smaller, so the video won.
- `/api/clips` stops at 500 (see API requests), so with more than 500 waiting the rest can't be opened until the queue shrinks.
- The demo can't show a live recording from the seed alone; the screenshots add one after start (scratchpad `sc_recording.py`).
- Not checked: a real Post for Me account, a real Twitch device-code login, Safari/Firefox, a screen reader end to end (roles, names and focus order were checked in Chromium only).

## Lessons
- A modifier class can collide with a child's class in the same stylesheet: `.review.empty` picked up the empty box's `.empty { max-width: 500px }`. Grep the stylesheet for a class name before using it as a modifier.
- A time estimate rendered once goes stale: "about 11 minutes" stayed on screen for ten. Anything that counts toward a time must be recomputed by the clock tick, from the rule that produces it.
- From Git Bash, a Python script given `/c/Users/...` writes to `C:\c\Users\...`. Use `C:/...` paths in Python.
