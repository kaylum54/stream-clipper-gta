"""The whole chain with no network: fake live check -> real recorder on a synthetic stream -> fake chat ->
real analyser -> real cutter -> Discord mock. Catches wiring bugs no unit test sees."""
import asyncio
import json
import time

import pytest

from clipper import config, db
from clipper.pipeline import Pipeline
from clipper.platforms import LiveInfo
from tests.media_helpers import source_cmd
from tests.mock_services import MockServices

SECONDS, BURST = 26, 15


@pytest.mark.slow
async def test_live_stream_becomes_a_clip_in_discord(data_dir):
    # bug caught: sections that each pass alone but never hand over (chunks not registered, chat not
    # aligned, clip window wrong, Discord not told)
    with MockServices() as mock:
        config.save({"chunk_seconds": 10, "preroll": 8, "postroll": 4, "vertical": False, "use_whisper": False,
                     "min_gap_seconds": 20, "score_threshold": 2.5,
                     "discord_webhook_url": f"{mock.base_url}/api/webhooks/1/abc"})
        live = {"on": True}

        async def check_live(streamers, cfg, http):
            return {s["id"]: LiveInfo(live=live["on"], title="test stream") for s in streamers}

        t0 = {}

        async def fake_chat(platform, channel, info, cfg, on_message, stop, http, **kw):
            t0.setdefault("t", time.time())
            while not stop.is_set():
                el = time.time() - t0["t"]
                n = 12 if BURST <= el <= BURST + 6 else 1   # chat explodes with the audio burst (wide: recorder start-up lag shifts the audio by 1-2 s)
                for _ in range(n):
                    on_message(time.time(), "KEKW KEKW" if n > 1 else "hello")
                await asyncio.sleep(0.25)
            return "stopped"

        p = Pipeline(check_live=check_live, collect_chat=fake_chat, use_whisper=False,
                     source_cmd_for=lambda s: source_cmd(SECONDS, [(BURST, 2)]))
        conn = p.conn
        conn.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch','tester','https://www.twitch.tv/tester',0)")

        await p.monitor_once()
        sess = conn.execute("SELECT * FROM sessions").fetchone()
        assert sess["status"] == "recording"
        for _ in range(SECONDS + 20):                 # the synthetic stream ends by itself
            await asyncio.sleep(1)
            p._flush_chat()
            await p.analyse_once()                   # chunks get analysed while recording goes on
            p._reap_recorders()
            if conn.execute("SELECT status FROM sessions").fetchone()["status"] != "recording":
                break
        assert conn.execute("SELECT status FROM sessions").fetchone()["status"] == "ended"
        await p.analyse_once()                       # the last chunks once the stream is over

        chunks = db.rows(conn.execute("SELECT * FROM chunks ORDER BY start_ts"))
        assert len(chunks) >= 2 and all(c["analyzed"] for c in chunks), chunks
        assert conn.execute("SELECT COUNT(*) FROM chat_events").fetchone()[0] > 30

        clips = db.rows(conn.execute("SELECT * FROM clips"))
        assert len(clips) == 1, clips
        c = clips[0]
        rec_start = chunks[0]["start_ts"]
        assert abs((c["peak_ts"] - rec_start) - BURST) <= 3, (c["peak_ts"] - rec_start)
        sig = json.loads(c["signals"])
        assert sig["chat"] > 0                       # chat contributed, not audio alone
        assert len(sig["curve"]["audio_db"]) >= 10
        from clipper.cutter import probe_duration
        assert abs(probe_duration(c["path"]) - 12) <= 1.5

        await asyncio.sleep(1)                       # Discord posts are background tasks now (F6)
        hooks = [r for r in mock.requests if r[1].startswith("/api/webhooks/1/abc")]
        assert len(hooks) == 2                       # "went live" + "clip ready" with the file
        assert hooks[-1][3]["files"], hooks[-1]
        events = [e["kind"] for e in db.rows(conn.execute("SELECT kind FROM events"))]
        assert {"live", "record", "chunk", "clip"} <= set(events)
        await p.stop()
