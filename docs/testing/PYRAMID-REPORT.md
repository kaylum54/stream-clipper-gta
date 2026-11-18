# Testing pyramid report: Stream Clipper

**Second pass, 18 Nov 2026.** This pass added a static layer, coverage, gap tests, a mutation sample, stress scenarios and CI. The first pass was on 16 Nov.

**Machine:** Python 3.12.10, streamlink 8.6.3, ffmpeg (Chocolatey), Windows 11, no GPU, about 27 GB free disk, about 1.3 GB free RAM during the stress runs.

## Shape now

| Layer | Where | Count | Time | What runs for real |
|---|---|---|---|---|
| Static | `ruff check .` (pyflakes and bugbear rules), `node --check web/*.js` | whole repo | 2 s | |
| Unit | `tests/unit_*.py` | 62 functions, more cases once parametrised | under 5 s | parsers, scoring, peak picking, window maths, payloads, masking |
| Integration | `tests/int_*.py` | 32 | about 90 s | real ffmpeg on synthetic streams, real SQLite, the recorder pipe, the API through TestClient, the whole chain from live to Discord |
| Contract | `tests/contract_*.py` | 26 | under 2 s | Post for Me requests checked against its saved OpenAPI excerpt; Twitch, Kick, YouTube and Discord doc fixtures replayed through the real adapters |
| Review | `tests/review_*.py` | 41 | about 20 s | one test per finding from the code review, the rehearsals and the stress tests |
| Browser | `e2e/` (Playwright) | 10 functions, 15 cases | about 50 s | the real app in Chromium: setup flow, add a streamer, review and approve, reject and undo, settings secrets, theme, a 600-clip queue, 121 streamers, every view at 390 px |
| Smoke | `scripts/smoke.py` | 12 checks | 2 s | the running app, read-only |
| Live check | `scripts/check_live.py` | 3 probes plus 6 keyed checks | 15 s | streamlink on real channels; read-only API calls once keys exist |
| Stress | `stress/s1` to `s5` | 5 scenarios | 1 to 3 min each | 200 streamers, 6 concurrent recordings, 30-client API load, failure injection, a 60-chunk soak |
| Live rehearsal | `scripts/rehearse.py` | | 4 to 8 min | the real app on real Twitch, Kick and YouTube streams |

`pytest -q` runs unit, integration, contract and review together: 218 tests in about 105 s. The 60 s target is missed because three tests must run in real time (the recorder, the whole pipeline, and "Clip last minute") and one does a 1080x1920 encode. `-m "not slow"` takes about 15 s.

## Coverage (branch), after the gap tests
`coverage run --branch --source=clipper -m pytest`: 84% overall before the gap tests.
- Lowest: `app.py` at 66% (settings test buttons and the Twitch login were never run) and `pipeline.py` at 82% (cleanup and loop survival).
- Gap tests added in `tests/int_gaps.py`:
  - Twitch device login
  - video Range requests (needed for seeking)
  - the system check
  - test buttons with no keys
  - cleanup keeping unanalysed chunks
  - a crashing loop that keeps running
  - two offline checks stopping a recording
  - "Clip last minute" on a growing chunk
  - media traversal with a backslash on Windows
  - posted clips that can't be rejected

## Mutation sample (hand-made, 12 mutants on posting and security code)
11 of 12 were killed.
- **Three survivors in the first run:** two led to the last two gap tests above, which then killed them.
- **The one left** (`bool(summary) and …`) is equivalent, because summaries always list every account.
- **One real gap found this way:** `/media/clips/..%5Csecret.jpg`. On Windows, a backslash reaches the handler as a path separator. The folder check blocks it, and now a test proves that.

## Real findings, all fixed with a test

| Found by | Bug |
|---|---|
| int_app | An empty first results poll marked a clip "posted" |
| int_pipeline | Chunk times from 1 s file names overlapped, so clips came out 2.2 s too long |
| e2e (first GUI) | Three bugs: the streamer list stopped updating; accounts never loaded; messages were wiped |
| Live rehearsal 1 | Overlapping live checks; Stop restarting on the next check; repeat "went live"; orphaned processes; stalled recorders |
| Code review | **Two double-posting blockers** (retry after a partial failure; retry after a timeout), plus 14 more |
| Stress (S3) | **The API shared one SQLite connection across threads.** With 8 or more clients about half of requests failed (30 clients: 89% failed); after the fix, 0 errors. Also: `/api/state` took 0.5 s at 2,000 clips (now 0.17 s, with indexes) |
| Stress (other) | Moments lost on transient errors; chunks never registered after a restart; Discord spam and blocking; outages not logged; one locked file aborting cleanup; double "Clip last minute"; tables never pruned; analysis serial; no disk-full guard; ffprobe blocking the loop |
| Mutation sample | Backslash traversal had no test |

## Measures
- **Flakes:**
  - Fast layers: 5 runs, 182 of 182 each time (16 Nov).
  - Full suite: 3 runs after this round. The result is in the build log.
  - One flaky integration test was found: the chat burst in `int_pipeline` was narrower than the recorder's start-up lag. The burst was widened and the reason noted.
- **Stress after the fixes:**
  - 30 clients: 0 errors, about 18 to 19 requests a second, `/api/state` p50 0.94 s and p95 1.25 s on a 2,000-clip database.
  - Soak: 60 chunks in 17.7 s, memory flat.

## Not covered, and why
- **Real sends:** nothing real has been sent to Discord or Post for Me, because no keys are set. `check_live.py` turns each SKIP into PASS or FAIL once they are.
- **Chat:** Twitch IRC and YouTube chat have only run against fakes and doc fixtures. Kick chat isn't read.
- **Other systems:** macOS and Linux (`start.sh`), Safari and Firefox, and a full screen-reader pass were not tested.
- **CI:** the workflow file (`.github/workflows/test.yml`) has not run, because the repo has no remote yet.
- **Real-life conditions not seen:** a real Twitch ad break, a 720p60 encode under load with 4 or more streams, and a disk actually filling up.

## Next three actions
1. Add the keys and run `scripts/check_live.py`.
2. Post one approved clip to a private or test account.
3. On GTA launch day, watch one full 10-minute chunk cycle on 2 or 3 real streams, then tune "How big a moment must be".
