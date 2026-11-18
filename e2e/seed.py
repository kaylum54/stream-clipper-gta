"""Fills a data folder with demo streamers, clips and events for the browser tests and screenshots.
python e2e/seed.py <data_dir>"""
import json
import math
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clipper import db  # noqa: E402

PEAK = 40  # seconds into each demo clip, like the real 40 s of build-up


def make_clip(out: Path, seconds: int, freq: int):
    # a 9:16 test clip with a tone that gets loud at the peak, like a real moment
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"testsrc2=size=360x640:rate=15:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    "-af", f"volume='if(between(t,{PEAK - 2},{PEAK + 2}),1,0.08)':eval=frame",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    "-movflags", "+faststart", str(out)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "2", "-i", str(out), "-frames:v", "1",
                    "-vf", "scale=360:-2", str(out.with_suffix(".jpg"))], check=True)


def curves(seconds: int, seed: int, with_chat: bool):
    """Per-second loudness (dB) and chat counts shaped like a real moment: a build-up, a peak, chat reacting a few seconds later."""
    audio, chat = [], []
    for t in range(seconds):
        wobble = 2.2 * math.sin(t * 1.7 + seed) + 1.3 * math.sin(t * 0.43 + seed * 2)
        build = max(0.0, 1 - abs(t - PEAK) / 14) * 16
        burst = 9 if abs(t - PEAK) <= 2 else 0
        audio.append(round(-34 + wobble + build + burst, 1))
        if with_chat:
            react = max(0.0, 1 - abs(t - (PEAK + 4)) / 9) * 22
            chat.append(max(0, int(3 + 1.5 * math.sin(t * 0.9 + seed) + react)))
        else:
            chat.append(0)
    return audio, chat


def seed(data_dir: Path, clips_seconds: int = 65):
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(data_dir / "clipper.db")
    now = time.time()
    clips_dir = data_dir / "clips"
    clips_dir.mkdir(exist_ok=True)
    rec_dir = data_dir / "rec" / "demo"
    rec_dir.mkdir(parents=True, exist_ok=True)

    ids = {}
    for platform, channel, url, live, title, cat, err in [
        ("twitch", "gaules", "https://www.twitch.tv/gaules", 1, "GTA VI day one", "Grand Theft Auto VI", None),
        ("kick", "classybeef", "https://kick.com/classybeef", 0, None, None, None),
        ("youtube", "@LofiGirl", "https://www.youtube.com/@LofiGirl/live", 0, None, None, "streamlink timed out after 40 s"),
    ]:
        ids[channel] = conn.execute(
            "INSERT INTO streamers(platform, channel, url, live, title, category, checked_at, check_error, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (platform, channel, url, live, title, cat, now - 50, err, now - 86400)).lastrowid
    sess = conn.execute("INSERT INTO sessions(streamer_id, started_at, dir, title, status) VALUES (?,?,?,?, 'recording')",
                        (ids["gaules"], now - 3700, str(rec_dir), "GTA VI day one")).lastrowid
    for i in range(6):
        conn.execute("INSERT INTO chunks(session_id, path, start_ts, analyzed) VALUES (?,?,?,?)",
                     (sess, str(rec_dir / f"chunk_{i}.ts"), now - 3700 + i * 600, int(i < 5)))

    # score = loudness + chat + emotes + 2 x laughter, the same sum the analyser makes
    demo = [("gaules", (3.6, 1.9, 0.9, 0.5), 660, "Ele caiu do prédio KKKKKK não acredito", "pending"),
            ("gaules", (2.9, 1.5, 0.7, 0.0), 520, "", "pending"),
            ("classybeef", (2.6, 0.0, 0.0, 0.5), 440, "no way no way no way", "pending"),
            ("gaules", (3.2, 1.8, 1.0, 0.0), 380, "", "posted"),
            ("classybeef", (4.2, 0.0, 0.0, 0.0), 300, "", "rejected"),
            ("gaules", (2.6, 1.3, 0.6, 0.15), 340, "", "failed")]
    for n, (channel, (audio_s, chat_s, hype_s, laugh), freq, transcript, status) in enumerate(demo):
        f = clips_dir / f"demo-{n}.mp4"
        if not f.exists():
            make_clip(f, clips_seconds, freq)
        score = round(audio_s + chat_s + hype_s + 2 * laugh, 1)
        created = now - 3400 + n * 420
        peak = created - 100
        audio, chat = curves(clips_seconds, n, with_chat=chat_s > 0)
        sig = {"audio": audio_s, "chat": chat_s, "hype": hype_s, "laugh": laugh,
               "peak_offset": PEAK, "curve": {"audio_db": audio, "chat": chat, "hype": [c // 4 for c in chat]}}
        platform = "twitch" if channel == "gaules" else "kick"
        decided = created + 300 if status != "pending" else None
        cid = conn.execute(
            "INSERT INTO clips(session_id, streamer_id, streamer_name, platform, peak_ts, start_ts, end_ts, score, signals, path, thumb, transcript, caption, status, created_at, decided_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sess, ids[channel], channel, platform, peak, peak - PEAK, peak + clips_seconds - PEAK, score,
             json.dumps(sig), str(f), str(f.with_suffix(".jpg")), transcript, f"{channel} #gta6 #gta #clips", status, created,
             decided)).lastrowid
        if status == "posted":
            conn.execute("INSERT INTO posts(clip_id, pfm_post_id, accounts, status, results, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                         (cid, "sp_demo", json.dumps(["spc_tt1", "spc_yt1"]), "partial",
                          json.dumps([{"account_id": "spc_tt1", "platform": "tiktok", "username": "tester_tt", "success": True, "error": None, "url": "https://www.tiktok.com/@tester_tt"},
                                      {"account_id": "spc_yt1", "platform": "youtube", "username": "tester_yt", "success": False, "error": "Daily upload limit reached", "url": None}]),
                          decided, decided + 100))
        if status == "failed":
            conn.execute("INSERT INTO posts(clip_id, pfm_post_id, accounts, status, results, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                         (cid, "sp_demo_2", json.dumps(["spc_tt1"]), "failed",
                          json.dumps([{"account_id": "spc_tt1", "platform": "tiktok", "username": "tester_tt", "success": False,
                                       "error": "Account needs reconnecting in Post for Me", "url": None}]),
                          decided, decided + 50))
    for ts, level, kind, msg in [(now - 3705, "info", "live", "gaules went live on twitch: GTA VI day one"),
                                 (now - 3700, "info", "record", "gaules: recording in 10 min chunks"),
                                 (now - 3420, "info", "chunk", "chunk chunk_0.ts: 2 moment(s)"),
                                 (now - 3400, "info", "clip", "gaules: new clip, score 7.4"),
                                 (now - 1740, "warn", "post", "clip 4: posted to 1 of 2 account(s)"),
                                 (now - 950, "error", "post", "clip 6 not posted: Account needs reconnecting in Post for Me"),
                                 (now - 60, "warn", "error", "@LofiGirl: live check failed: streamlink timed out after 40 s")]:
        conn.execute("INSERT INTO events(ts, level, kind, message, streamer_id) VALUES (?,?,?,?,NULL)", (ts, level, kind, msg))
    conn.close()


if __name__ == "__main__":
    seed(Path(sys.argv[1]))
    print("seeded", sys.argv[1])
