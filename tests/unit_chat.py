import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from clipper.chat import collect_chat, hype_count, parse_irc_privmsg
from clipper.platforms import LiveInfo

FX = Path(__file__).parent / "fixtures"
TOK = ["LUL", "KEKW", "lol", "clip", "+2", "\U0001F602"]


def load(path):
    d = json.loads((FX / path).read_text(encoding="utf-8"))
    return d["status"] if "status" in d else 200, d["response"]


# --- hype_count ---

def test_hype_emote_is_case_sensitive():  # 'lul' in normal speech counted as the LUL emote
    assert hype_count("LUL lul Lul", TOK) == 1


def test_hype_words_are_case_insensitive():  # 'LOL'/'Clip' missed
    assert hype_count("LOL lol Lol clip CLIP", TOK) == 5


def test_hype_whole_word_only():  # 'LULU' or 'collol' counted as hype
    assert hype_count("LULU collol clipboard", TOK) == 0


def test_hype_emoji_and_plus_two_count_every_occurrence():  # repeated emoji counted once
    assert hype_count("\U0001F602\U0001F602\U0001F602 +2 +2", TOK) == 5


def test_hype_punctuation_neighbours_still_match():  # 'KEKW!' or '(lol)' missed because of the regex boundary
    assert hype_count("KEKW! (lol) clip.", TOK) == 3


def test_hype_empty_and_blank_token():  # empty token matching everywhere
    assert hype_count("", TOK) == 0 and hype_count("abc", [""]) == 0


# --- IRC parse ---

LINES = json.loads((FX / "twitch/irc_lines.json").read_text(encoding="utf-8"))["response"]["lines"]


def test_privmsg_with_tags():  # tags prefix leaking into the message text
    assert parse_irc_privmsg(LINES[0]) == "HeyGuys"


def test_privmsg_without_tags_keeps_spaces_and_colons():  # text truncated at the second colon or space
    assert parse_irc_privmsg(":a!a@a.tmi.twitch.tv PRIVMSG #xyz :ratio: 2 : 1\r\n") == "ratio: 2 : 1"


@pytest.mark.parametrize("i", [2, 3, 4, 5])
def test_non_privmsg_lines_return_none(i):  # PING/NOTICE/JOIN/numerics counted as chat messages
    assert parse_irc_privmsg(LINES[i]) is None


# --- simple early exits ---

async def test_kick_and_missing_credentials_return_immediately():  # collector hanging or crashing without credentials
    stop = asyncio.Event()
    msgs = []
    assert await collect_chat("kick", "x", LiveInfo(True), {}, lambda t, s: msgs.append(s), stop, None) == "kick chat not supported (webhooks only)"
    assert await collect_chat("twitch", "x", LiveInfo(True), {"twitch_user_token": "t"}, lambda t, s: None, stop, None) == "no twitch chat credentials"
    assert await collect_chat("youtube", "x", LiveInfo(True), {"youtube_api_key": "k"}, lambda t, s: None, stop, None) == "no youtube chat credentials"
    assert await collect_chat("youtube", "x", LiveInfo(True, video_id="v"), {}, lambda t, s: None, stop, None) == "no youtube chat credentials"


# --- Twitch loop with a fake websocket ---

class FakeWS:
    def __init__(self, frames, stop=None, close_after=False):
        self.frames, self.sent, self.stop, self.close_after = list(frames), [], stop, close_after

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, s):
        self.sent.append(s)

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        if self.stop is not None:
            self.stop.set()
        if self.close_after:
            raise ConnectionResetError("dropped")
        await asyncio.sleep(10)


TCFG = {"twitch_user_token": "TOKEN", "twitch_login": "Kay"}


async def test_twitch_handshake_messages_ping_pong():  # wrong PASS/NICK/JOIN, no PONG (server drops us), tags leak
    stop = asyncio.Event()
    ws = FakeWS([LINES[4] + "\r\n" + LINES[1] + "\r\n", LINES[2] + "\r\n", LINES[0] + "\r\n"], stop)
    got = []
    out = await collect_chat("twitch", "XYZ", LiveInfo(True), TCFG, lambda t, s: got.append(s), stop, None, connect=lambda url: ws)
    assert ws.sent == ["PASS oauth:TOKEN", "NICK kay", "JOIN #xyz", "PONG :tmi.twitch.tv"]
    assert got == ["KEKW KEKW that was insane", "HeyGuys"] and out == "stopped"


async def test_twitch_login_failure_returns_status():  # retrying forever with a bad token
    stop = asyncio.Event()
    ws = FakeWS([LINES[3] + "\r\n"])
    out = await collect_chat("twitch", "x", LiveInfo(True), TCFG, lambda t, s: None, stop, None, connect=lambda url: ws)
    assert out == "twitch auth failed"


async def test_twitch_reconnects_with_doubling_backoff_capped_at_60():  # reconnect hammering, or backoff never capped
    stop = asyncio.Event()
    conns, waits = [], []

    def connect(url):
        conns.append(1)
        return FakeWS([], close_after=True)

    async def sleep(ev, secs):
        waits.append(secs)
        if len(waits) == 9:
            ev.set()
    out = await collect_chat("twitch", "x", LiveInfo(True), TCFG, lambda t, s: None, stop, None, connect=connect, sleep=sleep)
    assert waits == [1, 2, 4, 8, 16, 32, 60, 60, 60] and len(conns) == 9 and out == "stopped"


async def test_twitch_stop_ends_idle_connection():  # stop ignored while the socket is silent
    stop = asyncio.Event()
    ws = FakeWS([])
    task = asyncio.create_task(collect_chat("twitch", "x", LiveInfo(True), TCFG, lambda t, s: None, stop, None, connect=lambda url: ws))
    await asyncio.sleep(0.05)
    stop.set()
    assert await asyncio.wait_for(task, 3) == "stopped"


# --- YouTube loop through MockTransport ---

YT = {"youtube_api_key": "KEY"}
INFO = LiveInfo(True, video_id="VID123")
STARTED = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)


def resp(path):
    status, body = load(path)
    return httpx.Response(status, json=body)


def yt_client(chat_replies, videos="youtube/videos_live.json"):
    calls = []
    replies = iter(chat_replies)

    def h(req):
        calls.append(req)
        if req.url.path.endswith("/videos"):
            return resp(videos)
        return resp(next(replies))
    return httpx.AsyncClient(transport=httpx.MockTransport(h)), calls


async def run_yt(chat_replies, stop_after=None, videos="youtube/videos_live.json"):
    http, calls = yt_client(chat_replies, videos)
    stop = asyncio.Event()
    got, waits = [], []

    async def sleep(ev, secs):
        waits.append(secs)
        if stop_after is not None and len(waits) >= stop_after:
            ev.set()
    out = await collect_chat("youtube", "c", INFO, YT, lambda t, s: got.append(s), stop, http, sleep=sleep, started=STARTED)
    return out, got, waits, calls


async def test_youtube_skips_backlog_and_delivers_new_messages():  # old chat replayed as a hype spike at start
    out, got, waits, calls = await run_yt(["youtube/chat_page1.json", "youtube/chat_page2.json"], stop_after=2)
    assert got == ["KEKW so good", "clip it \U0001F602"] and out == "stopped"


async def test_youtube_uses_chat_id_page_token_and_key():  # wrong liveChatId, page token not passed on (duplicate messages)
    out, got, waits, calls = await run_yt(["youtube/chat_page1.json", "youtube/chat_page2.json"], stop_after=2)
    assert calls[0].url.params["id"] == "VID123" and calls[0].url.params["part"] == "liveStreamingDetails"
    first, second = calls[1].url.params, calls[2].url.params
    assert first["liveChatId"] == "CHAT_ID_1" and "pageToken" not in first and first["key"] == "KEY"
    assert second["pageToken"] == "TOKEN2"


async def test_youtube_wait_is_at_least_20s_even_if_google_says_5s():  # quota burned by following the 5 s hint
    out, got, waits, calls = await run_yt(["youtube/chat_page1.json"], stop_after=1)
    assert waits == [20.0]


async def test_youtube_obeys_longer_server_interval(monkeypatch):  # server asking for a longer wait ignored
    status, body = load("youtube/chat_page1.json")
    body["pollingIntervalMillis"] = 45000
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: resp("youtube/videos_live.json") if r.url.path.endswith("/videos") else httpx.Response(200, json=body)))
    stop, waits = asyncio.Event(), []

    async def sleep(ev, s):
        waits.append(s)
        ev.set()
    await collect_chat("youtube", "c", INFO, YT, lambda t, s: None, stop, http, sleep=sleep, started=STARTED)
    assert waits == [45.0]


async def test_youtube_quota_exceeded_stops_with_status():  # quota error retried until the key is banned
    out, got, waits, calls = await run_yt(["youtube/error_quota.json"])
    assert out == "youtube quota exceeded" and waits == []


async def test_youtube_quota_exceeded_on_first_call():  # quota error on videos.list reported as 'no chat'
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: resp("youtube/error_quota.json")))
    out = await collect_chat("youtube", "c", INFO, YT, lambda t, s: None, asyncio.Event(), http, started=STARTED)
    assert out == "youtube quota exceeded"


async def test_youtube_chat_ended_flag_and_error():  # collector polling a finished chat forever
    out, *_ = await run_yt(["youtube/chat_ended.json"])
    assert out == "youtube chat ended"
    out, *_ = await run_yt(["youtube/error_chat_ended.json"])
    assert out == "youtube chat ended"


async def test_youtube_no_active_chat_id():  # KeyError when the video has no live chat
    out, *_ = await run_yt([], videos="youtube/videos_no_chat.json")
    assert out == "youtube chat not available"


async def test_youtube_transient_5xx_retried_then_gives_up():  # one 503 killing chat, or endless retries
    out, got, waits, calls = await run_yt(["youtube/error_503.json"] * 5)
    assert out == "youtube chat error" and len(waits) == 4
    out, got, *_ = await run_yt(["youtube/error_503.json", "youtube/chat_page2.json"], stop_after=2)
    assert got == ["KEKW so good", "clip it \U0001F602"]


async def test_youtube_stop_event_ends_loop_promptly():  # real sleep ignoring stop (shutdown hangs 20 s)
    http, _ = yt_client(["youtube/chat_page2.json"] * 3)
    stop = asyncio.Event()
    task = asyncio.create_task(collect_chat("youtube", "c", INFO, YT, lambda t, s: None, stop, http, started=STARTED))
    await asyncio.sleep(0.1)
    stop.set()
    assert await asyncio.wait_for(task, 2) == "stopped"
