"""Scenario 3: API load on a big DB through real uvicorn, plus 25 concurrent approves of one clip.
Run: .venv\\Scripts\\python.exe stress\\s3_api_load.py [port=8791] [seconds=30] [clients=30]
(8765 and 8770-8772 are never used.)"""
import asyncio
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

if len(sys.argv) > 1 and sys.argv[1] == "serve":   # child: the real app, no loops (nothing else writes)
    import faulthandler
    import uvicorn
    from clipper.app import create_app
    if os.environ.get("STRESS_DUMP"):          # diagnosis: print every thread's stack once, N seconds after start
        faulthandler.dump_traceback_later(int(os.environ["STRESS_DUMP"]), exit=False)
    if os.environ.get("STRESS_FIXED"):          # experiment only: one SQLite connection per thread, to prove that is the smallest fix
        import threading
        from clipper import db as _db
        from clipper.pipeline import Pipeline
        _tl = threading.local()

        def _get(self):
            if getattr(_tl, "c", None) is None:
                _tl.c = _db.connect(self.db_path)
            return _tl.c
        Pipeline.conn = property(_get, lambda self, v: None)
    uvicorn.run(create_app(start_loops=False), host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
    raise SystemExit

DATA = common.init("s3")
import httpx  # noqa: E402

from clipper import config, db  # noqa: E402
from tests.mock_services import MockServices  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 30
CLIENTS = int(sys.argv[3]) if len(sys.argv) > 3 else 30
RACE = not (len(sys.argv) > 4 and sys.argv[4] == "norace")
assert PORT not in (8765, 8770, 8771, 8772)


def seed():
    (DATA / "clips").mkdir(exist_ok=True)
    mp4 = DATA / "clips" / "tiny.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=1", "-pix_fmt", "yuv420p", str(mp4)], check=True)
    c = db.connect(DATA / "clipper.db")
    rnd = random.Random(2)
    now = time.time()
    c.execute("BEGIN")
    for i in range(100):
        c.execute("INSERT INTO streamers(platform, channel, url, live, created_at) VALUES ('twitch',?,?,?,?)", (f"streamer{i}", f"https://www.twitch.tv/streamer{i}", int(i < 20), now))
    for i in range(100):
        sid = c.execute("INSERT INTO sessions(streamer_id, started_at, dir, status) VALUES (?,?,?,?)",
                        (i + 1, now - 3600, str(DATA / "rec" / f"x{i}"), "recording" if i < 20 else "ended")).lastrowid
        c.executemany("INSERT INTO chunks(session_id, path, start_ts, duration, analyzed) VALUES (?,?,?,?,1)",
                      [(sid, str(DATA / "rec" / f"x{i}" / f"chunk_{k}.ts"), now - 3600 + k * 600, 600) for k in range(50)])
        c.executemany("INSERT INTO chat_events(session_id, ts, hype) VALUES (?,?,?)",
                      [(sid, now - 3600 + rnd.random() * 3600, rnd.randint(0, 3)) for _ in range(500)])
    curve = {"audio_db": [round(rnd.uniform(-60, -10), 1) for _ in range(60)], "chat": [rnd.randint(0, 9) for _ in range(60)], "hype": [rnd.randint(0, 5) for _ in range(60)]}
    sig = json.dumps({"audio": 3.1, "chat": 1.2, "hype": 0.4, "laugh": 0.0, "peak_offset": 40.0, "curve": curve})
    for i in range(2000):
        st = "pending" if i < 1500 else "posted" if i < 1800 else "failed" if i < 1900 else "rejected"
        cid = c.execute("INSERT INTO clips(session_id, streamer_id, streamer_name, platform, peak_ts, start_ts, end_ts, score, signals, path, thumb, transcript, caption, status, created_at, decided_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (i % 100 + 1, i % 100 + 1, f"streamer{i % 100}", "twitch", now - i * 30, now - i * 30 - 40, now - i * 30 + 20, rnd.uniform(3, 12), sig,
                         str(mp4), str(mp4), "they said a long transcript " * 8, "caption #clips", st, now - i * 30, now - i * 20 if st != "pending" else None)).lastrowid
        if st in ("posted", "failed"):
            c.execute("INSERT INTO posts(clip_id, pfm_post_id, accounts, status, results, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                      (cid, f"sp{cid}", '["spc_tt1","spc_yt1"]', "done" if st == "posted" else "failed",
                       json.dumps([{"account_id": "spc_tt1", "success": st == "posted", "error": None, "url": "https://x.test/1"}]), now, now))
    c.executemany("INSERT INTO events(ts, level, kind, message, streamer_id) VALUES (?,?,?,?,?)",
                  [(now - k, "info", "chunk", f"chunk chunk_{k}.ts: 1 moment(s)", k % 100 + 1) for k in range(5000)])
    c.execute("COMMIT")
    if os.environ.get("STRESS_INDEX"):          # experiment only: the three indexes that /api/state needs
        c.executescript("CREATE INDEX IF NOT EXISTS clips_streamer ON clips(streamer_id, created_at);"
                        "CREATE INDEX IF NOT EXISTS chunks_session ON chunks(session_id);"
                        "CREATE INDEX IF NOT EXISTS sessions_streamer ON sessions(streamer_id, status);")
    c.close()
    return mp4


async def hammer(base, mix, seconds, clients, results, errors, write_every=0):
    stop = time.time() + seconds
    limits = httpx.Limits(max_connections=clients, max_keepalive_connections=clients)
    async with httpx.AsyncClient(base_url=base, limits=limits, timeout=20) as cl:
        async def worker(w):
            n = 0
            while time.time() < stop:
                n += 1
                path = mix[(w + n) % len(mix)]
                t = time.perf_counter()
                try:
                    if write_every and n % write_every == 0:
                        r = await cl.patch(f"/api/clips/{(w * 7 + n) % 1500 + 1}", json={"caption": f"edit {w} {n}"}, headers={"x-clipper": "1"})
                        key = "PATCH caption"
                    else:
                        r = await cl.get(path)
                        key = path.split("?")[0] + ("?" + path.split("?")[1] if "?" in path else "")
                    dt = time.perf_counter() - t
                    results.setdefault(key, []).append(dt)
                    if r.status_code != 200:
                        errors.append((key, r.status_code, r.text[:100]))
                except Exception as e:
                    errors.append((path, type(e).__name__, str(e)[:100]))
        await asyncio.gather(*(worker(w) for w in range(clients)))


async def main():
    mp4 = seed()
    with MockServices() as mock:
        config.save({"postforme_api_key": "test-key"})
        env = dict(os.environ, POSTFORME_BASE=mock.base_url, CLIPPER_DATA=str(DATA))
        logf = open(DATA / "server.log", "w")   # a file, not a pipe: 500 tracebacks fill a pipe and block the server
        srv = subprocess.Popen([sys.executable, __file__, "serve", str(PORT)], env=env, stdout=logf, stderr=subprocess.STDOUT, text=True)
        base = f"http://127.0.0.1:{PORT}"
        try:
            async with httpx.AsyncClient() as cl:
                for _ in range(100):
                    try:
                        if (await cl.get(base + "/api/health")).status_code == 200:
                            break
                    except Exception:
                        await asyncio.sleep(0.2)
            out = []
            # payload sizes
            async with httpx.AsyncClient(base_url=base, timeout=60) as cl:
                for path in ("/api/state", "/api/clips?status=pending", "/api/clips?status=done", "/api/clips?status=pending&limit=500"):
                    t = time.perf_counter()
                    r = await cl.get(path)
                    out.append(f"3 single {path}: {r.status_code} {(time.perf_counter() - t) * 1000:.0f} ms {len(r.content) / 1024:.0f} KB")

            # race: 25 concurrent approves of clip 1
            async def approve(cl):
                r = await cl.post("/api/clips/1/approve", json={"caption": "race", "accounts": ["spc_tt1"]}, headers={"x-clipper": "1"})
                return r.status_code
            async with httpx.AsyncClient(base_url=base, timeout=60) as cl:
                codes = [] if not RACE else await asyncio.gather(*(approve(cl) for _ in range(25)))
            await asyncio.sleep(3)
            creates = [r for r in mock.requests if r[0] == "POST" and r[1] == "/v1/social-posts"]
            uploads = [r for r in mock.requests if r[1] == "/v1/media/create-upload-url"]
            posts_rows = db.connect(DATA / "clipper.db").execute("SELECT COUNT(*) FROM posts WHERE clip_id=1").fetchone()[0]
            out.append(f"3 race 25x approve: status codes {sorted(set(codes))} (200 x{codes.count(200)}, 409 x{codes.count(409)}), Post for Me creates={len(creates)}, uploads={len(uploads)}, posts rows={posts_rows}")

            # load: read mix, then read+write mix
            for label, mix, wr in (("reads", ["/api/state", "/api/clips?status=pending", "/api/clips?status=done"], 0),
                                   ("reads + 1 in 10 caption PATCH", ["/api/state", "/api/clips?status=pending", "/api/clips?status=done"], 10)):
                results, errors = {}, []
                t = time.time()
                await hammer(base, mix, SECONDS, CLIENTS, results, errors, wr)
                el = time.time() - t
                allx = [x for v in results.values() for x in v]
                out.append(f"3 load [{label}] {CLIENTS} clients {el:.0f}s: {len(allx)} requests = {len(allx) / el:.1f}/s, errors {len(errors)} {errors[:3]}")
                for k, v in sorted(results.items()):
                    out.append(f"    {k}: {common.lat_summary(v)}")
            print("\n".join(out))
        finally:
            srv.terminate()
            try:
                srv.wait(5)
            except Exception:
                srv.kill()
            logs = srv.stdout.read() if srv.stdout else ""
            if logs.strip():
                print("server log tail:\n" + logs[-1500:])


asyncio.run(main())
