"""Fixer-1 tests: rehearsal findings R1-R5 plus the posting decisions B1/S1/B2. Each names the bug it catches."""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from clipper import cutter, recorder
from clipper.pipeline import Pipeline
from clipper.platforms import LiveInfo
from tests.review_posting import ACCS, base_routes, close, make_pipeline, seed_clip, settle


class FakeRec:
    def __init__(self, *a, **kw):
        self.out_dir, self.stopped = Path(a[1]), False

    def start(self):
        pass

    def poll(self):
        return 0 if self.stopped else None

    def stop(self, timeout=15):
        self.stopped = True


def live_pipeline(live):
    async def check(streamers, cfg, http):
        return {s["id"]: live["info"] for s in streamers}

    async def no_chat(*a, **kw):
        return "stopped"

    p = Pipeline(http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), recorder_cls=FakeRec,
                 check_live=check, collect_chat=no_chat, use_whisper=False)
    p.conn.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch','a','https://www.twitch.tv/a',0)")
    return p


def count(p, q="SELECT COUNT(*) FROM sessions"):
    return p.conn.execute(q).fetchone()[0]


async def test_r1_overlapping_monitor_passes_start_one_recorder(data_dir):
    # bug: adding three streamers at once ran three monitor passes together, starting two recorders and logging "went live" twice
    p = live_pipeline({"info": LiveInfo(live=True, title="t")})
    try:
        await asyncio.gather(p.monitor_once(), p.monitor_once())
        assert count(p) == 1
        assert count(p, "SELECT COUNT(*) FROM events WHERE kind='live'") == 1
    finally:
        await close(p)


async def test_r2_user_stop_is_not_undone_while_the_stream_stays_live(data_dir):
    # bug: the next monitor pass restarted a recording the user had just stopped
    live = {"info": LiveInfo(live=True)}
    p = live_pipeline(live)
    try:
        await p.monitor_once()
        sid = p.conn.execute("SELECT streamer_id FROM sessions").fetchone()[0]
        assert await p.stop_streamer(sid)
        await p.monitor_once()
        assert count(p) == 1, "restarted after the user stopped it"
        live["info"] = LiveInfo(live=False)
        await p.monitor_once()
        live["info"] = LiveInfo(live=True)
        await p.monitor_once()
        assert count(p) == 2, "a new stream after going offline must record again"
    finally:
        await close(p)


async def test_r3_failed_live_check_keeps_previous_live_state(data_dir):
    # bug: a timed-out check wrote live=0, so recovery logged a second "went live" for the same stream
    live = {"info": LiveInfo(live=True, title="keep me")}
    p = live_pipeline(live)
    try:
        await p.monitor_once()
        live["info"] = LiveInfo(live=False, error="timeout")
        await p.monitor_once()
        row = p.conn.execute("SELECT live, title, check_error FROM streamers").fetchone()
        assert (row["live"], row["title"], row["check_error"]) == (1, "keep me", "timeout")
        live["info"] = LiveInfo(live=True, title="keep me")
        await p.monitor_once()
        assert count(p, "SELECT COUNT(*) FROM events WHERE kind='live'") == 1
    finally:
        await close(p)


def test_r4_job_object_takes_a_child_on_windows():
    # bug: closing the console left streamlink and ffmpeg running; the job object kills them with this process
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
    try:
        assert recorder.assign_to_job(child) is (sys.platform == "win32")
    finally:
        child.kill()
        child.wait()


async def test_r5_stalled_recorder_is_ended_and_restarted_next_pass(data_dir):
    # bug: streamlink sending no data kept ffmpeg alive forever, so the monitor saw an active session and never restarted it
    p = live_pipeline({"info": LiveInfo(live=True)})
    try:
        await p.monitor_once()
        d = Path(p.conn.execute("SELECT dir FROM sessions").fetchone()[0])
        chunk = d / "chunk_20261116-120000.ts"
        chunk.write_bytes(b"x")
        old = time.time() - 400
        os.utime(chunk, (old, old))
        await p.monitor_once()
        assert p.conn.execute("SELECT error FROM sessions ORDER BY id").fetchone()[0] == "stalled"
        assert count(p) == 1
        await p.monitor_once()
        assert count(p) == 2
    finally:
        await close(p)


# ---- posting decisions ----
def results_for(*ok):
    return lambda pid: httpx.Response(200, json={"data": [
        {"social_account_id": a["id"], "post_id": pid, "success": o, "error": None if o else {"message": "no"}, "platform_data": {}}
        for a, o in zip(ACCS, ok)]})


@pytest.mark.parametrize("ok,post_status,clip_status", [((True, True), "done", "posted"), ((True, False), "partial", "posted"),
                                                        ((False, False), "failed", "failed")])
async def test_b1_clip_status_follows_how_many_accounts_succeeded(data_dir, ok, post_status, clip_status):
    # bug: any single failed account marked the clip failed, which let Try again repost to accounts that already had it
    p = make_pipeline(lambda req: base_routes(req, [], results_for(*ok)))
    try:
        cid = seed_clip(p, data_dir)
        await p.approve(cid, "c", ["acc_tt", "acc_yt"])
        await settle(p)
        await p.results_once()
        assert p.conn.execute("SELECT status FROM posts").fetchone()[0] == post_status
        assert p.conn.execute("SELECT status FROM clips WHERE id=?", (cid,)).fetchone()[0] == clip_status
    finally:
        await close(p)


async def test_s1_post_without_results_after_24h_fails(data_dir):
    # bug: a post whose platforms never report stayed 'submitted' and the clip 'posting' forever
    p = make_pipeline(lambda req: base_routes(req, [], lambda pid: httpx.Response(200, json={"data": []})))
    try:
        cid = seed_clip(p, data_dir, "posting")
        p.conn.execute("INSERT INTO posts(clip_id, pfm_post_id, accounts, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                       (cid, "sp_1", json.dumps(["acc_tt"]), "submitted", time.time() - 25 * 3600, 0))
        await p.results_once()
        row = p.conn.execute("SELECT status, error FROM posts").fetchone()
        assert (row[0], row[1]) == ("failed", "no result from Post for Me after 24 h")
        assert p.conn.execute("SELECT status FROM clips WHERE id=?", (cid,)).fetchone()[0] == "failed"
    finally:
        await close(p)


async def test_b2_retry_of_an_unknown_post_reuses_the_found_post(data_dir):
    # bug: a post that was created but whose reply was lost got a second post on retry
    found = {"id": "sp_found", "external_id": "clip-1"}

    def handler(req):
        if req.url.path == "/v1/social-posts" and req.method == "GET":
            return httpx.Response(200, json={"data": [found], "meta": {"next": None}})
        return base_routes(req, [], results_for(True, True))

    p = make_pipeline(handler)
    try:
        cid = seed_clip(p, data_dir, "failed")
        await p.approve(cid, "c", ["acc_tt"])
        await settle(p)
        row = p.conn.execute("SELECT status, pfm_post_id FROM posts").fetchone()
        assert (row[0], row[1]) == ("submitted", "sp_found")
    finally:
        await close(p)


def test_s2_ffprobe_path_keeps_the_folder_and_extension():
    # bug: replacing every "ffmpeg" in the path also renamed the ffmpeg folder
    assert cutter.ffprobe_path("C:\\ffmpeg\\bin\\ffmpeg.exe") == "C:\\ffmpeg\\bin\\ffprobe.exe"
    assert cutter.ffprobe_path("ffmpeg") == "ffprobe"
