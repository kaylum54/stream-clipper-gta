"""Scenario 1: monitor scale. 200 streamers, mixed live/offline/error, slow checks, 5 concurrent passes, flapping.
Run: .venv\\Scripts\\python.exe stress\\s1_monitor_scale.py"""
import asyncio
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

common.init("s1")

import httpx  # noqa: E402

from clipper import config  # noqa: E402
from clipper.pipeline import Pipeline  # noqa: E402
from clipper.platforms import LiveInfo  # noqa: E402

N = 200
discord_hits = []


class FakeRec:
    def __init__(self, *a, **kw):
        self.out_dir, self.stopped = Path(a[1]), False

    def start(self):
        pass

    def poll(self):
        return 0 if self.stopped else None

    def stop(self, timeout=15):
        self.stopped = True


def handler(req):
    if req.url.path.startswith("/api/webhooks/"):
        discord_hits.append(time.time())
    return httpx.Response(200, json={})


def make(check):
    async def no_chat(platform, channel, info, cfg, on_message, stop, http, **kw):
        await stop.wait()
        return "stopped"

    config.save({"discord_webhook_url": "http://127.0.0.1:9/api/webhooks/1/x"})
    p = Pipeline(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), recorder_cls=FakeRec,
                 check_live=check, collect_chat=no_chat, use_whisper=False)
    for i in range(N):
        p.conn.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch',?,?,0)", (f"s{i}", f"https://www.twitch.tv/s{i}"))
    return p


def q(p, sql):
    return p.conn.execute(sql).fetchone()[0]


async def main():
    rnd = random.Random(1)
    state = {"mode": "mixed", "pass": 0}

    async def check(streamers, cfg, http):
        async def one(s):
            lat = 2.0 if rnd.random() < 0.03 else rnd.uniform(0.01, 0.25)   # a few slow answers
            await asyncio.sleep(lat)
            i = s["id"]
            if state["mode"] == "flap":                      # every streamer flips every pass
                return s["id"], LiveInfo(live=(state["pass"] % 2 == 0), title="t")
            if i % 5 == 0:
                return i, LiveInfo(live=True, title="live")
            if i % 5 == 1:
                return i, LiveInfo(live=False, error="timeout")
            return i, LiveInfo(live=False)
        return dict(await asyncio.gather(*(one(s) for s in streamers)))

    p = make(check)
    out = []

    # 1a: first pass (40 streamers go live), then steady passes
    with common.LoopLag() as lag:
        t = time.perf_counter()
        await p.monitor_once()
        first = time.perf_counter() - t
    out.append(f"1a first pass ({N} streamers, 40 live, 40 errors, 3% 2s-slow): {first:.2f}s, max loop block {lag.max_ms:.0f} ms, sessions={q(p, 'select count(*) from sessions')}")
    times = []
    for _ in range(5):
        t = time.perf_counter()
        await p.monitor_once()
        times.append(time.perf_counter() - t)
    out.append(f"1a steady passes: {', '.join(f'{x:.2f}s' for x in times)}")

    # 1a': the same pass with instant checks isolates our own overhead (DB writes + loop)
    async def instant(streamers, cfg, http):
        return {s["id"]: (LiveInfo(live=True) if s["id"] % 5 == 0 else LiveInfo(live=False)) for s in streamers}
    p.check_live = instant
    cpu0, t0 = time.process_time(), time.perf_counter()
    for _ in range(5):
        await p.monitor_once()
    wall = (time.perf_counter() - t0) / 5
    out.append(f"1a' own overhead with instant checks: {wall * 1000:.0f} ms/pass wall, {(time.process_time() - cpu0) / 5 * 1000:.0f} ms/pass CPU")
    p.check_live = check

    # 1b: 5 concurrent passes on a fresh DB state (all offline->live) must give one session per live streamer
    p.conn.execute("DELETE FROM sessions")
    p.recorders.clear()
    p.conn.execute("UPDATE streamers SET live=0")
    t = time.perf_counter()
    await asyncio.gather(*(p.monitor_once() for _ in range(5)))
    dt = time.perf_counter() - t
    dup = q(p, "select count(*) from (select streamer_id from sessions where status='recording' group by streamer_id having count(*)>1)")
    out.append(f"1b 5 concurrent monitor_once: {dt:.2f}s total, sessions={q(p, 'select count(*) from sessions')} (expect 40), streamers with >1 session={dup}, recorders={len(p.recorders)}")

    # 1c: flapping every pass for 40 passes
    state["mode"] = "flap"
    tasks0, rss0, h0 = len(p.tasks), common.rss_mb(), common.handle_count()
    discord_hits.clear()
    ev0 = q(p, "select count(*) from events")
    for k in range(40):
        state["pass"] = k
        await p.monitor_once()
    await asyncio.sleep(0.1)
    live_sess = q(p, "select count(*) from sessions where status='recording'")
    out.append(f"1c flap x40 passes: recorders={len(p.recorders)} chat_stops={len(p.chat_stops)} strikes={len(p.offline_strikes)} "
               f"recording sessions={live_sess} total sessions={q(p, 'select count(*) from sessions')} "
               f"tasks list {tasks0}->{len(p.tasks)} (done={sum(1 for t in p.tasks if t.done())}) "
               f"RSS {rss0:.0f}->{common.rss_mb():.0f} MB handles {h0}->{common.handle_count()} "
               f"events +{q(p, 'select count(*) from events') - ev0} discord 'live' messages={len(discord_hits)}")
    # a final offline pass x2 must close everything
    state["pass"] = 1
    for _ in range(2):
        await p.monitor_once()
    out.append(f"1c after two offline passes: recorders={len(p.recorders)} chat_stops={len(p.chat_stops)} recording={q(p, 'select count(*) from sessions where status=\"recording\"')}")
    await p.stop()
    print("\n".join(out))


asyncio.run(main())
