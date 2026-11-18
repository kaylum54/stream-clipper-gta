"""Post for Me client. Spec saved in tests/fixtures/postforme/openapi-excerpt.json."""
import os
from pathlib import Path

import httpx

TIKTOK_MAX = 2200
_SECRET_FIELDS = {"access_token", "refresh_token", "access_token_expires_at", "refresh_token_expires_at"}


class PostForMeError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = (body or "")[:500]


def build_post_body(caption, account_ids, media_url, platform_configurations=None, external_id=None) -> dict:
    if not isinstance(caption, str):
        raise ValueError("caption must be a string")
    if not account_ids:
        raise ValueError("at least one account id is required")
    if not isinstance(media_url, str) or not media_url.startswith("https://"):
        raise ValueError("media_url must be https")
    body = {"caption": caption[:TIKTOK_MAX], "social_accounts": list(account_ids), "media": [{"url": media_url}]}
    if platform_configurations:
        body["platform_configurations"] = platform_configurations
    if external_id:
        body["external_id"] = external_id
    return body


def default_platform_configurations(title: str) -> dict:
    return {
        "tiktok": {"privacy_status": "public", "allow_comment": True, "allow_duet": True,
                   "allow_stitch": True, "is_ai_generated": False},
        "youtube": {"title": title[:100], "privacy_status": "public", "made_for_kids": False,
                    "localizations": None},  # spec marks it required but nullable
        "instagram": {"placement": "reels"},
    }


def summarize_results(results: list[dict], accounts_by_id: dict[str, dict]) -> list[dict]:
    out, seen = [], set()
    for r in results:
        aid = r.get("social_account_id")
        seen.add(aid)
        acc = accounts_by_id.get(aid) or {}
        ok = r.get("success")
        err = r.get("error")
        if ok:
            err = None
        elif err and not isinstance(err, str):
            err = str(err.get("message") or err) if isinstance(err, dict) else str(err)
        out.append({"account_id": aid, "platform": acc.get("platform"), "username": acc.get("username"),
                    "success": bool(ok) if ok is not None else None, "error": err or None,
                    "url": (r.get("platform_data") or {}).get("url")})
    for aid, acc in accounts_by_id.items():  # accounts still processing have no result yet
        if aid not in seen:
            out.append({"account_id": aid, "platform": acc.get("platform"), "username": acc.get("username"),
                        "success": None, "error": None, "url": None})
    return out


class PostForMe:
    def __init__(self, api_key: str, http: httpx.AsyncClient, base: str = "https://api.postforme.dev"):
        self._key, self._http, self._base = api_key, http, base.rstrip("/")

    async def _req(self, method: str, url: str, **kw):
        if url.startswith("/"):
            url = self._base + url
        headers = kw.pop("headers", {})
        if url.startswith(self._base + "/v1/"):  # never leak the key to signed storage URLs
            headers["Authorization"] = f"Bearer {self._key}"
        try:
            r = await self._http.request(method, url, headers=headers, **kw)
        except httpx.HTTPError as e:
            raise PostForMeError(f"network error: {type(e).__name__}", None, str(e)) from e
        if r.status_code >= 400:
            raise PostForMeError(f"{method} {url.split('?')[0]} -> {r.status_code}", r.status_code, r.text)
        return r

    async def list_accounts(self) -> list[dict]:
        url, params, out = "/v1/social-accounts", {"status": "connected", "limit": 100}, []
        while url:
            body = (await self._req("GET", url, params=params)).json()
            out += [{k: v for k, v in a.items() if k not in _SECRET_FIELDS} for a in body.get("data", [])]
            url, params = (body.get("meta") or {}).get("next"), None
        return out

    async def upload(self, file_path) -> str:
        info = (await self._req("POST", "/v1/media/create-upload-url")).json()
        path = Path(file_path)
        async def chunks():  # AsyncClient needs an async stream; 1 MB reads keep memory flat
            with open(path, "rb") as fh:
                while block := fh.read(1 << 20):
                    yield block
        await self._req("PUT", info["upload_url"], content=chunks(),
                        headers={"Content-Type": "video/mp4", "Content-Length": str(os.path.getsize(path))})
        return info["media_url"]

    async def create_post(self, caption, account_ids, media_url, platform_configurations=None, external_id=None) -> dict:
        body = build_post_body(caption, account_ids, media_url, platform_configurations, external_id)
        return (await self._req("POST", "/v1/social-posts", json=body)).json()

    async def find_by_external_id(self, external_id: str, ignore=()) -> dict | None:
        """The post made for this external_id, if any. GET /v1/social-posts takes external_id (in the saved spec)."""
        body = (await self._req("GET", "/v1/social-posts", params={"external_id": external_id, "limit": 100})).json()
        for post in body.get("data", []):
            if post.get("external_id") == external_id and post.get("id") not in ignore:
                return post
        return None

    async def get_results(self, post_id: str) -> list[dict]:
        return (await self._req("GET", "/v1/social-post-results", params={"post_id": post_id})).json().get("data", [])

    async def get_post(self, post_id: str) -> dict:
        return (await self._req("GET", f"/v1/social-posts/{post_id}")).json()


async def post_clip(pfm: PostForMe, file_path, caption: str, account_ids: list[str], title: str,
                    external_id: str | None = None) -> dict:
    media_url = await pfm.upload(file_path)
    return await pfm.create_post(caption, account_ids, media_url, default_platform_configurations(title), external_id)
