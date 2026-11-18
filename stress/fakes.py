"""Extra fakes for the stress scenarios (tests/mock_services.py keeps no post state and cannot hang)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ACCOUNTS = [{"id": "spc_tt1", "platform": "tiktok", "username": "tt", "status": "connected"},
            {"id": "spc_yt1", "platform": "youtube", "username": "yt", "status": "connected"}]


class StatefulPFM:
    """Post for Me that remembers posts by external_id and can fail on demand.
    mode: ok | 429 | 500 | hang (no reply for hang_s) | create_then_hang (create the post, then do not answer)"""

    def __init__(self, hang_s=8.0):
        self.mode, self.hang_s = "ok", hang_s
        self.posts: dict[str, dict] = {}      # post id -> body
        self.creates = 0
        self.calls: list[tuple[str, str, str]] = []
        self._lock = threading.Lock()
        self.base_url = ""
        self.results_ok = True

    def __enter__(self):
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj):
                data = json.dumps(obj).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def _h(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                with me._lock:
                    me.calls.append((self.command, u.path, me.mode))
                mode = me.mode
                creating = self.command == "POST" and u.path == "/v1/social-posts"
                if mode in ("429", "500"):
                    return self._send(int(mode), {"message": "injected"})
                if mode == "hang":
                    time.sleep(me.hang_s)
                    return self._send(200, {})
                if u.path == "/v1/social-accounts":
                    return self._send(200, {"data": ACCOUNTS, "meta": {"next": None}})
                if u.path == "/v1/media/create-upload-url":
                    return self._send(200, {"upload_url": f"http://{self.headers['Host']}/upload/m", "media_url": "https://cdn.test/m.mp4"})
                if u.path.startswith("/upload/"):
                    return self._send(200, {})
                if creating:
                    body = json.loads(raw)
                    with me._lock:
                        me.creates += 1
                        pid = f"sp{me.creates}"
                        me.posts[pid] = {"id": pid, "external_id": body.get("external_id"), "status": "processing"}
                    if mode == "create_then_hang":
                        time.sleep(me.hang_s)
                    return self._send(200, me.posts[pid])
                if u.path == "/v1/social-posts" and self.command == "GET":
                    ext = (q.get("external_id") or [None])[0]
                    return self._send(200, {"data": [p for p in me.posts.values() if ext in (None, p["external_id"])], "meta": {"next": None}})
                if u.path == "/v1/social-post-results":
                    pid = (q.get("post_id") or [""])[0]
                    rows = [{"id": f"r{i}", "social_account_id": a["id"], "post_id": pid, "success": True, "error": None,
                             "platform_data": {"url": f"https://x.test/{pid}/{i}"}} for i, a in enumerate(ACCOUNTS)] if me.results_ok else []
                    return self._send(200, {"data": rows, "meta": {"next": None}})
                return self._send(404, {"message": "nf"})

            do_GET = do_POST = do_PUT = _h

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self._srv.shutdown()
        self._srv.server_close()


class HangingDiscord:
    """A webhook that accepts the request and then says nothing for `hang_s` seconds."""

    def __init__(self, hang_s=300.0):
        self.hang_s, self.hits = hang_s, []

    def __enter__(self):
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _h(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                me.hits.append(time.time())
                time.sleep(me.hang_s)

            do_POST = _h

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self._srv.server_close()   # shutdown() would wait for the sleeping handlers
