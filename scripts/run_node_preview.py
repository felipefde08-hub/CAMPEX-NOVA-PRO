from __future__ import annotations

import json
import math
import mimetypes
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
STATIC_DIRS = {"assets": FRONTEND_DIR / "assets", "css": FRONTEND_DIR / "css", "vendor": FRONTEND_DIR / "vendor"}
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
        if path == "/api/events":
            self._send_json(_events_payload())
            return
        if path.startswith("/api/cameras/") and path.endswith("/vision/status"):
            self._send_json(_vision_payload(path.split("/")[3]))
            return
        prefix = path.split("/")[1] if path.count("/") >= 2 else ""
        if prefix in STATIC_DIRS:
            self._send_static(STATIC_DIRS[prefix], path.removeprefix(f"/{prefix}/"))
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

    def _send_static(self, directory: Path, name: str) -> None:
        root = directory.resolve()
        target = (directory / name).resolve()
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


STARTED = datetime.now(timezone.utc)
CAMERAS = [
    {"id": "cam_canal6", "name": "Canal 6, porta do galpão", "status": "ONLINE", "vision": True},
    {"id": "cam_corte_1", "name": "Máquina de corte 1", "status": "ONLINE", "vision": True},
    {"id": "cam_docas", "name": "Docas", "status": "DEGRADED", "vision": False},
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status_payload() -> dict:
    elapsed = (_now() - STARTED).total_seconds()
    return {
        "node_id": "node_preview_01",
        "version": "0.3.2",
        "update": {"state": "idle", "message": "Versão mais recente instalada", "available_version": None,
                   "checked_at": (_now() - timedelta(hours=2)).isoformat()},
        "cloud_url": "https://campex-api.vercel.app/api/v1",
        "organization_id": "rba-industria",
        "paired": True,
        "cloud_configured": True,
        "queue_size": 3,
        "last_sync_at": (_now() - timedelta(seconds=12)).isoformat(),
        "last_heartbeat_at": (_now() - timedelta(seconds=20)).isoformat(),
        "last_cloud_ok_at": (_now() - timedelta(seconds=12)).isoformat(),
        "last_cloud_error": None,
        "uptime_seconds": int(elapsed) + 3 * 3600 + 17 * 60,
        "data_dir": r"C:\Users\operador\AppData\Local\CAMPEX Node",
        "logs_dir": r"C:\Users\operador\AppData\Local\CAMPEX Node\logs",
        "cpu_percent": 34 + 8 * math.sin(elapsed / 7),
        "ram_percent": 61.0,
        "cameras_total": len(CAMERAS),
        "cameras_online": sum(1 for camera in CAMERAS if camera["status"] == "ONLINE"),
        "cameras": [
            {
                "id": camera["id"],
                "name": camera["name"],
                "status": camera["status"],
                "frames_received": int(elapsed * 15),
                "last_frame_at": (_now() - timedelta(seconds=0 if camera["status"] == "ONLINE" else 14)).isoformat(),
            }
            for camera in CAMERAS
        ],
    }


def _vision_payload(camera_id: str) -> dict:
    camera = next((item for item in CAMERAS if item["id"] == camera_id), None)
    elapsed = (_now() - STARTED).total_seconds()
    running = bool(camera and camera["vision"])
    seed = sum(map(ord, camera_id))
    return {
        "camera_id": camera_id,
        "status": "RUNNING" if running else "STOPPED",
        "metrics": {
            "camera_fps": round(14.6 + (seed % 5) / 10, 1) if camera and camera["status"] == "ONLINE" else 3.2,
            "vision_fps": round(2.6 + 0.35 * math.sin(elapsed / 3 + seed), 2) if running else 0,
            "frames_processed": int(elapsed * 2.7) + seed * 40 if running else 0,
            "frames_received": int(elapsed * 15),
        },
        "components": {"mapping": {"state": "STOPPED", "poses": 0, "error": None}},
    }


def _events_payload() -> list[dict]:
    base = _now().replace(microsecond=0)
    rows = [
        ("PERSON_MONITORED_ZONE", "cam_canal6", "Porta", 2, 7.0, True),
        ("PERSON_MONITORED_ZONE", "cam_corte_1", "Estação de corte", 9, 312.0, True),
        ("PERSON_RESTRICTED_ZONE", "cam_corte_1", "Área da lâmina", 26, 4.0, True),
        ("PERSON_MONITORED_ZONE", "cam_canal6", "Porta", 41, 11.0, True),
        ("PERSON_MONITORED_ZONE", "cam_canal6", "Porta", 0, None, False),
    ]
    events = []
    for index, (kind, camera, zone, minutes_ago, duration, closed) in enumerate(rows):
        started = base - timedelta(minutes=minutes_ago, seconds=20)
        events.append({
            "id": f"evt_preview{index}",
            "type": kind,
            "camera_id": camera,
            "zone_id": f"zone_{zone}",
            "track_id": 10 + index,
            "status": "CLOSED" if closed else "OPEN",
            "started_at": started.isoformat(),
            "ended_at": (started + timedelta(seconds=duration)).isoformat() if closed else None,
            "duration": duration,
            "metadata": {"zone_name": zone, **({"clip_path": "clip.webm"} if closed else {})},
        })
    return sorted(events, key=lambda item: item["started_at"], reverse=True)


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
