"""Wires the sections together: monitor -> recorder + chat -> analyser -> cutter -> Discord/review -> Post for Me."""
import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path

import httpx

from clipper import analysis, chat, config, cutter, db, notify, platforms, poster, recorder, twitch_auth

ANALYSE_EVERY = 10      # seconds between looks for finished chunks
RESULTS_EVERY = 30      # seconds between Post for Me result polls
CHUNK_GRACE = 5         # extra seconds after a chunk's post-roll is on disk
STALL_SECONDS = 180     # a recorder whose newest chunk has not grown for this long is stuck
USER_STOP = "stopped by you"
POST_DEADLINE = 24 * 3600  # a post still waiting for results after this is failed
MAX_ATTEMPTS = 3        # analysis tries per chunk before giving up
MIN_FREE_BYTES = 2 * 10**9  # below this no new recording starts
WARN_EVERY = 600        # seconds between repeats of the same warning


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:40] or "stream"


class Pipeline:
    def __init__(self, db_path=None, *, http: httpx.AsyncClient | None = None, recorder_cls=recorder.Recorder,
                 check_live=platforms.check_live, collect_chat=chat.collect_chat, source_cmd_for=None,
                 use_whisper: bool | None = None):
        self.db_path = db_path or (config.DATA / "clipper.db")
        self.conn = db.connect(self.db_path)
        self.http = http or httpx.AsyncClient(timeout=60)
        self.recorder_cls = recorder_cls
        self.check_live = check_live
        self.collect_chat = collect_chat
        self.source_cmd_for = source_cmd_for      # tests: streamer -> fake media command instead of streamlink
        self.use_whisper = use_whisper
        self.recorders: dict[int, recorder.Recorder] = {}   # session_id -> Recorder
        self.chat_stops: dict[int, asyncio.Event] = {}
        self.offline_strikes: dict[int, int] = {}            # streamer_id -> consecutive offline checks
        self.chat_buffer: list[tuple[int, float, int]] = []
        self.media_lock = asyncio.Semaphore(2)               # ffmpeg/whisper jobs at once; the laptop has no GPU
        self.tasks: list[asyncio.Task] = []
        self.accounts_cache: tuple[float, list] = (0, [])
        self.monitor_lock = asyncio.Lock()                   # overlapping monitor passes would double-start recorders
        self.twitch_lock = asyncio.Lock()                    # one Twitch token refresh at a time
        self.user_stopped: set[int] = set()                  # streamer ids the user stopped; skipped until seen offline
        self.poll_errors: set[str] = set()                   # result-poll error kinds already logged (cleared after a clean pass)
        self._warned: dict[tuple, float] = {}                # (kind, streamer_id) -> last time a rate-limited warning was logged
        self._swept = False                                  # first analyse pass registers every session's chunk files
        self._clip_locks: dict[int, asyncio.Lock] = {}       # one manual clip per streamer at a time
        self._cuts: list[tuple[int, float, asyncio.Future]] = []  # clips being cut: (session, peak_ts, future of clip id)
        self._probed: dict[str, float] = {}                  # durations probed off-loop, used by the next _register_chunks
        self._recover()

    # ---------- lifecycle ----------
    def _recover(self):
        """Sessions left 'recording' by a crash or restart are ended; their chunks still get analysed."""
        n = self.conn.execute("UPDATE sessions SET status='ended', ended_at=?, error='app restarted' "
                              "WHERE status='recording'", (time.time(),)).rowcount
        if n:
            db.log(self.conn, "record", f"{n} recording(s) interrupted by a restart; finished chunks will still be analysed", "warn")
        # a post interrupted mid-upload never reached Post for Me
        self.conn.execute("UPDATE posts SET status='failed', error='app restarted during upload', updated_at=? WHERE status='uploading'", (time.time(),))
        self.conn.execute("UPDATE clips SET status='failed' WHERE status='posting' AND id NOT IN (SELECT clip_id FROM posts WHERE status='submitted')")

    def _throttled(self, kind, streamer_id, every=WARN_EVERY) -> bool:
        """True when this warning was already logged within `every` seconds; otherwise records it as logged now."""
        now = time.time()
        if now - self._warned.get((kind, streamer_id), 0) < every:
            return True
        self._warned[(kind, streamer_id)] = now
        return False

    def _discord(self, cfg, content, streamer_id, what, **kw):
        """Discord never blocks the pipeline: send in the background, log a failure."""
        async def run():
            ok, detail = await notify.discord_send(self.http, cfg["discord_webhook_url"], content, **kw)
            if cfg["discord_webhook_url"] and not ok:
                db.log(self.conn, "discord", f"{what} not sent: {detail}", "warn", streamer_id)
        t = asyncio.create_task(run())
        self.tasks.append(t)
        t.add_done_callback(lambda t: self.tasks.remove(t) if t in self.tasks else None)

    def start(self):
        for coro in (self._monitor_loop(), self._analyse_loop(), self._results_loop(), self._cleanup_loop(), self._chat_flush_loop()):
            self.tasks.append(asyncio.create_task(coro))

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        for ev in self.chat_stops.values():
            ev.set()
        for sid in list(self.recorders):
            await asyncio.to_thread(self.recorders[sid].stop)
            self._end_session(sid, "app stopped")
        self._flush_chat()
        await self.http.aclose()

    async def _forever(self, every, fn, name):
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a loop must survive one bad pass
                db.log(self.conn, "error", f"{name}: {type(e).__name__}: {e}", "error")
            await asyncio.sleep(every() if callable(every) else every)

    # ---------- monitor ----------
    async def _monitor_loop(self):
        await self._forever(lambda: config.load()["poll_seconds"], self.monitor_once, "monitor")

    async def monitor_once(self):
        async with self.monitor_lock:
            await self._monitor_once()

    def _stalled(self, session) -> bool:
        rec = self.recorders.get(session["id"])
        if rec is None or rec.poll() is not None:
            return False
        chunks = recorder.list_chunks(session["dir"])
        try:
            newest = os.path.getmtime(chunks[-1][0]) if chunks else session["started_at"]
        except OSError:
            return False
        return time.time() - newest > STALL_SECONDS

    async def _monitor_once(self):
        cfg = config.load()
        streamers = db.rows(self.conn.execute("SELECT * FROM streamers WHERE enabled=1"))
        self._reap_recorders()
        if not streamers:
            return
        infos = await self.check_live(streamers, cfg, self.http)
        now = time.time()
        for s in streamers:
            info = infos.get(s["id"]) or platforms.LiveInfo(live=False, error="no result")
            was_live = bool(s["live"])
            if info.error and not info.live:  # a failed check says nothing about live/title: keep them
                self.conn.execute("UPDATE streamers SET checked_at=?, check_error=? WHERE id=?", (now, info.error, s["id"]))
            else:
                self.conn.execute("UPDATE streamers SET live=?, title=?, category=?, video_id=?, checked_at=?, check_error=? WHERE id=?",
                                  (int(info.live), info.title, info.category, info.video_id, now, info.error, s["id"]))
            active = self._active_session(s["id"])
            stalled = False
            if active and self._stalled(active):
                db.log(self.conn, "record", f"{s['channel']}: recording stalled (no new data for {STALL_SECONDS} s), restarting", "warn", s["id"])
                await self.stop_streamer(s["id"], "stalled")
                active, stalled = None, True  # the next pass starts a fresh session
            if info.live:
                self.offline_strikes.pop(s["id"], None)
                if not was_live:
                    db.log(self.conn, "live", f"{s['channel']} went live on {s['platform']}: {info.title or ''}", streamer_id=s["id"])
                if not active and not stalled and s["id"] not in self.user_stopped:
                    await self._start_session(s, info, cfg, announce=not was_live)
            elif info.error:
                db.log(self.conn, "error", f"{s['channel']}: live check failed: {info.error}", "warn", s["id"])
            else:
                self.user_stopped.discard(s["id"])
                if was_live:
                    db.log(self.conn, "offline", f"{s['channel']} is offline", streamer_id=s["id"])
                if active:
                    strikes = self.offline_strikes.get(s["id"], 0) + 1
                    self.offline_strikes[s["id"]] = strikes
                    if strikes >= 2:  # one flaky check must not cut a recording
                        await self.stop_streamer(s["id"], "stream went offline")

    def _active_session(self, streamer_id):
        return self.conn.execute("SELECT * FROM sessions WHERE streamer_id=? AND status='recording'", (streamer_id,)).fetchone()

    async def _start_session(self, s, info, cfg, announce=True):
        config.DATA.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(config.DATA).free
        if free < MIN_FREE_BYTES:
            if not self._throttled("disk", None):
                db.log(self.conn, "record", f"disk nearly full: {free / 1e9:.1f} GB free; not starting new recordings", "warn")
            return
        started = time.time()
        out_dir = config.DATA / "rec" / f"{s['platform']}-{slug(s['channel'])}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(started))}"
        out_dir.mkdir(parents=True, exist_ok=True)
        rec = self.recorder_cls(s["url"], out_dir, cfg["chunk_seconds"], cfg["quality"], cfg["streamlink"], cfg["ffmpeg"],
                                source_cmd=self.source_cmd_for(s) if self.source_cmd_for else None,
                                log_path=out_dir / "recorder.log")
        try:
            rec.start()
        except Exception as e:
            db.log(self.conn, "record", f"{s['channel']}: recorder failed to start: {e}", "error", s["id"])
            return
        sid = self.conn.execute("INSERT INTO sessions(streamer_id, started_at, dir, title) VALUES (?,?,?,?)",
                                (s["id"], started, str(out_dir), info.title)).lastrowid
        self.recorders[sid] = rec
        db.log(self.conn, "record", f"{s['channel']}: recording in {cfg['chunk_seconds'] // 60} min chunks", streamer_id=s["id"])
        stop = asyncio.Event()
        self.chat_stops[sid] = stop
        self.tasks.append(asyncio.create_task(self._run_chat(sid, s, info, cfg, stop)))
        if announce:  # only offline -> live, not every recorder restart
            self._discord(cfg, notify.live_message(s["channel"], s["platform"], info.title, s["url"]), s["id"], "live message")

    async def _run_chat(self, sid, s, info, cfg, stop):
        tokens = cfg["hype_tokens"]

        def on_message(ts, text):
            self.chat_buffer.append((sid, ts, chat.hype_count(text, tokens)))

        status = None
        for _ in range(5):  # Twitch user tokens last about 4 h, so a long stream needs a refresh mid-way
            if s["platform"] == "twitch":
                cfg = await self._fresh_twitch_token(cfg)
            try:
                status = await self.collect_chat(s["platform"], s["channel"], info, cfg, on_message, stop, self.http)
            except Exception as e:
                status = f"chat crashed: {type(e).__name__}: {e}"
            if stop.is_set() or status != "twitch auth failed" or not cfg.get("twitch_refresh_token"):
                break
        if status and status != "stopped" and not stop.is_set():
            db.log(self.conn, "record", f"{s['channel']}: chat not read ({status}); clips use audio only", "warn", s["id"])

    async def _fresh_twitch_token(self, cfg):
        """Validate the stored user token; refresh it if Twitch says it has expired. Twitch rotates refresh tokens,
        so read the saved config under a lock: a stream holding an old copy must not reuse a consumed token."""
        async with self.twitch_lock:
            cfg = config.load()
            if not cfg.get("twitch_user_token"):
                return cfg
            try:
                if await twitch_auth.validate_token(cfg["twitch_user_token"], self.http):
                    return cfg
                if not cfg.get("twitch_refresh_token"):
                    return cfg
                tok = await twitch_auth.refresh_user_token(cfg["twitch_client_id"], cfg["twitch_client_secret"],
                                                           cfg["twitch_refresh_token"], self.http)
                cfg = config.save({"twitch_user_token": tok["access_token"],
                                   "twitch_refresh_token": tok.get("refresh_token") or cfg["twitch_refresh_token"]})
                db.log(self.conn, "config", "Twitch chat token refreshed")
            except Exception as e:
                db.log(self.conn, "config", f"Twitch token refresh failed: {e}. Reconnect Twitch in Settings.", "warn")
            return cfg

    async def _chat_flush_loop(self):
        await self._forever(5, self._flush_async, "chat")

    async def _flush_async(self):
        self._flush_chat()

    def _flush_chat(self):
        buf, self.chat_buffer = self.chat_buffer, []
        if buf:
            self.conn.executemany("INSERT INTO chat_events(session_id, ts, hype) VALUES (?,?,?)", buf)

    def _reap_recorders(self):
        for sid, rec in list(self.recorders.items()):
            code = rec.poll()
            if code is not None:
                self._end_session(sid, None if code == 0 else f"recorder exited with code {code}")

    def _end_session(self, sid, reason):
        self.recorders.pop(sid, None)
        ev = self.chat_stops.pop(sid, None)
        if ev:
            ev.set()
        row = self.conn.execute("SELECT s.streamer_id, st.channel FROM sessions s JOIN streamers st ON st.id=s.streamer_id WHERE s.id=?", (sid,)).fetchone()
        self.conn.execute("UPDATE sessions SET status='ended', ended_at=?, error=? WHERE id=? AND status='recording'",
                          (time.time(), reason, sid))
        if row and reason and reason.startswith("recorder exited"):  # a crash loop would log every pass
            if not self._throttled("crash", row["streamer_id"]):
                db.log(self.conn, "record", f"{row['channel']}: recorder keeps stopping: {reason}", "warn", row["streamer_id"])
        elif row:
            db.log(self.conn, "record", f"{row['channel']}: recording stopped" + (f" ({reason})" if reason else ""), streamer_id=row["streamer_id"])

    async def stop_streamer(self, streamer_id, reason=USER_STOP):
        s = self._active_session(streamer_id)
        if not s:
            return False
        if reason == USER_STOP:
            self.user_stopped.add(streamer_id)
        rec = self.recorders.get(s["id"])
        if rec:
            await asyncio.to_thread(rec.stop)
        self._end_session(s["id"], reason)
        return True

    # ---------- chunks + analysis ----------
    async def _analyse_loop(self):
        await self._forever(ANALYSE_EVERY, self.analyse_once, "analyser")

    def _probe(self, path):
        d = self._probed.pop(str(path), None)
        return d if d is not None else cutter.probe_duration(path, cutter.ffprobe_path(config.load()["ffmpeg"]))

    def _register_chunks(self, session):
        """Chunks are one continuous stream, so each starts where the previous one ended. File names only have
        1 s resolution; chaining measured durations keeps the timeline gap-free and non-overlapping."""
        known = {r["path"]: dict(r) for r in self.conn.execute("SELECT * FROM chunks WHERE session_id=?", (session["id"],))}
        prev = None
        for path, named_start in recorder.list_chunks(session["dir"]):
            if path not in known:
                start = named_start
                if prev is not None:  # prev is finished: a newer chunk exists
                    if prev["duration"] is None:
                        try:
                            prev["duration"] = self._probe(prev["path"])
                            self.conn.execute("UPDATE chunks SET duration=? WHERE id=?", (prev["duration"], prev["id"]))
                        except Exception:
                            pass
                    if prev["duration"]:
                        start = prev["start_ts"] + prev["duration"]
                        if named_start - start > 3:  # a real wall-clock gap (stall, reconnect, dropped ads): follow the file name
                            start = named_start
                cid = self.conn.execute("INSERT OR IGNORE INTO chunks(session_id, path, start_ts) VALUES (?,?,?)",
                                        (session["id"], path, start)).lastrowid
                known[path] = {"id": cid, "path": path, "start_ts": start, "duration": None}
            prev = known[path]

    async def _register_chunks_async(self, session):
        """_register_chunks with the ffprobe calls moved off the event loop; the DB writes stay on it."""
        known = {r["path"]: r["duration"] for r in self.conn.execute("SELECT path, duration FROM chunks WHERE session_id=?", (session["id"],))}
        files = recorder.list_chunks(session["dir"])
        # a file needs its duration when a newer file exists and it is new to us or still has none
        todo = [p for (p, _), _n in zip(files, files[1:], strict=False) if known.get(p) is None]
        ffprobe = cutter.ffprobe_path(config.load()["ffmpeg"])

        def probe_all():
            out = {}
            for p in todo:
                try:
                    out[p] = cutter.probe_duration(p, ffprobe)
                except Exception:
                    pass
            return out
        if todo:
            self._probed.update(await asyncio.to_thread(probe_all))
        try:
            self._register_chunks(session)
        finally:
            for p in todo:
                self._probed.pop(p, None)

    async def analyse_once(self):
        cfg = config.load()
        if not self._swept:  # once: files left behind by a crash or restart, whatever state their session is in
            self._swept = True
            for sess in db.rows(self.conn.execute("SELECT * FROM sessions")):
                if Path(sess["dir"]).exists():
                    await self._register_chunks_async(sess)
        sessions = db.rows(self.conn.execute(
            "SELECT * FROM sessions WHERE status='recording' OR id IN (SELECT session_id FROM chunks WHERE analyzed=0 AND deleted=0)"))
        now = time.time()
        ready = []
        for sess in sessions:
            if Path(sess["dir"]).exists():
                await self._register_chunks_async(sess)
            chunks = db.rows(self.conn.execute("SELECT * FROM chunks WHERE session_id=? AND deleted=0 ORDER BY start_ts", (sess["id"],)))
            for i, ch in enumerate(chunks):
                if ch["analyzed"]:
                    continue
                nxt = chunks[i + 1] if i + 1 < len(chunks) else None
                ended = sess["status"] != "recording"
                # ready once the next chunk holds the post-roll, or the stream is over
                if not ended and not (nxt and now >= nxt["start_ts"] + cfg["postroll"] + CHUNK_GRACE):
                    continue
                ready.append(self._analyse_chunk(sess, ch, chunks, cfg))
        # media_lock keeps at most two running; each chunk's DB writes happen on this loop
        for r in await asyncio.gather(*ready, return_exceptions=True):
            if isinstance(r, Exception):
                db.log(self.conn, "error", f"analyser: {type(r).__name__}: {r}", "error")

    def _chunk_table(self, chunks, cfg):
        """(path, start, duration) for the cutter; the newest chunk may still be growing."""
        out = []
        for ch in chunks:
            dur = ch["duration"]
            if dur is None:
                try:
                    dur = cutter.probe_duration(ch["path"], cutter.ffprobe_path(cfg["ffmpeg"]))
                except Exception:
                    dur = max(0.0, time.time() - ch["start_ts"])
            out.append((ch["path"], ch["start_ts"], dur))
        return out

    async def _analyse_chunk(self, sess, ch, chunks, cfg):
        async with self.media_lock:
            try:
                if not ch["duration"]:
                    dur = await asyncio.to_thread(cutter.probe_duration, ch["path"], cutter.ffprobe_path(cfg["ffmpeg"]))
                    self.conn.execute("UPDATE chunks SET duration=? WHERE id=?", (dur, ch["id"]))
                    ch["duration"] = dur
                events = [(r[0], r[1]) for r in self.conn.execute(
                    "SELECT ts, hype FROM chat_events WHERE session_id=? AND ts BETWEEN ? AND ?",
                    (sess["id"], ch["start_ts"] - 60, ch["start_ts"] + (ch["duration"] or 0) + 60))]
                moments = await asyncio.to_thread(analysis.find_moments, ch["path"], ch["start_ts"], events, cfg)
                self.conn.execute("UPDATE chunks SET analyzed=1, error=NULL WHERE id=?", (ch["id"],))
                db.log(self.conn, "chunk", f"chunk {Path(ch['path']).name}: {len(moments)} moment(s)", streamer_id=sess["streamer_id"])
            except Exception as e:
                # a missing ffmpeg or a locked file may be fixed in a minute: keep the chunk for the next pass
                tries = (ch.get("attempts") or 0) + 1
                final = tries >= MAX_ATTEMPTS
                self.conn.execute("UPDATE chunks SET attempts=?, error=?, analyzed=? WHERE id=?", (tries, str(e)[:500], int(final), ch["id"]))
                name = Path(ch["path"]).name
                if final:
                    db.log(self.conn, "error", f"gave up on chunk {name} after {MAX_ATTEMPTS} tries: {e}", "error", sess["streamer_id"])
                else:
                    db.log(self.conn, "chunk", f"analysis of {name} failed (try {tries} of {MAX_ATTEMPTS}), will retry: {e}", "warn", sess["streamer_id"])
                return
        for m in moments:
            await self.make_clip(sess, m.peak_ts, m.score, m.signals, cfg)

    async def make_clip(self, sess, peak_ts, score, signals, cfg, start_ts=None, end_ts=None):
        # a second call for the same moment while the first is still cutting waits for it and shares its clip id
        for sid, peak, fut in self._cuts:
            if sid == sess["id"] and abs(peak - peak_ts) < cfg["min_gap_seconds"] / 2:
                return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        entry = (sess["id"], peak_ts, fut)
        self._cuts.append(entry)
        result = None
        try:
            result = await self._make_clip(sess, peak_ts, score, signals, cfg, start_ts, end_ts)
            return result
        finally:
            self._cuts.remove(entry)
            fut.set_result(result)

    async def _make_clip(self, sess, peak_ts, score, signals, cfg, start_ts=None, end_ts=None):
        streamer = self.conn.execute("SELECT * FROM streamers WHERE id=?", (sess["streamer_id"],)).fetchone()
        start_ts = peak_ts - cfg["preroll"] if start_ts is None else start_ts
        end_ts = peak_ts + cfg["postroll"] if end_ts is None else end_ts
        dup = self.conn.execute("SELECT id FROM clips WHERE session_id=? AND ABS(peak_ts-?) < ?",
                                (sess["id"], peak_ts, cfg["min_gap_seconds"] / 2)).fetchone()
        if dup:
            return dup["id"]
        chunks = db.rows(self.conn.execute("SELECT * FROM chunks WHERE session_id=? AND deleted=0 ORDER BY start_ts", (sess["id"],)))
        clips_dir = config.DATA / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        name = f"{streamer['platform']}-{slug(streamer['channel'])}-s{sess['id']}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(peak_ts))}"
        out = clips_dir / f"{name}.mp4"
        thumb = clips_dir / f"{name}.jpg"
        async with self.media_lock:
            try:
                table = await asyncio.to_thread(self._chunk_table, chunks, cfg)
                await asyncio.to_thread(cutter.cut_clip, table, start_ts, end_ts, out, cfg["vertical"], cfg["ffmpeg"])
                clip_len = await asyncio.to_thread(cutter.probe_duration, out, cutter.ffprobe_path(cfg["ffmpeg"]))
                peak_in_clip = max(0.0, min(clip_len, peak_ts - max(start_ts, table[0][1] if table else start_ts)))
                await asyncio.to_thread(cutter.make_thumb, out, thumb, peak_in_clip, cfg["ffmpeg"])
                audio_curve = (await asyncio.to_thread(analysis.audio_db_per_second, out, cfg["ffmpeg"])).round(1).tolist()
                laugh, transcript = 0.0, ""
                use_whisper = cfg["use_whisper"] if self.use_whisper is None else self.use_whisper
                if use_whisper:
                    laugh, transcript = await asyncio.to_thread(
                        analysis.laughter_and_transcript, out, max(0.0, peak_in_clip - 20), 40, cfg["whisper_model"], cfg["ffmpeg"])
            except Exception as e:
                db.log(self.conn, "error", f"{streamer['channel']}: clip failed: {e}", "error", streamer["id"])
                return None
        events = [(r[0], r[1]) for r in self.conn.execute(
            "SELECT ts, hype FROM chat_events WHERE session_id=? AND ts BETWEEN ? AND ?", (sess["id"], start_ts, end_ts + 1))]
        n = len(audio_curve)
        msgs, hype = analysis.chat_series(events, start_ts, n) if n else ([], [])
        signals = dict(signals or {}, laugh=round(laugh, 2))
        score = float(score) + 2.0 * laugh  # laughter in the speech lifts a clip up the queue
        sig = {**signals, "peak_offset": round(peak_in_clip, 1),
               "curve": {"audio_db": audio_curve, "chat": [int(x) for x in msgs], "hype": [int(x) for x in hype]}}
        caption = cfg["default_caption"].replace("{streamer}", streamer["channel"])
        cid = self.conn.execute(
            "INSERT INTO clips(session_id, streamer_id, streamer_name, platform, peak_ts, start_ts, end_ts, score, signals, path, thumb, transcript, caption, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sess["id"], streamer["id"], streamer["channel"], streamer["platform"], peak_ts, start_ts, end_ts, round(score, 2),
             json.dumps(sig), str(out), str(thumb), transcript, caption, time.time())).lastrowid
        db.log(self.conn, "clip", f"{streamer['channel']}: new clip, score {score:.1f}", streamer_id=streamer["id"])
        self._discord(cfg, notify.clip_message(streamer["channel"], streamer["platform"], score, transcript, f"{cfg['public_url']}/#clip-{cid}"),
                      streamer["id"], "clip message", file_path=str(out), max_file_mb=20)  # Discord's per-file limit is 20 MiB (docs, checked 16 Nov 2026)
        return cid

    async def clip_now(self, streamer_id, lag: float = 8.0):
        """Your own 'clip that' button: the last preroll+postroll seconds, ending a few seconds ago."""
        async with self._clip_locks.setdefault(streamer_id, asyncio.Lock()):  # a double click waits, then finds the first clip
            cfg = config.load()
            sess = self._active_session(streamer_id)
            if not sess:
                return None
            sess = dict(sess)
            recent = self.conn.execute("SELECT id FROM clips WHERE session_id=? AND created_at > ? AND signals LIKE '%\"manual\": 1%' ORDER BY id DESC",
                                       (sess["id"], time.time() - 20)).fetchone()
            if recent:
                return recent["id"]
            await self._register_chunks_async(sess)
            end = time.time() - lag  # the ffmpeg pipe lags the live edge
            start = end - (cfg["preroll"] + cfg["postroll"])
            return await self.make_clip(sess, end - cfg["postroll"], 0.0, {"manual": 1}, cfg, start, end)

    # ---------- posting ----------
    async def list_accounts(self, force=False):
        cfg = config.load()
        if not cfg["postforme_api_key"]:
            return []
        if not force and time.time() - self.accounts_cache[0] < 300:
            return self.accounts_cache[1]
        accounts = await poster.PostForMe(cfg["postforme_api_key"], self.http, self._pfm_base()).list_accounts()
        self.accounts_cache = (time.time(), accounts)
        return accounts

    def _pfm_base(self):
        import os
        return os.environ.get("POSTFORME_BASE", "https://api.postforme.dev")  # tests point this at the mock

    async def approve(self, clip_id, caption: str, account_ids: list[str]):
        cfg = config.load()
        if not cfg["postforme_api_key"]:
            raise ValueError("Add your Post for Me API key in Settings first")
        if not account_ids:
            raise ValueError("Pick at least one account")
        clip = self.conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        if not clip:
            raise KeyError(clip_id)
        # claim the clip atomically so a double click can't post twice
        claimed = self.conn.execute("UPDATE clips SET status='posting', caption=?, decided_at=? WHERE id=? AND status IN ('pending','failed')",
                                    (caption, time.time(), clip_id)).rowcount
        if not claimed:
            raise ValueError(f"clip is already {clip['status']}")
        now = time.time()
        pid = self.conn.execute("INSERT INTO posts(clip_id, accounts, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                                (clip_id, json.dumps(account_ids), "uploading", now, now)).lastrowid
        self.tasks.append(asyncio.create_task(self._post(pid, dict(clip), caption, account_ids, cfg)))
        return pid

    async def _post(self, pid, clip, caption, account_ids, cfg):
        pfm = poster.PostForMe(cfg["postforme_api_key"], self.http, self._pfm_base())
        ext = f"clip-{clip['id']}"
        try:
            title = caption.split("#")[0].strip() or clip["streamer_name"]
            # an earlier attempt may have created the post (timeout, crash): look before making a second one.
            # posts whose results are already settled are not reused, or a retry of a failed post would never send.
            settled = {r[0] for r in self.conn.execute(
                "SELECT pfm_post_id FROM posts WHERE clip_id=? AND status IN ('done','partial','failed') AND pfm_post_id IS NOT NULL", (clip["id"],))}
            post = await pfm.find_by_external_id(ext, ignore=settled)
            if not post:
                media_url = await pfm.upload(clip["path"])
                try:
                    post = await pfm.create_post(caption, account_ids, media_url, poster.default_platform_configurations(title), ext)
                except poster.PostForMeError as e:
                    if e.status is not None:
                        raise
                    # network error: Post for Me may have created it. The retry looks it up by external_id.
                    msg = "not sure if it posted: will check before retrying"
                    self.conn.execute("UPDATE posts SET status='unknown', error=?, updated_at=? WHERE id=?", (msg, time.time(), pid))
                    self.conn.execute("UPDATE clips SET status='failed' WHERE id=?", (clip["id"],))
                    db.log(self.conn, "post", f"clip {clip['id']}: {msg} ({e})", "warn", clip["streamer_id"])
                    return
            self.conn.execute("UPDATE posts SET pfm_post_id=?, status='submitted', updated_at=? WHERE id=?", (post.get("id"), time.time(), pid))
            db.log(self.conn, "post", f"clip {clip['id']} sent to Post for Me ({len(account_ids)} account(s))", streamer_id=clip["streamer_id"])
        except Exception as e:
            self.conn.execute("UPDATE posts SET status='failed', error=?, updated_at=? WHERE id=?", (str(e)[:500], time.time(), pid))
            self.conn.execute("UPDATE clips SET status='failed' WHERE id=?", (clip["id"],))
            db.log(self.conn, "post", f"clip {clip['id']} not posted: {e}", "error", clip["streamer_id"])

    async def _results_loop(self):
        await self._forever(RESULTS_EVERY, self.results_once, "post results")

    def _give_up_on(self, p, why):
        self.conn.execute("UPDATE posts SET status='failed', error=?, updated_at=? WHERE id=?", (why, time.time(), p["id"]))
        self.conn.execute("UPDATE clips SET status='failed' WHERE id=?", (p["clip_id"],))
        db.log(self.conn, "post", f"clip {p['clip_id']}: {why}", "error")

    async def results_once(self):
        cfg = config.load()
        pending = db.rows(self.conn.execute("SELECT * FROM posts WHERE status='submitted'"))
        if not pending or not cfg["postforme_api_key"]:
            return
        pfm = poster.PostForMe(cfg["postforme_api_key"], self.http, self._pfm_base())
        try:
            accounts = {a["id"]: a for a in await self.list_accounts()}
        except Exception:
            accounts = {}
        expired = lambda p: time.time() - p["created_at"] > POST_DEADLINE
        clean = True
        for p in pending:
            try:
                results = await pfm.get_results(p["pfm_post_id"])
            except Exception as e:  # one bad post must not stop the ones after it
                clean = False
                kind = f"{type(e).__name__} {getattr(e, 'status', '')}"
                if kind not in self.poll_errors:
                    self.poll_errors.add(kind)
                    db.log(self.conn, "post", f"result check failed for clip {p['clip_id']}: {e}", "warn")
                if expired(p):
                    self._give_up_on(p, "no result from Post for Me after 24 h")
                continue
            summary = poster.summarize_results([r for r in results if r.get("post_id") in (None, p["pfm_post_id"])],
                                               {a: accounts.get(a, {"id": a}) for a in json.loads(p["accounts"])})
            done = bool(summary) and all(s["success"] is not None for s in summary)
            ok = sum(1 for s in summary if s["success"])
            status = ("done" if ok == len(summary) else "partial" if ok else "failed") if done else "submitted"
            self.conn.execute("UPDATE posts SET results=?, status=?, updated_at=? WHERE id=?", (json.dumps(summary), status, time.time(), p["id"]))
            if done:
                # at least one account got it: 'posted' (retrying would send it again to those); none did: 'failed', retry allowed
                self.conn.execute("UPDATE clips SET status=? WHERE id=?", ("posted" if ok else "failed", p["clip_id"]))
                db.log(self.conn, "post", f"clip {p['clip_id']}: posted to {ok} of {len(summary)} account(s)", "info" if ok == len(summary) else "warn")
            elif expired(p):
                self._give_up_on(p, "no result from Post for Me after 24 h")

        if clean:  # after a good pass the next outage is news again
            self.poll_errors.clear()

    # ---------- cleanup ----------
    async def _cleanup_loop(self):
        await self._forever(600, self.cleanup_once, "cleanup")

    async def cleanup_once(self):
        cfg = config.load()
        cutoff = time.time() - cfg["keep_chunks_hours"] * 3600
        old = db.rows(self.conn.execute("SELECT id, path FROM chunks WHERE analyzed=1 AND deleted=0 AND start_ts < ?", (cutoff,)))
        gone = 0
        for ch in old:
            try:  # one locked file must not stop the rest
                Path(ch["path"]).unlink(missing_ok=True)
            except OSError as e:
                db.log(self.conn, "chunk", f"could not delete {Path(ch['path']).name}: {e}", "warn")
                continue
            self.conn.execute("UPDATE chunks SET deleted=1 WHERE id=?", (ch["id"],))
            gone += 1
        if gone:
            db.log(self.conn, "chunk", f"deleted {gone} analysed chunk(s) older than {cfg['keep_chunks_hours']} h")
        self.conn.execute("DELETE FROM chat_events WHERE ts < ?", (cutoff - 3600,))
        self.conn.execute("DELETE FROM events WHERE ts < ?", (time.time() - 30 * 86400,))
