# fixer-2 notes
- F2/migrations: db.py indexes (5), MIGRATIONS list + ALTER TABLE in connect(); chunks.attempts column
- F1: app.py thread-local conn() via db.connect per thread; b8: analysis.audio_db_per_second raises on ffmpeg failure with no output; b6: notify.discord_send timeout=15 param passed per request
- pipeline.py: F3 attempts/retry, F4 first-pass sweep, F5 announce only offline->live + throttled crash warn, F6 _discord background task, b7 clear poll_errors, b9 per-file unlink, b10 clip_now lock + in-flight make_clip dedupe + 20s manual check, b11 retention, b12 gather, S-disk, S-ffprobe async register/_chunk_table to_thread
- tests: new tests/review_stress_fixes.py (7 tests); review_stress.py b5 waits 0.2 s (Discord is a background task), b8 raises RuntimeError (ruff B017); int_pipeline.py waits 1 s before counting webhooks and widens the fake chat burst to 6 s (alignment flake, chat score 0 when audio peak lags the chat burst)
- s3 30 clients: 0 errors, 18-19 req/s; /api/state single 514-559 ms -> 169 ms; under load p50 state 0.9 s, pending 1.6 s, done 2.3 s
- s5 soak: 60/60 chunks 17.7 s (was 25 s), max concurrent analysis threads 2, cleanup with locked file leaves only the locked one
