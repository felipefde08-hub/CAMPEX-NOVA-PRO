from __future__ import annotations

import json
import mimetypes
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = ROOT / "frontend" / "assets"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from campex_node.node_ui import NODE_HTML


class NodePreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(NODE_HTML)
            return
        if path == "/api/status":
            self._send_json(_status_payload())
            return
        if path == "/api/diagnostics":
            self._send_json(_diagnostics_payload())
            return
        if path == "/api/pairing/status":
            payload = _status_payload()
            payload.update({"ok": True, "status": "pending"})
            self._send_json(payload)
            return
        if path.startswith("/assets/"):
            self._send_asset(path.removeprefix("/assets/"))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/connect":
            self._read_body()
            payload = _status_payload()
            payload.update({"ok": True, "message": "Preview: pareamento simulado."})
            self._send_json(payload)
            return
        if path == "/api/sync":
            payload = _status_payload()
            payload.update({"ok": True, "cameras_loaded": len(payload["cameras"])})
            self._send_json(payload)
            return
        if path == "/api/pairing/start":
            self._read_body()
            payload = _status_payload()
            payload.update({
                "ok": True,
                "session_id": "nps_preview",
                "pairing_code": "482 917",
                "expires_at": "2026-09-24T12:45:00+00:00",
                "status": "pending",
            })
            self._send_json(payload)
            return
        self.send_error(404)

    def log_message(self, format: str, *args) -> None:
        return None

    def _send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def _send_asset(self, name: str) -> None:
        root = ASSETS_DIR.resolve()
        target = (ASSETS_DIR / name).resolve()
        if root not in target.parents or not target.exists():
            self.send_error(404)
            return
        data = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def _status_payload() -> dict:
    return {
        "node_id": "node_preview_01",
        "cloud_url": None,
        "organization_id": "default",
        "paired": False,
        "cloud_configured": False,
        "queue_size": 3,
        "events_pending": 3,
        "metrics_pending": 14,
        "cameras_total": 3,
        "cameras_online": 2,
        "cameras": [
            {"id": "cam_entrada", "name": "Entrada principal", "status": "ONLINE"},
            {"id": "cam_corte_1", "name": "Maquina de corte 1", "status": "ONLINE"},
            {"id": "cam_docas", "name": "Docas", "status": "DEGRADED"},
        ],
    }


def _diagnostics_payload() -> dict:
    return {
        "ok": True,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "node_id": "node_preview_01",
        "version": "0.1.0-preview",
        "queue_size": 3,
        "events_pending": 3,
        "metrics_pending": 14,
        "paired": False,
        "cloud_configured": True,
        "cameras": {"cameras_total": 3, "cameras_online": 2},
    }


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 8787), NodePreviewHandler)
    print("CAMPEX Node preview: http://127.0.0.1:8787")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
