"""Discord webhook notifications. Never raises; returns (ok, detail)."""
import asyncio
import json
import os
import re
from urllib.parse import urlparse

import httpx

DISCORD_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com",
                 "ptb.discordapp.com", "canary.discordapp.com"}
_MD = re.compile(r"([\\*_~`|>#\[\]])")


def _valid_webhook(url: str) -> bool:
    try:
        u = urlparse(url)
        port = u.port  # raises on a bad port
    except ValueError:
        return False
    if u.scheme == "http":
        return u.hostname == "127.0.0.1" and port is not None  # local mock only
    return u.scheme == "https" and u.hostname in DISCORD_HOSTS and u.path.startswith("/api/webhooks/")


def _esc(text: str) -> str:
    return _MD.sub(r"\\\1", " ".join((text or "").split()))


async def discord_send(http: httpx.AsyncClient, webhook_url: str, content: str,
                       file_path: str | None = None, max_file_mb: float = 10.0, timeout: float = 15.0) -> tuple[bool, str]:
    if not webhook_url:
        return False, "no webhook"
    if not _valid_webhook(webhook_url):
        return False, "bad webhook url"
    note = ""
    if file_path:
        try:
            size = os.path.getsize(file_path)
            if size > max_file_mb * 1024 * 1024:
                note, file_path = f" (file {size / 1048576:.1f} MB over the {max_file_mb:g} MB limit, sent without it)", None
        except OSError:
            note, file_path = " (file missing, sent without it)", None
    payload = {"content": (content or "")[:2000], "allowed_mentions": {"parse": []}}
    url = httpx.URL(webhook_url).copy_merge_params({"wait": "true"})
    try:
        limited = False
        for _ in range(3):
            if file_path:
                with open(file_path, "rb") as fh:
                    r = await http.post(url, data={"payload_json": json.dumps(payload)},
                                        files={"files[0]": (os.path.basename(file_path), fh, "video/mp4")},
                                        timeout=max(timeout, 120.0))  # a 20 MB clip on a slow uplink needs longer; it runs in the background
            else:
                r = await http.post(url, json=payload, timeout=timeout)
            if r.status_code == 429 and not limited:
                limited = True
                try:
                    wait = float(r.json().get("retry_after"))
                except (ValueError, TypeError, AttributeError):
                    wait = float(r.headers.get("retry-after") or 1)
                await asyncio.sleep(min(max(wait, 0), 10))
                continue
            if r.status_code == 413 and file_path:  # file over the server's real limit: still send the alert
                note, file_path = note + " (Discord said the file is too large, sent without it)", None
                continue
            if r.status_code in (200, 204):
                return True, "sent" + note
            return False, f"discord {r.status_code}: {r.text[:200]}" + note
    except (httpx.HTTPError, OSError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"[:200]
    return False, "unreachable"


def live_message(streamer: str, platform: str, title: str | None, url: str) -> str:
    t = f" - {_esc(title)}" if title else ""
    return f"{_esc(streamer)} is live on {_esc(platform)}{t}\n{url}"


def clip_message(streamer: str, platform: str, score: float, transcript: str | None, review_url: str) -> str:
    said = f'\nThey said: "{_esc(transcript)[:300]}"' if transcript else ""
    return f"New clip from {_esc(streamer)} ({_esc(platform)}), score {score:.1f}.{said}\nReview: {review_url}"
