# Agent C: Discord (S7) and Post for Me (S8)

## Discord docs check, 16 Nov 2026
- Docs moved: discord.com/developers/docs/... 301s to docs.discord.com/developers/... .
- Execute Webhook: `POST /webhooks/{id}/{token}`; query `wait` (default false; true returns the message), `thread_id`. `content` up to 2000 chars. Files go in `files[n]` of a multipart/form-data body, other fields in `payload_json`. `allowed_mentions` controls pings.
- Attachment limit (docs.discord.com/developers/reference, Uploading Files): "The default limit is 20 MiB for all users", per file; higher with Nitro/Boost. News reports date the 10 -> 20 MB raise to Aug 2026. The brief assumed 10 MB; the docs say 20 MiB. `discord_send` keeps the requested default of 10.0 MB (conservative); callers may pass 20.
- 429: body `{"message", "retry_after": <seconds, float>, "global": bool}` plus a Retry-After header. The reference page only points to a Rate Limits page and I did not fetch it, so the body shape is from prior knowledge, not re-verified today. The code reads the body, then the header, else 1 s.
- Fixture: tests/fixtures/discord/execute-webhook.json.

## Post for Me (from the saved excerpt)
- Results filter param is `post_id` (array style, single value used).
- Result: `success` bool, `error` object, `platform_data.url`; result carries `social_account_id`, so platform/username come from the accounts map.
- Spec quirks: CreateSocialPostDto's `external_id` description is a copy-paste of social_accounts; treated as a string. Excerpt has no `meta`-less list shape beyond data/meta; `meta.next` is a full URL string or null.
- TikTok DTO has no required fields; keys used are all in the excerpt. Instagram: placement 'reels'. YouTube DTO lists `localizations` as required in the excerpt, but it is nullable and unknown if the API enforces it; we send `localizations: null` (valid per the spec, satisfies the required list); live check should confirm the API accepts it.
- Upload: signed PUT carries no Authorization header; Content-Type video/mp4.
- Status filter on accounts: `status=connected&limit=100` per the brief.
- `GET /v1/social-posts/{id}` (get_post) is NOT in the saved excerpt's paths (only list/create on /v1/social-posts). The brief required the method, so it exists, but the contract test records it as unverified. Live check needed.
- Auth header is added only to `<base>/v1/` URLs, so a signed upload URL that happens to share the host (the mock) still gets none.
