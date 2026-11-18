"""REVIEW-1: recording, timeline, clip files and disk. Each test fails today and names the bug it proves."""
import subprocess
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from clipper import analysis, cutter
from clipper.app import create_app
from clipper.pipeline import Pipeline
from clipper.recorder import Recorder


def offline_pipeline(**kw):
    return Pipeline(http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), **kw)


async def test_ffprobe_path_breaks_when_ffmpeg_lives_in_a_folder_named_ffmpeg(data_dir, monkeypatch):
    # bug: cfg["ffmpeg"].replace("ffmpeg", "ffprobe") rewrites the folder too, so C:/ffmpeg/bin/ffmpeg.exe probes C:/ffprobe/bin/ffprobe.exe
    seen = []
    monkeypatch.setattr(cutter, "probe_duration", lambda path, ffprobe="ffprobe": seen.append(ffprobe) or 10.0)
    p = offline_pipeline(use_whisper=False)
    try:
        p._chunk_table([{"path": "x.ts", "start_ts": 0.0, "duration": None}], {"ffmpeg": "C:/ffmpeg/bin/ffmpeg.exe"})
    finally:
        await p.http.aclose(); p.conn.close()
    assert seen == ["C:/ffmpeg/bin/ffprobe.exe"]


def test_streamlink_left_running_when_ffmpeg_fails_to_start(tmp_path):
    # bug: Recorder.start raises after the source Popen; nothing kills it, and the monitor spawns another every poll
    rec = Recorder("https://www.twitch.tv/x", tmp_path, 600, "best", "streamlink", ffmpeg=str(tmp_path / "no-such-ffmpeg.exe"),
                   source_cmd=[sys.executable, "-c", "import time; time.sleep(30)"])
    with pytest.raises(OSError):
        rec.start()
    try:
        time.sleep(0.5)
        assert rec._src.poll() is not None, "source process still running with nobody to stop it"
    finally:
        if rec._src is not None and rec._src.poll() is None:
            rec._src.kill()


def _fake_media(monkeypatch):
    monkeypatch.setattr(cutter, "cut_clip", lambda table, s, e, out, vertical=True, ffmpeg="ffmpeg": (Path(out).write_bytes(b"v"), str(out))[1])
    monkeypatch.setattr(cutter, "probe_duration", lambda path, ffprobe="ffprobe": 60.0)
    monkeypatch.setattr(cutter, "make_thumb", lambda clip, out, at, ffmpeg="ffmpeg": Path(out).write_bytes(b"j"))
    monkeypatch.setattr(analysis, "audio_db_per_second", lambda path, ffmpeg="ffmpeg": np.zeros(60))


def _session(p, data_dir, platform, channel):
    sid = p.conn.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES (?,?,?,0)", (platform, channel, f"https://x/{channel}")).lastrowid
    d = data_dir / "rec" / f"{platform}-{channel}"
    d.mkdir(parents=True, exist_ok=True)
    se = p.conn.execute("INSERT INTO sessions(streamer_id, started_at, dir, status) VALUES (?,?,?, 'ended')", (sid, 0, str(d))).lastrowid
    return {"id": se, "streamer_id": sid, "dir": str(d), "status": "ended"}


async def test_same_channel_on_two_platforms_overwrites_one_clip_file(data_dir, cfg, monkeypatch):
    # bug: clip file name is slug(channel)+peak second with no platform; a multistreamer's Twitch and Kick clips share one mp4, so approving one posts the other's video
    _fake_media(monkeypatch)
    p = offline_pipeline(use_whisper=False)
    try:
        a, b = _session(p, data_dir, "twitch", "foo"), _session(p, data_dir, "kick", "foo")
        c1 = await p.make_clip(a, 1_800_000_000.0, 5.0, {}, cfg)
        c2 = await p.make_clip(b, 1_800_000_000.0, 5.0, {}, cfg)
        paths = [p.conn.execute("SELECT path FROM clips WHERE id=?", (c,)).fetchone()[0] for c in (c1, c2)]
    finally:
        await p.http.aclose(); p.conn.close()
    assert paths[0] != paths[1], f"both clips point at {paths[0]}"


async def test_whisper_ignores_the_configured_ffmpeg(data_dir, cfg, monkeypatch):
    # bug: laughter_and_transcript runs a bare "ffmpeg"; with ffmpeg set only in Settings, laughter and transcripts silently vanish
    _fake_media(monkeypatch)
    calls = []
    monkeypatch.setattr(analysis.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 1, b"", b""))
    cfg = dict(cfg, ffmpeg="C:/tools/ff/bin/ffmpeg.exe")
    p = offline_pipeline(use_whisper=True)
    try:
        await p.make_clip(_session(p, data_dir, "twitch", "bar"), 1_800_000_000.0, 5.0, {}, cfg)
    finally:
        await p.http.aclose(); p.conn.close()
    assert calls and calls[0][0] == "C:/tools/ff/bin/ffmpeg.exe", calls


async def test_chunk_start_ignores_a_wall_clock_gap_in_the_file_names(data_dir, monkeypatch):
    # bug: _register_chunks chains durations and ignores the file-name time, so a 100 s gap (ad break, stall) shifts every later chunk, desyncing chat and Clip now
    monkeypatch.setattr(cutter, "probe_duration", lambda path, ffprobe="ffprobe": 600.0)
    p = offline_pipeline(use_whisper=False)
    try:
        sess = _session(p, data_dir, "twitch", "gap")
        d = Path(sess["dir"])
        (d / "chunk_20261116-120000.ts").write_bytes(b"")
        (d / "chunk_20261116-121140.ts").write_bytes(b"")  # starts 700 s after the first; the first holds 600 s of media
        p._register_chunks(sess)
        starts = [r[0] for r in p.conn.execute("SELECT start_ts FROM chunks WHERE session_id=? ORDER BY start_ts", (sess["id"],))]
    finally:
        await p.http.aclose(); p.conn.close()
    t0 = time.mktime(time.strptime("20261116-120000", "%Y%m%d-%H%M%S"))
    assert abs(starts[1] - (t0 + 700)) < 2, f"second chunk placed at +{starts[1] - t0:.0f} s, its file says +700 s"


def test_removing_a_streamer_leaves_its_recordings_on_disk_forever(data_dir):
    # bug: DELETE /api/streamers cascades the chunk rows away, and cleanup only deletes files it has rows for
    app = create_app(lambda: offline_pipeline(use_whisper=False), start_loops=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        p = app.state.pipeline
        sess = _session(p, data_dir, "twitch", "gone")
        f = Path(sess["dir"]) / "chunk_20261116-120000.ts"
        f.write_bytes(b"\x00" * 1024)
        p.conn.execute("INSERT INTO chunks(session_id, path, start_ts, duration, analyzed) VALUES (?,?,?,?,1)",
                       (sess["id"], str(f), time.time() - 10 * 3600, 600))
        assert c.delete(f"/api/streamers/{sess['streamer_id']}", headers={"X-Clipper": "1"}).status_code == 200
        c.portal.call(p.cleanup_once)
    assert not f.exists(), "a 10 h old recording of a removed streamer is never deleted"
