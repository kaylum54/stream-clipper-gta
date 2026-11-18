"""The API with a real pipeline object (loops off), real SQLite, and the local mock Post for Me / Discord."""
import time

import pytest
from fastapi.testclient import TestClient

from clipper import config
from clipper.app import create_app
from clipper.pipeline import Pipeline
from tests.mock_services import MockServices

H = {"X-Clipper": "1"}


async def no_live(streamers, cfg, http):
    from clipper.platforms import LiveInfo
    return {s["id"]: LiveInfo(live=False) for s in streamers}


@pytest.fixture
def client(data_dir):
    app = create_app(lambda: Pipeline(check_live=no_live, use_whisper=False), start_loops=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        yield c


@pytest.fixture
def mock(monkeypatch):
    with MockServices() as m:
        monkeypatch.setenv("POSTFORME_BASE", m.base_url)
        yield m


def seed_clip(data_dir, client, status="pending"):
    clips = data_dir / "clips"
    clips.mkdir(exist_ok=True)
    f = clips / "demo.mp4"
    f.write_bytes(b"\x00" * 4096)
    from clipper import db
    c = db.connect(data_dir / "clipper.db")
    sid = c.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch','demo','https://www.twitch.tv/demo',0)").lastrowid
    cid = c.execute("INSERT INTO clips(streamer_id, streamer_name, platform, peak_ts, start_ts, end_ts, score, path, caption, status, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (sid, "demo", "twitch", 100, 60, 120, 5.0, str(f), "hi", status, time.time())).lastrowid
    c.close()
    return cid


def test_mutations_need_the_csrf_header(client):
    # bug caught: any website the user visits could add streamers or approve posts through the browser
    r = client.post("/api/streamers", json={"url": "https://www.twitch.tv/gaules"})
    assert r.status_code == 403


def test_foreign_host_refused(client):
    # bug caught: DNS rebinding lets a remote page read /api/state as if it were local
    r = client.get("/api/state", headers={"Host": "evil.example"})
    assert r.status_code == 403


def test_add_streamer_validates_and_dedupes(client):
    # bug caught: junk URLs saved as streamers, or one channel recorded twice
    assert client.post("/api/streamers", json={"url": "https://example.com/x"}, headers=H).status_code == 400
    assert client.post("/api/streamers", json=["x"], headers=H).status_code == 400
    r = client.post("/api/streamers", json={"url": "https://www.twitch.tv/Gaules/"}, headers=H)
    assert r.status_code == 201 and r.json()["channel"] == "gaules"
    assert client.post("/api/streamers", json={"url": "twitch.tv/gaules"}, headers=H).status_code in (400, 409)
    assert client.post("/api/streamers", json={"url": "https://twitch.tv/gaules"}, headers=H).status_code == 409
    st = client.get("/api/state").json()
    assert [s["channel"] for s in st["streamers"]] == ["gaules"]


def test_pause_and_remove_streamer(client):
    sid = client.post("/api/streamers", json={"url": "https://kick.com/classybeef"}, headers=H).json()["id"]
    assert client.patch(f"/api/streamers/{sid}", json={"enabled": "no"}, headers=H).status_code == 400
    assert client.patch(f"/api/streamers/{sid}", json={"enabled": False}, headers=H).status_code == 200
    assert client.delete(f"/api/streamers/{sid}", headers=H).status_code == 200
    assert client.get("/api/state").json()["streamers"] == []


def test_settings_never_return_secrets_and_bools_mean_unchanged(client):
    # bug caught: GUI round-trip overwriting a saved key with "true", or the key showing up in the page
    r = client.put("/api/settings", json={"postforme_api_key": "pfm_secret_123", "preroll": 35}, headers=H)
    assert r.status_code == 200 and r.json()["postforme_api_key"] is True and "pfm_secret_123" not in r.text
    r = client.put("/api/settings", json={"postforme_api_key": True, "preroll": 45}, headers=H)
    assert config.load()["postforme_api_key"] == "pfm_secret_123" and config.load()["preroll"] == 45
    assert "pfm_secret_123" not in client.get("/api/settings").text
    assert client.put("/api/settings", json={"nope": 1}, headers=H).status_code == 400


def test_media_route_blocks_traversal(client, data_dir):
    # bug caught: /media serving any file on disk, e.g. data/config.json with the keys in it
    (data_dir / "config.json").write_text("{}")
    assert client.get("/media/clips/..%2Fconfig.json").status_code == 404
    assert client.get("/media/clips/config.json").status_code == 404


def test_reject_restore_state_machine(client, data_dir):
    cid = seed_clip(data_dir, client)
    assert client.post(f"/api/clips/{cid}/restore", headers=H).status_code == 409
    assert client.post(f"/api/clips/{cid}/reject", headers=H).status_code == 200
    assert client.get("/api/clips?status=pending").json() == []
    assert client.post(f"/api/clips/{cid}/restore", headers=H).status_code == 200
    assert [c["id"] for c in client.get("/api/clips").json()] == [cid]


def test_approve_needs_key_and_accounts(client, data_dir):
    # bug caught: approve silently doing nothing when Post for Me isn't set up
    cid = seed_clip(data_dir, client)
    r = client.post(f"/api/clips/{cid}/approve", json={"caption": "x", "accounts": ["spc_tt1"]}, headers=H)
    assert r.status_code == 409 and "Post for Me" in r.json()["detail"]
    config.save({"postforme_api_key": "test-key"})
    r = client.post(f"/api/clips/{cid}/approve", json={"caption": "x", "accounts": []}, headers=H)
    assert r.status_code == 409
    assert client.post(f"/api/clips/{cid}/approve", json={"caption": "", "accounts": ["a"]}, headers=H).status_code == 400


def test_approve_posts_once_and_results_land(client, data_dir, mock):
    # bug caught: a double click posting the same clip twice, or results never marking it posted
    config.save({"postforme_api_key": "test-key"})
    cid = seed_clip(data_dir, client)
    assert [a["id"] for a in client.get("/api/accounts").json()] == ["spc_tt1", "spc_yt1"]
    r1 = client.post(f"/api/clips/{cid}/approve", json={"caption": "big moment #gta6", "accounts": ["spc_tt1", "spc_yt1"]}, headers=H)
    r2 = client.post(f"/api/clips/{cid}/approve", json={"caption": "big moment #gta6", "accounts": ["spc_tt1"]}, headers=H)
    assert r1.status_code == 200 and r2.status_code == 409
    for _ in range(50):  # the upload runs as a background task in the app's loop
        clip = client.get(f"/api/clips/{cid}").json()
        if clip["post"]["status"] != "uploading":
            break
        time.sleep(0.1)
    assert clip["post"]["status"] == "submitted", clip["post"]
    posts = [r for r in mock.requests if r[0] == "POST" and r[1] == "/v1/social-posts"]
    assert len(posts) == 1 and posts[0][3]["caption"] == "big moment #gta6"
    assert list(mock.uploads.values()) == [4096]
    # first results call is empty (processing), second has one result per account
    client.portal.call(client.app.state.pipeline.results_once)
    client.portal.call(client.app.state.pipeline.results_once)
    clip = client.get(f"/api/clips/{cid}").json()
    assert clip["status"] == "posted", clip
    assert {r["success"] for r in clip["post"]["results"]} == {True}
    assert [c["id"] for c in client.get("/api/clips?status=done").json()] == [cid]


def test_post_failure_marks_clip_failed_and_retry_allowed(client, data_dir, mock):
    # bug caught: a failed upload leaving the clip stuck in 'posting' forever
    config.save({"postforme_api_key": "test-key"})
    cid = seed_clip(data_dir, client)
    mock.fail_next("/v1/social-posts", 500, "boom", method="POST")  # the create, not the duplicate lookup before it
    assert client.post(f"/api/clips/{cid}/approve", json={"caption": "x", "accounts": ["spc_tt1"]}, headers=H).status_code == 200
    for _ in range(50):
        clip = client.get(f"/api/clips/{cid}").json()
        if clip["status"] != "posting":
            break
        time.sleep(0.1)
    assert clip["status"] == "failed" and "500" in (clip["post"]["error"] or "")
    assert client.post(f"/api/clips/{cid}/approve", json={"caption": "x", "accounts": ["spc_tt1"]}, headers=H).status_code == 200
