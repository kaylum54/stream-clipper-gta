"""Opens the GUI on the live rehearsal's clips (data-rehearsal/) without recording anything. Port 8772."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
env = dict(os.environ, CLIPPER_DATA=str(ROOT / "data-rehearsal"), CLIPPER_PORT="8772", PYTHONUTF8="1")
print("rehearsal clips on http://127.0.0.1:8772", flush=True)
subprocess.run([sys.executable, str(ROOT / "e2e" / "serve.py")], env=env)
