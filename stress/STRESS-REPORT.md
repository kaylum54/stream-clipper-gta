# Stress report: Stream Clipper

Date: 18 Nov 2026. Laptop: Intel Core Ultra 5 225H (14 logical CPUs), Windows 11, about 1.3 GB free RAM during the run, Python 3.12.10, SQLite 3.49.1, ffmpeg from Chocolatey. Everything ran against throwaway data folders (`%TEMP%\stress-*`), fakes for every network service, and no port from the forbidden list (the API run used 8791). Source code was not changed.
Scripts are in `stress/`, the failing tests are in `tests/review_stress.py`.

**verified** = reproduced with a script or a failing test. **suspicion** = reasoned from code or extrapolated, not reproduced.

## Summary

| # | Finding | Severity | Status | Test |
|---|---|---|---|---|
| F1 | One SQLite connection shared by all API threads: concurrent GUI requests get 500s and can receive another request's rows | blocker | verified | b1 |
| F2 | `/api/state` scans whole tables (no index on `clips.streamer_id`, `chunks.session_id`): 0.5 s per poll at 2,000 clips, polled every 3 s per open tab | should fix | verified | b2 |
| F3 | Any analysis exception (missing ffmpeg, locked file) sets `analyzed=1`: the chunk is never retried and later cleaned up | should fix | verified | b3 |
| F4 | Chunks not yet registered when the app restarts (or when the analyser is busy and the app dies) are never analysed and never cleaned | should fix | verified | b4 |
| F5 | Every recorder restart posts another "is live" message to Discord | should fix | verified | b5 |
| F6 | A silent Discord webhook blocks the monitor (one 60 s timeout per new stream, under the monitor lock) and the analyser (per clip) | should fix | verified | b6 |
| F7 | A second Post for Me outage is never logged (`poll_errors` is never cleared) | minor | verified | b7 |
| F8 | An undecodable chunk is "analysed" with no moments and no error (`audio_db_per_second` ignores ffmpeg's exit code) | minor | verified | b8 |
| F9 | One locked old chunk aborts the whole cleanup sweep | minor | verified | b9 |
| F10 | A double click on "Clip last minute" (or analyser plus manual clip) cuts two clips of one moment | minor | verified | b10 |
| F11 | `chat_events` and `events` are never deleted | minor | verified | b11 |
| F12 | Analysis is serial: the second slot of `media_lock` is never used, so capacity is half the design | minor (capacity) | verified | b12 |

Run `tests\review_stress.py`: all 12 fail today.

## Scenario 1: monitor scale (200 streamers)

Script `stress/s1_monitor_scale.py`. Fake `check_live` (40 live, 40 errors, rest offline, 3% of answers take 2 s, the rest 10 to 250 ms), `FakeRec` recorders, Discord on `httpx.MockTransport`.

| Measure | Result |
|---|---|
| One `monitor_once`, 200 streamers, 40 go live | 2.4 s (the slowest fake answer); max event-loop block 12 ms |
| Steady passes | 2.2 to 2.4 s |
| Own overhead with instant checks | 403 ms wall, 69 ms CPU per pass (about 2 ms per autocommit write, one fsync each) |
| 5 concurrent `monitor_once` | serialised by the lock: 16.5 s for all five, 40 sessions for 40 live streamers, 0 streamers with 2 sessions |
| 40 passes of every streamer flipping live/offline | 1 session per streamer (200). A stream needs two offline passes in a row to stop, so flapping creates nothing extra. RSS 43 to 45 MB, handles 234 to 234. Two offline passes at the end close every recorder and chat stop |

Held up: the lock, one session per live streamer, no leaks under flapping.
Verified minor: `Pipeline.tasks` only grows (80 to 240 over the run; finished tasks are never removed). 40 streamers with a failing check log 720 events a day each, forever (F11).

## Scenario 2: 6 concurrent recordings (real Recorder, real ffmpeg)

Script `stress/s2_concurrent_recording.py`. 6 realtime 320x180 15 fps sources, 45 s, 10 s chunks, two bursts each (so 12 clips expected).

| Measure | Result |
|---|---|
| Wall time | 65 s (45 s of media plus the tail) |
| Python CPU | 1.0 s (2% of one core). Whole machine averaged 40% in 5 s samples (typeperf), but that includes my other work; ffmpeg children did most of it |
| Memory | RSS 50 to 52 MB, peak 53 MB |
| Starting 6 sessions | 0.11 s |
| Longest event-loop block | 778 ms (synchronous ffprobe calls in `_register_chunks` and `_chunk_table`, minor) |
| Chunks | 30 files, 30 rows, all play (`ffmpeg -f null` clean, durations 5.0 to 10.0 s, the 5 s ones are the last chunk of each stream) |
| Timeline | worst gap or overlap between neighbouring chunks: 0 ms |
| Analysis backlog (finished but unanalysed chunks) | rises to 6 to 18 in waves, since each chunk waits postroll plus 5 s grace; back to 0 at 65 s |
| Clips | 12 of 12 expected; peaks within 2.0 s of the burst; each clip about 12 s |
| Orphan processes | ffmpeg, ffprobe and streamlink counts 0 before and 0 after |

Held up: all of it. The analyser keeps up at this size; see F12 and the capacity arithmetic below for real stream sizes.

## Scenario 3: API load (real uvicorn, port 8791)

Script `stress/s3_api_load.py`. DB: 2,000 clips (1,500 pending), 100 streamers, 100 sessions, 5,000 chunks, 50,000 chat events, 5,000 events. Child server runs `create_app(start_loops=False)`.

Single requests: `/api/state` 514 to 559 ms (43 KB), `/api/clips?status=pending` 35 ms (298 KB, 200 clips), `status=done` 38 ms (352 KB), `limit=500` 90 to 180 ms (746 KB).

**Race, 25 concurrent approves of one clip: held.** 1 x 200, 24 x 409, exactly one create and one upload at Post for Me, one `posts` row.

**30 concurrent clients for 30 s, as shipped:** 129 requests (4.2/s), 115 errors (89%): every one a 500. Server log: `sqlite3.InterfaceError: bad parameter or other API misuse` (92), `TypeError: the JSON object must be str ... not NoneType` (40, one request read another request's half-fetched row), `IndexError: tuple index out of range`, `JSONDecodeError`. Successful requests had p50 7.3 to 7.9 s and p95 8.8 to 12 s. Even 8 clients gave 52% errors, and 4 clients gave 500s on a repeat. Same result with 1 in 10 requests a PATCH. (F1, F2)

Experiments that change only the stress server, not the source:
- One connection per thread (`threading.local`): 30 clients, 0 errors, 9.7 req/s, but `/api/state` p50 7.2 s because of the table scans.
- Plus three indexes (`clips(streamer_id, created_at)`, `chunks(session_id)`, `sessions(streamer_id, status)`): `/api/state` 27 ms alone; 30 clients: 0 errors, 22 to 24 req/s, p50 `/api/state` 0.8 s, `status=pending` 1.3 s, `status=done` 1.9 s, PATCH p50 25 ms. The list endpoints are then CPU-bound (N+1 query per clip plus 300 KB of JSON), still fine for one user.

A real person hits this with two tabs open (3 s `/api/state` poll plus clip list loads) on a database of a few thousand clips, or whenever the pipeline loop writes while a request reads.

## Scenario 4: failure injection (`stress/s4_failures.py [a c d e f g h]`)

| | Injection | Result |
|---|---|---|
| a | ffmpeg path missing | No session row, no crash, one "recorder failed to start" event per pass (6 of 6 visible in Activity). With the real loops running 12 s: 0.09 s CPU in 14 s, no hot loop. Source process killed, no leaked handles (checked 41 passes: 235 handles flat). Each failed pass leaves an empty `rec/<ts>/` folder plus `recorder.log` (litter, minor). **Chunks already recorded**: analysis fails with WinError 2, all 3 chunks become `analyzed=1` with an error, and repairing the path later analyses nothing (F3) |
| b | streamlink path missing | Same: 6 events, no session, no process, handles flat |
| c | Post for Me 429, 500, no reply (3 s client timeout), create-then-no-reply, then recovery | 5 clips: every phase ends `clip failed / post failed`, except create-then-timeout which ends `failed / unknown`. After recovery, re-approving all five gave 5 posts, **creates = 5 total, 0 duplicates** (found by `external_id`), results poll marks all `posted`. Held up. Not held up: during a result-poll outage only the first error is logged; a second outage after recovery logs nothing (F7) |
| d | Discord webhook that never answers, client at production timeout 60 s | `make_clip` returns after 61 s; the clip row is written first, so the clip exists and the review queue sees it, but the analyser is stuck for 61 s per clip (3 clips per chunk, 3 min). `monitor_once` starting 3 sessions with a 5 s timeout: 15.1 s, so at 60 s it is 3 min per pass and every other check, "check now" and the Streamers page wait (F6) |
| e | Truncated, random-bytes and zero-byte chunks among good ones | Truncated: analysed normally (3.4 s). Garbage and empty: "cannot probe", `analyzed=1` with the error, error event, the pipeline continues. A clip whose 8 s pre-roll reaches the corrupt neighbour fails completely ("Impossible to open", clip lost; minor, suspicion that it should cut the readable part) |
| f | Muxer ffmpeg killed by PID mid-stream | The source dies within 3 s (broken pipe), no orphans. The session stays `recording` until the next monitor pass (up to `poll_seconds`, default 120 s); that pass ends it ("recorder exited with code 1") and starts session 2 in the same pass. Killing both children of the new recorder gave the same. End state: 0 ffmpeg/streamlink processes |
| g | New Pipeline on the same DB mid-session | After chunk 0 was analysed and one clip cut: restart gives still 1 clip (no duplicate), 0 unanalysed chunks, no orphan files. **But** if the analyser had not yet registered anything (2 chunk files on disk, 0 rows): the restart ends the session and 3 analyse passes later still 0 rows, 0 clips; the files stay forever and cleanup never sees them (F4) |
| h | Another connection holds a write transaction 10 s | `monitor_once` waited 10.2 s inside a synchronous sqlite call (the loop cannot run anything meanwhile), a second thread reading through the shared connection saw p99 9.6 s (the connection mutex is held), a separate connection read in 10 ms. No error, no data lost; `timeout=30` covers it. Minor: all DB work is synchronous on the event loop |

## Scenario 5: compressed soak (`stress/s5_soak.py`)

60 pre-made 10 s chunks (20 with bursts) in one session.
- 60 of 60 analysed in 25 s wall, 420 ms per chunk including 20 clip cuts (non-vertical); Python CPU 1.7 s.
- Memory: RSS 50 to 53 MB (peak 55), tracemalloc 1.2 MB current, 2.7 MB peak. No growth to speak of.
- Clips: 20 for 20 bursts; 0 errors; all durations set.
- Concurrency: at most 1 analysis thread at a time (F12).
- `cleanup_once`: with 30 old and 30 recent chunks and 2 old ones left unanalysed, exactly the 28 old analysed chunks were deleted, files gone, 0 wrongly deleted, 2 unanalysed old and 30 recent untouched. Held up.
- A chunk held open by another program: `PermissionError`, and all 30 eligible chunks stayed on disk (F9).

## Scenario 6: disk and retention

Measured: the synthetic 320x180 15 fps stream (testsrc2 plus 64 kbps AAC) recorded at 586 kbit/s (17.6 MB for 240 s). Scaling by pixels times frames (64x) gives 37 Mbps, which is not credible, because real platform streams are bitrate-capped and the synthetic test pattern is unusually noisy. I use the platform ceilings instead:

| Assumption | Rate | Per hour per stream |
|---|---|---|
| README figure, 720p60 | 3.3 Mbps | 1.5 GB |
| Twitch 720p60 ceiling | about 6 Mbps | 2.7 GB |

Bound with `keep_chunks_hours` = 3: a chunk lives for 3 h plus its own length (10 min) plus the cleanup interval (up to 10 min) plus analysis lag, so about 3.4 h of recording per stream:

| Streams recording | 1.5 GB/h | 2.7 GB/h |
|---|---|---|
| 1 | 5 GB | 9 GB |
| 3 | 15 GB | 28 GB |
| 6 | 31 GB | 56 GB |

This laptop has 27 GB free (the brief said 37). So 5 or more streams at the README rate, or 3 at the ceiling, fill the disk before the retention sweep helps. `cleanup_once` itself held: only analysed chunks older than the cutoff are removed (scenario 5). Not bounded: unregistered chunk files (F4), clips (by design), `recorder.log` and empty session folders (never removed), `chat_events` and `events` (F11).
Suspicion (not run): nothing in `pipeline.py` or `recorder.py` checks free disk, so on a full disk ffmpeg exits and the monitor restarts it every pass; only `/api/system` shows `disk_free_gb`. The `keep_chunks_hours` minimum is 1.

Capacity (suspicion, arithmetic from the README): per chunk the analyser does one audio decode plus up to 3 clips at about 30 s cut and 20 s whisper each, run one after another. Worst case about 160 s of work per 600 s chunk, so 3 to 4 streamers saturate it; at one clip per chunk about 60 s, so about 10 streamers. With a second slot (F12) double that.

## Findings in detail

Format: severity, where, failure, test, smallest fix.

**F1 blocker, `clipper/db.py:79` with `clipper/app.py:49` (`conn()` returns the one `Pipeline.conn`).** `check_same_thread=False` lets threadpool workers share a single connection; interleaved `execute` and `fetch` calls on it fail or return wrong rows. Scenario: two browser tabs polling `/api/state` while a clip list loads gives a 500 or a clip list built from half-read rows. Test `test_b1_gui_requests_at_the_same_time_corrupt_the_shared_sqlite_connection` (fails 3 of 3 with `InterfaceError`). Fix: give each thread its own connection (`threading.local()` in a `Pipeline.conn` property, WAL already allows concurrent readers). The stress experiment above shows 0 errors with it.

**F2 should fix, `clipper/db.py` schema, `clipper/app.py:67-85` (`get_state`).** Per-streamer correlated counts scan `clips` and `chunks`. 0.5 s per poll at 2,000 clips, linear in clips times streamers. Test `test_b2_...`. Fix: `CREATE INDEX clips_streamer ON clips(streamer_id, created_at); CREATE INDEX chunks_session ON chunks(session_id); CREATE INDEX sessions_streamer ON sessions(streamer_id, status);` (27 ms measured).

**F3 should fix, `clipper/pipeline.py:325-328`.** Any exception sets `analyzed=1`. A missing ffmpeg for one pass, an antivirus lock or a full disk loses that chunk's moments for good, and `cleanup_once` later deletes the file. Test `test_b3_...`. Fix: on exception set `error` and leave `analyzed=0`, with a retry count (for example `attempts` column, give up at 3).

**F4 should fix, `clipper/pipeline.py:282-283`.** The session query only returns recording sessions or ones with unanalysed rows, and files are only registered inside that loop. After a crash or restart (`_recover` sets them ended) a session whose newest chunks were never registered is skipped forever. Likelihood rises if the analyser is stuck (F6). Test `test_b4_...`. Fix: also select sessions that ended within `keep_chunks_hours` and register their directory, or call `_register_chunks` for every session `_recover` ended.

**F5 should fix, `clipper/pipeline.py:167-170`.** The Discord "is live" message is outside the `if not was_live` check. A recorder that exits right away (geo block, expired login, streamlink error) gets restarted every poll, 720 messages a day. Test `test_b5_...` (4 messages for 4 passes). Fix: send only when `not was_live`.

**F6 should fix, `clipper/pipeline.py:167` (inside `monitor_lock`) and `:377` (inside `analyse_once`).** The Discord post is awaited with the 60 s client timeout, serially. Test `test_b6_...`. Fix: `asyncio.create_task` the post (keep a reference), or give `discord_send` a 10 s timeout.

**F7 minor, `clipper/pipeline.py:485-488`.** Test `test_b7_...`. Fix: clear `poll_errors` after a pass with no error, or key it by hour.

**F8 minor, `clipper/analysis.py:23`.** Test `test_b8_...`. Fix: raise if `r.returncode != 0 and not r.stdout`.

**F9 minor, `clipper/pipeline.py:514`.** Test `test_b9_...`. Fix: wrap the unlink in `try/except OSError: continue` and do not mark it deleted.

**F10 minor, `clipper/pipeline.py:336-339`.** Duplicate check then a 20 to 40 s cut then insert. Test `test_b10_...`. Fix: keep a set of in-flight peaks per session and return early.

**F11 minor, `clipper/pipeline.py:509-517`.** Test `test_b11_...`. Fix: in `cleanup_once` also `DELETE FROM chat_events WHERE ts < cutoff` and old `events` (say 30 days).

**F12 minor (capacity), `clipper/pipeline.py:285-297`.** Sequential `await`. Test `test_b12_...`. Fix: gather the ready chunks of different sessions (`asyncio.gather`), the semaphore already bounds the work.

### Suspicions, not verified
- Disk full is not detected (see scenario 6).
- `_register_chunks` and `_chunk_table` run ffprobe synchronously on the event loop: measured 778 ms loop block with 6 streams. Harmless at this size, grows with streams.
- `events` rows for failed live checks: 720 per streamer per day (verified by arithmetic, not by a day-long run).
- A corrupt chunk anywhere in a clip window loses the whole clip rather than the readable part (verified for one case in 4e).
- Real streamlink behaviour, whisper time and real 720p60 encode cost were not run; the laptop had 1.3 GB free RAM, so I kept loads small.

## What held up well
- Monitor: one session per live streamer under 5 concurrent passes; flapping creates no leaks; failed checks keep the last live state.
- Recorder: 6 concurrent real recordings gave playable, gap-free chunks, 12 of 12 clips at the right second, and no orphan processes. Killing the muxer by PID leaves no orphan and recovers on the next pass. Missing ffmpeg or streamlink fails cleanly with a visible event, no spin, no handle leak.
- Approve race: 25 concurrent approvals produce exactly one post.
- Post for Me: through 429, 500, hangs and a created-but-unanswered post, no duplicates after recovery; every state ends sane and retryable.
- Restart mid-session: no duplicate clips, finished chunks analysed (once registered).
- Cleanup: only analysed chunks older than the cutoff go.
- Memory: flat in the soak (50 to 53 MB).
- SQLite lock for 10 s: waits it out with no error.

## Re-run commands (from the repo folder; on Windows, open it with a capital C: drive letter)

```
.venv\Scripts\python.exe stress\s1_monitor_scale.py              (about 2 min)
.venv\Scripts\python.exe stress\s2_concurrent_recording.py       (about 75 s; args: streams seconds)
.venv\Scripts\python.exe stress\s3_api_load.py 8791 30 30        (about 2.5 min; args: port seconds clients; add norace to skip the approve race)
   set STRESS_FIXED=1  and  set STRESS_INDEX=1  before it to repeat the "after the fix" experiments
.venv\Scripts\python.exe stress\s4_failures.py a c e f g h d     (parts a, c, d, e, f, g, h; part a also runs the streamlink case b; d takes 80 s, g 50 s, the rest under 20 s)
.venv\Scripts\python.exe stress\s5_soak.py                       (about 30 s)
.venv\Scripts\python.exe -m pytest tests\review_stress.py -q     (12 tests, all fail today, about 8 s)
```
`stress/common.py` and `stress/fakes.py` are helpers. Scenario 6 has no script of its own: its inputs are the s2 bitrate (re-measure with ffprobe on a chunk folder under `%TEMP%\stress-s2-*`) and the s5 cleanup run. The s3 script writes the server log to `%TEMP%\stress-s3-*\server.log`.
