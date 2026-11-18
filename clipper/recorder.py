"""Records a live stream into fixed-length MPEG-TS chunks: streamlink | ffmpeg segment muxer."""
import subprocess
import sys
import time
from pathlib import Path

CHUNK_PREFIX = "chunk_"
_FMT = "%Y%m%d-%H%M%S"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _kill_tree(p) -> None:
    # Windows launchers (pip's streamlink.exe, Chocolatey's ffmpeg shim) run the real program as a child;
    # killing only the launcher leaves the child holding the pipe open and ffmpeg never sees EOF.
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, creationflags=_NO_WINDOW)
    else:
        p.terminate()


_job = None  # one Job Object for the life of this process; closing the console closes it and Windows kills the children


def assign_to_job(proc) -> bool:
    """Windows: put a child in a kill-on-close Job Object so it dies with us. False elsewhere or on failure."""
    global _job
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    try:
        if _job is None:
            job = k32.CreateJobObjectW(None, None)
            info = Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9 = extended limits
                return False
            _job = job
        return bool(k32.AssignProcessToJobObject(_job, int(proc._handle)))
    except Exception:
        return False


def chunk_start_ts(path) -> float:
    name = Path(path).name
    if not (name.startswith(CHUNK_PREFIX) and name.endswith(".ts")):
        raise ValueError(f"not a chunk file: {name}")
    try:
        return time.mktime(time.strptime(name[len(CHUNK_PREFIX):-3], _FMT))  # local time
    except ValueError as e:
        raise ValueError(f"not a chunk file: {name}") from e


def list_chunks(out_dir) -> list[tuple[str, float]]:
    out = []
    for p in Path(out_dir).glob(CHUNK_PREFIX + "*.ts"):
        try:
            out.append((str(p), chunk_start_ts(p)))
        except ValueError:
            pass
    return sorted(out, key=lambda t: t[1])


class Recorder:
    def __init__(self, url, out_dir, chunk_seconds, quality, streamlink, ffmpeg="ffmpeg",
                 source_cmd=None, log_path=None):
        self.url, self.out_dir = url, Path(out_dir)
        self.chunk_seconds, self.quality = chunk_seconds, quality
        self.streamlink, self.ffmpeg = streamlink, ffmpeg
        self.source_cmd, self.log_path = source_cmd, log_path
        self._src = self._mux = self._log = None

    def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # --twitch-disable-ads was removed in streamlink 8.x, so it is not passed.
        src = self.source_cmd or [self.streamlink, "--stdout", "--stream-segment-threads", "2", self.url, self.quality]
        # shortcut: segment names have 1 s resolution, so chunk_seconds must be >= 10 (two files in one second collide); never go lower.
        mux = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-analyzeduration", "1000000", "-i", "pipe:0",
               "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "segment",
               "-segment_time", str(self.chunk_seconds), "-reset_timestamps", "1", "-strftime", "1",
               str(self.out_dir / (CHUNK_PREFIX + "%Y%m%d-%H%M%S.ts"))]
        self._log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        self._src = subprocess.Popen(src, stdout=subprocess.PIPE, stderr=self._log, creationflags=_NO_WINDOW)
        assign_to_job(self._src)
        try:
            self._mux = subprocess.Popen(mux, stdin=self._src.stdout, stdout=subprocess.DEVNULL,
                                         stderr=self._log, creationflags=_NO_WINDOW)
        except Exception:
            _kill_tree(self._src)  # nobody would stop the source otherwise, and the monitor retries every poll
            raise
        assign_to_job(self._mux)
        self._src.stdout.close()

    def poll(self):
        return self._mux.poll() if self._mux else None

    @property
    def running(self) -> bool:
        return self._mux is not None and self._mux.poll() is None

    def stop(self, timeout: float = 15) -> None:
        if self._src is None:
            return
        # End the source first: ffmpeg then sees EOF on stdin and flushes the last chunk.
        if self._src.poll() is None:
            _kill_tree(self._src)
            try:
                self._src.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._src.kill()
        try:
            self._mux.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._mux.kill()
            self._mux.wait()
        if self._log not in (None, subprocess.DEVNULL):
            self._log.close()
        self._log = None
