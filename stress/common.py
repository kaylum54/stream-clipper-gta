"""Shared helpers for the stress scenarios. Import and call init() BEFORE importing anything from clipper:
clipper.config reads CLIPPER_DATA once at import, so every scenario runs against a throwaway folder."""
import asyncio
import ctypes
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def init(name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"stress-{name}-"))
    os.environ["CLIPPER_DATA"] = str(d)
    return d


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def lat_summary(xs):
    return f"n={len(xs)} p50={pct(xs, 50) * 1000:.0f}ms p95={pct(xs, 95) * 1000:.0f}ms p99={pct(xs, 99) * 1000:.0f}ms max={max(xs, default=0) * 1000:.0f}ms"


# ---- process / memory probes, no psutil ----
class _PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def rss_mb() -> float:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ps = ctypes.WinDLL("psapi", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    ps.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    c = _PMC()
    c.cb = ctypes.sizeof(c)
    ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.WorkingSetSize / 1048576


def peak_rss_mb() -> float:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ps = ctypes.WinDLL("psapi", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    ps.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    c = _PMC()
    c.cb = ctypes.sizeof(c)
    ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.PeakWorkingSetSize / 1048576


def handle_count() -> int:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    n = wintypes.DWORD()
    k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(n))
    return n.value


def procs(*names) -> dict[str, int]:
    """Count running processes by image name via tasklist."""
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout.lower()
    return {n: sum(1 for line in out.splitlines() if line.startswith(f'"{n.lower()}.exe"')) for n in names}


class LoopLag:
    """Measures how long the asyncio loop was blocked: a ticker that wants to run every 20 ms."""

    def __init__(self):
        self.gaps, self._t = [], None

    async def _run(self):
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            self.gaps.append(now - last - 0.02)
            last = now

    def __enter__(self):
        self._t = asyncio.get_running_loop().create_task(self._run())
        return self

    def __exit__(self, *a):
        self._t.cancel()

    @property
    def max_ms(self):
        return max(self.gaps, default=0) * 1000


def row(conn, q, *a):
    return conn.execute(q, a).fetchone()[0]
