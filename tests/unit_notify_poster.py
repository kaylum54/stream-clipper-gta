import json

import httpx
import pytest

from clipper import notify
from clipper.notify import clip_message, discord_send, live_message
from clipper.poster import PostForMe, PostForMeError, build_post_body, summarize_results

HOOK = "https://discord.com/api/webhooks/123/abc"


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_signed_put_has_no_auth_header(tmp_path):
    # catches: leaking the Post for Me key to the signed storage host
    f = tmp_path / "c.mp4"; f.write_bytes(b"x" * 50)
    seen = {}
    def h(req):
        seen[req.method] = req
        if req.method == "POST":
            return httpx.Response(200, json={"upload_url": "https://storage.test/put?sig=1", "media_url": "https://cdn.test/a.mp4"})
        return httpx.Response(200)
    async with client(h) as c:
        assert await PostForMe("k", c).upload(f) == "https://cdn.test/a.mp4"
    assert "authorization" not in seen["PUT"].headers
    assert seen["PUT"].headers["content-type"] == "video/mp4"
    assert seen["POST"].headers["authorization"] == "Bearer k"


@pytest.mark.parametrize("status", [401, 500])
async def test_error_status_raises_with_status_and_truncated_body(status):
    # catches: HTTP errors swallowed, or a huge error body stored whole
    async with client(lambda r: httpx.Response(status, text="e" * 2000)) as c:
        with pytest.raises(PostForMeError) as ei:
            await PostForMe("k", c).list_accounts()
    assert ei.value.status == status and len(ei.value.body) == 500


async def test_list_accounts_strips_tokens_and_follows_next():
    # catches: tokens reaching the GUI; only the first page of accounts shown
    def h(req):
        if req.url.params.get("offset") == "1":
            return httpx.Response(200, json={"data": [{"id": "b", "platform": "youtube", "access_token": "S"}], "meta": {"next": None}})
        return httpx.Response(200, json={"data": [{"id": "a", "platform": "tiktok", "access_token": "S", "refresh_token": "R"}],
                                         "meta": {"next": "https://api.postforme.dev/v1/social-accounts?offset=1&limit=100"}})
    async with client(h) as c:
        accs = await PostForMe("k", c).list_accounts()
    assert [a["id"] for a in accs] == ["a", "b"]
    assert "S" not in json.dumps(accs) and "R" not in json.dumps(accs)


def test_summarize_results_pending_failed_success():
    # catches: a pending account shown as failed, or a failure with no reason
    accs = {i: {"platform": p, "username": u} for i, p, u in [("1", "tiktok", "t"), ("2", "youtube", "y"), ("3", "instagram", "i")]}
    res = [{"social_account_id": "1", "success": True, "error": {}, "platform_data": {"url": "https://t/1"}},
           {"social_account_id": "2", "success": False, "error": {"message": "quota"}, "platform_data": {}}]
    out = {r["account_id"]: r for r in summarize_results(res, accs)}
    assert out["1"]["success"] is True and out["1"]["url"] == "https://t/1" and out["1"]["error"] is None
    assert out["2"]["success"] is False and out["2"]["error"] == "quota"
    # an account with no result yet is pending (None); leaving it out let an empty first poll count as "all posted"
    assert out["3"]["success"] is None and out["3"]["platform"] == "instagram"


def test_build_post_body_validation_and_truncation():
    # catches: posting with no accounts, an http media url, or a caption TikTok rejects
    for bad in [("c", [], "https://x/a.mp4"), ("c", ["a"], "http://x/a.mp4"), (5, ["a"], "https://x/a.mp4")]:
        with pytest.raises(ValueError):
            build_post_body(*bad)
    assert len(build_post_body("x" * 3000, ["a"], "https://x/a.mp4")["caption"]) == 2200


async def test_discord_429_retries_once_then_gives_up(monkeypatch):
    # catches: infinite retry loop or no retry on a rate limit
    sleeps, calls = [], []
    async def fake_sleep(s): sleeps.append(s)
    monkeypatch.setattr(notify.asyncio, "sleep", fake_sleep)
    def h(req):
        calls.append(1)
        return httpx.Response(429, json={"retry_after": 99})
    async with client(h) as c:
        ok, detail = await discord_send(c, HOOK, "hi")
    assert not ok and len(calls) == 2 and sleeps == [10] and "429" in detail


async def test_discord_429_then_success(monkeypatch):
    # catches: retry that does not actually resend
    async def fake_sleep(s): pass
    monkeypatch.setattr(notify.asyncio, "sleep", fake_sleep)
    rs = iter([httpx.Response(429, json={"retry_after": 0.1}), httpx.Response(200, json={"id": "1"})])
    async with client(lambda r: next(rs)) as c:
        assert (await discord_send(c, HOOK, "hi"))[0] is True


@pytest.mark.parametrize("url,detail", [
    ("", "no webhook"), ("http://discord.com/api/webhooks/1/a", "bad webhook url"),
    ("https://evil.example/api/webhooks/1/a", "bad webhook url"), ("https://discord.com.evil.example/api/webhooks/1/a", "bad webhook url"),
    ("https://discord.com/other/1/a", "bad webhook url"), ("http://127.0.0.1:99999/x", "bad webhook url"),
])
async def test_bad_webhooks_refused_without_a_request(url, detail):
    # catches: sending the message (or token) to an attacker-chosen host
    async with client(lambda r: pytest.fail("request made")) as c:
        assert await discord_send(c, url, "hi") == (False, detail)


async def test_big_or_missing_file_sent_without_it(tmp_path):
    # catches: a 413 from Discord dropping the whole notification
    big = tmp_path / "b.mp4"; big.write_bytes(b"x" * 2_000_000)
    bodies = []
    def h(req):
        bodies.append(req.headers["content-type"]); return httpx.Response(200, json={})
    async with client(h) as c:
        ok1, d1 = await discord_send(c, HOOK, "hi", str(big), max_file_mb=1)
        ok2, d2 = await discord_send(c, HOOK, "hi", str(tmp_path / "nope.mp4"))
    assert ok1 and "without it" in d1 and ok2 and "missing" in d2
    assert all(b.startswith("application/json") for b in bodies)


async def test_mentions_suppressed_and_content_truncated():
    # catches: a streamer title pinging @everyone; content over 2000 chars rejected
    got = {}
    def h(req):
        got["p"] = json.loads(req.content); got["q"] = req.url.query; return httpx.Response(200, json={})
    async with client(h) as c:
        await discord_send(c, HOOK, "@everyone " + "x" * 3000)
    assert got["p"]["allowed_mentions"] == {"parse": []} and len(got["p"]["content"]) == 2000
    assert b"wait=true" in got["q"]


async def test_discord_network_error_does_not_raise():
    # catches: a dead network crashing the pipeline task
    def h(req): raise httpx.ConnectError("down")
    async with client(h) as c:
        ok, d = await discord_send(c, HOOK, "hi")
    assert not ok and "ConnectError" in d


def test_messages_escape_markdown_and_have_no_pings():
    # catches: a title like **bold** or a link breaking the message layout
    m = clip_message("a_b", "twitch", 4.26, "he said *wow* [x](y)", "http://127.0.0.1:8765/clip/1")
    assert "a\\_b" in m and "\\*wow\\*" in m and "\\[x\\]" in m and "4.3" in m
    assert "**" not in live_message("s", "kick", "**GIVEAWAY**", "https://kick.com/s") or "\\*\\*" in live_message("s", "kick", "**GIVEAWAY**", "https://kick.com/s")
