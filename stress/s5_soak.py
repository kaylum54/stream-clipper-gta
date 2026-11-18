"""Scenario 5: compressed soak. 60 pre-made 10 s chunks (bursts scattered) in one session; analyse repeatedly, then cleanup.
Run: .venv\\Scripts\\python.exe stress\\s5_soak.py"""
import asyncio
import shutil
import sys
import threading
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DATA = common.init("s5")

from clipper import analysis, config, db, recorder  # noqa: E402
from clipper.pipeline import Pipeline  # noqa: E402
from tests.media_helpers import make_ts  # noqa: E402

N = 60


async def main():
    config.save({"chunk_seconds": 10, "preroll": 8, "postroll": 4, "vertical": False, "use_whisper": False,
                 "min_gap_seconds": 20, "score_threshold": 2.5, "keep_chunks_hours": 1})
    p = Pipeline(use_whisper=False)
    sid_st = p.conn.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch','soak','u',0)").lastrowid
    d = config.DATA / "rec" / "soak"
    d.mkdir(parents=True)
    # 6 variants: a burst in variant k at second 3+k (or none)
    tmp = DATA / "variants"
    tmp.mkdir()
    variants = []
    for k in range(6):
        f = tmp / f"v{k}.ts"
        make_ts(f, 10, [(3 + k, 2)] if k else [])
        variants.append(f)
    now = int(time.time())
    t0 = now - 2 * 3600 - 300              # first chunks are about 2 h old (older than keep_chunks_hours=1), the last ones about 1 h 40 min... see below
    # spread: chunk i starts at t0 + i*10 for i<30 (old, ~2 h), chunk i>=30 starts at now - 600 + (i-30)*10 (recent)
    bursts = 0
    starts = []
    for i in range(N):
        start = t0 + i * 10 if i < 30 else now - 600 + (i - 30) * 10
        k = (i * 7) % 6 if i % 3 == 1 else 0
        bursts += 1 if k else 0
        shutil.copy(variants[k], d / f"chunk_{time.strftime(recorder._FMT, time.localtime(start))}.ts")
        starts.append(start)
    sid = p.conn.execute("INSERT INTO sessions(streamer_id, started_at, dir, status) VALUES (?,?,?, 'recording')", (sid_st, t0, str(d))).lastrowid
    # observe concurrency of the analysis worker threads
    conc = {"cur": 0, "max": 0}
    lock = threading.Lock()
    orig = analysis.find_moments

    def wrapped(*a, **kw):
        with lock:
            conc["cur"] += 1
            conc["max"] = max(conc["max"], conc["cur"])
        try:
            return orig(*a, **kw)
        finally:
            with lock:
                conc["cur"] -= 1
    analysis.find_moments = wrapped

    tracemalloc.start()
    rss0, t0w, cpu0 = common.rss_mb(), time.time(), time.process_time()
    await p.analyse_once()                      # registers all 60 and analyses all but the newest
    p.conn.execute("UPDATE sessions SET status='ended', ended_at=? WHERE id=?", (time.time(), sid))
    passes = [time.time() - t0w]
    for _ in range(4):
        t = time.time()
        await p.analyse_once()
        passes.append(time.time() - t)
    wall, cpu = time.time() - t0w, time.process_time() - cpu0
    cur, peak = tracemalloc.get_traced_memory()
    chunks = db.rows(p.conn.execute("SELECT * FROM chunks ORDER BY start_ts"))
    clips = db.rows(p.conn.execute("SELECT peak_ts, score FROM clips ORDER BY peak_ts"))
    print(f"5 analysed {sum(c['analyzed'] for c in chunks)}/{len(chunks)} chunks in {wall:.1f}s wall ({wall / N * 1000:.0f} ms per 10 s chunk incl. clips), python CPU {cpu:.1f}s, "
          f"pass times {[round(x, 1) for x in passes]}")
    print(f"5 memory: RSS {rss0:.0f}->{common.rss_mb():.0f} MB (peak {common.peak_rss_mb():.0f}), tracemalloc current {cur / 1e6:.1f} MB peak {peak / 1e6:.1f} MB; max concurrent analysis threads {conc['max']} (media_lock allows 2)")
    print(f"5 clips {len(clips)} for {bursts} bursts; errors {n_err(p)}; chunk durations all set={all(c['duration'] for c in chunks)}")
    # time-order check: do clips land within 3 s of a burst second?
    # cleanup: mark two old chunks unanalysed, two recent ones analysed, then clean
    old = [c for c in chunks if c["start_ts"] < time.time() - 3600]
    recent = [c for c in chunks if c["start_ts"] >= time.time() - 3600]
    p.conn.execute("UPDATE chunks SET analyzed=0 WHERE id IN (?,?)", (old[0]["id"], old[1]["id"]))
    await p.cleanup_once()
    gone = [c for c in db.rows(p.conn.execute("SELECT * FROM chunks")) if c["deleted"]]
    files_missing = [c for c in db.rows(p.conn.execute("SELECT * FROM chunks")) if not Path(c["path"]).exists()]
    wrong = [c for c in gone if not c["analyzed"] or c["start_ts"] >= time.time() - 3600]
    print(f"5 cleanup: old={len(old)} (2 held back as unanalysed) recent={len(recent)}; marked deleted={len(gone)} (expect {len(old) - 2}), files gone={len(files_missing)}, "
          f"wrongly deleted={len(wrong)}, unanalysed old still on disk={sum(Path(c['path']).exists() for c in old[:2])}/2, recent still on disk={sum(Path(c['path']).exists() for c in recent)}/{len(recent)}")
    # a locked file stops cleanup for everything after it
    victims = [c for c in db.rows(p.conn.execute("SELECT * FROM chunks WHERE deleted=0 AND analyzed=1"))]
    p.conn.execute("UPDATE chunks SET start_ts=start_ts-7200 WHERE deleted=0 AND analyzed=1")
    held = open(victims[0]["path"], "rb")
    try:
        await p.cleanup_once()
        err = None
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:70]}"
    held.close()
    left = sum(1 for c in victims if Path(c["path"]).exists())
    print(f"5 cleanup with one old chunk held open by another program: raised={err}; of {len(victims)} eligible chunks {left} are still on disk (all of them when the first one is locked)")
    await p.stop()


def n_err(p):
    return p.conn.execute("select count(*) from events where level='error'").fetchone()[0]


asyncio.run(main())
