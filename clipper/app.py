"""Local web app: JSON API + the review GUI. Binds to 127.0.0.1 only."""
import asyncio
import json
import shutil
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from clipper import config, cutter, db, notify, platforms, poster, twitch_auth
from clipper.pipeline import Pipeline

WEB = Path(__file__).resolve().parent.parent / "web"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def create_app(pipeline_factory=Pipeline, start_loops=True) -> FastAPI:
    state = {}

    @asynccontextmanager
    async def lifespan(app):
        p = pipeline_factory()
        state["p"] = app.state.pipeline = p
        if start_loops:
            p.start()
        yield
        await p.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # DNS-rebinding guard: only answer to our own host names
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"error": "bad host"}, status_code=403)
        # CSRF guard: other websites can't add this header without a CORS preflight we never allow
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-clipper") != "1":
            return JSONResponse({"error": "missing X-Clipper header"}, status_code=403)
        return await call_next(request)

    def P() -> Pipeline:
        return state["p"]

    local = threading.local()

    def conn():
        # one connection per thread: sharing the pipeline's across worker threads corrupts reads (stress b1)
        path = P().db_path
        if getattr(local, "path", None) != path:
            local.conn, local.path = db.connect(path), path
        return local.conn

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "body must be JSON")
        if not isinstance(data, dict):
            raise HTTPException(400, "body must be a JSON object")
        return data

    # ---------- state ----------
    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/system")
    def system():
        """What a new user's machine has: drives the setup checklist in the GUI."""
        cfg = config.load()

        def found(exe):
            return bool(shutil.which(exe) or (Path(exe).is_file()))

        config.DATA.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(config.DATA).free
        hf = Path.home() / ".cache" / "huggingface" / "hub" / f"models--Systran--faster-whisper-{cfg['whisper_model']}"
        return {
            "ffmpeg": found(cfg["ffmpeg"]),
            "ffprobe": found(cutter.ffprobe_path(cfg["ffmpeg"])),
            "streamlink": found(cfg["streamlink"]),
            "disk_free_gb": round(free / 1e9, 1),
            "whisper_model": cfg["whisper_model"],
            "whisper_cached": hf.exists(),   # first use downloads it (75 to 500 MB)
            "data_dir": str(config.DATA),
            "platform": sys.platform,
        }

    @app.get("/api/state")
    def get_state():
        c = conn()
        streamers = db.rows(c.execute("""
            SELECT st.*, se.id AS session_id, se.started_at AS recording_since,
              (SELECT COUNT(*) FROM chunks ch WHERE ch.session_id = se.id) AS chunk_count,
              (SELECT COUNT(*) FROM clips cl WHERE cl.streamer_id = st.id) AS clip_count,
              (SELECT MAX(created_at) FROM clips cl WHERE cl.streamer_id = st.id) AS last_clip_at
            FROM streamers st LEFT JOIN sessions se ON se.streamer_id = st.id AND se.status = 'recording'
            ORDER BY st.live DESC, st.channel"""))
        counts = dict(c.execute("SELECT status, COUNT(*) FROM clips GROUP BY status").fetchall())
        day = time.time() - 86400
        cfg = config.load()
        return {
            "now": time.time(),
            "streamers": streamers,
            "counts": counts,
            "posted_24h": c.execute("SELECT COUNT(*) FROM clips WHERE status='posted' AND decided_at > ?", (day,)).fetchone()[0],
            "clips_24h": c.execute("SELECT COUNT(*) FROM clips WHERE created_at > ?", (day,)).fetchone()[0],
            "events": db.rows(c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 80")),
            "poll_seconds": cfg["poll_seconds"],
            "setup": {k: bool(cfg[k]) for k in ("discord_webhook_url", "postforme_api_key", "twitch_user_token", "youtube_api_key")},
        }

    # ---------- streamers ----------
    @app.post("/api/streamers", status_code=201)
    async def add_streamer(request: Request):
        data = await body(request)
        try:
            platform, channel, url = platforms.parse_channel_url(str(data.get("url", "")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        c = conn()
        if c.execute("SELECT 1 FROM streamers WHERE platform=? AND channel=?", (platform, channel)).fetchone():
            raise HTTPException(409, f"{channel} on {platform} is already on the list")
        sid = c.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES (?,?,?,?)",
                        (platform, channel, url, time.time())).lastrowid
        db.log(c, "config", f"added {channel} ({platform})", streamer_id=sid)
        asyncio.create_task(P().monitor_once())  # check it straight away, not in two minutes
        return dict(c.execute("SELECT * FROM streamers WHERE id=?", (sid,)).fetchone())

    def streamer_or_404(sid):
        row = conn().execute("SELECT * FROM streamers WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "no such streamer")
        return row

    @app.patch("/api/streamers/{sid}")
    async def patch_streamer(sid: int, request: Request):
        data = await body(request)
        streamer_or_404(sid)
        if not isinstance(data.get("enabled"), bool):
            raise HTTPException(400, "enabled must be true or false")
        conn().execute("UPDATE streamers SET enabled=? WHERE id=?", (int(data["enabled"]), sid))
        if not data["enabled"]:
            await P().stop_streamer(sid, "tracking paused")
        return {"ok": True}

    @app.delete("/api/streamers/{sid}")
    async def delete_streamer(sid: int):
        s = streamer_or_404(sid)
        await P().stop_streamer(sid, "removed")
        rec_root = (config.DATA / "rec").resolve()
        for r in conn().execute("SELECT dir FROM sessions WHERE streamer_id=?", (sid,)).fetchall():
            d = Path(r["dir"]).resolve()
            if d != rec_root and d.is_relative_to(rec_root):  # recordings only, never clips
                shutil.rmtree(d, ignore_errors=True)
        conn().execute("DELETE FROM streamers WHERE id=?", (sid,))
        db.log(conn(), "config", f"removed {s['channel']} ({s['platform']})")
        return {"ok": True}

    @app.post("/api/streamers/{sid}/stop")
    async def stop_recording(sid: int):
        streamer_or_404(sid)
        return {"stopped": await P().stop_streamer(sid)}

    @app.post("/api/streamers/{sid}/clip")
    async def clip_now(sid: int):
        streamer_or_404(sid)
        cid = await P().clip_now(sid)
        if cid is None:
            raise HTTPException(409, "not recording, or the clip could not be cut (see Activity)")
        return {"clip_id": cid}

    @app.post("/api/monitor/check")
    async def check_now():
        await P().monitor_once()
        return {"ok": True}

    # ---------- clips ----------
    def clip_view(row):
        d = dict(row)
        d["signals"] = json.loads(d["signals"] or "{}")
        d["video"] = f"/media/clips/{Path(d['path']).name}"
        d["thumb_url"] = f"/media/clips/{Path(d['thumb']).name}" if d.get("thumb") else None
        d["duration"] = round(d["end_ts"] - d["start_ts"], 1)
        post = conn().execute("SELECT * FROM posts WHERE clip_id=? ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone()
        d["post"] = None
        if post:
            p = dict(post)
            p["accounts"] = json.loads(p["accounts"])
            p["results"] = json.loads(p["results"]) if p["results"] else []
            d["post"] = p
        return d

    @app.get("/api/clips")
    def list_clips(status: str = "pending", limit: int = 200):
        q = "SELECT * FROM clips"
        args = []
        if status == "pending":
            q += " WHERE status='pending'"
        elif status == "done":
            q += " WHERE status IN ('posting','posted','failed')"
        elif status == "rejected":
            q += " WHERE status='rejected'"
        elif status != "all":
            raise HTTPException(400, "status must be pending, done, rejected or all")
        q += " ORDER BY " + ("score DESC, created_at DESC" if status == "pending" else "COALESCE(decided_at, created_at) DESC") + " LIMIT ?"
        args.append(max(1, min(limit, 500)))
        return [clip_view(r) for r in conn().execute(q, args).fetchall()]

    def clip_or_404(cid):
        row = conn().execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
        if not row:
            raise HTTPException(404, "no such clip")
        return row

    @app.get("/api/clips/{cid}")
    def get_clip(cid: int):
        return clip_view(clip_or_404(cid))

    @app.patch("/api/clips/{cid}")
    async def patch_clip(cid: int, request: Request):
        data = await body(request)
        clip_or_404(cid)
        if not isinstance(data.get("caption"), str) or len(data["caption"]) > 2200:
            raise HTTPException(400, "caption must be text up to 2200 characters")
        conn().execute("UPDATE clips SET caption=? WHERE id=?", (data["caption"], cid))
        return {"ok": True}

    @app.post("/api/clips/{cid}/approve")
    async def approve(cid: int, request: Request):
        data = await body(request)
        clip_or_404(cid)
        caption, accounts = data.get("caption"), data.get("accounts")
        if not isinstance(caption, str) or not caption.strip() or len(caption) > 2200:
            raise HTTPException(400, "write a caption (up to 2200 characters)")
        if not isinstance(accounts, list) or not all(isinstance(a, str) for a in accounts):
            raise HTTPException(400, "accounts must be a list of account ids")
        try:
            post_id = await P().approve(cid, caption.strip(), accounts)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"post_id": post_id}

    @app.post("/api/clips/{cid}/reject")
    async def reject(cid: int):
        clip_or_404(cid)
        n = conn().execute("UPDATE clips SET status='rejected', decided_at=? WHERE id=? AND status IN ('pending','failed')", (time.time(), cid)).rowcount
        if not n:
            raise HTTPException(409, "only clips waiting for review can be rejected")
        return {"ok": True}

    @app.post("/api/clips/{cid}/restore")
    async def restore(cid: int):
        clip_or_404(cid)
        n = conn().execute("UPDATE clips SET status='pending', decided_at=NULL WHERE id=? AND status='rejected'", (cid,)).rowcount
        if not n:
            raise HTTPException(409, "only rejected clips can go back to the queue")
        return {"ok": True}

    @app.get("/media/clips/{name}")
    def media(name: str):
        clips_dir = (config.DATA / "clips").resolve()
        path = (clips_dir / name).resolve()
        if path.parent != clips_dir or path.suffix not in (".mp4", ".jpg") or not path.exists():
            raise HTTPException(404, "not found")
        return FileResponse(path)

    # ---------- accounts + settings ----------
    @app.get("/api/accounts")
    async def accounts(refresh: bool = False):
        try:
            return await P().list_accounts(force=refresh)
        except poster.PostForMeError as e:
            raise HTTPException(502, f"Post for Me said {e.status}: {e.body[:200]}")

    @app.get("/api/settings")
    def get_settings():
        return config.public_view(config.load())

    @app.put("/api/settings")
    async def put_settings(request: Request):
        data = await body(request)
        # masked secrets come back as booleans from the GUI: those mean "unchanged"
        updates = {k: v for k, v in data.items() if not (k in config.SECRET_KEYS and isinstance(v, bool))}
        try:
            cfg = config.save(updates)
        except ValueError as e:
            raise HTTPException(400, str(e))
        changed = ", ".join(sorted(updates)) or "nothing"
        db.log(conn(), "config", f"settings saved: {changed}")
        P().accounts_cache = (0, [])
        return config.public_view(cfg)

    @app.post("/api/test/{service}")
    async def test_service(service: str):
        cfg = config.load()
        http = P().http
        if service == "discord":
            ok, detail = await notify.discord_send(http, cfg["discord_webhook_url"], "Stream Clipper is connected. Clips will arrive here.")
            return {"ok": ok, "detail": detail}
        if service == "postforme":
            try:
                accs = await P().list_accounts(force=True)
                return {"ok": True, "detail": f"{len(accs)} connected account(s): " + ", ".join(f"{a.get('platform')} @{a.get('username')}" for a in accs)}
            except Exception as e:
                return {"ok": False, "detail": str(e)[:300]}
        if service == "twitch":
            if not cfg["twitch_user_token"]:
                return {"ok": False, "detail": "Connect Twitch first"}
            login = await twitch_auth.validate_token(cfg["twitch_user_token"], http)
            return {"ok": bool(login), "detail": f"signed in as {login}" if login else "token expired or revoked: connect again"}
        if service == "streamlink":
            info = await platforms.probe_streamlink("https://www.youtube.com/@LofiGirl/live", cfg["streamlink"])
            return {"ok": info.error is None, "detail": "streamlink works" if info.error is None else info.error}
        raise HTTPException(404, "unknown service")

    # ---------- Twitch device login ----------
    @app.post("/api/twitch/device/start")
    async def twitch_start():
        cfg = config.load()
        if not cfg["twitch_client_id"]:
            raise HTTPException(400, "Save your Twitch client ID first")
        try:
            flow = await twitch_auth.start_device_flow(cfg["twitch_client_id"], P().http)
        except Exception as e:
            raise HTTPException(502, f"Twitch refused: {e}")
        state["twitch_flow"] = flow
        return {k: flow[k] for k in ("user_code", "verification_uri", "expires_in", "interval")}

    @app.post("/api/twitch/device/poll")
    async def twitch_poll():
        flow = state.get("twitch_flow")
        if not flow:
            raise HTTPException(400, "start the Twitch login first")
        cfg = config.load()
        try:
            tok = await twitch_auth.poll_device_token(cfg["twitch_client_id"], flow["device_code"], P().http)
        except twitch_auth.TwitchAuthError as e:
            state.pop("twitch_flow", None)
            return {"status": "failed", "detail": str(e)}
        if tok is None:
            return {"status": "pending"}
        login = await twitch_auth.validate_token(tok["access_token"], P().http)
        config.save({"twitch_user_token": tok["access_token"], "twitch_refresh_token": tok.get("refresh_token", ""), "twitch_login": login or ""})
        state.pop("twitch_flow", None)
        db.log(conn(), "config", f"Twitch connected as {login}")
        return {"status": "connected", "login": login}

    # ---------- GUI ----------
    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
