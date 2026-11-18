"""Scenario 4: failure injection. Run one part or all: .venv\\Scripts\\python.exe stress\\s4_failures.py [a b c d e f g h]"""
import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DATA = common.init("s4")

import httpx  # noqa: E402

from clipper import config, db, recorder  # noqa: E402
from clipper.pipeline import Pipeline  # noqa: E402
from clipper.platforms import LiveInfo  # noqa: E402
from fakes import HangingDiscord, StatefulPFM  # noqa: E402
from tests.media_helpers import make_ts, source_cmd  # noqa: E402

FAST = {"chunk_seconds": 10, "preroll": 8, "postroll": 4, "vertical": False, "use_whisper": False, "min_gap_seconds": 20, "score_threshold": 2.5}
LIVE = {"on": True}


async def check_live(streamers, cfg, http):
    return {s["id"]: LiveInfo(live=LIVE["on"], title="t") for s in streamers}


async def no_chat(platform, channel, info, cfg, on_message, stop, http, **kw):
    await stop.wait()
    return "stopped"


def fresh(name, **kw):
    """A clean data dir + pipeline per sub-scenario."""
    d = DATA / name
    d.mkdir(parents=True, exist_ok=True)
    config.DATA = d
    os.environ["CLIPPER_DATA"] = str(d)
    config.save(FAST)
    LIVE["on"] = True
    p = Pipeline(db_path=d / "clipper.db", check_live=check_live, collect_chat=no_chat, use_whisper=False, **kw)
    return p


def add_streamer(p, name="s0"):
    return p.conn.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch',?,?,0)", (name, f"https://www.twitch.tv/{name}")).lastrowid


def n(p, sql):
    return p.conn.execute(sql).fetchone()[0]


def errs(p, k=4):
    return [e["message"][:110] for e in db.rows(p.conn.execute("SELECT message FROM events WHERE level IN ('error','warn') ORDER BY id LIMIT ?", (k,)))]


def seed_session(p, sid_streamer, name, chunk_specs, status="ended", register=True):
    """Session whose dir holds real chunk files, named like the recorder names them. chunk_specs: [(seconds, bursts)]"""
    d = config.DATA / "rec" / name
    d.mkdir(parents=True, exist_ok=True)
    t0 = int(time.time()) - 600
    sid = p.conn.execute("INSERT INTO sessions(streamer_id, started_at, dir, status, ended_at) VALUES (?,?,?,?,?)",
                         (sid_streamer, t0, str(d), status, t0 + 100)).lastrowid
    paths = []
    for i, (secs, bursts) in enumerate(chunk_specs):
        f = d / f"chunk_{time.strftime(recorder._FMT, time.localtime(t0 + i * 10))}.ts"
        make_ts(f, secs, bursts)
        paths.append(f)
    if register:
        p._register_chunks(db.rows(p.conn.execute("select * from sessions where id=?", (sid,)))[0])
    return sid, d, t0, paths


# ---------------------------------------------------------------- a, b
async def part_a():
    out = []
    # a1: ffmpeg path wrong while recording starts
    config_dir = DATA / "a"
    p = fresh("a", source_cmd_for=lambda s: source_cmd(30, [(5, 2)]))
    config.save({"ffmpeg": r"C:\nope\ffmpeg.exe"})
    add_streamer(p)
    h0, cpu0, t0 = common.handle_count(), time.process_time(), time.time()
    for _ in range(6):
        await p.monitor_once()
    dirs = len(list((config.DATA / "rec").glob("*"))) if (config.DATA / "rec").exists() else 0
    out.append(f"4a ffmpeg missing, 6 monitor passes: sessions={n(p, 'select count(*) from sessions')} rec dirs left={dirs} handles {h0}->{common.handle_count()} "
               f"ffmpeg procs after={common.procs('ffmpeg')['ffmpeg']} events={errs(p, 2)}")
    # a2: hot loop check: run the real loops for 12 s with a short poll
    config.save({"poll_seconds": 30})
    p.start()
    await asyncio.sleep(12)
    cpu = time.process_time() - cpu0
    out.append(f"4a loops running 12 s with bad ffmpeg: CPU {cpu:.2f}s over {time.time() - t0:.0f}s (no hot spin if << wall), error events={n(p, chr(115) + 'elect count(*) from events where level=' + chr(39) + 'error' + chr(39))}")
    await p.stop()
    # a3: chunks recorded fine, ffmpeg path breaks during analysis, then is repaired
    config.save({"ffmpeg": "ffmpeg"})
    p = fresh("a3")
    sid = add_streamer(p)
    s, d, t0s, paths = seed_session(p, sid, "a3", [(10, [(4, 2)]), (10, []), (10, [])])
    config.save({"ffmpeg": r"C:\nope\ffmpeg.exe"})
    await p.analyse_once()
    bad = db.rows(p.conn.execute("SELECT analyzed, error FROM chunks"))
    config.save({"ffmpeg": "ffmpeg"})
    await p.analyse_once()
    out.append(f"4a3 ffmpeg broken during analysis, then repaired: chunks {[(b['analyzed'], (b['error'] or '')[:30]) for b in bad]} -> after repair clips={n(p, 'select count(*) from clips')} "
               f"(burst at 4 s of chunk 1 was in the data), still analyzed+errored: {n(p, 'select count(*) from chunks where analyzed=1 and error is not null')}")
    await p.stop()
    # b: streamlink missing, real recorder, default source
    p = fresh("b")
    config.save({"streamlink": r"C:\nope\streamlink.exe"})
    add_streamer(p)
    h0 = common.handle_count()
    for _ in range(6):
        await p.monitor_once()
    dirs = len(list((config.DATA / "rec").glob("*")))
    out.append(f"4b streamlink missing, 6 passes: sessions={n(p, 'select count(*) from sessions')} rec dirs created={dirs} handles {h0}->{common.handle_count()} "
               f"'recorder failed' events={n(p, chr(115) + 'elect count(*) from events where message like ' + chr(39) + '%failed to start%' + chr(39))}, procs {common.procs('ffmpeg', 'streamlink')}")
    await p.stop()
    return out


# ---------------------------------------------------------------- c
async def part_c():
    out = []
    with StatefulPFM(hang_s=6) as pfm:
        os.environ["POSTFORME_BASE"] = pfm.base_url
        p = fresh("c", http=httpx.AsyncClient(timeout=3))
        config.save({"postforme_api_key": "k"})
        sid = add_streamer(p)
        (config.DATA / "clips").mkdir(exist_ok=True)
        mp4 = config.DATA / "clips" / "t.mp4"
        make_ts(mp4, 2)
        ids = [p.conn.execute("INSERT INTO clips(streamer_id, streamer_name, platform, peak_ts, start_ts, end_ts, score, path, caption, created_at) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?)", (sid, "s0", "twitch", 100 + i, 60, 120, 5, str(mp4), "hi", time.time())).lastrowid for i in range(5)]

        async def approve_all(label):
            for cid in ids:
                try:
                    await p.approve(cid, "cap", ["spc_tt1", "spc_yt1"])
                except ValueError as e:
                    pass
            await asyncio.gather(*p.tasks, return_exceptions=True)
            p.tasks.clear()
            st = {}
            for r in p.conn.execute("SELECT c.status cs, po.status ps FROM clips c LEFT JOIN posts po ON po.clip_id=c.id AND po.id=(SELECT MAX(id) FROM posts WHERE clip_id=c.id)"):
                st[(r["cs"], r["ps"])] = st.get((r["cs"], r["ps"]), 0) + 1
            out.append(f"4c {label}: states {st}, PFM creates so far={pfm.creates}")

        for mode in ("429", "500", "hang", "create_then_hang"):
            pfm.mode = mode
            t = time.time()
            await approve_all(f"mode {mode} ({time.time() - t:.0f}s so far)")
        pfm.mode = "ok"
        await approve_all("recovered, re-approve all")
        await p.results_once()
        out.append(f"4c after results poll: clips {dict(p.conn.execute('select status, count(*) from clips group by status').fetchall())}, creates per external_id "
                   f"{sorted([x['external_id'] for x in pfm.posts.values()])}, duplicates={pfm.creates - len({x['external_id'] for x in pfm.posts.values()})}")
        # results polling through an outage and recovery, and error-log visibility
        pfm.mode = "500"
        before = n(p, "select count(*) from events where kind='post' and level='warn'")
        p.conn.execute("UPDATE posts SET status='submitted' WHERE id=(SELECT MIN(id) FROM posts WHERE status='done')")
        for _ in range(3):
            await p.results_once()
        mid = n(p, "select count(*) from events where kind='post' and level='warn'")
        pfm.mode = "ok"
        await p.results_once()
        pfm.mode = "500"
        p.conn.execute("UPDATE posts SET status='submitted' WHERE id=(SELECT MIN(id) FROM posts WHERE status='done')")
        await p.results_once()
        after = n(p, "select count(*) from events where kind='post' and level='warn'")
        out.append(f"4c result-poll warn events: first outage 3 polls -> {mid - before}; after recovery, a SECOND outage -> {after - mid} more (0 means the second outage is invisible)")
        await p.http.aclose()
    return out


# ---------------------------------------------------------------- d
async def part_d():
    out = []
    with HangingDiscord() as dc:
        p = fresh("d", source_cmd_for=lambda s: source_cmd(30, [(5, 2)]))
        config.save({"discord_webhook_url": f"{dc.base_url}/api/webhooks/1/x"})
        p.http = httpx.AsyncClient(timeout=60)   # production default is 60 s
        sid = add_streamer(p)
        s, d, t0s, paths = seed_session(p, sid, "d", [(10, [(6, 2)]), (10, []), (10, [])])
        # d1: one clip, Discord silent
        p._register_chunks(db.rows(p.conn.execute("select * from sessions"))[0])
        sess = db.rows(p.conn.execute("select * from sessions"))[0]
        t = time.time()
        cid = await p.make_clip(sess, t0s + 6.5, 5.0, {}, config.load())
        out.append(f"4d make_clip with a webhook that never answers: returned after {time.time() - t:.0f}s (httpx timeout 60 s); clip row exists={cid is not None}, "
                   f"clip created before the wait={n(p, 'select count(*) from clips')}, warn events={errs(p, 1)}")
        # d2: monitor pass starting 3 sessions, timeout 5 s, shows the per-streamer serial cost holding monitor_lock
        p.http = httpx.AsyncClient(timeout=5)
        for i in range(1, 4):
            add_streamer(p, f"x{i}")
        p.conn.execute("DELETE FROM streamers WHERE id=?", (sid,))
        t = time.time()
        await p.monitor_once()
        out.append(f"4d monitor_once starting 3 sessions with a silent webhook (client timeout 5 s): {time.time() - t:.1f}s, sessions={n(p, 'select count(*) from sessions where status=' + chr(39) + 'recording' + chr(39))} "
                   f"(one pass = N x timeout; at the production 60 s that is 3 min and every other check waits)")
        await p.stop()
    return out


# ---------------------------------------------------------------- e
async def part_e():
    out = []
    p = fresh("e")
    sid = add_streamer(p)
    s, d, t0s, paths = seed_session(p, sid, "e", [(10, []), (10, []), (10, []), (10, []), (10, [(1, 2)])], register=False)
    good = paths[0].read_bytes()
    paths[1].write_bytes(good[: len(good) // 3])       # truncated
    paths[2].write_bytes(os.urandom(50000))             # garbage
    paths[3].write_bytes(b"")                           # empty
    p._register_chunks(db.rows(p.conn.execute("select * from sessions"))[0])
    await p.analyse_once()
    rows = db.rows(p.conn.execute("SELECT path, analyzed, error, duration FROM chunks ORDER BY start_ts"))
    out.append("4e chunks: " + "; ".join(f"{Path(r['path']).name[-9:-3]} analyzed={r['analyzed']} dur={r['duration'] and round(r['duration'], 1)} err={(r['error'] or '')[:35]!r}" for r in rows))
    out.append(f"4e pipeline continued: all analysed={all(r['analyzed'] for r in rows)}, error events={n(p, 'select count(*) from events where level=' + chr(39) + 'error' + chr(39))}, clips={n(p, 'select count(*) from clips')}")
    # a clip whose pre-roll reaches into the corrupt neighbour
    sess = db.rows(p.conn.execute("select * from sessions"))[0]
    last = rows[-1]
    st = p.conn.execute("select start_ts from chunks order by start_ts desc limit 1").fetchone()[0]
    cid = await p.make_clip(sess, st + 3, 5.0, {}, config.load())
    out.append(f"4e clip at 3 s into the last good chunk (pre-roll 8 s reaches the empty neighbour): clip id={cid}, events={errs(p, 8)[-1:]}")
    await p.stop()
    return out


# ---------------------------------------------------------------- f
async def part_f():
    out = []
    p = fresh("f", source_cmd_for=lambda s: source_cmd(90, [(5, 2)]))
    add_streamer(p)
    await p.monitor_once()
    await asyncio.sleep(12)
    sid = list(p.recorders)[0]
    rec = p.recorders[sid]
    before = common.procs("ffmpeg")["ffmpeg"]
    subprocess.run(["taskkill", "/PID", str(rec._mux.pid), "/T", "/F"], capture_output=True)   # kill the segment muxer only
    t = time.time()
    await asyncio.sleep(3)
    src_alive = rec._src.poll() is None
    status_now = p.conn.execute("select status from sessions where id=?", (sid,)).fetchone()[0]
    out.append(f"4f muxer killed: ffmpeg procs {before}->{common.procs('ffmpeg')['ffmpeg']}, source still alive after 3 s={src_alive}, session status before next monitor pass='{status_now}'")
    await p.monitor_once()     # what the monitor loop would do at its next tick (up to poll_seconds later)
    rows = db.rows(p.conn.execute("select id, status, error from sessions order by id"))
    out.append(f"4f next monitor pass: sessions {[(r['id'], r['status'], r['error']) for r in rows]}, new recorder running={[r.running for r in p.recorders.values()]}")
    # kill both children of the new recorder, by PID only (never by image name: other ffmpeg jobs may be running)
    for r in p.recorders.values():
        for proc in (r._mux, r._src):
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    await asyncio.sleep(1)
    await p.monitor_once()
    rows = db.rows(p.conn.execute("select id, status, error from sessions order by id"))
    out.append(f"4f both children of the new recorder killed, then one more pass: sessions {[(r['id'], r['status'], (r['error'] or '')[:30]) for r in rows]}")
    LIVE["on"] = False
    for _ in range(2):
        await p.monitor_once()
    await p.stop()
    await asyncio.sleep(1)
    out.append(f"4f end: procs {common.procs('ffmpeg', 'streamlink')}")
    return out


# ---------------------------------------------------------------- g
async def part_g():
    out = []
    p1 = fresh("g", source_cmd_for=lambda s: source_cmd(40, [(8, 2)]))
    sid = add_streamer(p1)
    await p1.monitor_once()
    await asyncio.sleep(16)                       # chunk 0 finished, chunk 1 growing; analyser never ran (busy or stuck)
    for rec in p1.recorders.values():             # "crash": kill the children, abandon p1 without stop()
        subprocess.run(["taskkill", "/PID", str(rec._mux.pid), "/T", "/F"], capture_output=True)
        subprocess.run(["taskkill", "/PID", str(rec._src.pid), "/T", "/F"], capture_output=True)
    files = len(recorder.list_chunks(db.rows(p1.conn.execute("select * from sessions"))[0]["dir"]))
    rows_before = n(p1, "select count(*) from chunks")
    p2 = Pipeline(db_path=config.DATA / "clipper.db", check_live=check_live, collect_chat=no_chat, use_whisper=False)
    LIVE["on"] = False
    for _ in range(3):
        await p2.analyse_once()
        await asyncio.sleep(1)
    out.append(f"4g crash before the analyser registered anything: chunk files on disk={files}, chunk rows before restart={rows_before}; after restart + 3 analyse passes: "
               f"chunk rows={n(p2, 'select count(*) from chunks')}, clips={n(p2, 'select count(*) from clips')}, session={db.rows(p2.conn.execute('select status,error from sessions'))}")
    # variant: crash after one pass registered and analysed the chunk, then again
    p3 = fresh("g2", source_cmd_for=lambda s: source_cmd(40, [(8, 2)]))
    sid = add_streamer(p3)
    await p3.monitor_once()
    for _ in range(26):
        await asyncio.sleep(1)
        await p3.analyse_once()
    clips_before = n(p3, "select count(*) from clips")
    for rec in p3.recorders.values():
        subprocess.run(["taskkill", "/PID", str(rec._mux.pid), "/T", "/F"], capture_output=True)
        subprocess.run(["taskkill", "/PID", str(rec._src.pid), "/T", "/F"], capture_output=True)
    p4 = Pipeline(db_path=config.DATA / "clipper.db", check_live=check_live, collect_chat=no_chat, use_whisper=False)
    for _ in range(3):
        await p4.analyse_once()
    out.append(f"4g crash mid-session after chunk 0 was analysed: clips before={clips_before}, after restart + 3 passes={n(p4, 'select count(*) from clips')} (no duplicate), "
               f"unanalysed chunks left={n(p4, 'select count(*) from chunks where analyzed=0')}, orphan files on disk not in chunks table="
               f"{len(recorder.list_chunks(db.rows(p4.conn.execute('select * from sessions'))[0]['dir'])) - n(p4, 'select count(*) from chunks')}")
    await p3.http.aclose()
    await p1.http.aclose()
    return out


# ---------------------------------------------------------------- h
async def part_h():
    out = []
    p = fresh("h")
    add_streamer(p)
    locker = sqlite3.connect(config.DATA / "clipper.db", isolation_level=None, check_same_thread=False)
    locker.execute("BEGIN IMMEDIATE")
    locker.execute("INSERT INTO events(ts, level, kind, message) VALUES (0,'info','x','held')")

    def release():
        time.sleep(10)
        locker.execute("COMMIT")
    threading.Thread(target=release).start()
    reader_lat = []

    def reader():
        for _ in range(20):
            t = time.perf_counter()
            p.conn.execute("SELECT COUNT(*) FROM events").fetchone()      # what a GUI request thread does on the shared connection
            reader_lat.append(time.perf_counter() - t)
            time.sleep(0.5)
    rt = threading.Thread(target=reader)
    rt.start()
    t = time.perf_counter()
    with common.LoopLag() as lag:
        await asyncio.sleep(0.2)
        await p.monitor_once()       # writes streamers + events: has to wait for the lock
    out.append(f"4h write blocked behind another connection's write transaction (held 10 s): monitor_once took {time.perf_counter() - t:.1f}s, max event-loop block {lag.max_ms / 1000:.1f}s")
    rt.join()
    out.append(f"4h concurrent read on the same shared connection from a second thread: {common.lat_summary(reader_lat)}")
    # independent connection reads (what a per-thread connection would do)
    locker2 = sqlite3.connect(config.DATA / "clipper.db", isolation_level=None)
    t = time.perf_counter()
    locker2.execute("SELECT COUNT(*) FROM events").fetchone()
    out.append(f"4h read on a separate connection: {(time.perf_counter() - t) * 1000:.1f} ms; errors logged={errs(p, 2)}")
    await p.stop()
    return out


PARTS = {"a": part_a, "b": None, "c": part_c, "d": part_d, "e": part_e, "f": part_f, "g": part_g, "h": part_h}


async def main():
    want = sys.argv[1:] or ["a", "c", "d", "e", "f", "g", "h"]
    for k in want:
        t = time.time()
        for line in await PARTS[k]():
            print(line, flush=True)
        print(f"   [part {k} took {time.time() - t:.0f}s]", flush=True)


asyncio.run(main())
