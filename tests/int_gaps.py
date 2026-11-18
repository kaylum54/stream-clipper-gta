"""Gaps the coverage run showed (pyramid pass, 18 Nov 2026): paths no earlier test executed."""
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from clipper import config, db
from clipper.app import create_app
from clipper.pipeline import Pipeline
from clipper.platforms import LiveInfo
from tests.media_helpers import make_ts

H = {"X-Clipper": "1"}


async def offline(streamers, cfg, http):
    return {s["id"]: LiveInfo(live=False) for s in streamers}


def twitch_transport():
    """Fake id.twitch.tv: device start, one 'pending' poll, then a token; validate returns a login."""
    polls = {"n": 0}

    def handler(req: httpx.Request):
        url = str(req.url)
        if url.endswith("/oauth2/device"):
            return httpx.Response(200, json={"device_code": "dc", "user_code": "ABCD-EFGH", "verification_uri": "https://www.twitch.tv/activate",
                                             "interval": 5, "expires_in": 1800})
        if url.endswith("/oauth2/token"):
            polls["n"] += 1
            if polls["n"] == 1:
                return httpx.Response(400, json={"status": 400, "message": "authorization_pending"})
            return httpx.Response(200, json={"access_token": "user-tok", "refresh_token": "ref-tok", "expires_in": 14000})
        if url.endswith("/oauth2/validate"):
            return httpx.Response(200, json={"login": "kaytest", "client_id": "cid"})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def make_client(http=None):
    app = create_app(lambda: Pipeline(check_live=offline, use_whisper=False, http=http), start_loops=False)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_twitch_device_login_saves_tokens_and_hides_them(data_dir):
    # bug caught: the "Connect Twitch chat" flow never saving the token, or the token leaking to the page
    with make_client(httpx.AsyncClient(transport=twitch_transport())) as c:
        assert c.post("/api/twitch/device/start", headers=H).status_code == 400   # no client id yet
        config.save({"twitch_client_id": "cid"})
        assert c.post("/api/twitch/device/poll", headers=H).status_code == 400    # poll before start
        r = c.post("/api/twitch/device/start", headers=H).json()
        assert r["user_code"] == "ABCD-EFGH" and "device_code" not in r
        assert c.post("/api/twitch/device/poll", headers=H).json()["status"] == "pending"
        assert c.post("/api/twitch/device/poll", headers=H).json() == {"status": "connected", "login": "kaytest"}
        cfg = config.load()
        assert cfg["twitch_user_token"] == "user-tok" and cfg["twitch_refresh_token"] == "ref-tok"
        assert "user-tok" not in c.get("/api/settings").text


def test_clip_video_supports_range_requests(data_dir):
    # bug caught: the review player unable to seek (browsers need 206 partial content for <video>)
    clips = data_dir / "clips"
    clips.mkdir()
    (clips / "x.mp4").write_bytes(b"\x00" * 10_000)
    with make_client() as c:
        r = c.get("/media/clips/x.mp4", headers={"Range": "bytes=100-199"})
        assert r.status_code == 206 and len(r.content) == 100
        assert c.get("/media/clips/x.txt").status_code == 404


def test_system_check_reports_tools_and_disk(data_dir):
    # bug caught: the setup checklist saying "ready" on a machine without ffmpeg
    with make_client() as c:
        ok = c.get("/api/system").json()
        assert ok["ffmpeg"] is True and ok["disk_free_gb"] > 0
        config.save({"ffmpeg": str(data_dir / "nope" / "ffmpeg.exe")})
        bad = c.get("/api/system").json()
        assert bad["ffmpeg"] is False and bad["ffprobe"] is False


def test_test_buttons_fail_cleanly_without_keys(data_dir):
    # bug caught: Settings test buttons throwing 500s instead of telling a new user what's missing
    with make_client() as c:
        assert c.post("/api/test/discord", headers=H).json()["ok"] is False
        assert c.post("/api/test/twitch", headers=H).json() == {"ok": False, "detail": "Connect Twitch first"}
        assert c.post("/api/test/nonsense", headers=H).status_code == 404
        assert c.put("/api/settings", json={"poll_seconds": 1}, headers=H).status_code == 400


async def test_cleanup_deletes_only_old_analysed_chunks(data_dir):
    # bug caught: retention deleting a chunk that hasn't been analysed yet (its clips would be lost)
    p = Pipeline(check_live=offline, use_whisper=False)
    config.save({"keep_chunks_hours": 1})
    c = p.conn
    sid = c.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('kick','a','u',0)").lastrowid
    se = c.execute("INSERT INTO sessions(streamer_id, started_at, dir, status) VALUES (?,?,?, 'ended')", (sid, 0, str(data_dir))).lastrowid
    old = time.time() - 7200
    files = {}
    for name, start, analysed in [("old_done", old, 1), ("old_pending", old + 600, 0), ("new_done", time.time() - 60, 1)]:
        f = data_dir / f"{name}.ts"
        f.write_bytes(b"x")
        files[name] = f
        c.execute("INSERT INTO chunks(session_id, path, start_ts, analyzed) VALUES (?,?,?,?)", (se, str(f), start, analysed))
    await p.cleanup_once()
    assert not files["old_done"].exists() and files["old_pending"].exists() and files["new_done"].exists()
    assert c.execute("SELECT deleted FROM chunks WHERE path=?", (str(files["old_done"]),)).fetchone()[0] == 1
    await p.stop()


async def test_a_crashing_loop_keeps_running_and_logs(data_dir):
    # bug caught: one exception (a bad API reply, a locked file) silently killing the monitor for good
    p = Pipeline(check_live=offline, use_whisper=False)
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise RuntimeError("bad pass")

    t = asyncio.create_task(p._forever(0.01, boom, "test loop"))
    await asyncio.sleep(0.1)
    t.cancel()
    assert calls["n"] >= 3
    msgs = [r["message"] for r in db.rows(p.conn.execute("SELECT message FROM events WHERE level='error'"))]
    assert any("test loop: RuntimeError: bad pass" in m for m in msgs)
    await p.stop()


async def test_two_offline_checks_stop_a_recording(data_dir):
    # bug caught: a recorder left running for hours after the streamer ended (one flaky check must not stop it)
    live = {"on": True}

    async def check(streamers, cfg, http):
        return {s["id"]: LiveInfo(live=live["on"]) for s in streamers}

    class FakeRec:
        def __init__(self, *a, **k):
            self.stopped = False
        def start(self): pass
        def poll(self): return None
        def stop(self, timeout=15): self.stopped = True
        @property
        def running(self): return not self.stopped

    async def no_chat(*a, **k):
        return "stopped"

    p = Pipeline(check_live=check, recorder_cls=FakeRec, collect_chat=no_chat, use_whisper=False)
    p.conn.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch','a','https://www.twitch.tv/a',0)")
    await p.monitor_once()
    assert p.conn.execute("SELECT status FROM sessions").fetchone()[0] == "recording"
    live["on"] = False
    await p.monitor_once()
    assert p.conn.execute("SELECT status FROM sessions").fetchone()[0] == "recording"   # one offline check: keep going
    await p.monitor_once()
    assert p.conn.execute("SELECT status FROM sessions").fetchone()[0] == "ended"
    await p.stop()


@pytest.mark.slow
async def test_clip_last_minute_cuts_from_the_growing_chunk(data_dir):
    # bug caught: "Clip last minute" failing on the chunk that is still being recorded
    async def no_chat(*a, **k):
        return "stopped"
    p = Pipeline(check_live=offline, collect_chat=no_chat, use_whisper=False)
    config.save({"preroll": 10, "postroll": 5, "vertical": False})
    c = p.conn
    sid = c.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('kick','hand','https://kick.com/hand',0)").lastrowid
    rec = data_dir / "rec" / "hand"
    rec.mkdir(parents=True)
    start = time.time() - 30
    make_ts(rec / datetime.fromtimestamp(start).strftime("chunk_%Y%m%d-%H%M%S.ts"), 30)
    c.execute("INSERT INTO sessions(streamer_id, started_at, dir, status) VALUES (?,?,?, 'recording')", (sid, start, str(rec)))
    cid = await p.clip_now(sid, lag=2)
    assert cid, db.rows(c.execute("SELECT message FROM events"))
    clip = c.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    assert json.loads(clip["signals"])["manual"] == 1 and Path(clip["path"]).exists()
    from clipper.cutter import probe_duration
    assert abs(probe_duration(clip["path"]) - 15) <= 1.5
    await p.stop()


def test_media_route_refuses_images_outside_clips(data_dir):
    # bug caught (mutation sample): dropping the folder check lets ..%2F reach any .jpg/.mp4 on the disk
    (data_dir / "clips").mkdir()
    (data_dir / "secret.jpg").write_bytes(b"x")
    with make_client() as c:
        assert c.get("/media/clips/..%2Fsecret.jpg").status_code == 404
        assert c.get("/media/clips/%2E%2E%2Fsecret.jpg").status_code == 404
        assert c.get("/media/clips/..%5Csecret.jpg").status_code == 404   # Windows: a backslash is a separator


def test_posted_or_posting_clips_cannot_be_rejected(data_dir):
    # bug caught (mutation sample): Reject on a clip already posting would hide it while the upload goes out
    with make_client() as c:
        conn = db.connect(data_dir / "clipper.db")
        for status in ("posting", "posted"):
            cid = conn.execute("INSERT INTO clips(streamer_name, platform, peak_ts, start_ts, end_ts, score, path, status, created_at) "
                               "VALUES ('a','kick',1,0,2,1,'x.mp4',?,0)", (status,)).lastrowid
            assert c.post(f"/api/clips/{cid}/reject", headers=H).status_code == 409
            assert conn.execute("SELECT status FROM clips WHERE id=?", (cid,)).fetchone()[0] == status
        conn.close()
