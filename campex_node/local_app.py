from __future__ import annotations

import os
import platform
import sys
from dataclasses import replace
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from campex_node.core.config import NodeSettings
from campex_node.main import build_lifecycle


class CloudConnectionPayload(BaseModel):
    cloud_url: str = Field(min_length=1, max_length=500)
    pairing_code: str = Field(min_length=1, max_length=40)
    node_name: str = Field(default="CAMPEX Node", max_length=120)


class LocalNodeRuntime:
    def __init__(self) -> None:
        self.lifecycle = self._build_from_persisted_connection()
        self.lifecycle.initialize()
        self.lifecycle.start()

    def reconfigure(self, payload: CloudConnectionPayload) -> dict | None:
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

    def status(self) -> dict:
        settings = self.lifecycle.settings
        summary = self.lifecycle.camera_manager.summary()
        return {
            "node_id": self.lifecycle.node_id,
            "cloud_url": settings.cloud_url,
            "organization_id": settings.organization_id,
            "paired": bool(settings.cloud_token and settings.node_id),
            "cloud_configured": bool(settings.cloud_url),
            "queue_size": self.lifecycle.store.outbound_queue_size(),
            "cameras_total": summary["cameras_total"],
            "cameras_online": summary["cameras_online"],
            "cameras": summary["cameras"],
        }

    def sync_now(self) -> dict:
        if self.lifecycle.config_sync is None:
            return {"ok": False, "error": "Config sync service is not running."}
        cameras = self.lifecycle.config_sync.sync_once()
        return {"ok": True, "cameras_loaded": len(cameras), **self.status()}

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
        lifecycle = build_lifecycle()
        lifecycle.settings.data_dir.mkdir(parents=True, exist_ok=True)
        lifecycle.store.initialize()
        meta = lifecycle.store.meta_dict()
        if not meta.get("cloud_url") and not lifecycle.settings.cloud_url:
            return lifecycle
        settings = replace(
            lifecycle.settings,
            cloud_url=lifecycle.settings.cloud_url or meta.get("cloud_url"),
            node_id=lifecycle.settings.node_id or meta.get("node_id"),
            cloud_token=lifecycle.settings.cloud_token or meta.get("node_token"),
            organization_id=lifecycle.settings.organization_id or meta.get("organization_id"),
        )
        os.environ["CAMPEX_NODE_CLOUD_URL"] = settings.cloud_url or ""
        os.environ["CAMPEX_NODE_ID"] = settings.node_id or ""
        os.environ["CAMPEX_NODE_TOKEN"] = settings.cloud_token or ""
        os.environ["CAMPEX_NODE_ORGANIZATION_ID"] = settings.organization_id or ""
        return build_lifecycle(settings)


def create_app() -> FastAPI:
    runtime = LocalNodeRuntime()
    app = FastAPI(title="CAMPEX Node Local", version="0.1.0")
    app.state.runtime = runtime

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

    @app.post("/api/sync")
    def sync() -> dict:
        return runtime.sync_now()

    @app.get("/api/diagnostics")
    def diagnostics() -> dict:
        return runtime.diagnostics()

    return app


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
    code{color:var(--accent);word-break:break-all}.status{color:var(--muted);min-height:22px}@media(max-width:820px){.shell{grid-template-columns:1fr}.side{border-right:0;border-bottom:1px solid var(--border)}.grid{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}}
  </style>
</head>
<body>
  <div class="shell">
    <aside class="side"><div class="brand">CAMP<span>EX</span></div><p>Node local</p><p>Captura câmeras na rede da empresa e sincroniza com a Cloud.</p></aside>
    <main>
      <div class="top"><div><p class="eyebrow">Serviço local 24/7</p><h1>CAMPEX Node</h1></div><div class="badge" id="cloud-badge">Carregando...</div></div>
      <div class="grid">
        <section class="panel"><h2>Conexão com a Cloud</h2><form id="connect-form">
          <label>Backend Cloud</label><input name="cloud_url" placeholder="https://campexback.vercel.app/api/v1" />
          <label>Código de pareamento</label><input name="pairing_code" autocomplete="off" placeholder="CXP-7KQ2-N91P" />
          <label>Nome deste Node</label><input name="node_name" placeholder="RBA-NODE-01" />
          <div class="actions"><button type="submit">Parear Node</button><button class="secondary" type="button" id="sync-button">Sincronizar agora</button><button class="secondary" type="button" id="diagnostics-button">Diagnóstico</button></div>
          <p class="status" id="form-status"></p>
        </form></section>
        <section class="panel"><h2>Status</h2><div class="stats">
          <div class="stat"><strong id="total">0</strong><span>Câmeras</span></div>
          <div class="stat"><strong id="online">0</strong><span>Online</span></div>
          <div class="stat"><strong id="node">-</strong><span>Node</span></div>
          <div class="stat"><strong id="queue">0</strong><span>Fila</span></div>
        </div><p>Node ID: <code id="node-id">-</code></p></section>
      </div>
      <section class="panel" style="margin-top:16px"><h2>Câmeras sincronizadas</h2><div id="cameras"></div></section>
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
      statusEl.textContent=data.ok?(data.message||'Pareado. Sincronizando câmeras cadastradas.'):(data.error||'Falha ao parear.');
      await load();
    });
    document.querySelector('#sync-button').addEventListener('click',async()=>{statusEl.textContent='Sincronizando...';const res=await fetch('/api/sync',{method:'POST'});const data=await res.json();statusEl.textContent=data.ok?`Sincronizadas: ${data.cameras_loaded}`:(data.error||'Falha na sincronização');await load();});
    document.querySelector('#diagnostics-button').addEventListener('click',async()=>{const data=await fetch('/api/diagnostics').then(r=>r.json());statusEl.textContent=`Diagnóstico OK · fila ${data.queue_size} · câmeras ${data.cameras.cameras_total}`;});
    load(); setInterval(load,5000);
  </script>
</body>
</html>"""
