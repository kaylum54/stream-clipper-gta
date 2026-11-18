import json
import sqlite3

import pytest

from clipper import config, db


def test_secrets_are_masked_in_public_view(cfg):
    # bug caught: the settings API leaking a key to the browser
    cfg = config.save({"postforme_api_key": "pfm_live_abc", "discord_webhook_url": "https://discord.com/api/webhooks/1/x"})
    view = config.public_view(cfg)
    assert view["postforme_api_key"] is True and view["discord_webhook_url"] is True
    assert "pfm_live_abc" not in json.dumps(view)
    assert view["youtube_api_key"] is False


def test_save_refuses_unknown_keys_and_wrong_types(cfg):
    # bug caught: the GUI writing junk (or a string chunk length) that crashes the recorder later
    with pytest.raises(ValueError):
        config.save({"rm_rf": True})
    with pytest.raises(ValueError):
        config.save({"chunk_seconds": "600"})
    with pytest.raises(ValueError):
        config.save({"chunk_seconds": True})
    assert config.save({"chunk_seconds": 300.0})["chunk_seconds"] == 300


def test_save_persists_and_trims(cfg, data_dir):
    config.save({"twitch_client_id": "  abc  "})
    assert config.load()["twitch_client_id"] == "abc"
    assert json.loads((data_dir / "config.json").read_text())["twitch_client_id"] == "abc"


def test_schema_unique_streamer_and_cascade(conn):
    # bug caught: the same channel added twice, so it's recorded twice
    conn.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch','a','u',0)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch','a','u',0)")
    db.log(conn, "config", "hello")
    assert db.rows(conn.execute("SELECT kind, message FROM events")) == [{"kind": "config", "message": "hello"}]
