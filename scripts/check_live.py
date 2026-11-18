"""Read-only live check against real services. Not part of pytest.
Usage: .venv\\Scripts\\python.exe scripts\\check_live.py [channel urls...]
Never prints secret values; never POSTs anywhere except the OAuth token endpoints."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from clipper import config, platforms, twitch_auth  # noqa: E402

DEFAULT_URLS = ["https://www.youtube.com/@LofiGirl/live", "https://kick.com/classybeef", "https://www.twitch.tv/gaules"]
fails = 0
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # titles carry emoji; cp1252 consoles crash


def report(status: str, name: str, detail: str = "") -> None:
    global fails
    if status == "FAIL":
        fails += 1
    print(f"{status:<5} {name}" + (f" - {detail}" if detail else ""))


async def guarded(name: str, coro) -> None:
    try:
        await coro
    except Exception as e:  # a check must report, never crash the rest
        report("FAIL", name, e.__class__.__name__ + (f" {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else ""))


async def main() -> int:
    cfg = config.load()
    urls = sys.argv[1:] or DEFAULT_URLS
    youtube_vid = None
    async with httpx.AsyncClient(timeout=20) as http:
        for url in urls:
            info = await platforms.probe_streamlink(url, cfg["streamlink"])
            if info.error:
                report("FAIL", f"probe {url}", info.error)
            else:
                report("PASS", f"probe {url}", f"live={info.live} title={info.title!r} category={info.category!r} id={info.video_id}")
            if info.live and "youtube" in url and not youtube_vid:
                youtube_vid = info.video_id

        async def twitch():
            cid, sec = cfg["twitch_client_id"], cfg["twitch_client_secret"]
            if not (cid and sec):
                return report("SKIP", "twitch helix", "no client id/secret")
            r = await platforms._authed_get("twitch", platforms.TWITCH_TOKEN, cid, sec, platforms.HELIX_STREAMS,
                                            [("user_login", "gaules")], http, {"Client-Id": cid})
            report("PASS", "twitch helix streams", f"{len(r.json().get('data', []))} live of 1 asked")
        await guarded("twitch helix", twitch())

        async def twitch_user():
            tok = cfg["twitch_user_token"]
            if not tok:
                return report("SKIP", "twitch user token", "not connected")
            login = await twitch_auth.validate_token(tok, http)
            report("PASS" if login else "FAIL", "twitch user token", f"login={login}" if login else "token invalid")
        await guarded("twitch user token", twitch_user())

        async def kick():
            cid, sec = cfg["kick_client_id"], cfg["kick_client_secret"]
            if not (cid and sec):
                return report("SKIP", "kick api", "no client id/secret")
            r = await platforms._authed_get("kick", platforms.KICK_TOKEN, cid, sec, platforms.KICK_CHANNELS,
                                            [("slug", "classybeef")], http, {})
            report("PASS", "kick channels", f"{len(r.json().get('data', []))} channel(s) returned")
        await guarded("kick api", kick())

        async def youtube():
            key = cfg["youtube_api_key"]
            if not key:
                return report("SKIP", "youtube videos.list", "no api key")
            if not youtube_vid:
                return report("SKIP", "youtube videos.list", "no live YouTube video found by the probes")
            r = await http.get("https://www.googleapis.com/youtube/v3/videos",
                               params={"part": "liveStreamingDetails", "id": youtube_vid, "key": key})
            r.raise_for_status()
            items = r.json().get("items") or []
            chat = bool(items and (items[0].get("liveStreamingDetails") or {}).get("activeLiveChatId"))
            report("PASS", "youtube videos.list", f"items={len(items)} activeLiveChatId={'yes' if chat else 'no'}")
        await guarded("youtube videos.list", youtube())

        async def postforme():
            key = cfg["postforme_api_key"]
            if not key:
                return report("SKIP", "post for me accounts", "no api key")
            r = await http.get("https://api.postforme.dev/v1/social-accounts", headers={"Authorization": f"Bearer {key}"})
            r.raise_for_status()
            j = r.json()
            accts = j.get("data", j) if isinstance(j, dict) else j
            plats = sorted({a.get("platform", "?") for a in accts if isinstance(a, dict)})
            report("PASS", "post for me accounts", f"{len(accts)} account(s) on {plats}")
        await guarded("post for me accounts", postforme())

        async def discord():
            url = cfg["discord_webhook_url"]
            if not url:
                return report("SKIP", "discord webhook", "no webhook url")
            r = await http.get(url)  # GET returns the webhook's details and sends nothing
            r.raise_for_status()
            report("PASS", "discord webhook", f"name={r.json().get('name')!r}")
        await guarded("discord webhook", discord())
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
