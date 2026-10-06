"""Entry point: python -m app.main [port]"""
from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import db
from .server import app


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _read_req(self, method):
        url = urlparse(self.path)
        from urllib.parse import unquote
        path = unquote(url.path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        form = parse_form(body, ctype)
        return {"method": method, "path": path, "headers": {k: v for k, v in self.headers.items()},
                "query": __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(url.query),
                "body": body, "form": form}

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def _serve(self, method):
        req = self._read_req(method)
        try:
            status, headers, body = app.handle(req)
        except Exception as exc:  # pragma: no cover
            import traceback; traceback.print_exc()
            status, headers, body = 500, {"Content-Type": "text/plain"}, str(exc).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if method != "HEAD" and body:
            self.wfile.write(body)


def parse_form(body, ctype):
    from urllib.parse import parse_qs
    if "application/json" in ctype:
        import json
        try:
            data = json.loads(body.decode() or "{}")
            return {k: [str(v)] for k, v in data.items()}
        except Exception:
            return {}
    return parse_qs(body.decode(), keep_blank_values=True)


def main():
    db.init_db()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"PodHost listening on http://0.0.0.0:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
