"""The GUI with demo data and mock Post for Me/Discord, for looking at it: python e2e/demo.py  (port 8770)"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e2e.seed import seed  # noqa: E402
from tests.mock_services import MockServices  # noqa: E402

if __name__ == "__main__":
    d = ROOT / "data-demo"
    shutil.rmtree(d, ignore_errors=True)
    seed(d)
    (d / "config.json").write_text(json.dumps({"postforme_api_key": "test-key"}))
    with MockServices() as m:
        env = dict(os.environ, CLIPPER_DATA=str(d), CLIPPER_PORT=os.environ.get("PORT", "8770"), POSTFORME_BASE=m.base_url, PYTHONUTF8="1")
        print(f"demo on http://127.0.0.1:{env['CLIPPER_PORT']}", flush=True)
        subprocess.run([sys.executable, str(ROOT / "e2e" / "serve.py")], env=env)
