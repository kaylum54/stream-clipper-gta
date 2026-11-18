"""Live rehearsal: the real app against real live streams, short chunks, then a hand clip per stream.
python scripts/rehearse.py [minutes] [url ...]   Uses data-rehearsal/, port 8771. Posts nothing anywhere."""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / os.environ.get("REHEARSAL_DIR", "data-rehearsal")
os.environ["CLIPPER_DATA"] = str(DATA)
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 4
urls = sys.argv[2:] or ["https://kick.com/classybeef", "https://www.twitch.tv/gaules", "https://www.youtube.com/@LofiGirl/live"]

shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir()
(DATA / "config.json").write_text(json.dumps({"chunk_seconds": 60, "poll_seconds": 30, "public_url": "http://127.0.0.1:8771"}))

from clipper.app import create_app  # noqa: E402

server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=8771, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
H = {"X-Clipper": "1"}
c = httpx.Client(base_url="http://127.0.0.1:8771", timeout=600)
for _ in range(50):
    try:
        c.get("/api/health"); break
    except Exception:
        time.sleep(0.2)

t0 = time.time()
for u in urls:
    print("add", u, c.post("/api/streamers", json={"url": u}, headers=H).status_code, flush=True)

end = t0 + minutes * 60
while time.time() < end:
    time.sleep(30)
    st = c.get("/api/state").json()
    line = " | ".join(f"{s['channel']}: {'REC ' + str(s['chunk_count']) + ' chunks' if s['session_id'] else ('live' if s['live'] else 'off')}"
                      f"{' ERR ' + s['check_error'] if s['check_error'] else ''}" for s in st["streamers"])
    print(f"{int(time.time() - t0):4d}s  {line}  clips={st['clips_24h']}", flush=True)

st = c.get("/api/state").json()
for s in st["streamers"]:
    if s["session_id"]:
        t = time.time()
        r = c.post(f"/api/streamers/{s['id']}/clip", headers=H)
        print(f"hand clip {s['channel']}: {r.status_code} {r.json()} in {time.time() - t:.0f}s", flush=True)

for s in st["streamers"]:
    c.post(f"/api/streamers/{s['id']}/stop", headers=H)
time.sleep(70)  # two monitor passes: a stopped stream that is still live must stay stopped
st = c.get("/api/state").json()
print("after stop, still recording:", [s["channel"] for s in st["streamers"] if s["session_id"]] or "none", flush=True)
time.sleep(90)  # let the last chunks finish analysing

clips = c.get("/api/clips?status=all").json()
print(f"\n{len(clips)} clip(s):")
for k in clips:
    sig = k["signals"]
    print(f"  #{k['id']} {k['streamer_name']} ({k['platform']}) score {k['score']} dur {k['duration']}s "
          f"audio={sig.get('audio')} chat={sig.get('chat')} laugh={sig.get('laugh')} manual={sig.get('manual', 0)}\n"
          f"     {k['path']}\n     transcript: {(k['transcript'] or '')[:140]!r}")
print("\nevents:")
for e in reversed(c.get("/api/state").json()["events"]):
    print(f"  {time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['level']:5} {e['kind']:8} {e['message']}")
server.should_exit = True
time.sleep(3)
