"""Scenario 2: 6 simultaneous realtime synthetic sources through the real Recorder + Pipeline, 10 s chunks.
Run: .venv\\Scripts\\python.exe stress\\s2_concurrent_recording.py [streams=6] [seconds=45]"""
import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DATA = common.init("s2")

from clipper import config, cutter, db, recorder  # noqa: E402
from clipper.pipeline import Pipeline  # noqa: E402
from clipper.platforms import LiveInfo  # noqa: E402
from tests.media_helpers import source_cmd  # noqa: E402

STREAMS = int(sys.argv[1]) if len(sys.argv) > 1 else 6
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 45


def burst_for(i):
    return [(22 + i, 2), (38, 2)]


async def main():
    config.save({"chunk_seconds": 10, "preroll": 8, "postroll": 4, "vertical": False, "use_whisper": False,
                 "min_gap_seconds": 20, "score_threshold": 2.5, "ffmpeg": "ffmpeg"})
    live = {"on": True}

    async def check_live(streamers, cfg, http):
        return {s["id"]: LiveInfo(live=live["on"], title="stress") for s in streamers}

    async def no_chat(platform, channel, info, cfg, on_message, stop, http, **kw):
        await stop.wait()
        return "stopped"

    chans = {}
    p = Pipeline(check_live=check_live, collect_chat=no_chat, use_whisper=False,
                 source_cmd_for=lambda s: source_cmd(SECONDS, burst_for(chans[s["channel"]])))
    for i in range(STREAMS):
        chans[f"s{i}"] = i
        p.conn.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch',?,?,0)", (f"s{i}", f"https://www.twitch.tv/s{i}"))

    before = common.procs("ffmpeg", "streamlink", "ffprobe")
    tp = subprocess.Popen(["typeperf", "-si", "5", "-sc", str((SECONDS + 40) // 5 + 2), r"\Processor(_Total)\% Processor Time"],
                          stdout=subprocess.PIPE, text=True)
    cpu0, t0 = time.process_time(), time.time()
    rss0 = common.rss_mb()
    await p.monitor_once()
    started = time.time() - t0
    asyncio.create_task(p._analyse_loop())
    asyncio.create_task(p._chat_flush_loop())
    backlog, peak_procs, lag = [], 0, common.LoopLag()
    lag.__enter__()
    live_off_at = SECONDS + 2
    reaped = False
    # sample once a second until everything is analysed or 40 s past the end
    while time.time() - t0 < SECONDS + 45:
        await asyncio.sleep(1)
        el = time.time() - t0
        if el > live_off_at:
            live["on"] = False
        if el > SECONDS + 3 and not reaped:
            await p.monitor_once()
            await p.monitor_once()
            reaped = all(r["status"] != "recording" for r in db.rows(p.conn.execute("SELECT status FROM sessions")))
        pending = 0
        for s in db.rows(p.conn.execute("SELECT * FROM sessions")):
            files = recorder.list_chunks(s["dir"])
            known = {r["path"]: r["analyzed"] for r in p.conn.execute("SELECT path, analyzed FROM chunks WHERE session_id=?", (s["id"],))}
            done_files = files if s["status"] != "recording" else files[:-1]   # the newest file is still growing
            pending += sum(1 for f, _ in done_files if not known.get(f))
        backlog.append((round(el), pending))
        if reaped and pending == 0 and el > SECONDS + 5:
            break
    lag.__exit__()
    wall = time.time() - t0
    cpu = time.process_time() - cpu0
    p._flush_chat()
    out = [f"2 wall {wall:.1f}s, python CPU {cpu:.1f}s ({cpu / wall * 100:.0f}% of one core), RSS {rss0:.0f}->{common.rss_mb():.0f} MB, peak {common.peak_rss_mb():.0f} MB, "
           f"monitor_once start of {STREAMS} sessions {started:.2f}s, max event-loop block {lag.max_ms:.0f} ms"]
    out.append("2 backlog (second: unanalysed finished chunks): " + " ".join(f"{a}:{b}" for a, b in backlog[::3]))
    out.append(f"2 max backlog {max(b for _, b in backlog)}, final backlog {backlog[-1][1]}")

    # playable + contiguous
    ffprobe = cutter.ffprobe_path("ffmpeg")
    bad, total, worst_gap, durs = [], 0, 0.0, []
    for s in db.rows(p.conn.execute("SELECT * FROM sessions")):
        ch = db.rows(p.conn.execute("SELECT * FROM chunks WHERE session_id=? ORDER BY start_ts", (s["id"],)))
        on_disk = recorder.list_chunks(s["dir"])
        if len(ch) != len(on_disk):
            bad.append(f"session {s['id']}: {len(ch)} chunk rows vs {len(on_disk)} files")
        for c in ch:
            total += 1
            r = subprocess.run(["ffmpeg", "-v", "error", "-i", c["path"], "-f", "null", "-"], capture_output=True, text=True)
            d = cutter.probe_duration(c["path"], ffprobe)
            durs.append(d)
            if r.returncode != 0 or r.stderr.strip() or d <= 0:
                bad.append(f"unplayable {c['path']}: rc={r.returncode} {r.stderr[:100]}")
        for a, b in zip(ch, ch[1:]):
            worst_gap = max(worst_gap, abs(b["start_ts"] - (a["start_ts"] + (a["duration"] or 0))))
        if any(not c["analyzed"] or c["error"] for c in ch):
            bad.append(f"session {s['id']}: unanalysed or errored chunks {[(c['id'], c['analyzed'], c['error']) for c in ch if not c['analyzed'] or c['error']]}")
    sess = db.rows(p.conn.execute("SELECT * FROM sessions"))
    out.append(f"2 sessions {len(sess)} ({[s['status'] + ('/' + s['error'] if s['error'] else '') for s in sess][:3]}...), chunks {total}, durations min/max {min(durs):.1f}/{max(durs):.1f}s, "
               f"worst timeline gap/overlap {worst_gap * 1000:.0f} ms, problems: {bad or 'none'}")

    clips = db.rows(p.conn.execute("SELECT c.*, s.started_at FROM clips c JOIN sessions s ON s.id=c.session_id"))
    errs = []
    for c in clips:
        i = chans_i = int(c["streamer_name"][1:])
        first = p.conn.execute("SELECT MIN(start_ts) FROM chunks WHERE session_id=?", (c["session_id"],)).fetchone()[0]
        off = c["peak_ts"] - first
        want = min(((b + 1) for b, _ in burst_for(i)), key=lambda x: abs(x - off))
        errs.append(abs(off - want))
        if abs(cutter.probe_duration(c["path"], ffprobe) - 12) > 2:
            errs.append(99)
    out.append(f"2 clips {len(clips)} (expected {STREAMS * 2}), peak error max {max(errs, default=0):.1f}s, error events: "
               f"{[e['message'][:80] for e in db.rows(p.conn.execute(chr(83) + 'ELECT message FROM events WHERE level=' + chr(39) + 'error' + chr(39)))][:5]}")

    await p.stop()
    time.sleep(1)
    after = common.procs("ffmpeg", "streamlink", "ffprobe")
    out.append(f"2 processes before {before} after {after}")
    tp.terminate()
    cpus = [float(l.split('","')[1].strip('"\r\n ')) for l in (tp.stdout.read() or "").splitlines() if l.startswith('"') and '","' in l and l.split('","')[1].strip('"\r\n ').replace('.', '', 1).isdigit()]
    out.append(f"2 system CPU samples (5 s apart, whole machine, incl. my other load): {[round(c) for c in cpus]} avg {sum(cpus) / max(1, len(cpus)):.0f}%")
    print("\n".join(out))


asyncio.run(main())
