# Fixer 1 notes

Decisions from REVIEW-1 and the rehearsal, with the files changed.

- B1 partial success: clipper/pipeline.py results_once. All ok -> post 'done', clip 'posted'; some ok -> post 'partial', clip 'posted'; none -> post 'failed', clip 'failed'. approve still accepts only pending/failed.
- B2 duplicate post: clipper/poster.py find_by_external_id (GET /v1/social-posts?external_id=..., param confirmed in tests/fixtures/postforme/openapi-excerpt.json); pipeline._post looks first, reuses a found post, and a network error on create_post sets post 'unknown' and clip 'failed'. Posts already done/partial/failed for the clip are skipped in the lookup, so retrying a fully failed post still sends. Unverified: whether Post for Me accepts a second create with the same external_id.
- S1: results_once try/except per post, one log per error kind; 24 h deadline from posts.created_at.
- S2: cutter.ffprobe_path, used at all four pipeline sites.
- S3: recorder.Recorder.start kills the source tree if the mux Popen fails.
- S4: pipeline._fresh_twitch_token reloads config under Pipeline.twitch_lock.
- S5: clip names are platform-channel-s<session>-<time>.
- S6: app.py delete_streamer removes session dirs inside DATA/rec only.
- S7, S8, M4, M5: web/app.js (e.repeat guard, S.ticked per clip, failed clips open in review, http(s) links only).
- S9: _register_chunks uses the file-name time when it is more than 3 s after the chained start.
- M1: analysis.laughter_and_transcript(ffmpeg=...). M2: notify retries a 413 without the file. M3: config.RANGES.
- R1 monitor_lock; R2 user_stopped set; R3 failed checks keep live/title; R4 recorder.assign_to_job (Windows Job Object); R5 STALL_SECONDS=180 stall stop. Tests in tests/review_rehearsal.py.

Test-support edits: tests/mock_services.py answers GET /v1/social-posts (empty list); tests/review_posting.py base_routes does the same. No review assertion was changed.
