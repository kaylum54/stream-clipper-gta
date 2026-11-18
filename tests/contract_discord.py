import json
from pathlib import Path

import httpx

from clipper.notify import discord_send

FIX = json.loads((Path(__file__).parent / "fixtures/discord/execute-webhook.json").read_text(encoding="utf-8"))
HOOK = "https://discord.com/api/webhooks/123/abc"


async def test_json_payload_shape():
    # catches: a payload field Discord doesn't define, content over the documented cap, missing wait
    got = {}
    def h(req):
        got["p"] = json.loads(req.content); got["q"] = dict(req.url.params); return httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        await discord_send(c, HOOK, "y" * 5000)
    assert set(got["p"]) <= set(FIX["json_fields"])
    assert len(got["p"]["content"]) == FIX["content_max_chars"]
    assert got["p"]["allowed_mentions"] == {"parse": []} and got["q"]["wait"] == "true"


async def test_multipart_field_names(tmp_path):
    # catches: file sent under the wrong field name, or payload_json missing, so Discord drops the file
    f = tmp_path / "c.mp4"; f.write_bytes(b"abc")
    got = {}
    def h(req):
        got["ct"] = req.headers["content-type"]; got["body"] = req.content; return httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        assert (await discord_send(c, HOOK, "hi", str(f)))[0]
    assert got["ct"].startswith("multipart/form-data")
    assert b'name="payload_json"' in got["body"] and b'name="files[0]"' in got["body"]
    assert b'filename="c.mp4"' in got["body"]


def test_default_size_limit_not_above_documented():
    # catches: default max_file_mb above what Discord accepts (docs: 20 MiB)
    import inspect
    assert inspect.signature(discord_send).parameters["max_file_mb"].default <= FIX["attachment_limit_mib"]
