# Stream Clipper HTTP API

Base: `http://127.0.0.1:8765`, or the `CLIPPER_PORT` you set. Everything is JSON.

**Two rules every client must follow:**
1. Send a `Host` of `127.0.0.1` or `localhost` (normal clients do).
2. Every request that isn't a GET must carry the header `X-Clipper: 1`. Without it the server answers `403`.

Errors look like `{"detail": "<human message>"}`:
- `400`: bad input
- `404`: not found
- `409`: wrong state, e.g. approving a clip that is already posting
- `502`: Post for Me or Twitch refused

```bash
curl http://127.0.0.1:8765/api/state
curl -X POST -H "X-Clipper: 1" -H "Content-Type: application/json" -d "{\"url\":\"https://www.twitch.tv/name\"}" http://127.0.0.1:8765/api/streamers
```

## Status
| Method and path | Returns |
|---|---|
| `GET /api/health` | `{"ok": true}` |
| `GET /api/system` | `{ffmpeg, ffprobe, streamlink, disk_free_gb, whisper_model, whisper_cached, data_dir, platform}`: is this machine ready? |
| `GET /api/state` | `{now, streamers[], counts{pending,posting,posted,failed,rejected}, posted_24h, clips_24h, events[80], poll_seconds, setup{discord_webhook_url, postforme_api_key, twitch_user_token, youtube_api_key}}` |

Each `streamers[]` item has:
- `id`, `platform`, `channel`, `url`, `enabled`
- `live`, `title`, `category`, `checked_at`, `check_error`
- `session_id` (set while recording), `recording_since`
- `chunk_count`, `clip_count`, `last_clip_at`

## Streamers
| Method and path | Body | Notes |
|---|---|---|
| `POST /api/streamers` | `{"url": "https://www.twitch.tv/x"}` | Twitch, Kick or YouTube (`@handle`, `/channel/UC…`, `/c/name`). `201` gives the row; `400` for a bad link; `409` for a duplicate. Starts a live check straight away |
| `PATCH /api/streamers/{id}` | `{"enabled": false}` | Pause or resume tracking. Pausing stops a running recording |
| `DELETE /api/streamers/{id}` | none | Stops recording, removes the streamer and its recording folders. Clips stay |
| `POST /api/streamers/{id}/stop` | none | Stops this recording. It won't restart until they go offline and live again |
| `POST /api/streamers/{id}/clip` | none | Clips the last 65 s (`preroll` + `postroll`, ending about 8 s ago). Gives `{"clip_id"}`; `409` if not recording. Takes 20 to 180 s depending on CPU |
| `POST /api/monitor/check` | none | Runs a live check now |

## Clips
| Method and path | Body | Notes |
|---|---|---|
| `GET /api/clips?status=pending\|done\|rejected\|all&limit=200` | none | Pending clips come best score first; the others most recent first |
| `GET /api/clips/{id}` | none | One clip, with `signals` (`audio`, `chat`, `hype`, `laugh`, `peak_offset`, `manual`, and `curve{audio_db[], chat[], hype[]}`), `video`, `thumb_url`, `duration`, `transcript`, `caption`, and `post{status, accounts, results[], error}` |
| `PATCH /api/clips/{id}` | `{"caption": "…"}` | Up to 2200 characters |
| `POST /api/clips/{id}/approve` | `{"caption": "…", "accounts": ["spc_…"]}` | **This posts to real social accounts.** Only for `pending` or `failed` clips. Gives `{"post_id"}`; the upload runs in the background |
| `POST /api/clips/{id}/reject` | none | Only for `pending` or `failed` clips |
| `POST /api/clips/{id}/restore` | none | Moves a rejected clip back to the queue |
| `GET /media/clips/{file}.mp4\|.jpg` | none | The clip or thumbnail. Supports Range requests |

**Clip status:**
- `pending`: waiting for review.
- `posting`: uploading, or waiting for results.
- `posted`: at least one account worked. Per-account results are in `post.results`.
- `failed`: nothing went out. It can be approved again.
- `rejected`: you turned it down.

**Post status:** `uploading`, `submitted`, `done`, `partial`, `failed`, or `unknown` (a timeout; the app checks with Post for Me before any retry).

## Settings and accounts
| Method and path | Body | Notes |
|---|---|---|
| `GET /api/settings` | none | Every setting. Secrets come back as `true` or `false` only |
| `PUT /api/settings` | `{"key": value, …}` | Only known keys with the right type and range (see `docs/HOW-IT-WORKS.md`). A secret sent as `true` or `false` means unchanged; `""` clears it |
| `GET /api/accounts?refresh=true` | none | Connected Post for Me accounts `[{id, platform, username, profile_photo_url, status}]`, cached for 5 min |
| `POST /api/test/discord` | none | **Sends a real test message** to the webhook |
| `POST /api/test/postforme` | none | Lists your accounts (read-only) |
| `POST /api/test/twitch` | none | Checks the Twitch chat token |
| `POST /api/test/streamlink` | none | Checks a known live channel |
| `POST /api/twitch/device/start` | none | Gives `{user_code, verification_uri, expires_in, interval}`. The user enters the code at that address |
| `POST /api/twitch/device/poll` | none | Gives `{"status": "pending"\|"connected"\|"failed"}`. Call every `interval` seconds |
