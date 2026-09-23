from __future__ import annotations

import os
from dataclasses import replace

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from campex_node.core.config import NodeSettings
from campex_node.main import build_lifecycle


class CloudConnectionPayload(BaseModel):
    cloud_url: str = Field(min_length=1, max_length=500)
    cloud_api_token: str = Field(default="", max_length=500)
    organization_id: str = Field(default="", max_length=120)


class LocalNodeRuntime:
    def __init__(self) -> None:
        self.lifecycle = self._build_from_persisted_connection()
        self.lifecycle.initialize()
        self.lifecycle.start()

    def reconfigure(self, payload: CloudConnectionPayload) -> None:
        self.lifecycle.stop()
        settings = replace(
            NodeSettings.from_env(),
            cloud_url=payload.cloud_url.rstrip("/"),
            cloud_api_token=payload.cloud_api_token.strip() or None,
            organization_id=payload.organization_id.strip() or None,
        )
        os.environ["CAMPEX_NODE_CLOUD_URL"] = settings.cloud_url or ""
        os.environ["CAMPEX_NODE_CLOUD_API_TOKEN"] = settings.cloud_api_token or ""
        os.environ["CAMPEX_NODE_ORGANIZATION_ID"] = settings.organization_id or ""
        self.lifecycle = build_lifecycle(settings)
        self.lifecycle.initialize()
        self.lifecycle.store.set_meta("cloud_url", settings.cloud_url or "")
        self.lifecycle.store.set_meta("cloud_api_token", settings.cloud_api_token or "")
        self.lifecycle.store.set_meta("organization_id", settings.organization_id or "")
        self.lifecycle.start()

    def status(self) -> dict:
        settings = self.lifecycle.settings
        summary = self.lifecycle.camera_manager.summary()
        return {
            "node_id": self.lifecycle.node_id,
            "cloud_url": settings.cloud_url,
            "organization_id": settings.organization_id,
            "cloud_configured": bool(settings.cloud_url),
            "cameras_total": summary["cameras_total"],
            "cameras_online": summary["cameras_online"],
            "cameras": summary["cameras"],
        }

    def sync_now(self) -> dict:
        if self.lifecycle.config_sync is None:
            return {"ok": False, "error": "Config sync service is not running."}
        cameras = self.lifecycle.config_sync.sync_once()
        return {"ok": True, "cameras_loaded": len(cameras), **self.status()}

    def _build_from_persisted_connection(self):
        lifecycle = build_lifecycle()
        lifecycle.settings.data_dir.mkdir(parents=True, exist_ok=True)
        lifecycle.store.initialize()
        meta = lifecycle.store.meta_dict()
        if not meta.get("cloud_url"):
            return lifecycle
        settings = replace(
            lifecycle.settings,
            cloud_url=meta.get("cloud_url") or lifecycle.settings.cloud_url,
            cloud_api_token=meta.get("cloud_api_token") or lifecycle.settings.cloud_api_token,
            organization_id=meta.get("organization_id") or lifecycle.settings.organization_id,
        )
        os.environ["CAMPEX_NODE_CLOUD_URL"] = settings.cloud_url or ""
        os.environ["CAMPEX_NODE_CLOUD_API_TOKEN"] = settings.cloud_api_token or ""
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
        runtime.reconfigure(payload)
        return {"ok": True, **runtime.status()}

    @app.post("/api/sync")
    def sync() -> dict:
        return runtime.sync_now()

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
          <label>Token da API</label><input name="cloud_api_token" type="password" autocomplete="off" placeholder="CAMPEXTOKEN / CAMPEX_API_TOKEN" />
          <label>Organização</label><input name="organization_id" placeholder="default" />
          <div class="actions"><button type="submit">Conectar</button><button class="secondary" type="button" id="sync-button">Sincronizar agora</button></div>
          <p class="status" id="form-status"></p>
        </form></section>
        <section class="panel"><h2>Status</h2><div class="stats">
          <div class="stat"><strong id="total">0</strong><span>Câmeras</span></div>
          <div class="stat"><strong id="online">0</strong><span>Online</span></div>
          <div class="stat"><strong id="node">-</strong><span>Node</span></div>
        </div><p>Node ID: <code id="node-id">-</code></p></section>
      </div>
      <section class="panel" style="margin-top:16px"><h2>Câmeras sincronizadas</h2><div id="cameras"></div></section>
    </main>
  </div>
  <script>
    const statusEl=document.querySelector('#form-status');
    async function load(){
      const data=await fetch('/api/status').then(r=>r.json());
      document.querySelector('#cloud-badge').textContent=data.cloud_configured?'Cloud configurada':'Cloud não configurada';
      document.querySelector('#total').textContent=data.cameras_total;
      document.querySelector('#online').textContent=data.cameras_online;
      document.querySelector('#node').textContent=data.node_id.slice(5,9);
      document.querySelector('#node-id').textContent=data.node_id;
      document.querySelector('[name=cloud_url]').value=data.cloud_url||'';
      document.querySelector('[name=organization_id]').value=data.organization_id||'';
      document.querySelector('#cameras').innerHTML=(data.cameras||[]).map(cam=>`<div class="camera ${cam.status}"><span><span class="dot"></span>${cam.name}</span><span>${cam.status}</span></div>`).join('')||'<p class="status">Nenhuma câmera sincronizada ainda.</p>';
    }
    document.querySelector('#connect-form').addEventListener('submit',async e=>{
      e.preventDefault(); statusEl.textContent='Conectando...';
      const form=e.currentTarget;
      const body={cloud_url:form.cloud_url.value,cloud_api_token:form.cloud_api_token.value,organization_id:form.organization_id.value};
      const res=await fetch('/api/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      statusEl.textContent=res.ok?'Conectado. Sincronizando câmeras cadastradas.':'Falha ao conectar.';
      await load();
    });
    document.querySelector('#sync-button').addEventListener('click',async()=>{statusEl.textContent='Sincronizando...';const res=await fetch('/api/sync',{method:'POST'});const data=await res.json();statusEl.textContent=data.ok?`Sincronizadas: ${data.cameras_loaded}`:(data.error||'Falha na sincronização');await load();});
    load(); setInterval(load,5000);
  </script>
</body>
</html>"""
