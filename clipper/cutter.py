"""Cuts a clip out of consecutive recorded chunks, optionally as 1080x1920 with a blurred backdrop."""
import subprocess
import sys
import tempfile
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

VERTICAL_FILTER = ("[0:v]split[a][b];"
                   "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];"
                   "[b]scale=1080:-2[fg];"
                   "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]")


def ffprobe_path(ffmpeg: str) -> str:
    """Swap only the file name (ffmpeg -> ffprobe), so a folder called ffmpeg stays as it is."""
    i = max(ffmpeg.rfind("/"), ffmpeg.rfind("\\")) + 1
    return ffmpeg[:i] + ffmpeg[i:].replace("ffmpeg", "ffprobe")


class CutError(Exception):
    pass


def probe_duration(path, ffprobe="ffprobe") -> float:
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True, creationflags=_NO_WINDOW)
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise CutError(f"cannot probe {path}: {r.stderr[-300:]}")


def window_parts(chunks, start_ts, end_ts):
    parts = []
    for path, cs, dur in sorted(chunks, key=lambda c: c[1]):
        lo, hi = max(start_ts, cs), min(end_ts, cs + dur)
        if hi > lo:
            parts.append((path, lo - cs, hi - cs))
    return parts


def _quote(path) -> str:
    # concat demuxer: forward slashes work on Windows; a single quote is written as '\''
    return "'" + str(path).replace("\\", "/").replace("'", "'\\''") + "'"


def cut_clip(chunks, start_ts, end_ts, out_path, vertical=True, ffmpeg="ffmpeg") -> str:
    parts = window_parts(chunks, start_ts, end_ts)
    if not parts:
        raise CutError("no recorded media covers that window")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        lst = Path(d) / "list.txt"
        lst.write_text("".join(f"file {_quote(p)}\ninpoint {a:.3f}\noutpoint {b:.3f}\n" for p, a, b in parts), encoding="utf-8")
        cmd = [ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst)]
        cmd += ["-filter_complex", VERTICAL_FILTER, "-map", "[v]", "-map", "0:a?"] if vertical else ["-pix_fmt", "yuv420p"]
        # shortcut: concat inpoint starts at the keyframe before it, so a clip can begin up to one GOP early; -t caps the length.
        # Upgrade to a per-part trim filter if exact starts matter.
        cmd += ["-t", f"{sum(b - a for _, a, b in parts):.3f}"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(out_path)]
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if r.returncode != 0 or not out_path.exists():
        raise CutError(r.stderr[-800:] or "ffmpeg failed")
    return str(out_path)


def make_thumb(clip_path, out_path, at, ffmpeg="ffmpeg") -> str:
    r = subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", str(at), "-i", str(clip_path), "-frames:v", "1",
                        "-vf", "scale=360:-2", str(out_path)], capture_output=True, text=True, creationflags=_NO_WINDOW)
    if r.returncode != 0 or not Path(out_path).exists():
        raise CutError(r.stderr[-800:] or "thumbnail failed")
    return str(out_path)
