"""Local fake Post for Me + Discord webhook. `with MockServices() as m:` or `python tests/mock_services.py --port 8799`."""
import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

API_KEY = "test-key"
SUMMARY_ACCOUNTS = [
    {"id": "spc_tt1", "platform": "tiktok", "username": "tester_tt", "user_id": "u1", "profile_photo_url": None,
     "access_token": "SECRET-A", "refresh_token": "SECRET-R", "status": "connected"},
    {"id": "spc_yt1", "platform": "youtube", "username": "tester_yt", "user_id": "u2", "profile_photo_url": None,
     "access_token": "SECRET-A2", "refresh_token": None, "status": "connected"},
]


class MockServices:
    def __init__(self, port: int = 0):
        self.requests: list[tuple] = []  # (method, path, headers-without-auth-values, json-or-size)
        self.uploads: dict[str, int] = {}
        self.results_calls = 0
        self._fail: list[tuple] = []
        self._lock = threading.Lock()
        self._port = port
        self._srv = None

    def fail_next(self, path_prefix: str, status: int, body: str = "injected failure", method: str | None = None):
        with self._lock:
            self._fail.append((path_prefix, status, body, method))

    def _take_fail(self, path, method=None):
        with self._lock:
            for i, (pre, st, body, m) in enumerate(self._fail):
                if path.startswith(pre) and (m is None or m == method):
                    del self._fail[i]
                    return st, body
        return None

    def __enter__(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj):
                data = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _handle(self):
                u = urlparse(self.path)
                path, q = u.path, parse_qs(u.query)
                raw = self._body()
                hdrs = {k: ("<set>" if k.lower() in ("authorization", "cookie") else v) for k, v in self.headers.items()}
                ctype = self.headers.get("Content-Type", "")
                data = None
                if "json" in ctype and raw:
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        data = None
                if path.startswith("/api/webhooks/"):
                    data = mock._discord_info(ctype, raw, data)
                elif data is None:
                    data = len(raw)
                with mock._lock:
                    mock.requests.append((self.command, path + (("?" + u.query) if u.query else ""), hdrs, data))
                f = mock._take_fail(path, self.command)
                if f:
                    return self._send(f[0], f[1])
                if path.startswith("/v1/") and self.headers.get("Authorization") != f"Bearer {API_KEY}":
                    return self._send(401, {"message": "Unauthorized"})
                m = self.command
                if path == "/v1/social-accounts" and m == "GET":
                    return self._send(200, {"data": SUMMARY_ACCOUNTS, "meta": {"total": 2, "offset": 0, "limit": 100, "next": None}})
                if path == "/v1/media/create-upload-url" and m == "POST":
                    mid = f"m{len(mock.uploads) + 1}"
                    base = f"http://{self.headers.get('Host')}"
                    return self._send(200, {"upload_url": f"{base}/upload/{mid}", "media_url": f"https://cdn.example.test/{mid}.mp4"})
                if path.startswith("/upload/") and m == "PUT":
                    mock.uploads[path.rsplit("/", 1)[1]] = len(raw)
                    return self._send(200, {})
                if path == "/v1/social-posts" and m == "POST":
                    if not isinstance(data, dict) or not isinstance(data.get("caption"), str) or not data.get("social_accounts"):
                        return self._send(400, {"message": "caption and social_accounts are required"})
                    return self._send(200, {"id": "sp_test_1", "external_id": data.get("external_id"), "caption": data["caption"],
                                            "status": "processing", "scheduled_at": None, "media": data.get("media"),
                                            "social_accounts": [a for a in SUMMARY_ACCOUNTS if a["id"] in data["social_accounts"]],
                                            "platform_configurations": data.get("platform_configurations"),
                                            "account_configurations": None, "created_at": "2026-11-16T00:00:00Z", "updated_at": "2026-11-16T00:00:00Z"})
                if path == "/v1/social-posts" and m == "GET":  # lookup by external_id: the mock never keeps posts
                    return self._send(200, {"data": [], "meta": {"total": 0, "offset": 0, "limit": 100, "next": None}})
                if path == "/v1/social-post-results" and m == "GET":
                    mock.results_calls += 1
                    if mock.results_calls == 1:
                        rows = []
                    else:
                        rows = [{"id": f"spr_{i}", "social_account_id": a["id"], "post_id": (q.get("post_id") or [""])[0],
                                 "success": True, "error": None, "details": {},
                                 "platform_data": {"id": f"p{i}", "url": f"https://example.test/{a['platform']}/p{i}"}}
                                for i, a in enumerate(SUMMARY_ACCOUNTS)]
                    return self._send(200, {"data": rows, "meta": {"total": len(rows), "offset": 0, "limit": 50, "next": None}})
                mm = re.fullmatch(r"/v1/social-posts/([\w-]+)", path)
                if mm and m == "GET":
                    return self._send(200, {"id": mm.group(1), "status": "processed", "caption": "x", "social_accounts": []})
                if path.startswith("/api/webhooks/") and m == "POST":
                    return self._send(200, {"id": "1", "content": (data or {}).get("content", ""), "type": 0})
                return self._send(404, {"message": "not found"})

            do_GET = do_POST = do_PUT = _handle

        self._srv = ThreadingHTTPServer(("127.0.0.1", self._port), H)
        self.base_url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def _discord_info(self, ctype, raw, data):
        """Records JSON content, or for multipart: payload_json plus which file parts arrived and their sizes."""
        if "multipart" not in ctype:
            return data
        boundary = ctype.split("boundary=")[1].strip().strip('"').encode()
        info = {"files": {}, "payload_json": None}
        for part in raw.split(b"--" + boundary):
            head, _, body = part.partition(b"\r\n\r\n")
            m = re.search(rb'name="([^"]+)"', head)
            if not m:
                continue
            body = body[:-2] if body.endswith(b"\r\n") else body
            name = m.group(1).decode()
            if b"filename=" in head:
                info["files"][name] = len(body)
            elif name == "payload_json":
                info["payload_json"] = json.loads(body)
        return info

    def __exit__(self, *a):
        self._srv.shutdown()
        self._srv.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    with MockServices(args.port) as m:
        print(m.base_url, flush=True)
        threading.Event().wait()
