"""Tests for the stress-fix decisions that review_stress.py does not already cover. Each names its bug."""
import asyncio
import json
import shutil
import sqlite3
import time

from clipper import analysis, config, db
from tests.review_stress import FakeRec, pipe, seed_chunks, streamer


def test_old_database_gets_new_columns_and_keeps_rows(tmp_path):
    # bug guard (F3 migration): an existing data/clipper.db lacks chunks.attempts, so every chunk query would fail after the upgrade
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE streamers (id INTEGER PRIMARY KEY, platform TEXT NOT NULL, channel TEXT NOT NULL, url TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1, live INTEGER NOT NULL DEFAULT 0, title TEXT, category TEXT, video_id TEXT,
          checked_at REAL, check_error TEXT, created_at REAL NOT NULL, UNIQUE(platform, channel));
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, streamer_id INTEGER NOT NULL, started_at REAL NOT NULL, ended_at REAL,
          dir TEXT NOT NULL, title TEXT, status TEXT NOT NULL DEFAULT 'recording', error TEXT);
        CREATE TABLE chunks (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, path TEXT NOT NULL UNIQUE, start_ts REAL NOT NULL,
          duration REAL, analyzed INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0, error TEXT);
        INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch','a','u',0);
        INSERT INTO sessions(streamer_id, started_at, dir) VALUES (1, 0, 'd');
        INSERT INTO chunks(session_id, path, start_ts, analyzed) VALUES (1, 'p.ts', 5, 1);
    """)
    c.close()
    n = db.connect(path)
    assert "attempts" in {r[1] for r in n.execute("PRAGMA table_info(chunks)")}
    row = n.execute("SELECT path, analyzed, attempts FROM chunks").fetchone()
    assert tuple(row) == ("p.ts", 1, 0)
    n.close()


async def test_chunk_is_abandoned_after_three_failed_tries(data_dir):
    # bug guard (F3): a chunk that can never be analysed must stop retrying after 3 tries and log once at error level
    config.save({"preroll": 2, "postroll": 2, "ffmpeg": r"C:\nope\ffmpeg.exe"})
    p = pipe()
    try:
        seed_chunks(p, streamer(p), data_dir / "rec" / "x", 1, secs=3)
        for _ in range(5):
            await p.analyse_once()
        row = p.conn.execute("SELECT analyzed, attempts, error FROM chunks").fetchone()
        assert (row["analyzed"], row["attempts"]) == (1, 3) and row["error"]
        assert p.conn.execute("SELECT COUNT(*) FROM events WHERE level='error' AND message LIKE 'gave up on chunk%'").fetchone()[0] == 1
    finally:
        await p.http.aclose()


async def test_crash_looping_recorder_logs_one_warning_per_ten_minutes(data_dir):
    # bug guard (F5): a recorder that dies every pass must not write an activity line per pass
    FakeRec.crash = False
    p = pipe(recorder_cls=FakeRec)
    try:
        streamer(p)
        await p.monitor_once()
        FakeRec.crash = True
        for _ in range(4):
            await p.monitor_once()
        n = p.conn.execute("SELECT COUNT(*) FROM events WHERE level='warn' AND message LIKE '%recorder keeps stopping%'").fetchone()[0]
        assert n == 1
    finally:
        FakeRec.crash = False
        await p.http.aclose()


async def test_no_new_recording_when_disk_is_nearly_full(data_dir, monkeypatch):
    # bug guard (S-disk): with under 2 GB free a new recording only makes ffmpeg fail every pass
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(100 * 10**9, 99 * 10**9, 10**9))
    p = pipe(recorder_cls=FakeRec)
    try:
        streamer(p)
        await p.monitor_once()
        await p.monitor_once()
        assert p.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert p.conn.execute("SELECT COUNT(*) FROM events WHERE level='warn' AND message LIKE 'disk nearly full%'").fetchone()[0] == 1
    finally:
        await p.http.aclose()


async def test_clip_now_double_click_returns_the_same_clip(data_dir, monkeypatch):
    # bug guard (b10): two "Clip last minute" requests for one streamer must not cut two clips
    p = pipe()
    cuts = []

    async def fake_make_clip(sess, peak, score, signals, cfg, start=None, end=None):
        cuts.append(peak)
        await asyncio.sleep(0.2)
        return p.conn.execute("INSERT INTO clips(session_id, streamer_id, streamer_name, platform, peak_ts, start_ts, end_ts, score, signals, path, created_at) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (sess["id"], sess["streamer_id"], "a", "twitch", peak, 0, 1, 0, json.dumps(signals), "x", time.time())).lastrowid
    monkeypatch.setattr(p, "make_clip", fake_make_clip)
    try:
        sid = streamer(p)
        p.conn.execute("INSERT INTO sessions(streamer_id, started_at, dir) VALUES (?,?,?)", (sid, time.time(), str(data_dir)))
        a, b = await asyncio.gather(p.clip_now(sid), p.clip_now(sid))
        assert a is not None and a == b and len(cuts) == 1
    finally:
        await p.http.aclose()


async def test_cleanup_drops_old_events_but_keeps_recent_ones(data_dir):
    # bug guard (b11): events older than 30 days are deleted, newer ones stay
    p = pipe()
    try:
        db.log(p.conn, "config", "recent")
        p.conn.execute("INSERT INTO events(ts, level, kind, message) VALUES (?,?,?,?)", (time.time() - 31 * 86400, "info", "config", "old"))
        await p.cleanup_once()
        assert [r[0] for r in p.conn.execute("SELECT message FROM events")] == ["recent"]
    finally:
        await p.http.aclose()


def test_decode_failure_with_no_output_raises(tmp_path):
    # bug guard (b8/F3): an empty file must raise too, so the chunk is retried and not silently "analysed"
    f = tmp_path / "empty.ts"
    f.write_bytes(b"")
    try:
        analysis.audio_db_per_second(f)
    except RuntimeError:
        return
    raise AssertionError("no error for an undecodable file")
