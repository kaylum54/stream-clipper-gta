"""Who is live: URL parsing, official-API checks (Twitch Helix, Kick public API) and the streamlink probe."""
import asyncio
import json
import os
import re
import time
import weakref
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

HELIX_STREAMS = "https://api.twitch.tv/helix/streams"
TWITCH_TOKEN = "https://id.twitch.tv/oauth2/token"
KICK_TOKEN = "https://id.kick.com/oauth/token"
KICK_CHANNELS = "https://api.kick.com/public/v1/channels"


@dataclass
class LiveInfo:
    live: bool
    title: str | None = None
    category: str | None = None
    video_id: str | None = None   # YouTube video id (needed for chat); Twitch/Kick may leave None or set stream id
    error: str | None = None      # set when the check itself failed (network, auth); live is then False


# ---------- URL parsing ----------

_TWITCH_LOGIN = re.compile(r"^[A-Za-z0-9_]{1,25}$")
_TWITCH_RESERVED = {"directory", "videos", "video", "clip", "clips", "settings", "downloads", "turbo", "p", "jobs", "store"}
_KICK_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,25}$")
_KICK_RESERVED = {"video", "videos", "categories", "category", "clips", "browse", "following", "dashboard", "search"}
_YT_HANDLE = re.compile(r"^@[\w.\-]{1,100}$")
_YT_CHANNEL = re.compile(r"^UC[\w-]{20,}$")


def parse_channel_url(url: str) -> tuple[str, str, str]:
    """-> (platform, channel, canonical_url). Raises ValueError with a human message for anything unsupported."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Paste a channel link, for example https://www.twitch.tv/name")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError("The link must start with http or https")
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    segs = [s for s in parts.path.split("/") if s]

    if host == "twitch.tv":
        if len(segs) != 1 or segs[0].lower() in _TWITCH_RESERVED or not _TWITCH_LOGIN.match(segs[0]):
            raise ValueError("Use a Twitch channel link like https://www.twitch.tv/name (not videos, clips or directory pages)")
        login = segs[0].lower()
        return "twitch", login, f"https://www.twitch.tv/{login}"

    if host == "kick.com":
        if len(segs) != 1 or segs[0].lower() in _KICK_RESERVED or not _KICK_SLUG.match(segs[0]):
            raise ValueError("Use a Kick channel link like https://kick.com/name (not videos or clips)")
        slug = segs[0].lower()
        return "kick", slug, f"https://kick.com/{slug}"

    if host == "youtube.com":
        if segs and segs[0].startswith("@") and len(segs) <= 2 and (len(segs) == 1 or segs[1].lower() == "live") and _YT_HANDLE.match(segs[0]):
            return "youtube", segs[0], f"https://www.youtube.com/{segs[0]}/live"
        if len(segs) in (2, 3) and segs[0] == "channel" and _YT_CHANNEL.match(segs[1]) and (len(segs) == 2 or segs[2].lower() == "live"):
            return "youtube", segs[1], f"https://www.youtube.com/channel/{segs[1]}/live"
        if len(segs) in (2, 3) and segs[0] == "c" and re.match(r"^[\w.\-]{1,100}$", segs[1]) and (len(segs) == 2 or segs[2].lower() == "live"):
            return "youtube", f"c/{segs[1]}", f"https://www.youtube.com/c/{segs[1]}/live"
        raise ValueError("Use a YouTube channel link like https://www.youtube.com/@name (not a video, playlist or short)")

    raise ValueError("Only Twitch, Kick and YouTube channel links are supported")


# ---------- streamlink probe ----------

_sems: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _sem() -> asyncio.Semaphore:
    """Semaphore(4) per event loop (a module-level one breaks when tests use a new loop each)."""
    loop = asyncio.get_running_loop()
    if loop not in _sems:
        _sems[loop] = asyncio.Semaphore(4)
    return _sems[loop]


async def probe_streamlink(url: str, streamlink: str, timeout: float = 40) -> LiveInfo:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        proc = await asyncio.create_subprocess_exec(
            streamlink, "--json", url, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        return LiveInfo(False, error=f"cannot start streamlink: {e.__class__.__name__}")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return LiveInfo(False, error=f"streamlink timed out after {int(timeout)}s")
    return _parse_probe(out.decode("utf-8", errors="replace"))


def _parse_probe(text: str) -> LiveInfo:
    try:
        data = json.loads(text)
    except ValueError:
        return LiveInfo(False, error="streamlink gave unreadable output")
    if not isinstance(data, dict):
        return LiveInfo(False, error="streamlink gave unreadable output")
    if data.get("streams"):
        meta = data.get("metadata") or {}
        vid = meta.get("id")
        return LiveInfo(True, title=meta.get("title"), category=meta.get("category"),
                        video_id=str(vid) if vid is not None else None)
    err = str(data.get("error") or "")
    if "No playable streams" in err:
        return LiveInfo(False)
    return LiveInfo(False, error=(err[:120] or "streamlink found no streams"))


async def _probe_limited(s: dict, cfg: dict) -> LiveInfo:
    async with _sem():
        return await probe_streamlink(s["url"], cfg["streamlink"])


# ---------- app tokens (cached until expiry) ----------

_tokens: dict[str, tuple[str, str, float]] = {}   # kind -> (client_id, token, expires at monotonic)


def _reset_token_cache() -> None:
    _tokens.clear()


async def _app_token(kind: str, url: str, cid: str, secret: str, http: httpx.AsyncClient, force: bool = False) -> str:
    cached = _tokens.get(kind)
    if cached and not force and cached[0] == cid and cached[2] > time.monotonic() + 60:
        return cached[1]
    r = await http.post(url, data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
    r.raise_for_status()
    j = r.json()
    try:
        ttl = float(j.get("expires_in") or 3600)
    except (TypeError, ValueError):
        ttl = 3600.0
    _tokens[kind] = (cid, j["access_token"], time.monotonic() + ttl)
    return j["access_token"]


async def _authed_get(kind: str, token_url: str, cid: str, secret: str, url: str, params: list,
                      http: httpx.AsyncClient, headers: dict) -> httpx.Response:
    """GET with a bearer app token; one token refresh when the API answers 401."""
    for attempt in (0, 1):
        tok = await _app_token(kind, token_url, cid, secret, http, force=attempt == 1)
        r = await http.get(url, params=params, headers={**headers, "Authorization": f"Bearer {tok}"})
        if r.status_code != 401:
            break
    r.raise_for_status()
    return r


# ---------- per-platform API checks ----------

async def _helix(streamers: list[dict], cfg: dict, http: httpx.AsyncClient) -> dict[int, LiveInfo]:
    out: dict[int, LiveInfo] = {}
    for i in range(0, len(streamers), 100):
        batch = streamers[i:i + 100]
        params = [("user_login", s["channel"]) for s in batch] + [("first", "100")]
        r = await _authed_get("twitch", TWITCH_TOKEN, cfg["twitch_client_id"], cfg["twitch_client_secret"],
                              HELIX_STREAMS, params, http, {"Client-Id": cfg["twitch_client_id"]})
        live = {(x.get("user_login") or "").lower(): x for x in r.json().get("data", []) if x.get("type") == "live"}
        for s in batch:
            x = live.get(s["channel"].lower())
            out[s["id"]] = LiveInfo(True, x.get("title"), x.get("game_name"), x.get("id")) if x else LiveInfo(False)
    return out


async def _kick_api(streamers: list[dict], cfg: dict, http: httpx.AsyncClient) -> dict[int, LiveInfo]:
    out: dict[int, LiveInfo] = {}
    for i in range(0, len(streamers), 50):
        batch = streamers[i:i + 50]
        params = [("slug", s["channel"]) for s in batch]
        r = await _authed_get("kick", KICK_TOKEN, cfg["kick_client_id"], cfg["kick_client_secret"],
                              KICK_CHANNELS, params, http, {})
        found = {(c.get("slug") or "").lower(): c for c in r.json().get("data", [])}
        for s in batch:
            c = found.get(s["channel"].lower())
            if c is None:
                out[s["id"]] = LiveInfo(False, error="channel not found on Kick")
            elif (c.get("stream") or {}).get("is_live"):
                out[s["id"]] = LiveInfo(True, c.get("stream_title"), (c.get("category") or {}).get("name"))
            else:
                out[s["id"]] = LiveInfo(False)
    return out


async def check_live(streamers: list[dict], cfg: dict, http: httpx.AsyncClient) -> dict[int, LiveInfo]:
    """A LiveInfo for every streamer id. API paths fall back to streamlink probes when they fail."""
    results: dict[int, LiveInfo] = {}
    to_probe: list[tuple[dict, str | None]] = []   # (streamer, api failure reason)

    async def api_group(group: list[dict], fn, name: str):
        if not group:
            return
        try:
            results.update(await fn(group, cfg, http))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            reason = f"{name} API failed ({e.__class__.__name__})"
            to_probe.extend((s, reason) for s in group)

    tw = [s for s in streamers if s["platform"] == "twitch"]
    kk = [s for s in streamers if s["platform"] == "kick"]
    other = [s for s in streamers if s["platform"] not in ("twitch", "kick")]
    if cfg.get("twitch_client_id") and cfg.get("twitch_client_secret"):
        await api_group(tw, _helix, "Twitch")
    else:
        to_probe.extend((s, None) for s in tw)
    if cfg.get("kick_client_id") and cfg.get("kick_client_secret"):
        await api_group(kk, _kick_api, "Kick")
    else:
        to_probe.extend((s, None) for s in kk)
    to_probe.extend((s, None) for s in other)

    infos = await asyncio.gather(*(_probe_limited(s, cfg) for s, _ in to_probe))
    for (s, reason), info in zip(to_probe, infos, strict=True):
        if info.error and reason:
            info.error = f"{reason}; probe: {info.error}"
        results[s["id"]] = info
    return results
