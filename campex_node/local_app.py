from __future__ import annotations

import os
import platform
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.cameras.security import sanitize_error_message

from campex_node.core.config import NodeCameraConfig, NodeSettings


class CloudConnectionPayload(BaseModel):
    cloud_url: str = Field(min_length=1, max_length=500)
    pairing_code: str = Field(min_length=1, max_length=40)
    node_name: str = Field(default="CAMPEX Node", max_length=120)


class PairingStartPayload(BaseModel):
    cloud_url: str = Field(min_length=1, max_length=500)
    node_name: str = Field(default="CAMPEX Node", max_length=120)


class LocalCameraPayload(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    rtsp_url: str = Field(min_length=1, max_length=1000)
    enabled: bool = True


class CameraTestPayload(BaseModel):
    rtsp_url: str = Field(min_length=1, max_length=1000)


class LocalNodeRuntime:
    def __init__(self) -> None:
        self.lifecycle = self._build_from_persisted_connection()
        self.lifecycle.initialize()
        self.lifecycle.start()

    def reconfigure(self, payload: CloudConnectionPayload) -> dict | None:
        from campex_node.main import build_lifecycle

        current_status = self.status()
        if current_status.get("paired"):
            if self.lifecycle.config_sync is not None:
                self.lifecycle.config_sync.sync_once()
            return {"already_paired": True}
        self.lifecycle.stop()
        settings = replace(
            NodeSettings.from_env(),
            cloud_url=payload.cloud_url.rstrip("/"),
        )
        os.environ["CAMPEX_NODE_CLOUD_URL"] = settings.cloud_url or ""
        claim_lifecycle = build_lifecycle(settings)
        claim_lifecycle.store.initialize()
        result = claim_lifecycle.cloud_client.claim_pairing_code(
            code=payload.pairing_code,
            node_name=payload.node_name.strip() or "CAMPEX Node",
            version=settings.version,
        )
        if not result.ok or not isinstance(result.data, dict):
            self.lifecycle = self._build_from_persisted_connection()
            self.lifecycle.initialize()
            self.lifecycle.start()
            raise ValueError(result.error or "Não foi possível parear este Node.")
        data = result.data
        paired_settings = replace(
            settings,
            node_id=data["node_id"],
            cloud_token=data["node_token"],
            organization_id=data["organization_id"],
        )
        os.environ["CAMPEX_NODE_ID"] = paired_settings.node_id or ""
        os.environ["CAMPEX_NODE_TOKEN"] = paired_settings.cloud_token or ""
        os.environ["CAMPEX_NODE_ORGANIZATION_ID"] = paired_settings.organization_id or ""
        self.lifecycle = build_lifecycle(paired_settings)
        self.lifecycle.initialize()
        self.lifecycle.store.set_meta("cloud_url", paired_settings.cloud_url or "")
        self.lifecycle.store.set_meta("node_id", paired_settings.node_id or "")
        self.lifecycle.store.set_meta("node_token", paired_settings.cloud_token or "")
        self.lifecycle.store.set_meta("organization_id", paired_settings.organization_id or "")
        self.lifecycle.start()
        return None

    def add_camera(self, payload: LocalCameraPayload) -> dict:
        camera = NodeCameraConfig(
            id=_camera_id(payload.id),
            name=payload.name.strip(),
            rtsp_url=payload.rtsp_url.strip(),
            enabled=payload.enabled,
        )
        self.lifecycle.store.save_local_camera(camera)
        self._restart_with_persisted_connection()
        return {"ok": True, "camera_id": camera.id, **self.status()}

    def test_camera(self, payload: CameraTestPayload) -> dict:
        from campex_node.cameras.capture import test_rtsp_connection

        result = test_rtsp_connection(payload.rtsp_url.strip())
        return result

    def status(self) -> dict:
        settings = self.lifecycle.settings
        summary = self.lifecycle.camera_manager.summary()
        meta = self.lifecycle.store.meta_dict()
        uptime_seconds = 0
        if self.lifecycle.started_at is not None:
            uptime_seconds = int((datetime.now(timezone.utc) - self.lifecycle.started_at).total_seconds())
        resources = _system_resources()
        return {
            "node_id": self.lifecycle.node_id,
            "cloud_url": settings.cloud_url,
            "organization_id": settings.organization_id,
            "paired": bool(settings.cloud_token and settings.node_id),
            "cloud_configured": bool(settings.cloud_url),
            "queue_size": self.lifecycle.store.outbound_queue_size(),
            "queue": self.lifecycle.store.outbound_summary(),
            "last_sync_at": meta.get("last_sync_at"),
            "last_heartbeat_at": meta.get("last_heartbeat_at"),
            "last_cloud_ok_at": meta.get("last_cloud_ok_at"),
            "last_cloud_error": sanitize_error_message(meta.get("last_cloud_error")),
            "uptime_seconds": uptime_seconds,
            "data_dir": str(settings.data_dir),
            "logs_dir": str(settings.data_dir / "logs"),
            "cpu_percent": resources["cpu_percent"],
            "ram_percent": resources["ram_percent"],
            "ram_used_mb": resources["ram_used_mb"],
            "cameras_total": summary["cameras_total"],
            "cameras_online": summary["cameras_online"],
            "cameras": summary["cameras"],
        }

    def sync_now(self) -> dict:
        if self.lifecycle.config_sync is None:
            return {"ok": False, "error": "Config sync service is not running."}
        cameras = self.lifecycle.config_sync.sync_once()
        if self.lifecycle.sync is not None:
            self.lifecycle.sync.sync_once()
        return {"ok": True, "cameras_loaded": len(cameras), **self.status()}

    def start_pairing(self, payload: PairingStartPayload) -> dict:
        from campex_node.main import build_lifecycle

        self.lifecycle.store.set_meta("cloud_url", payload.cloud_url.rstrip("/"))
        settings = replace(
            NodeSettings.from_env(),
            cloud_url=payload.cloud_url.rstrip("/"),
        )
        client_lifecycle = build_lifecycle(settings)
        client_lifecycle.store.initialize()
        node_public_id = self.lifecycle.node_id
        result = client_lifecycle.cloud_client.start_pairing_session(
            node_public_id=node_public_id,
            node_name=payload.node_name.strip() or "CAMPEX Node",
            version=settings.version,
        )
        if not result.ok or not isinstance(result.data, dict):
            raise ValueError(result.error or "Nao foi possivel iniciar pareamento.")
        self.lifecycle.store.set_meta("pairing_session_id", result.data["session_id"])
        self.lifecycle.store.set_meta("pairing_node_public_id", node_public_id)
        os.environ["CAMPEX_NODE_CLOUD_URL"] = settings.cloud_url or ""
        return {"ok": True, **result.data, **self.status()}

    def pairing_status(self) -> dict:
        from campex_node.main import build_lifecycle

        session_id = self.lifecycle.store.get_meta("pairing_session_id")
        node_public_id = self.lifecycle.store.get_meta("pairing_node_public_id") or self.lifecycle.node_id
        if not session_id:
            return {"ok": False, "status": "missing"}
        settings = replace(
            NodeSettings.from_env(),
            cloud_url=self.lifecycle.settings.cloud_url or self.lifecycle.store.get_meta("cloud_url"),
        )
        client_lifecycle = build_lifecycle(settings)
        client_lifecycle.store.initialize()
        result = client_lifecycle.cloud_client.check_pairing_session(
            session_id=session_id,
            node_public_id=node_public_id,
        )
        if not result.ok or not isinstance(result.data, dict):
            return {"ok": False, "status": "pending", "error": result.error}
        data = result.data
        if data.get("status") == "authorized":
            self.lifecycle.stop()
            paired_settings = replace(
                settings,
                node_id=data["node_id"],
                cloud_token=data["node_token"],
                organization_id=data["organization_id"],
            )
            self.lifecycle = build_lifecycle(paired_settings)
            self.lifecycle.initialize()
            self.lifecycle.store.set_meta("cloud_url", paired_settings.cloud_url or "")
            self.lifecycle.store.set_meta("node_id", paired_settings.node_id or "")
            self.lifecycle.store.set_meta("node_token", paired_settings.cloud_token or "")
            self.lifecycle.store.set_meta("organization_id", paired_settings.organization_id or "")
            self.lifecycle.start()
        return {"ok": True, **data, **self.status()}

    def diagnostics(self) -> dict:
        settings = self.lifecycle.settings
        return {
            "ok": True,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "node_id": self.lifecycle.node_id,
            "version": settings.version,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "data_dir": str(settings.data_dir),
            "database_path": str(settings.database_path),
            "database_exists": settings.database_path.exists(),
            "cloud_configured": bool(settings.cloud_url),
            "paired": bool(settings.node_id and settings.cloud_token),
            "queue_size": self.lifecycle.store.outbound_queue_size(),
            "queue": self.lifecycle.store.outbound_summary(),
            "last_sync_at": self.lifecycle.store.get_meta("last_sync_at"),
            "last_heartbeat_at": self.lifecycle.store.get_meta("last_heartbeat_at"),
            "last_cloud_ok_at": self.lifecycle.store.get_meta("last_cloud_ok_at"),
            "last_cloud_error": sanitize_error_message(self.lifecycle.store.get_meta("last_cloud_error")),
            "resources": _system_resources(),
            "services": {
                "camera_manager": True,
                "config_sync": self.lifecycle.config_sync is not None,
                "sync": self.lifecycle.sync is not None,
                "telemetry": self.lifecycle.telemetry is not None,
                "heartbeat": self.lifecycle.heartbeat is not None,
            },
            "cameras": self.lifecycle.camera_manager.summary(),
        }

    def _build_from_persisted_connection(self):
        from campex_node.main import build_lifecycle

        lifecycle = build_lifecycle()
        lifecycle.settings.data_dir.mkdir(parents=True, exist_ok=True)
        lifecycle.store.initialize()
        meta = lifecycle.store.meta_dict()
        camera_configs = {
            camera.id: camera for camera in lifecycle.store.get_cached_cloud_cameras()
        }
        camera_configs.update({camera.id: camera for camera in lifecycle.store.get_local_cameras()})
        cached_cameras = tuple(camera_configs.values())
        if not meta.get("cloud_url") and not lifecycle.settings.cloud_url:
            if cached_cameras:
                settings = replace(lifecycle.settings, cameras=cached_cameras)
                return build_lifecycle(settings)
            return lifecycle
        settings = replace(
            lifecycle.settings,
            cloud_url=lifecycle.settings.cloud_url or meta.get("cloud_url"),
            node_id=lifecycle.settings.node_id or meta.get("node_id"),
            cloud_token=lifecycle.settings.cloud_token or meta.get("node_token"),
            organization_id=lifecycle.settings.organization_id or meta.get("organization_id"),
            cameras=cached_cameras,
        )
        os.environ["CAMPEX_NODE_CLOUD_URL"] = settings.cloud_url or ""
        os.environ["CAMPEX_NODE_ID"] = settings.node_id or ""
        os.environ["CAMPEX_NODE_TOKEN"] = settings.cloud_token or ""
        os.environ["CAMPEX_NODE_ORGANIZATION_ID"] = settings.organization_id or ""
        return build_lifecycle(settings)

    def _restart_with_persisted_connection(self) -> None:
        self.lifecycle.stop()
        self.lifecycle = self._build_from_persisted_connection()
        self.lifecycle.initialize()
        self.lifecycle.start()


def create_app() -> FastAPI:
    runtime = LocalNodeRuntime()
    app = FastAPI(title="CAMPEX Node Local", version="0.1.0")
    app.state.runtime = runtime
    assets_dir = Path(__file__).resolve().parents[1] / "frontend" / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.on_event("shutdown")
    def shutdown() -> None:
        runtime.lifecycle.stop()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return NODE_HTML

    @app.get("/api/status")
    def status() -> dict:
        return runtime.status()

    @app.post("/api/connect")
    def connect(payload: CloudConnectionPayload) -> dict:
        try:
            result = runtime.reconfigure(payload)
            if result and result.get("already_paired"):
                return {
                    "ok": True,
                    "message": "Este Node já está pareado. Use Sincronizar agora para carregar câmeras.",
                    **runtime.status(),
                }
            return {"ok": True, **runtime.status()}
        except ValueError as exc:
            return {"ok": False, "error": str(exc), **runtime.status()}

    @app.post("/api/pairing/start")
    def start_pairing(payload: PairingStartPayload) -> dict:
        try:
            return runtime.start_pairing(payload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), **runtime.status()}

    @app.get("/api/pairing/status")
    def pairing_status() -> dict:
        return runtime.pairing_status()


    @app.get("/api/cameras/{camera_id}/snapshot")
    def camera_snapshot(camera_id: str):
        import cv2

        frame, _frame_at = runtime.lifecycle.camera_manager.latest_frame(camera_id)
        if frame is None:
            raise HTTPException(status_code=404, detail="Frame ainda não disponível para esta câmera.")
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
        if not ok:
            raise HTTPException(status_code=500, detail="Não foi possível codificar o frame.")
        return Response(
            content=encoded.tobytes(),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/cameras/{camera_id}/stream")
    def camera_stream(camera_id: str):
        return StreamingResponse(
            _mjpeg_frames(runtime, camera_id),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/sync")
    def sync() -> dict:
        return runtime.sync_now()

    @app.post("/api/cameras/test")
    def test_camera(payload: CameraTestPayload) -> dict:
        return runtime.test_camera(payload)

    @app.post("/api/cameras")
    def add_camera(payload: LocalCameraPayload) -> dict:
        try:
            return runtime.add_camera(payload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), **runtime.status()}

    @app.get("/api/diagnostics")
    def diagnostics() -> dict:
        return runtime.diagnostics()

    return app


def _mjpeg_frames(runtime: LocalNodeRuntime, camera_id: str):
    import cv2

    last_payload: bytes | None = None
    while True:
        frame, _frame_at = runtime.lifecycle.camera_manager.latest_frame(camera_id)
        if frame is not None:
            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 68])
            if ok:
                last_payload = encoded.tobytes()
        if last_payload is not None:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-store\r\n\r\n"
                + last_payload
                + b"\r\n"
            )
        time.sleep(0.15)


NODE_HTML = """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CAMPEX Node</title>
  <style>
    :root{--black:#090a0c;--panel:#171b21;--soft:#20262e;--border:#313946;--text:#f2f5f8;--muted:#9aa6b4;--accent:#7db3ff;--online:#31c46b;--attention:#e2b245;--critical:#ef5b5b;--radius:8px}
    *{box-sizing:border-box}body{margin:0;min-height:100vh;background:#0b0d10;color:var(--text);font:15px/1.45 Inter,"SF Pro Text","Segoe UI",system-ui,sans-serif}
    .shell{display:grid;grid-template-columns:260px 1fr;min-height:100vh}.side{background:#090a0c;border-right:1px solid var(--border);padding:22px}.brand{font-weight:800;letter-spacing:.12em;font-size:22px}.brand span{color:var(--accent)}.side p{color:var(--muted);margin:12px 0 0}
    main{padding:28px;max-width:1180px;width:100%;margin:0 auto}.top{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:22px}.eyebrow{margin:0;color:var(--muted);text-transform:uppercase;font-size:12px;letter-spacing:.16em}h1{margin:4px 0 0;font-size:28px}
    .badge{border:1px solid var(--border);background:var(--panel);border-radius:999px;padding:8px 12px;color:var(--muted)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.panel{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:18px;box-shadow:0 .75rem 1.75rem rgba(0,0,0,.2)}
    .panel h2{margin:0 0 12px;font-size:17px}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.stat{background:var(--soft);border:1px solid #252b34;border-radius:8px;padding:14px}.stat strong{display:block;font-size:24px}.stat span{color:var(--muted)}
    label{display:block;color:var(--muted);margin:12px 0 6px}input{width:100%;height:40px;background:#0f1217;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:0 11px}button{height:40px;border:0;border-radius:6px;background:var(--accent);color:#07101d;font-weight:700;padding:0 14px;cursor:pointer}button.secondary{background:var(--soft);color:var(--text);border:1px solid var(--border)}
    .actions{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}.camera{display:flex;justify-content:space-between;gap:12px;padding:11px 0;border-top:1px solid #252b34}.camera:first-child{border-top:0}.dot{width:9px;height:9px;border-radius:99px;background:var(--critical);display:inline-block;margin-right:7px}.ONLINE .dot{background:var(--online)}.DEGRADED .dot,.CONNECTING .dot{background:var(--attention)}
    code{color:var(--accent);word-break:break-all}.status{color:var(--muted);min-height:22px}.flow{display:grid;grid-template-columns:1fr auto 1fr auto 1fr;gap:10px;align-items:center}.flow-step{background:var(--soft);border:1px solid #252b34;border-radius:8px;padding:12px}.flow-arrow{color:var(--accent);font-weight:800}.note{color:var(--muted);margin:10px 0 0}@media(max-width:820px){.shell{grid-template-columns:1fr}.side{border-right:0;border-bottom:1px solid var(--border)}.grid{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}.flow{grid-template-columns:1fr}.flow-arrow{display:none}}
  </style>
</head>
<body>
  <div class="shell">
    <aside class="side"><div class="brand">CAMP<span>EX</span></div><p>Node operacional</p><p>Processa câmeras dentro da empresa e envia dados úteis para a Cloud.</p></aside>
    <main>
      <div class="top"><div><p class="eyebrow">Serviço local 24/7 · saída HTTPS</p><h1>CAMPEX Node</h1></div><div class="badge" id="cloud-badge">Carregando...</div></div>
      <section class="panel" style="margin-bottom:16px"><h2>Fluxo operacional</h2><div class="flow">
        <div class="flow-step"><strong>Câmeras</strong><br><span class="status">RTSP/ONVIF na rede local</span></div><div class="flow-arrow">→</div>
        <div class="flow-step"><strong>Node</strong><br><span class="status">Captura, IA, eventos e fila offline</span></div><div class="flow-arrow">→</div>
        <div class="flow-step"><strong>Cloud</strong><br><span class="status">API, dashboard, relatórios e alertas</span></div>
      </div><p class="note">O painel web lê a Cloud. Ele não precisa acessar este PC diretamente.</p></section>
      <div class="grid">
        <section class="panel"><h2>Pareamento com a Cloud</h2><form id="connect-form">
          <label>Backend Cloud</label><input name="cloud_url" placeholder="https://campexback.vercel.app/api/v1" />
          <label>Código de pareamento</label><input name="pairing_code" autocomplete="off" placeholder="CXP-7KQ2-N91P" />
          <label>Nome deste Node</label><input name="node_name" placeholder="RBA-NODE-01" />
          <div class="actions"><button type="submit">Parear Node</button><button class="secondary" type="button" id="sync-button">Sincronizar agora</button><button class="secondary" type="button" id="diagnostics-button">Diagnóstico</button></div>
          <p class="status" id="form-status"></p>
        </form></section>
        <section class="panel"><h2>Status do Node</h2><div class="stats">
          <div class="stat"><strong id="total">0</strong><span>Câmeras</span></div>
          <div class="stat"><strong id="online">0</strong><span>Online</span></div>
          <div class="stat"><strong id="node">-</strong><span>Node</span></div>
          <div class="stat"><strong id="queue">0</strong><span>Fila</span></div>
        </div><p>Node ID: <code id="node-id">-</code></p></section>
      </div>
      <section class="panel" style="margin-top:16px"><h2>Câmeras recebidas da Cloud</h2><div id="cameras"></div></section>
    </main>
  </div>
  <script>
    const statusEl=document.querySelector('#form-status');
    async function load(){
      const data=await fetch('/api/status').then(r=>r.json());
      document.querySelector('#cloud-badge').textContent=data.paired?'Node pareado':(data.cloud_configured?'Cloud configurada':'Cloud não configurada');
      document.querySelector('#total').textContent=data.cameras_total;
      document.querySelector('#online').textContent=data.cameras_online;
      document.querySelector('#node').textContent=data.node_id.slice(5,9);
      document.querySelector('#queue').textContent=data.queue_size||0;
      document.querySelector('#node-id').textContent=data.node_id;
      document.querySelector('[name=cloud_url]').value=data.cloud_url||'';
      document.querySelector('#cameras').innerHTML=(data.cameras||[]).map(cam=>`<div class="camera ${cam.status}"><span><span class="dot"></span>${cam.name}</span><span>${cam.status}</span></div>`).join('')||'<p class="status">Nenhuma câmera sincronizada ainda.</p>';
    }
    document.querySelector('#connect-form').addEventListener('submit',async e=>{
      e.preventDefault(); statusEl.textContent='Conectando...';
      const form=e.currentTarget;
      const body={cloud_url:form.cloud_url.value,pairing_code:form.pairing_code.value,node_name:form.node_name.value};
      const res=await fetch('/api/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const data=await res.json();
      statusEl.textContent=data.ok?(data.message||'Pareado. O Node buscará configurações e enviará eventos para a Cloud.'):(data.error||'Falha ao parear.');
      await load();
    });
    document.querySelector('#sync-button').addEventListener('click',async()=>{statusEl.textContent='Sincronizando...';const res=await fetch('/api/sync',{method:'POST'});const data=await res.json();statusEl.textContent=data.ok?`Sincronizadas: ${data.cameras_loaded}`:(data.error||'Falha na sincronização');await load();});
    document.querySelector('#diagnostics-button').addEventListener('click',async()=>{const data=await fetch('/api/diagnostics').then(r=>r.json());statusEl.textContent=`Diagnóstico OK · fila ${data.queue_size} · câmeras ${data.cameras.cameras_total}`;});
    load(); setInterval(load,5000);
  </script>
</body>
</html>"""

from campex_node.node_ui import NODE_HTML


def _camera_id(value: str | None) -> str:
    raw_value = (value or "").strip()
    if raw_value:
        safe_value = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in raw_value)
        return safe_value[:120]
    return f"local_{uuid.uuid4().hex[:12]}"


def _system_resources() -> dict:
    try:
        import psutil

        memory = psutil.virtual_memory()
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "ram_percent": memory.percent,
            "ram_used_mb": int(memory.used / (1024 * 1024)),
        }
    except Exception:
        return {"cpu_percent": None, "ram_percent": None, "ram_used_mb": None}
