"""Synthetic media from ffmpeg lavfi: testsrc2 video, quiet noise with loud sine bursts."""
import subprocess


def _args(seconds, bursts, realtime=False):
    # volume expression: 1 inside a burst, 0 outside; noise carries the baseline, sine the burst
    gate = "+".join(f"between(t,{s},{s + d})" for s, d in bursts) or "0"
    re = ["-re"] if realtime else []
    return [*re, "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=15:duration={seconds}",
            *re, "-f", "lavfi", "-i", f"anoisesrc=amplitude=0.01:sample_rate=44100:duration={seconds}",
            *re, "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}",
            "-filter_complex", f"[2:a]volume='0.8*({gate})':eval=frame[s];[1:a][s]amix=inputs=2:duration=first:normalize=0[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-f", "mpegts"]


def make_ts(path, seconds, bursts=(), ffmpeg="ffmpeg"):
    r = subprocess.run([ffmpeg, "-v", "error", "-y", *_args(seconds, bursts), str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return str(path)


def source_cmd(seconds, bursts=(), ffmpeg="ffmpeg", realtime=True):
    """MPEG-TS on stdout. Realtime by default: segment file names have 1 s resolution, so a faster-than-realtime
    source would create several chunks inside one wall second and they would overwrite each other."""
    return [ffmpeg, "-v", "error", *_args(seconds, bursts, realtime), "pipe:1"]
