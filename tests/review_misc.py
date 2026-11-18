"""REVIEW-1: Twitch tokens, Discord, settings and GUI shortcuts. Each test fails today and names the bug it proves."""
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from clipper import config, notify
from clipper.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent


async def test_second_stream_refreshes_with_an_already_rotated_refresh_token(data_dir):
    # bug: _fresh_twitch_token works on the per-stream cfg copy, so a second live Twitch stream refreshes with the consumed token and its chat stays dead
    tw = {"access": "none-valid-yet", "refresh": "R1", "calls": 0}

    def h(req):
        if req.url.path == "/oauth2/validate":
            ok = req.headers["Authorization"].split()[1] == tw["access"]
            return httpx.Response(200, json={"login": "me"}) if ok else httpx.Response(401, json={"message": "invalid access token"})
        if req.url.path == "/oauth2/token":
            form = parse_qs(req.content.decode())
            tw["calls"] += 1
            if form["refresh_token"][0] != tw["refresh"]:
                return httpx.Response(400, json={"message": "Invalid refresh token"})
            tw["access"], tw["refresh"] = f"A{tw['calls']}", f"R{tw['calls'] + 1}"  # Twitch rotates the refresh token
            return httpx.Response(200, json={"access_token": tw["access"], "refresh_token": tw["refresh"], "expires_in": 14000})
        return httpx.Response(404)

    config.save({"twitch_client_id": "cid", "twitch_user_token": "A0-expired", "twitch_refresh_token": "R1"})
    stale = config.load()  # each _run_chat holds the cfg it was started with
    p = Pipeline(http=httpx.AsyncClient(transport=httpx.MockTransport(h)), use_whisper=False)
    try:
        await p._fresh_twitch_token(dict(stale))
        second = await p._fresh_twitch_token(dict(stale))
    finally:
        await p.http.aclose(); p.conn.close()
    assert second["twitch_user_token"] == tw["access"], f"second stream got {second['twitch_user_token']}, live token is {tw['access']}"


async def test_discord_413_on_the_file_drops_the_clip_message(tmp_path):
    # bug: a 413 (file over the server's real limit) returns failure with no retry without the file, so the clip alert never arrives
    f = tmp_path / "c.mp4"
    f.write_bytes(b"x" * 1000)

    def h(req):
        multipart = "multipart" in req.headers.get("content-type", "")
        return httpx.Response(413 if multipart else 200, json={"message": "Request entity too large"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        ok, detail = await notify.discord_send(c, "https://discord.com/api/webhooks/1/tok", "New clip", str(f))
    assert ok, detail


@pytest.mark.parametrize("key,value", [("poll_seconds", 0), ("chunk_seconds", 5), ("preroll", -40), ("keep_chunks_hours", 0)])
def test_settings_api_accepts_values_that_break_the_pipeline(data_dir, key, value):
    # bug: config.save checks types only; poll_seconds 0 hot-loops streamlink probes, chunk_seconds < 10 collides file names (recorder.py:54)
    with pytest.raises(ValueError):
        config.save({key: value})


def test_review_shortcuts_fire_on_key_autorepeat():
    # bug: the keydown handler ignores e.repeat; holding A approves the next clips (unwatched) with the default accounts, holding R rejects a run of clips
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    start = src.index('document.addEventListener("keydown"')
    handler = src[start:src.index("});", start)]
    assert "repeat" in handler
