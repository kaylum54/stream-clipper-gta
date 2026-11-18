"""Stress review: one small failing test per bug proven by stress/ scenarios (see stress/STRESS-REPORT.md). Each names its bug."""
import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from clipper import analysis, config, db, recorder
from clipper.pipeline import Pipeline
from clipper.platforms import LiveInfo
from tests.media_helpers import make_ts


class FakeRec:
    crash = False

    def __init__(self, *a, **kw):
        self.out_dir = Path(a[1])

    def start(self):
        pass

    def poll(self):
        return 1 if FakeRec.crash else None

    def stop(self, timeout=15):
        pass


async def _live(streamers, cfg, http):
    return {s["id"]: LiveInfo(live=True, title="t") for s in streamers}


async def _no_chat(platform, channel, info, cfg, on_message, stop, http, **kw):
    await stop.wait()
    return "stopped"


def pipe(handler=None, **kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler or (lambda r: httpx.Response(200, json={}))))
    return Pipeline(http=http, check_live=_live, collect_chat=_no_chat, use_whisper=False, **kw)


def streamer(p, name="a"):
    return p.conn.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch',?,?,0)", (name, f"https://www.twitch.tv/{name}")).lastrowid


def seed_chunks(p, sid_streamer, d, n, secs=3, status="ended", register=True):
    """A session whose dir holds n real chunk files named like the recorder's (10 s apart)."""
    d.mkdir(parents=True, exist_ok=True)
    t0 = int(time.time()) - 300
    sid = p.conn.execute("INSERT INTO sessions(streamer_id, started_at, dir, status, ended_at) VALUES (?,?,?,?,?)",
                         (sid_streamer, t0, str(d), status, t0 + 100)).lastrowid
    for i in range(n):
        make_ts(d / f"chunk_{time.strftime(recorder._FMT, time.localtime(t0 + i * 10))}.ts", secs, [(1, 1)] if i == 0 else [])
    if register:
        p._register_chunks(db.rows(p.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)))[0])
    return sid, t0


def test_b1_gui_requests_at_the_same_time_corrupt_the_shared_sqlite_connection(data_dir):
    # bug: one sqlite connection is shared by every API worker thread, so overlapping requests get 500s (InterfaceError) or another request's rows
    from fastapi.testclient import TestClient

    from clipper.app import create_app
    c = db.connect(data_dir / "clipper.db")
    now = time.time()
    c.execute("BEGIN")
    sig = json.dumps({"curve": {"audio_db": [0.0] * 60, "chat": [0] * 60, "hype": [0] * 60}})
    for i in range(10):
        c.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch',?,'u',0)", (f"s{i}",))
    for i in range(300):
        c.execute("INSERT INTO clips(streamer_id, streamer_name, platform, peak_ts, start_ts, end_ts, score, signals, path, created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)", (i % 10 + 1, "s", "twitch", 1, 0, 2, 5.0, sig, str(data_dir / "x.mp4"), now))
    c.execute("COMMIT")
    c.close()
    with TestClient(create_app(start_loops=False), base_url="http://127.0.0.1") as client:
        def hit(_):
            return [client.get("/api/clips?status=pending").status_code for _ in range(5)]
        with ThreadPoolExecutor(12) as ex:
            codes = [x for r in ex.map(hit, range(12)) for x in r]
    assert set(codes) == {200}, {k: codes.count(k) for k in set(codes)}


def test_b2_state_counts_scan_whole_tables_because_hot_columns_have_no_index(conn):
    # bug: /api/state counts clips and chunks per streamer with no index on clips.streamer_id or chunks.session_id (0.5 s per poll at 2,000 clips)
    for q in ("SELECT COUNT(*) FROM clips WHERE streamer_id=1", "SELECT MAX(created_at) FROM clips WHERE streamer_id=1",
              "SELECT COUNT(*) FROM chunks WHERE session_id=1"):
        plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + q))
        assert "USING" in plan and "INDEX" in plan, (q, plan)


async def test_b3_one_transient_ffmpeg_failure_loses_the_chunk_for_good(data_dir):
    # bug: _analyse_chunk marks analyzed=1 on any exception, so a missing/locked ffmpeg means that chunk is never looked at again
    config.save({"preroll": 2, "postroll": 2, "vertical": False, "min_gap_seconds": 10, "score_threshold": 2.0})
    p = pipe()
    try:
        sid = streamer(p)
        seed_chunks(p, sid, data_dir / "rec" / "x", 3, secs=6)
        config.save({"ffmpeg": r"C:\nope\ffmpeg.exe"})
        await p.analyse_once()
        config.save({"ffmpeg": "ffmpeg"})                   # the user fixes the path
        await p.analyse_once()
        assert p.conn.execute("SELECT COUNT(*) FROM chunks WHERE analyzed=1 AND error IS NOT NULL").fetchone()[0] == 0
    finally:
        await p.http.aclose()


async def test_b4_chunks_never_registered_before_a_restart_are_never_analysed_or_cleaned(data_dir):
    # bug: analyse_once only registers files for sessions that are recording or already have unanalysed chunk rows
    p = pipe()
    try:
        sid = streamer(p)
        seed_chunks(p, sid, data_dir / "rec" / "x", 2, status="ended", register=False)   # what _recover leaves after a crash before the first pass
        await p.analyse_once()
        assert p.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 2
    finally:
        await p.http.aclose()


async def test_b5_every_recorder_restart_sends_another_live_message_to_discord(data_dir):
    # bug: _start_session always posts "is live", so a recorder that keeps crashing spams the channel once per monitor pass
    hits = []

    def handler(r):
        hits.append(r.url.path)
        return httpx.Response(200, json={})
    config.save({"discord_webhook_url": "http://127.0.0.1:9/api/webhooks/1/x"})
    FakeRec.crash = False
    p = pipe(handler, recorder_cls=FakeRec)
    try:
        streamer(p)
        await p.monitor_once()
        FakeRec.crash = True                               # every recorder dies before the next pass
        for _ in range(3):
            await p.monitor_once()
        await asyncio.sleep(0.2)                           # F6: the Discord post is a background task now
        assert len([h for h in hits if h.startswith("/api/webhooks/")]) == 1, hits
    finally:
        FakeRec.crash = False
        await p.http.aclose()


async def test_b6_a_silent_discord_webhook_blocks_the_whole_monitor_pass(data_dir):
    # bug: _start_session awaits the Discord post inside monitor_lock, so each new stream costs one httpx timeout (60 s) before the next check can run
    async def slow(r):
        await asyncio.sleep(2)
        return httpx.Response(200, json={})
    config.save({"discord_webhook_url": "http://127.0.0.1:9/api/webhooks/1/x"})
    FakeRec.crash = False
    p = pipe(slow, recorder_cls=FakeRec)
    try:
        streamer(p)
        await asyncio.wait_for(p.monitor_once(), 0.8)
    finally:
        await p.http.aclose()


async def test_b7_second_post_for_me_outage_is_not_logged_at_all(data_dir):
    # bug: poll_errors remembers an error kind for the life of the process, so only the first outage ever shows in Activity
    config.save({"postforme_api_key": "k"})
    state = {"fail": True}

    def handler(r):
        if r.url.path == "/v1/social-accounts":
            return httpx.Response(200, json={"data": [], "meta": {"next": None}})
        if r.url.path == "/v1/social-post-results":
            return httpx.Response(500, json={}) if state["fail"] else httpx.Response(200, json={"data": []})
        return httpx.Response(404)
    p = pipe(handler)
    try:
        sid = streamer(p)
        cid = p.conn.execute("INSERT INTO clips(streamer_id, streamer_name, platform, peak_ts, start_ts, end_ts, score, path, created_at, status) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?)", (sid, "a", "twitch", 1, 0, 2, 5.0, "x", time.time(), "posting")).lastrowid
        p.conn.execute("INSERT INTO posts(clip_id, pfm_post_id, accounts, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                       (cid, "sp1", '["a"]', "submitted", time.time(), time.time()))
        await p.results_once()
        state["fail"] = False
        await p.results_once()
        n1 = p.conn.execute("SELECT COUNT(*) FROM events WHERE kind='post' AND level='warn'").fetchone()[0]
        state["fail"] = True
        await p.results_once()
        assert p.conn.execute("SELECT COUNT(*) FROM events WHERE kind='post' AND level='warn'").fetchone()[0] == n1 + 1
    finally:
        await p.http.aclose()


def test_b8_audio_decode_failure_looks_like_a_silent_chunk(tmp_path):
    # bug: audio_db_per_second ignores ffmpeg's exit code, so an undecodable chunk is "analysed" with no moments and no error
    bad = tmp_path / "chunk.ts"
    bad.write_bytes(os.urandom(20000))
    with pytest.raises(RuntimeError):
        analysis.audio_db_per_second(bad)


async def test_b9_one_locked_old_chunk_stops_cleanup_of_every_chunk_after_it(data_dir):
    # bug: cleanup_once unlinks without catching PermissionError, so a chunk held open by another program blocks all later deletions
    p = pipe()
    try:
        sid = streamer(p)
        d = data_dir / "rec" / "x"
        d.mkdir(parents=True)
        sess = p.conn.execute("INSERT INTO sessions(streamer_id, started_at, dir, status) VALUES (?,?,?,'ended')", (sid, 0, str(d))).lastrowid
        files = []
        for i in range(2):
            f = d / f"chunk_{i}.ts"
            f.write_bytes(b"x")
            files.append(f)
            p.conn.execute("INSERT INTO chunks(session_id, path, start_ts, analyzed) VALUES (?,?,?,1)", (sess, str(f), 1000 + i))
        with open(files[0], "rb"):
            try:
                await p.cleanup_once()
            except PermissionError:
                pass
        assert not files[1].exists()
    finally:
        await p.http.aclose()


async def test_b10_double_click_on_clip_now_cuts_two_clips_of_one_moment(data_dir):
    # bug: make_clip's duplicate check runs before the cut and the insert, so two overlapping calls for one peak both pass it
    config.save({"preroll": 2, "postroll": 2, "vertical": False})
    p = pipe()
    try:
        sid = streamer(p)
        s, t0 = seed_chunks(p, sid, data_dir / "rec" / "x", 1, secs=6)
        sess = db.rows(p.conn.execute("SELECT * FROM sessions"))[0]
        cfg = config.load()
        await asyncio.gather(p.make_clip(sess, t0 + 3.0, 5.0, {}, cfg), p.make_clip(sess, t0 + 3.2, 5.0, {}, cfg))
        assert p.conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 1
    finally:
        await p.http.aclose()


async def test_b11_chat_events_are_never_deleted(data_dir):
    # bug: cleanup_once removes old chunk files but chat_events (and events) grow forever
    p = pipe()
    try:
        sid = streamer(p)
        sess = p.conn.execute("INSERT INTO sessions(streamer_id, started_at, dir, status) VALUES (?,?,?,'ended')", (sid, 0, str(data_dir))).lastrowid
        p.conn.execute("INSERT INTO chat_events(session_id, ts, hype) VALUES (?,?,0)", (sess, time.time() - 30 * 3600))
        await p.cleanup_once()
        assert p.conn.execute("SELECT COUNT(*) FROM chat_events").fetchone()[0] == 0
    finally:
        await p.http.aclose()


async def test_b12_analysis_never_uses_the_second_media_slot(data_dir, monkeypatch):
    # bug: analyse_once awaits chunks one by one, so media_lock = Semaphore(2) is never used and capacity is half of what the design says
    conc = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def slow_find(*a, **kw):
        with lock:
            conc["cur"] += 1
            conc["max"] = max(conc["max"], conc["cur"])
        time.sleep(0.3)
        with lock:
            conc["cur"] -= 1
        return []
    monkeypatch.setattr(analysis, "find_moments", slow_find)
    p = pipe()
    try:
        for i in range(2):
            sid = streamer(p, f"s{i}")
            d = data_dir / "rec" / f"x{i}"
            d.mkdir(parents=True)
            sess = p.conn.execute("INSERT INTO sessions(streamer_id, started_at, dir, status) VALUES (?,?,?,'ended')", (sid, 0, str(d))).lastrowid
            p.conn.execute("INSERT INTO chunks(session_id, path, start_ts, duration) VALUES (?,?,?,10)", (sess, str(d / "c.ts"), 100))
        await p.analyse_once()
        assert conc["max"] == 2
    finally:
        await p.http.aclose()
