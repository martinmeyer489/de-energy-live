#!/usr/bin/env python3
"""Path-routing reverse proxy on :8000 (the only publicly open app port).

  /               -> unified layer map (static files under map/, served here)
  /map/*          -> static assets of the unified map app (map/ dir)
  /price*         -> electricity-price-map static app on :8004 (prefix stripped)
  /api/* /geo/* /chart.png /healthz  -> :8001 as-is (map uses absolute paths)
  everything else -> 404
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit, unquote
import mimetypes

BASE = Path(__file__).resolve().parent
MAP = BASE / "map"

HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer",
       "upgrade", "proxy-authorization", "proxy-authenticate"}

# bare prefixes -> trailing-slash redirect so relative assets resolve right
SLASH = {"/price": "/price/", "/map": "/"}

CTYPES = {".js": "text/javascript", ".geojson": "application/geo+json",
          ".map": "application/json"}


def route(raw_path):
    p = urlsplit(raw_path).path
    if p in SLASH:
        q = urlsplit(raw_path).query
        return "redirect", SLASH[p] + ("?" + q if q else "")
    if p in ("/", "/index.html"):
        return "map", "/index.html"
    if p.startswith("/map/"):
        return "map", unquote(p[4:])
    if p.startswith("/price/"):
        return 8004, raw_path[6:]
    if p == "/chart.png" or p == "/healthz" or \
       p.startswith("/api/") or p.startswith("/geo/"):
        return 8001, raw_path
    return None, raw_path


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _forward(self):
        port, path = route(self.path)
        if port == "redirect":
            self.send_response(301)
            self.send_header("Location", path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if port == "map":
            self._serve_map(path)
            return
        if port is None:
            self.send_error(404, "Not found")
            return
        body = None
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)
        conn = HTTPConnection("127.0.0.1", port, timeout=180)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP and k.lower() != "host"}
        headers["Host"] = f"127.0.0.1:{port}"
        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        except Exception:
            self.send_error(502, "Upstream unavailable")
            return
        finally:
            conn.close()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP and k.lower() != "content-length":
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _serve_map(self, path):
        """Serve a static file from the map/ dir (unified layer map app)."""
        rel = urlsplit(path).path.lstrip("/") or "index.html"
        target = (MAP / rel).resolve()
        if target.is_dir():
            target = target / "index.html"
        if MAP not in target.parents and target != MAP:
            self.send_error(403)
            return
        try:
            data = target.read_bytes()
        except OSError:
            self.send_error(404, "Not found")
            return
        ctype = CTYPES.get(target.suffix) or \
            mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if target.suffix in (".html", ".js", ".css"):
            # app code changes often; force revalidation so deploys show up
            self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = do_HEAD = do_POST = _forward

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
