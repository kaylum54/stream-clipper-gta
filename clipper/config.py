"""Settings in data/config.json. Secrets never leave this module unmasked through the API."""
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("CLIPPER_DATA", ROOT / "data"))

SECRET_KEYS = {
    "twitch_client_secret", "twitch_user_token", "twitch_refresh_token",
    "kick_client_secret", "youtube_api_key", "discord_webhook_url", "postforme_api_key",
}

DEFAULTS = {
    "poll_seconds": 120,          # how often the monitor checks who is live
    "chunk_seconds": 600,         # the video's 10-minute chunks
    "quality": "720p60,720p,best",
    "preroll": 40,                # seconds before the peak ("35 to 45 seconds earlier")
    "postroll": 25,                # 65 s clips: TikTok Creator Rewards wants videos of at least 1 minute (docs/MONETISATION.md)
    "max_clips_per_chunk": 3,
    "min_gap_seconds": 90,        # two peaks closer than this become one clip
    "score_threshold": 3.0,       # combined robust z-score a second must reach
    "vertical": True,             # 1080x1920 with blurred background
    "use_whisper": True,
    "whisper_model": "base",
    "keep_chunks_hours": 3,
    "public_url": "http://127.0.0.1:8765",
    "default_caption": "{streamer} #gta6 #gta #clips",
    "default_accounts": [],       # Post for Me account ids ticked by default
    "hype_tokens": ["LUL", "LULW", "KEKW", "OMEGALUL", "LMAO", "LOL", "ICANT", "Pog", "PogChamp",
                    "POGGERS", "W", "CLIP", "+2", "WTF", "\U0001F602", "\U0001F923", "\U0001F480", "\U0001F525"],
    "streamlink": "",             # path override; empty means the venv's streamlink
    "ffmpeg": "ffmpeg",
    "twitch_client_id": "",
    "twitch_login": "",
    "kick_client_id": "",
    **{k: "" for k in SECRET_KEYS},
}


RANGES = {"poll_seconds": (30, 3600), "chunk_seconds": (10, 3600), "preroll": (1, 300), "postroll": (1, 300),
          "max_clips_per_chunk": (1, 20), "min_gap_seconds": (10, 3600), "keep_chunks_hours": (1, 720),
          "score_threshold": (0.5, 20)}


def config_path() -> Path:
    return DATA / "config.json"


def load() -> dict:
    cfg = dict(DEFAULTS)
    p = config_path()
    if p.exists():
        stored = json.loads(p.read_text(encoding="utf-8"))
        cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
    if not cfg["streamlink"]:
        venv = ROOT / ".venv" / "Scripts" / "streamlink.exe"
        cfg["streamlink"] = str(venv) if venv.exists() else (shutil.which("streamlink") or "streamlink")
    return cfg


def save(updates: dict) -> dict:
    """Merge updates into the stored file. Unknown keys are refused; types must match the default's type."""
    cfg = load()
    stored = {}
    p = config_path()
    if p.exists():
        stored = json.loads(p.read_text(encoding="utf-8"))
    for k, v in updates.items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown setting: {k}")
        want = type(DEFAULTS[k])
        if want is float and isinstance(v, int):
            v = float(v)
        if want is int and isinstance(v, float) and v.is_integer():
            v = int(v)
        if not isinstance(v, want) or (want is int and isinstance(v, bool)):
            raise ValueError(f"{k} must be {want.__name__}")
        if k in RANGES and not RANGES[k][0] <= v <= RANGES[k][1]:
            raise ValueError(f"{k} must be between {RANGES[k][0]} and {RANGES[k][1]}")
        stored[k] = v.strip() if isinstance(v, str) else v
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(stored, indent=2), encoding="utf-8")
    os.replace(tmp, p)  # atomic, so a crash never leaves half a config
    cfg.update(stored)
    return cfg


def public_view(cfg: dict) -> dict:
    """What the GUI may see: secrets become booleans."""
    return {k: (bool(v) if k in SECRET_KEYS else v) for k, v in cfg.items()}
