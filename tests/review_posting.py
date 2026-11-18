"""REVIEW-1: posting to social accounts. Each test fails today and names the bug it proves."""
import asyncio
import json
import time

import httpx

from clipper import config
from clipper.pipeline import Pipeline

ACCS = [{"id": "acc_tt", "platform": "tiktok", "username": "tt"}, {"id": "acc_yt", "platform": "youtube", "username": "yt"}]


def make_pipeline(handler):
    config.save({"postforme_api_key": "k"})
    return Pipeline(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), use_whisper=False)


def seed_clip(p, data_dir, status="pending", channel="demo"):
    (data_dir / "clips").mkdir(exist_ok=True)
    f = data_dir / "clips" / f"{channel}.mp4"
    f.write_bytes(b"\x00" * 2048)
    sid = p.conn.execute("INSERT INTO streamers(platform, channel, url, created_at) VALUES ('twitch',?,'https://www.twitch.tv/x',0)", (channel,)).lastrowid
    return p.conn.execute("INSERT INTO clips(streamer_id, streamer_name, platform, peak_ts, start_ts, end_ts, score, path, caption, status, created_at) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (sid, "demo", "twitch", 100, 60, 120, 5.0, str(f), "hi", status, time.time())).lastrowid


def base_routes(req, creates, results_for):
    path = req.url.path
    if path == "/v1/social-accounts":
        return httpx.Response(200, json={"data": ACCS, "meta": {"next": None}})
    if path == "/v1/media/create-upload-url":
        return httpx.Response(200, json={"upload_url": "https://storage.test/u", "media_url": "https://cdn.test/m.mp4"})
    if path == "/v1/social-posts" and req.method == "GET":  # lookup by external_id: nothing exists yet
        return httpx.Response(200, json={"data": [], "meta": {"next": None}})
    if req.method == "PUT":
        return httpx.Response(200, json={})
    if path == "/v1/social-posts" and req.method == "POST":
        body = json.loads(req.content)
        creates.append(body)
        return httpx.Response(200, json={"id": f"sp_{len(creates)}", "external_id": body.get("external_id")})
    if path == "/v1/social-post-results":
        return results_for(req.url.params.get("post_id"))
    return httpx.Response(404, json={"message": "not found"})


async def settle(p):
    await asyncio.gather(*p.tasks, return_exceptions=True)


async def close(p):
    await p.http.aclose()
    p.conn.close()


async def test_retry_after_partial_failure_reposts_to_the_account_that_already_succeeded(data_dir):
    # bug: 1 of 2 accounts failing marks the clip 'failed', approve accepts 'failed', so Try again posts the clip to TikTok a second time
    creates = []

    def results(pid):
        return httpx.Response(200, json={"data": [
            {"social_account_id": "acc_tt", "post_id": pid, "success": True, "error": None, "platform_data": {"url": "https://tiktok.test/1"}},
            {"social_account_id": "acc_yt", "post_id": pid, "success": False, "error": {"message": "quota"}, "platform_data": {}}]})

    p = make_pipeline(lambda req: base_routes(req, creates, results))
    try:
        cid = seed_clip(p, data_dir)
        await p.approve(cid, "big moment", ["acc_tt", "acc_yt"])
        await settle(p)
        await p.results_once()
        try:  # the GUI's Try again sends the same ticked accounts
            await p.approve(cid, "big moment", ["acc_tt", "acc_yt"])
            await settle(p)
        except ValueError:
            pass
        reposted = [a for body in creates[1:] for a in body["social_accounts"]]
        assert "acc_tt" not in reposted, f"clip posted to acc_tt twice: {creates}"
    finally:
        await close(p)


async def test_create_post_timeout_then_retry_creates_a_second_post(data_dir):
    # bug: a read timeout on POST /v1/social-posts (post already created server side) marks the clip failed; retry posts again, external_id never looked up
    creates, server_posts = [], {}

    def handler(req):
        if req.url.path == "/v1/social-posts" and req.method == "POST":
            body = json.loads(req.content)
            creates.append(body)
            server_posts[body["external_id"]] = {"id": f"sp_{len(creates)}", "external_id": body["external_id"], "status": "processing"}
            raise httpx.ReadTimeout("timed out", request=req)
        if req.url.path == "/v1/social-posts" and req.method == "GET":
            ext = req.url.params.get("external_id")
            data = [server_posts[ext]] if ext in server_posts else []
            return httpx.Response(200, json={"data": data, "meta": {"next": None}})
        return base_routes(req, creates, lambda pid: httpx.Response(200, json={"data": []}))

    p = make_pipeline(handler)
    try:
        cid = seed_clip(p, data_dir)
        await p.approve(cid, "cap", ["acc_tt"])
        await settle(p)
        assert p.conn.execute("SELECT status FROM clips WHERE id=?", (cid,)).fetchone()[0] == "failed"
        try:
            await p.approve(cid, "cap", ["acc_tt"])
            await settle(p)
        except ValueError:
            pass
        assert len(creates) == 1, f"{len(creates)} posts created for one clip"
    finally:
        await close(p)


async def test_one_erroring_post_freezes_results_for_every_later_post(data_dir):
    # bug: results_once lets one get_results error escape the loop, so posts after it stay 'posting' forever
    def results(pid):
        if pid == "sp_gone":
            return httpx.Response(404, json={"message": "post not found"})
        return httpx.Response(200, json={"data": [{"social_account_id": "acc_tt", "post_id": pid, "success": True, "platform_data": {"url": "https://t/1"}}]})

    p = make_pipeline(lambda req: base_routes(req, [], results))
    try:
        c1, c2 = seed_clip(p, data_dir, "posting", "one"), seed_clip(p, data_dir, "posting", "two")
        for cid, pid in ((c1, "sp_gone"), (c2, "sp_ok")):
            p.conn.execute("INSERT INTO posts(clip_id, pfm_post_id, accounts, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                           (cid, pid, json.dumps(["acc_tt"]), "submitted", 0, 0))
        try:
            await p.results_once()
        except Exception:
            pass  # the loop wrapper swallows it the same way
        assert p.conn.execute("SELECT status FROM clips WHERE id=?", (c2,)).fetchone()[0] == "posted"
    finally:
        await close(p)
