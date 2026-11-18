"""Read-only checks against a running Stream Clipper. python scripts/smoke.py [http://127.0.0.1:8765]
Prints PASS/FAIL lines; exit 1 on any FAIL. Sends nothing anywhere except the app itself."""
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipper import config  # noqa: E402

base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765").rstrip("/")
fails = 0


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))


with httpx.Client(base_url=base, timeout=15) as c:
    r = c.get("/")
    check("GUI page loads", r.status_code == 200 and "Stream Clipper" in r.text, r.status_code)
    for asset in ("/static/app.js", "/static/style.css"):
        check(f"{asset} served", c.get(asset).status_code == 200)
    check("health", c.get("/api/health").json().get("ok") is True)
    st = c.get("/api/state")
    check("state has streamers, counts, events", st.status_code == 200 and {"streamers", "counts", "events"} <= set(st.json()), st.status_code)
    s = c.get("/api/settings").text
    secrets = [v for k, v in config.load().items() if k in config.SECRET_KEYS and v]
    check("settings never contain a saved secret", not any(v in s for v in secrets))
    check("POST without X-Clipper refused", c.post("/api/streamers", json={"url": "x"}).status_code == 403)
    check("bad streamer link is a 400, not a 500", c.post("/api/streamers", json={"url": "nope"}, headers={"X-Clipper": "1"}).status_code == 400)
    check("bad clip filter is a 400", c.get("/api/clips?status=zzz").status_code == 400)
    check("media can't escape the clips folder", c.get("/media/clips/..%2Fconfig.json").status_code == 404)
    check("unknown clip is a 404", c.get("/api/clips/999999999").status_code == 404)
    check("foreign Host header refused", c.get("/api/state", headers={"Host": "evil.example"}).status_code == 403)

print(f"\n{'all passed' if not fails else f'{fails} failed'}")
sys.exit(1 if fails else 0)
