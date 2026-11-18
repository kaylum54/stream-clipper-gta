import asyncio
import json

import pytest

from clipper import platforms
from clipper.platforms import LiveInfo, parse_channel_url, probe_streamlink


@pytest.mark.parametrize("url,expected", [
    ("https://www.twitch.tv/Gaules", ("twitch", "gaules", "https://www.twitch.tv/gaules")),  # login not lowercased
    ("http://twitch.tv/gaules/?sr=a", ("twitch", "gaules", "https://www.twitch.tv/gaules")),  # query/trailing slash/http rejected
    ("https://m.twitch.tv/gaules", ("twitch", "gaules", "https://www.twitch.tv/gaules")),  # m. host rejected
    ("twitch.tv/gaules", ("twitch", "gaules", "https://www.twitch.tv/gaules")),  # bare host without scheme rejected
    ("https://kick.com/ClassyBeef/", ("kick", "classybeef", "https://kick.com/classybeef")),  # kick slug not lowercased
    ("https://www.kick.com/classybeef?x=1", ("kick", "classybeef", "https://kick.com/classybeef")),  # www kick rejected
    ("https://www.youtube.com/@LofiGirl", ("youtube", "@LofiGirl", "https://www.youtube.com/@LofiGirl/live")),  # no /live appended
    ("https://youtube.com/@LofiGirl/live", ("youtube", "@LofiGirl", "https://www.youtube.com/@LofiGirl/live")),  # /live doubled or dropped
    ("https://m.youtube.com/@LofiGirl/live/", ("youtube", "@LofiGirl", "https://www.youtube.com/@LofiGirl/live")),  # m.youtube trailing slash
    ("https://www.youtube.com/channel/UCSJ4gkVC6NrvII8umztf0Ow", ("youtube", "UCSJ4gkVC6NrvII8umztf0Ow", "https://www.youtube.com/channel/UCSJ4gkVC6NrvII8umztf0Ow/live")),  # channel id form
    ("https://www.youtube.com/c/LofiGirl", ("youtube", "c/LofiGirl", "https://www.youtube.com/c/LofiGirl/live")),  # legacy /c/ form
])
def test_parse_channel_url_accepts(url, expected):
    assert parse_channel_url(url) == expected


@pytest.mark.parametrize("url", [
    "", "   ", "not a url",
    "https://twitch.tv/directory", "https://www.twitch.tv/videos/12345", "https://www.twitch.tv/gaules/clip/Abc",  # non-channel twitch pages accepted as streamers
    "https://kick.com/video/abc", "https://kick.com/classybeef/clips/x",  # kick vod/clip links accepted
    "https://www.youtube.com/watch?v=abc", "https://youtu.be/abc", "https://www.youtube.com/playlist?list=PL1",  # youtube video links accepted
    "https://www.youtube.com/shorts/abc", "https://www.youtube.com/channel/notachannelid",
    "https://example.com/gaules", "https://nottwitch.tv/gaules", "ftp://twitch.tv/gaules",  # other/lookalike hosts accepted
])
def test_parse_channel_url_rejects(url):
    with pytest.raises(ValueError) as e:
        parse_channel_url(url)
    assert str(e.value)  # a human message, not an empty error


# --- probe output parsing ---

def test_parse_probe_live_takes_metadata():  # live channel read as offline, or title/category/id dropped
    out = json.dumps({"plugin": "kick", "metadata": {"id": 77, "author": "a", "category": "GTA", "title": "T"}, "streams": {"best": {}}})
    assert platforms._parse_probe(out) == LiveInfo(True, "T", "GTA", "77")


def test_parse_probe_offline_has_no_error():  # an offline channel reported as a failed check
    out = json.dumps({"error": "No playable streams found on this URL: https://kick.com/x"})
    assert platforms._parse_probe(out) == LiveInfo(False, error=None)


@pytest.mark.parametrize("out", ["not json", "[]", json.dumps({"error": "Unable to open URL: boom"}), json.dumps({"streams": {}})])
def test_parse_probe_failures_set_error(out):  # a broken probe silently treated as 'offline'
    info = platforms._parse_probe(out)
    assert info.live is False and info.error


# --- subprocess handling (fake process, no streamlink needed) ---

class FakeProc:
    def __init__(self, out=b"", hang=False):
        self.out, self.hang, self.killed = out, hang, False

    async def communicate(self):
        if self.hang:
            await asyncio.sleep(30)
        return self.out, b""

    def kill(self):
        self.killed = True
        self.hang = False

    async def wait(self):
        return 0


def fake_exec(proc, seen=None):
    async def _exec(*args, **kw):
        if seen is not None:
            seen.update(args=args, env=kw.get("env"))
        return proc
    return _exec


async def test_probe_decodes_utf8_and_sets_encoding_env(monkeypatch):  # cp1252 decode crash on emoji titles
    body = json.dumps({"metadata": {"id": "x", "title": "\U0001F525 hot", "category": "c"}, "streams": {"b": {}}}, ensure_ascii=False).encode("utf-8")
    seen = {}
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec(FakeProc(body), seen))
    info = await probe_streamlink("https://kick.com/x", "streamlink")
    assert info.live and info.title == "\U0001F525 hot"
    assert seen["env"]["PYTHONIOENCODING"] == "utf-8"
    assert seen["args"][:2] == ("streamlink", "--json")


async def test_probe_timeout_kills_child(monkeypatch):  # hung streamlink process left running forever
    proc = FakeProc(hang=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec(proc))
    info = await probe_streamlink("https://kick.com/x", "streamlink", timeout=0.05)
    assert proc.killed and info.live is False and "timed out" in info.error


async def test_probe_missing_binary_is_an_error_not_a_crash(monkeypatch):  # FileNotFoundError escaping the monitor loop
    async def boom(*a, **k):
        raise FileNotFoundError("nope")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    info = await probe_streamlink("https://kick.com/x", "missing")
    assert info.live is False and info.error
