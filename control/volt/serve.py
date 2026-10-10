"""Local session. python3 -m volt.serve from control/."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from volt.session import list_assets, rundown, seek_hit
from volt.store import Store

ROOT = Path(__file__).resolve().parents[1] / "session"
DB_PATH = str(Path(__file__).resolve().parents[1] / "volt.db")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        store = Store(DB_PATH)
        try:
            self._route(store)
        finally:
            store.close()

    def _route(self, STORE):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = (ROOT / "index.html").read_bytes()
            self._send(200, "text/html", body)
            return
        if parsed.path == "/assets":
            rows = [
                {"id": row.id, "show": row.show_name, "camera": row.camera, "open": row.open}
                for row in list_assets(STORE)
            ]
            self._send(200, "application/json", json.dumps(rows).encode())
            return
        if parsed.path == "/rundown":
            show_id = parse_qs(parsed.query).get("show", [""])[0]
            rows = [
                {"id": row.id, "slug": row.slug, "status": row.status, "urls": row.urls,
                 "asRunIn": row.as_run_in, "asRunOut": row.as_run_out}
                for row in rundown(STORE, show_id)
            ]
            self._send(200, "application/json", json.dumps(rows).encode())
            return
        if parsed.path == "/seek":
            query = parse_qs(parsed.query).get("q", [""])[0]
            hit = seek_hit(STORE, query)
            payload = {} if hit is None else {
                "asset_id": hit.asset_id,
                "span_id": hit.span_id,
                "tc_in": hit.tc_in,
                "text": hit.text,
                "proxy": hit.proxy,
            }
            self._send(200, "application/json", json.dumps(payload).encode())
            return
        self._send(404, "text/plain", b"not found")

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Volt session on http://127.0.0.1:8765")
    server.serve_forever()


if __name__ == "__main__":
    main()
