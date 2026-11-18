import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Points the app at a throwaway data folder so tests never touch data/."""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("CLIPPER_DATA", str(d))
    import clipper.config as config
    monkeypatch.setattr(config, "DATA", d)
    return d


@pytest.fixture
def conn(data_dir):
    from clipper import db
    c = db.connect(data_dir / "clipper.db")
    yield c
    c.close()


@pytest.fixture
def cfg(data_dir):
    from clipper import config
    return config.load()
