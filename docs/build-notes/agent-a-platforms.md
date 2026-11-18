# Agent A: platforms, chat, twitch auth (S2 + S4)

16 Nov 2026. 80 tests pass (unit_platforms, unit_chat, contract_platforms). check_live.py: all 3 probes PASS, keyed checks SKIP (no credentials).

## Decisions
- Twitch device flow: the docs list `client_id`, `scopes`, `device_code`, `grant_type` for the token exchange and no secret (public client). Device flow sends no secret. `refresh_user_token` sends `client_secret` only if non-empty.
- Twitch device-flow denied/expired bodies are not documented. Anything other than `authorization_pending` raises TwitchAuthError. The `device_denied.json` fixture shape is assumed.
- Kick: `GET /public/v1/channels?slug=` (up to 50, repeated param), live flag at `data[].stream.is_live`, title `stream_title`, category `category.name`. Slug not in the reply gives `live False, error "channel not found on Kick"`. App token: `POST https://id.kick.com/oauth/token`, form body. Docs show blank example values, so the fixture values are invented to the shape.
- Helix 401 triggers one forced token refresh and one retry; a second 401, 429 or 5xx falls back to streamlink probes for the batch. The error field is set (`Twitch API failed (...); probe: ...`) only when the probe also fails.
- The 4-wide probe limit is a Semaphore per event loop, not one module global (a global one breaks when tests use a new loop each).
- `collect_chat` has extra keyword-only test hooks: `connect`, `sleep`, `started`. The required positional signature is unchanged. It returns `"stopped"` on a normal stop.
- Hype rule (documented in chat.py): letter/digit tokens match whole words; case-sensitive except lol/lmao/lmfao/clip/wtf/omg, which match any case; other tokens (emoji, `+2`) count every occurrence. In the default token list, `W` and `ICANT` are case-sensitive.
- Twitch IRC: no CAP REQ, so lines have no tags. Handles PING, PONG, and "Login authentication failed" or "Improperly formatted auth" both give `twitch auth failed`.

## YouTube quota maths (10,000 units/day)
videos.list is 1 unit; liveChatMessages.list is 5 units. At the 20 s floor that is 3 calls/min, 15 units/min, 900 units/hour, about 11 hours of one stream per day. At Google's usual 5 s hint it would be 3,600 units/hour (2.7 hours). Two simultaneous streams halve the hours. Not accounted for: other apps sharing the key.

## Not verified
- No real Twitch, Kick or YouTube API call was made (no credentials). All API shapes come from docs; Twitch Get Streams example and liveChatMessages item fields were summarised, not copied verbatim, by the fetch tool. The 401/429 bodies are assumed shapes.
- Real YouTube offline behaviour: `@MrBeast/live` returned "No playable streams found", which maps to offline. Other offline phrasings (private, members-only) will show as an error.
- The Twitch websocket loop is tested against a fake socket only, not irc-ws.chat.twitch.tv.
- YouTube backlog skipping depends on `publishedAt` vs the collector start time, so a clock that is off by more than a few seconds would let a few old messages through or drop new ones.
