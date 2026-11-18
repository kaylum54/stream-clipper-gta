import httpx
import pytest

from clipper.notify import discord_send
from clipper.poster import PostForMe, PostForMeError, post_clip, summarize_results
from tests.mock_services import MockServices


@pytest.fixture
def mock():
    with MockServices() as m:
        yield m


async def test_post_clip_end_to_end(mock, tmp_path):
    # catches: upload size mismatch, media_url not passed to the post, or wrong body shape
    f = tmp_path / "c.mp4"; f.write_bytes(b"v" * 300_000)
    async with httpx.AsyncClient() as c:
        pfm = PostForMe("test-key", c, mock.base_url)
        accs = await pfm.list_accounts()
        assert {a["platform"] for a in accs} == {"tiktok", "youtube"} and "access_token" not in accs[0]
        post = await post_clip(pfm, f, "cap", [a["id"] for a in accs], "Title", "clip-1")
    assert sum(mock.uploads.values()) == 300_000
    posted = [r for r in mock.requests if r[0] == "POST" and r[1] == "/v1/social-posts"][0][3]
    assert posted["media"] == [{"url": "https://cdn.example.test/m1.mp4"}] and posted["external_id"] == "clip-1"
    assert post["id"] == "sp_test_1" and post["status"] == "processing"
    put = [r for r in mock.requests if r[0] == "PUT"][0]
    assert "Authorization" not in put[2]


async def test_results_poll_turns_success(mock):
    # catches: treating empty (still processing) results as failure, or losing platform urls
    async with httpx.AsyncClient() as c:
        pfm = PostForMe("test-key", c, mock.base_url)
        accs = {a["id"]: a for a in await pfm.list_accounts()}
        assert await pfm.get_results("sp_test_1") == []
        out = summarize_results(await pfm.get_results("sp_test_1"), accs)
        assert (await pfm.get_post("sp_test_1"))["status"] == "processed"
    assert len(out) == 2 and all(r["success"] is True and r["url"].startswith("https://example.test/") for r in out)


async def test_injected_500_on_create_raises(mock, tmp_path):
    # catches: a failed create being reported as posted
    f = tmp_path / "c.mp4"; f.write_bytes(b"v")
    mock.fail_next("/v1/social-posts", 500, "boom")
    async with httpx.AsyncClient() as c:
        with pytest.raises(PostForMeError) as ei:
            await post_clip(PostForMe("test-key", c, mock.base_url), f, "cap", ["spc_tt1"], "T")
    assert ei.value.status == 500


async def test_wrong_key_is_401(mock):
    # catches: a bad key reported as an empty account list
    async with httpx.AsyncClient() as c:
        with pytest.raises(PostForMeError) as ei:
            await PostForMe("nope", c, mock.base_url).list_accounts()
    assert ei.value.status == 401


async def test_discord_file_reaches_mock(mock, tmp_path):
    # catches: the file part silently missing from the multipart upload
    f = tmp_path / "c.mp4"; f.write_bytes(b"z" * 12345)
    async with httpx.AsyncClient() as c:
        ok, _ = await discord_send(c, f"{mock.base_url}/api/webhooks/1/tok", "clip", str(f))
    info = [r for r in mock.requests if r[1].startswith("/api/webhooks/")][0][3]
    assert ok and info["files"] == {"files[0]": 12345} and info["payload_json"]["allowed_mentions"] == {"parse": []}
