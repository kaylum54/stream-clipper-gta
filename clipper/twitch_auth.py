"""Twitch device code grant (scope chat:read). Public client: no secret is sent for the device flow.
Docs: https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#device-code-grant-flow"""
import httpx

DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
SCOPES = "chat:read"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class TwitchAuthError(Exception):
    pass


def _message(r: httpx.Response) -> str:
    try:
        return str(r.json().get("message") or r.status_code)
    except ValueError:
        return str(r.status_code)


async def start_device_flow(client_id: str, http) -> dict:
    r = await http.post(DEVICE_URL, data={"client_id": client_id, "scopes": SCOPES})
    if r.status_code != 200:
        raise TwitchAuthError(f"could not start Twitch sign-in: {_message(r)}")
    j = r.json()
    return {k: j[k] for k in ("device_code", "user_code", "verification_uri", "interval", "expires_in")}


async def poll_device_token(client_id: str, device_code: str, http) -> dict | None:
    r = await http.post(TOKEN_URL, data={"client_id": client_id, "scopes": SCOPES,
                                         "device_code": device_code, "grant_type": DEVICE_GRANT})
    if r.status_code == 200:
        j = r.json()
        return {k: j.get(k) for k in ("access_token", "refresh_token", "expires_in")}
    msg = _message(r)
    if msg == "authorization_pending":
        return None
    raise TwitchAuthError(f"Twitch sign-in failed: {msg}")


async def refresh_user_token(client_id: str, client_secret: str, refresh_token: str, http) -> dict:
    data = {"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token}
    if client_secret:  # confidential clients send it; public clients refresh without
        data["client_secret"] = client_secret
    r = await http.post(TOKEN_URL, data=data)
    if r.status_code != 200:
        raise TwitchAuthError(f"Twitch token refresh failed: {_message(r)}")
    j = r.json()
    return {k: j.get(k) for k in ("access_token", "refresh_token", "expires_in")}


async def validate_token(token: str, http) -> str | None:
    r = await http.get(VALIDATE_URL, headers={"Authorization": f"OAuth {token}"})
    if r.status_code == 401:
        return None
    r.raise_for_status()
    return r.json().get("login")
