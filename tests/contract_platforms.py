"""Twitch/Kick/auth replies from dated fixtures (written from the docs) through the real adapters. No network."""
import json
from pathlib import Path

import httpx
import pytest

from clipper import platforms, twitch_auth
from clipper.platforms import LiveInfo, check_live

FX = Path(__file__).parent / "fixtures"


def fx(path):
    d = json.loads((FX / path).read_text(encoding="utf-8"))
    return httpx.Response(d["status"], json=d["response"])


@pytest.fixture(autouse=True)
def clean_tokens():
    platforms._reset_token_cache()
    yield
    platforms._reset_token_cache()


def client(handler):
    calls = []

    def wrapped(req):
        calls.append(req)
        return handler(req)
    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped)), calls


@pytest.fixture
def no_probe(monkeypatch):
    probed = []

    async def fake(url, streamlink, timeout=40):
        probed.append(url)
        return LiveInfo(False)
    monkeypatch.setattr(platforms, "probe_streamlink", fake)
    return probed


def tw(i, ch):
    return {"id": i, "platform": "twitch", "channel": ch, "url": f"https://www.twitch.tv/{ch}"}


TCFG = {"twitch_client_id": "cid", "twitch_client_secret": "sec", "streamlink": "sl"}


# --- Twitch Helix ---

async def test_helix_live_and_offline(no_probe):  # live flag/title/game mixed up between channels
    def h(req):
        if req.url.host == "id.twitch.tv":
            return fx("twitch/token_client_credentials.json")
        assert req.headers["Client-Id"] == "cid" and req.headers["Authorization"] == "Bearer fake-twitch-app-token"
        assert req.url.params.get_list("user_login") == ["gaules", "nobody"]
        return fx("twitch/streams_live.json")
    http, _ = client(h)
    res = await check_live([tw(1, "gaules"), tw(2, "nobody")], TCFG, http)
    assert res[1] == LiveInfo(True, "CS ao vivo \U0001F525", "Counter-Strike", "40952121099")
    assert res[2] == LiveInfo(False)
    assert no_probe == []


async def test_helix_token_cached_across_calls(no_probe):  # a new app token fetched on every poll
    def h(req):
        return fx("twitch/token_client_credentials.json") if req.url.host == "id.twitch.tv" else fx("twitch/streams_empty.json")
    http, calls = client(h)
    await check_live([tw(1, "a")], TCFG, http)
    await check_live([tw(1, "a")], TCFG, http)
    assert sum(c.url.host == "id.twitch.tv" for c in calls) == 1


async def test_helix_401_refreshes_token_once(no_probe):  # expired app token stops all Twitch checks
    state = {"streams": 0}

    def h(req):
        if req.url.host == "id.twitch.tv":
            return fx("twitch/token_client_credentials.json")
        state["streams"] += 1
        return fx("twitch/streams_401.json") if state["streams"] == 1 else fx("twitch/streams_live.json")
    http, calls = client(h)
    res = await check_live([tw(1, "gaules")], TCFG, http)
    assert res[1].live and state["streams"] == 2
    assert sum(c.url.host == "id.twitch.tv" for c in calls) == 2


async def test_helix_persistent_401_falls_back_without_looping(no_probe):  # infinite refresh loop on bad credentials
    def h(req):
        return fx("twitch/token_client_credentials.json") if req.url.host == "id.twitch.tv" else fx("twitch/streams_401.json")
    http, calls = client(h)
    res = await check_live([tw(1, "gaules")], TCFG, http)
    assert no_probe == ["https://www.twitch.tv/gaules"] and res[1] == LiveInfo(False)
    assert len(calls) == 4  # token, streams, token, streams


async def test_helix_429_falls_back_and_reports_reason_only_if_probe_fails(monkeypatch):  # fallback hiding why the API failed, or error set when probe worked
    def h(req):
        return fx("twitch/token_client_credentials.json") if req.url.host == "id.twitch.tv" else fx("twitch/rate_429.json")
    http, _ = client(h)

    async def ok(url, sl, timeout=40):
        return LiveInfo(True, "t", "c", "v")
    monkeypatch.setattr(platforms, "probe_streamlink", ok)
    assert (await check_live([tw(1, "x")], TCFG, http))[1] == LiveInfo(True, "t", "c", "v")

    async def bad(url, sl, timeout=40):
        return LiveInfo(False, error="timed out")
    monkeypatch.setattr(platforms, "probe_streamlink", bad)
    err = (await check_live([tw(1, "x")], TCFG, http))[1].error
    assert "Twitch API failed" in err and "timed out" in err


async def test_helix_batches_of_100(no_probe):  # more than 100 logins sent in one request (Helix rejects it)
    sizes = []

    def h(req):
        if req.url.host == "id.twitch.tv":
            return fx("twitch/token_client_credentials.json")
        sizes.append(len(req.url.params.get_list("user_login")))
        return fx("twitch/streams_empty.json")
    http, _ = client(h)
    res = await check_live([tw(i, f"c{i}") for i in range(150)], TCFG, http)
    assert sizes == [100, 50] and len(res) == 150


async def test_twitch_without_keys_uses_probe_only(no_probe):  # API call attempted without credentials
    http, calls = client(lambda r: pytest.fail("no HTTP expected"))
    res = await check_live([tw(1, "a")], {"twitch_client_id": "", "twitch_client_secret": "", "streamlink": "sl"}, http)
    assert calls == [] and no_probe == ["https://www.twitch.tv/a"] and res[1] == LiveInfo(False)


async def test_youtube_always_probes_and_every_id_gets_an_answer(no_probe):  # youtube routed to a quota-costing API, or id missing
    http, calls = client(lambda r: pytest.fail("no HTTP expected"))
    yt = {"id": 9, "platform": "youtube", "channel": "@a", "url": "https://www.youtube.com/@a/live"}
    res = await check_live([yt], dict(TCFG), http)
    assert set(res) == {9} and calls == []


# --- Kick ---

KCFG = {"kick_client_id": "kid", "kick_client_secret": "ksec", "streamlink": "sl"}


def kk(i, ch):
    return {"id": i, "platform": "kick", "channel": ch, "url": f"https://kick.com/{ch}"}


async def test_kick_api_live_offline_and_missing(no_probe):  # is_live read from the wrong place; unknown slug treated as live
    def h(req):
        if req.url.host == "id.kick.com":
            body = req.content.decode()
            assert "grant_type=client_credentials" in body and "client_secret=ksec" in body
            return fx("kick/token.json")
        assert req.url.path == "/public/v1/channels" and req.headers["Authorization"] == "Bearer fake-kick-app-token"
        return fx("kick/channels_live.json")
    http, _ = client(h)
    res = await check_live([kk(1, "classybeef"), kk(2, "sleepy"), kk(3, "ghost")], KCFG, http)
    assert res[1].live and res[1].category == "Grand Theft Auto V" and "Road to Rank" in res[1].title
    assert res[2] == LiveInfo(False)
    assert res[3].live is False and "not found" in res[3].error
    assert no_probe == []


async def test_kick_401_falls_back_to_probe(no_probe):  # Kick auth failure leaves streamers permanently unchecked
    def h(req):
        return fx("kick/token.json") if req.url.host == "id.kick.com" else fx("kick/channels_401.json")
    http, _ = client(h)
    await check_live([kk(1, "classybeef")], KCFG, http)
    assert no_probe == ["https://kick.com/classybeef"]


async def test_kick_token_rejected_falls_back(no_probe):  # bad client secret crashing the monitor instead of falling back
    http, _ = client(lambda r: fx("kick/token_400.json"))
    res = await check_live([kk(1, "classybeef")], KCFG, http)
    assert no_probe == ["https://kick.com/classybeef"] and res[1] == LiveInfo(False)


# --- Twitch device flow and user token ---

async def test_device_flow_start_and_request_shape():  # wrong endpoint/params; secret sent by a public client
    def h(req):
        assert req.url.path == "/oauth2/device"
        body = req.content.decode()
        assert "client_id=cid" in body and "scopes=chat%3Aread" in body and "secret" not in body
        return fx("twitch/device_start.json")
    http, _ = client(h)
    d = await twitch_auth.start_device_flow("cid", http)
    assert d["user_code"] == "ABCDEFGH" and d["interval"] == 5 and d["expires_in"] == 1800


async def test_poll_pending_then_approved():  # pending treated as failure, or approval returning a partial dict
    replies = iter(["twitch/device_pending.json", "twitch/device_token_ok.json"])

    def h(req):
        assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code" in req.content.decode()
        return fx(next(replies))
    http, _ = client(h)
    assert await twitch_auth.poll_device_token("cid", "dc", http) is None
    tok = await twitch_auth.poll_device_token("cid", "dc", http)
    assert tok == {"access_token": "fake-access-token", "refresh_token": "fake-refresh-token", "expires_in": 14124}


async def test_poll_denied_raises():  # denied sign-in polled forever
    http, _ = client(lambda r: fx("twitch/device_denied.json"))
    with pytest.raises(twitch_auth.TwitchAuthError):
        await twitch_auth.poll_device_token("cid", "dc", http)


async def test_refresh_sends_secret_only_when_given():  # secret sent as empty string, or missing for confidential clients
    bodies = []

    def h(req):
        bodies.append(req.content.decode())
        return fx("twitch/device_token_ok.json")
    http, _ = client(h)
    await twitch_auth.refresh_user_token("cid", "sec", "rt", http)
    await twitch_auth.refresh_user_token("cid", "", "rt", http)
    assert "client_secret=sec" in bodies[0] and "grant_type=refresh_token" in bodies[0]
    assert "client_secret" not in bodies[1]


async def test_refresh_invalid_raises():  # reused single-use refresh token swallowed
    http, _ = client(lambda r: fx("twitch/refresh_invalid.json"))
    with pytest.raises(twitch_auth.TwitchAuthError):
        await twitch_auth.refresh_user_token("cid", "sec", "old", http)


async def test_validate_token_login_and_invalid():  # wrong auth scheme (Bearer vs OAuth); 401 raising instead of None
    def h(req):
        assert req.headers["Authorization"] == "OAuth good"
        return fx("twitch/validate_ok.json") if "good" in req.headers["Authorization"] else fx("twitch/validate_401.json")
    http, _ = client(h)
    assert await twitch_auth.validate_token("good", http) == "twitchdev"
    http2, _ = client(lambda r: fx("twitch/validate_401.json"))
    assert await twitch_auth.validate_token("bad", http2) is None
