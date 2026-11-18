import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from e2e.seed import seed  # noqa: E402
from tests.mock_services import MockServices  # noqa: E402

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mock():
    with MockServices() as m:
        yield m


def start_server(data_dir: Path, mock_url: str):
    port = free_port()
    env = dict(os.environ, CLIPPER_DATA=str(data_dir), CLIPPER_PORT=str(port), POSTFORME_BASE=mock_url, PYTHONUTF8="1")
    proc = subprocess.Popen([PY, str(ROOT / "e2e" / "serve.py")], env=env, cwd=ROOT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            return proc, base
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("app did not start")


@pytest.fixture(scope="session")
def seeded(tmp_path_factory, mock):
    d = tmp_path_factory.mktemp("seeded")
    seed(d)
    (d / "config.json").write_text(json.dumps({"postforme_api_key": "test-key"}))
    proc, base = start_server(d, mock.base_url)
    yield base
    proc.terminate()
    proc.wait(10)


@pytest.fixture(scope="session")
def fresh(tmp_path_factory, mock):
    d = tmp_path_factory.mktemp("fresh")
    proc, base = start_server(d, mock.base_url)
    yield base
    proc.terminate()
    proc.wait(10)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    # 4xx replies the UI shows as messages are expected; anything else in the console is a bug
    expected = ("fonts.g", "status of 400", "status of 409")
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" and not any(x in m.text for x in expected) else None)
    yield pg
    ctx.close()
