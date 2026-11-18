"""Chat collectors: Twitch IRC over websocket and YouTube liveChatMessages polling. Kick chat is out of scope.

YouTube quota (10,000 units/day): videos.list = 1 unit, liveChatMessages.list = 5 units.
Polling every 20 s = 3 calls/min = 15 units/min = 900 units/hour, so one stream lasts about 11 hours a day.
Google's own pollingIntervalMillis is usually 5 s (3,600 units/hour, 2.7 hours), hence the 20 s floor.
"""
import asyncio
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from clipper.platforms import LiveInfo

IRC_URL = "wss://irc-ws.chat.twitch.tv:443"
YT_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
YT_CHAT = "https://www.googleapis.com/youtube/v3/liveChat/messages"
YT_MIN_POLL_MS = 20000

# Hype rule: a token made only of letters/digits counts as a whole word (not inside "KEKWait"), case-sensitive
# because emote names are (LUL, Pog), except plain words in _CASE_FREE which match any case. Any other token
# (emoji, "+2") counts every occurrence.
_CASE_FREE = {"lol", "lmao", "lmfao", "clip", "wtf", "omg"}


def hype_count(text: str, tokens: list[str]) -> int:
    n = 0
    for tok in tokens:
        if not tok:
            continue
        if re.fullmatch(r"\w+", tok):
            flags = re.IGNORECASE if tok.lower() in _CASE_FREE else 0
            n += len(re.findall(rf"(?<!\w){re.escape(tok)}(?!\w)", text, flags))
        else:
            n += text.count(tok)
    return n


_PRIVMSG = re.compile(r"^(?:@\S+ )?:[^ ]+ PRIVMSG #\S+ :(.*)$")


def parse_irc_privmsg(line: str) -> str | None:
    m = _PRIVMSG.match(line.rstrip("\r\n"))
    return m.group(1) if m else None


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Sleep that ends early when stop is set."""
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def collect_chat(platform: str, channel: str, info: LiveInfo, cfg: dict,
                       on_message: Callable[[float, str], None], stop: asyncio.Event, http,
                       *, connect=None, sleep=None, started: datetime | None = None) -> str:
    """Runs until stop is set; returns a short status. connect/sleep/started are test hooks."""
    sleep = sleep or _wait
    if platform == "kick":
        return "kick chat not supported (webhooks only)"
    if platform == "twitch":
        if not (cfg.get("twitch_user_token") and cfg.get("twitch_login")):
            return "no twitch chat credentials"
        return await _twitch(channel, cfg, on_message, stop, connect, sleep)
    if platform == "youtube":
        if not (cfg.get("youtube_api_key") and info.video_id):
            return "no youtube chat credentials"
        return await _youtube(info.video_id, cfg["youtube_api_key"], on_message, stop, http, sleep,
                              started or datetime.now(timezone.utc))
    return f"no chat support for {platform}"


async def _twitch(channel, cfg, on_message, stop, connect, sleep) -> str:
    if connect is None:
        from websockets.asyncio.client import connect as ws_connect
        connect = ws_connect
    backoff = 1.0
    while not stop.is_set():
        try:
            async with connect(IRC_URL) as ws:
                await ws.send(f"PASS oauth:{cfg['twitch_user_token']}")
                await ws.send(f"NICK {cfg['twitch_login'].lower()}")
                await ws.send(f"JOIN #{channel.lower()}")
                while not stop.is_set():
                    try:
                        frame = await asyncio.wait_for(ws.recv(), 1.0)
                    except asyncio.TimeoutError:
                        continue  # lets us notice stop
                    if isinstance(frame, bytes):
                        frame = frame.decode("utf-8", errors="replace")
                    for line in frame.split("\r\n"):
                        if not line:
                            continue
                        backoff = 1.0
                        if line.startswith("PING"):
                            await ws.send("PONG" + line[4:])
                        elif "Login authentication failed" in line or "Improperly formatted auth" in line:
                            return "twitch auth failed"
                        else:
                            text = parse_irc_privmsg(line)
                            if text is not None:
                                on_message(time.time(), text)
        except Exception as e:  # dropped socket or failed handshake: reconnect; anything else is a bug
            if not (isinstance(e, (OSError, EOFError)) or e.__class__.__module__.startswith("websockets")):
                raise
        if stop.is_set():
            break
        await sleep(stop, backoff)
        backoff = min(backoff * 2, 60.0)
    return "stopped"


def _parse_time(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _error_reason(r: httpx.Response) -> str:
    try:
        return r.json()["error"]["errors"][0].get("reason", "")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return ""


async def _youtube(video_id, key, on_message, stop, http, sleep, started: datetime) -> str:
    try:
        r = await http.get(YT_VIDEOS, params={"part": "liveStreamingDetails", "id": video_id, "key": key})
    except httpx.HTTPError:
        return "youtube chat error"
    if r.status_code == 403 and _error_reason(r) in ("quotaExceeded", "dailyLimitExceeded"):
        return "youtube quota exceeded"
    if r.status_code != 200:
        return f"youtube api error {r.status_code}"
    items = r.json().get("items") or []
    chat_id = (items[0].get("liveStreamingDetails") or {}).get("activeLiveChatId") if items else None
    if not chat_id:
        return "youtube chat not available"

    token = None
    failures = 0
    while not stop.is_set():
        params = {"liveChatId": chat_id, "part": "snippet", "key": key}
        if token:
            params["pageToken"] = token
        wait_ms = YT_MIN_POLL_MS
        try:
            r = await http.get(YT_CHAT, params=params)
        except httpx.HTTPError:
            r = None
        if r is not None and r.status_code == 200:
            failures = 0
            j = r.json()
            token = j.get("nextPageToken") or token
            wait_ms = max(int(j.get("pollingIntervalMillis") or 0), YT_MIN_POLL_MS)
            for it in j.get("items", []):
                sn = it.get("snippet") or {}
                when = _parse_time(sn.get("publishedAt"))
                if when is not None and when < started:
                    continue  # backlog from before we began
                text = sn.get("displayMessage")
                if text:
                    on_message(time.time(), text)
            if j.get("offlineAt"):
                return "youtube chat ended"
        elif r is not None and r.status_code == 403:
            reason = _error_reason(r)
            if reason in ("quotaExceeded", "dailyLimitExceeded"):
                return "youtube quota exceeded"
            if reason in ("liveChatEnded", "liveChatDisabled"):
                return "youtube chat ended"
            return "youtube api error 403"
        elif r is not None and r.status_code in (400, 401, 404):
            return f"youtube api error {r.status_code}"
        else:  # 5xx, 429 or network: retry a few times
            failures += 1
            if failures >= 5:
                return "youtube chat error"
        await sleep(stop, wait_ms / 1000)
    return "stopped"
