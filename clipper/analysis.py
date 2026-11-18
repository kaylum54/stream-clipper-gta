"""Scores every second of a chunk (audio loudness + chat rate + hype emotes), picks peaks, checks laughter with whisper."""
import re
import subprocess
import sys
from dataclasses import dataclass, field

import numpy as np

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
SR = 8000
MIN_CHAT_MESSAGES = 20
LAUGH_RE = re.compile(r"ha(ha)+|he(he)+|ja(ja)+|k{3,}|\blol+\b|\blmao+\b|[(\[*]\s*laugh(s|ter|ing)\s*[)\]*]", re.I)


@dataclass
class Moment:
    peak_ts: float
    score: float
    signals: dict = field(default_factory=dict)


def audio_db_per_second(path, ffmpeg="ffmpeg") -> np.ndarray:
    r = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"],
                       capture_output=True, creationflags=_NO_WINDOW)
    if r.returncode != 0 and not r.stdout:  # an undecodable file is an error, not a silent chunk
        raise RuntimeError(f"ffmpeg could not decode {path}: {r.stderr.decode(errors='replace')[:200]}")
    x = np.frombuffer(r.stdout[: len(r.stdout) // 2 * 2], dtype="<i2").astype(np.float64)
    n = len(x) // SR
    if n == 0:
        return np.zeros(0)
    rms = np.sqrt((x[: n * SR].reshape(n, SR) ** 2).mean(axis=1))
    return np.maximum(20 * np.log10(np.maximum(rms, 1e-9) / 32768), -90.0)


def robust_z(x: np.ndarray, floor: float = 1.0) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return x
    med = np.median(x)
    # scale floor: a flat or mostly-zero series (chat counts) must not blow up to infinity
    return (x - med) / max(1.4826 * np.median(np.abs(x - med)), floor)


def chat_series(events, start_ts, n):
    msgs, hype = np.zeros(n), np.zeros(n)
    for ts, h in events:
        i = int(ts - start_ts)
        if ts < start_ts or i >= n:
            continue
        msgs[i] += 1
        hype[i] += h
    return msgs, hype


def _smooth(x, w):
    # edge-padded so the first and last seconds are not dragged by zero padding
    lo = w // 2
    return np.convolve(np.pad(x, (lo, w - 1 - lo), mode="edge"), np.ones(w), mode="valid")


def score_series(audio_db, msgs=None, hype=None, weights=None):
    n = len(audio_db)
    chat_ok = msgs is not None and len(msgs) == n and msgs.sum() >= MIN_CHAT_MESSAGES
    w = dict(weights or {"audio": 1.0, "chat": 1.0, "hype": 0.7})
    if not chat_ok:
        w = {"audio": 1.5, "chat": 0.0, "hype": 0.0}
    zero = np.zeros(n)
    comp = {
        "audio": w["audio"] * np.clip(robust_z(_smooth(audio_db, 3) / 3), 0, 10),
        # centered 10 s window; chat reacts to a moment, so the window straddles the peak
        "chat": w["chat"] * np.clip(robust_z(_smooth(msgs, 10)), 0, 10) if chat_ok else zero,
        "hype": w["hype"] * np.clip(robust_z(_smooth(hype if hype is not None else zero, 10)), 0, 10) if chat_ok else zero,
    }
    return comp["audio"] + comp["chat"] + comp["hype"], comp


def pick_peaks(score, threshold, min_gap, max_n):
    chosen = []
    for i in np.argsort(-np.asarray(score), kind="stable"):
        if len(chosen) >= max_n or score[i] < threshold:
            break
        if all(abs(int(i) - c) >= min_gap for c in chosen):
            chosen.append(int(i))
    return sorted(chosen)


def find_moments(chunk_path, chunk_start_ts, chat_events, cfg):
    audio = audio_db_per_second(chunk_path, cfg.get("ffmpeg", "ffmpeg"))
    if len(audio) == 0:
        return []
    msgs, hype = chat_series(chat_events, chunk_start_ts, len(audio)) if chat_events else (None, None)
    score, comp = score_series(audio, msgs, hype)
    peaks = pick_peaks(score, cfg["score_threshold"], cfg["min_gap_seconds"], cfg["max_clips_per_chunk"])
    return [Moment(chunk_start_ts + i, float(score[i]),
                   {"audio": float(comp["audio"][i]), "chat": float(comp["chat"][i]),
                    "hype": float(comp["hype"][i]), "laugh": 0.0}) for i in peaks]


_models = {}


def laughter_and_transcript(media_path, start, duration, model_name="base", ffmpeg="ffmpeg"):
    try:
        # decode with ffmpeg and pass a float array: faster-whisper's own decode_audio is broken with the installed PyAV
        r = subprocess.run([ffmpeg, "-v", "error", "-ss", str(max(start, 0)), "-t", str(duration), "-i", str(media_path),
                            "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"], capture_output=True, creationflags=_NO_WINDOW)
        audio = np.frombuffer(r.stdout[: len(r.stdout) // 4 * 4], dtype="<f4")
        if r.returncode != 0 or audio.size == 0:
            return 0.0, ""
        if model_name not in _models:
            from faster_whisper import WhisperModel
            _models[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
        segs = [s.text.strip() for s in _models[model_name].transcribe(audio)[0]]
    except Exception:
        return 0.0, ""
    if not segs:
        return 0.0, ""
    laugh = sum(1 for t in segs if LAUGH_RE.search(t))
    return min(1.0, laugh / len(segs)), " ".join(segs)
