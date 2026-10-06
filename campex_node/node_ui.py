from __future__ import annotations


# The Node's own page. It shares /css/tokens.css with the CAMPEX panel and
# loads nothing from the internet: the Node often runs on a factory network.
NODE_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CAMPEX Node</title>
  <link rel="icon" href="/assets/simbolo.jpeg" />
  <link rel="stylesheet" href="/css/tokens.css" />
  <style>
    *{box-sizing:border-box}
    html{color-scheme:dark}
    body{margin:0;min-height:100vh;background:var(--color-canvas);color:var(--color-text);font:14px/1.5 var(--font-sans);font-feature-settings:"tnum" 1}
    button,input{font:inherit;color:inherit}
    a{color:inherit;text-decoration:none}
    svg{flex:none}
    :focus-visible{outline:2px solid var(--color-accent);outline-offset:2px}
    [hidden]{display:none!important}

    .shell{display:grid;grid-template-columns:var(--sidebar-width) minmax(0,1fr);min-height:100vh}
    .side{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:1.25rem;padding:1.1rem .75rem 1rem;background:var(--color-ink);border-right:1px solid var(--color-border-soft)}
    .brand{display:flex;align-items:center;gap:.6rem;min-height:2.5rem;padding:0 .35rem}
    .brand img{width:7.4rem;height:auto}
    .brand-tag{padding:.1rem .45rem;border:1px solid var(--color-border);border-radius:var(--radius-chip);color:var(--color-muted);font-size:.72rem;font-weight:600}
    .nav{display:grid;gap:1.1rem}
    .nav-group{display:grid;gap:1px}
    .nav-group h2{margin:0 0 .3rem;padding:0 .6rem;color:var(--color-subtle);font-size:.75rem;font-weight:600}
    .nav-group button{position:relative;display:flex;align-items:center;gap:.65rem;min-height:2.1rem;padding:.4rem .6rem;border:0;border-radius:var(--radius-control);background:none;color:var(--color-muted);font-size:.9rem;font-weight:500;text-align:left;cursor:pointer}
    .nav-group button svg{width:1.05rem;height:1.05rem;stroke-width:1.8}
    .nav-group button:hover{background:var(--color-panel);color:var(--color-text)}
    .nav-group button[aria-current=page]{background:var(--color-panel-soft);color:var(--color-text)}
    .nav-group button[aria-current=page]::before{content:"";position:absolute;left:-.75rem;top:.45rem;bottom:.45rem;width:2px;border-radius:0 2px 2px 0;background:var(--color-accent)}
    .nav-group button[aria-current=page] svg{color:var(--color-accent)}
    .side-foot{margin-top:auto;padding:0 .6rem;color:var(--color-subtle);font-size:.78rem}
    .side-foot strong{display:flex;align-items:center;gap:.45rem;color:var(--color-text);font-weight:550;font-size:.84rem;margin-bottom:.15rem}

    .work{min-width:0;display:flex;flex-direction:column}
    .top{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:1rem;min-height:var(--topbar-height);padding:.75rem var(--workspace-pad);background:rgba(14,17,20,.92);backdrop-filter:blur(10px);border-bottom:1px solid var(--color-border-soft)}
    .top h1{margin:0;font:650 1.35rem/1.2 var(--font-display);letter-spacing:-.01em}
    .top-actions{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;justify-content:flex-end}
    main{padding:var(--workspace-pad);display:grid;gap:1.25rem;max-width:100rem;width:100%}

    .chip{display:inline-flex;align-items:center;gap:.45rem;min-height:2rem;padding:0 .75rem;border:1px solid var(--color-border-soft);border-radius:var(--radius-chip);color:var(--color-muted);font-size:.8rem;font-weight:500;white-space:nowrap}
    .dot{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;background:var(--color-subtle)}
    .ok .dot,.dot.ok{background:var(--color-online)}
    .warn .dot,.dot.warn{background:var(--color-attention)}
    .bad .dot,.dot.bad{background:var(--color-critical)}

    .btn{display:inline-flex;align-items:center;justify-content:center;gap:.45rem;min-height:var(--control-height);padding:0 .9rem;border:1px solid var(--color-border);border-radius:var(--radius-control);background:transparent;color:var(--color-text);font-weight:550;font-size:.875rem;white-space:nowrap;cursor:pointer}
    .btn svg{width:1rem;height:1rem}
    .btn:hover{border-color:var(--color-subtle);background:var(--color-panel-soft)}
    .btn.primary{border-color:var(--color-paper);background:var(--color-paper);color:var(--color-black);font-weight:600}
    .btn.primary:hover{background:#fff;border-color:#fff}
    .btn:disabled{opacity:.55;cursor:default}

    .panel{border:1px solid var(--color-border-soft);border-radius:var(--radius);background:var(--color-panel)}
    .panel-head{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:.9rem 1rem;border-bottom:1px solid var(--color-border-soft)}
    .panel-head h2{margin:0;font-size:.95rem;font-weight:600}
    .panel-head p{margin:.1rem 0 0;color:var(--color-muted);font-size:.82rem}
    .panel-body{padding:1rem}
    .cols{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(18rem,1fr);gap:1.25rem;align-items:start}

    /* Instrument rack: one row per camera, readings as gauges. */
    .summary{display:flex;align-items:baseline;gap:.6rem;flex-wrap:wrap;min-width:0}
    .summary strong{font:650 2rem/1 var(--font-display);letter-spacing:-.02em}
    .summary span{color:var(--color-muted)}
    .rack{display:grid}
    .instrument{display:grid;grid-template-columns:minmax(11rem,1.2fr) repeat(4,minmax(5.5rem,1fr)) minmax(8rem,1.1fr);align-items:center;gap:1rem;padding:.95rem 1rem;border-top:1px solid var(--color-border-soft)}
    .instrument:first-child{border-top:0}
    .instrument-name strong{display:block;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .instrument-name .state{display:inline-flex;align-items:center;gap:.4rem;color:var(--color-muted);font-size:.8rem}
    .reading b{display:block;font:600 1.35rem/1.15 var(--font-display);letter-spacing:-.01em}
    .reading small{color:var(--color-subtle);font-size:.75rem}
    .reading.idle b{color:var(--color-subtle)}
    .trace{width:100%;height:2.4rem;display:block}
    .trace polyline{fill:none;stroke:var(--color-accent);stroke-width:1.5;vector-effect:non-scaling-stroke}
    .trace line{stroke:var(--color-border-soft);stroke-width:1;vector-effect:non-scaling-stroke}

    .rows{display:grid}
    .row{display:grid;grid-template-columns:4.5rem minmax(0,1fr) auto;gap:.85rem;align-items:center;padding:.7rem 1rem;border-top:1px solid var(--color-border-soft)}
    .row:first-child{border-top:0}
    .row time{color:var(--color-muted);font-size:.82rem}
    .row strong{display:block;font-weight:550}
    .row small{color:var(--color-muted);font-size:.8rem}
    .row .tail{display:flex;align-items:center;gap:.6rem;color:var(--color-muted);font-size:.82rem}
    .row .tail svg{width:1rem;height:1rem;color:var(--color-subtle)}
    .row.clickable{cursor:pointer}
    .row.clickable:hover{background:var(--color-panel-soft)}
    .facts{display:grid;margin:0}
    .facts div{display:flex;justify-content:space-between;gap:1rem;padding:.6rem 1rem;border-top:1px solid var(--color-border-soft)}
    .facts div:first-child{border-top:0}
    .facts dt{color:var(--color-muted)}
    .facts dd{margin:0;font-weight:550;text-align:right;overflow-wrap:anywhere}
    .badge{display:inline-flex;align-items:center;gap:.35rem;padding:.12rem .5rem;border-radius:var(--radius-chip);background:var(--color-panel-soft);color:var(--color-muted);font-size:.75rem;font-weight:550}
    .badge.open{background:rgba(229,174,72,.12);color:var(--color-attention)}
    .badge.critical{background:rgba(238,100,97,.12);color:var(--color-critical)}
    .empty{padding:1.75rem 1rem;color:var(--color-muted);text-align:center}
    .empty strong{display:block;color:var(--color-text);font-weight:600;margin-bottom:.25rem}

    form{display:grid;gap:.85rem}
    label{display:grid;gap:.35rem;color:var(--color-muted);font-size:.8rem;font-weight:500}
    input[type=text],input:not([type]){height:var(--control-height);padding:0 .7rem;border:1px solid var(--color-border);border-radius:var(--radius-control);background:var(--color-ink);color:var(--color-text)}
    input::placeholder{color:var(--color-subtle)}
    .check{display:flex;align-items:center;gap:.5rem;color:var(--color-text);font-size:.875rem}
    .actions{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}
    .note{min-height:1.2rem;margin:0;color:var(--color-muted);font-size:.82rem}
    .code{font:600 1.6rem/1.2 var(--font-mono);letter-spacing:.12em}
    code{font-family:var(--font-mono);font-size:.82rem;color:var(--color-text)}

    .player{display:grid;gap:.75rem}
    .player video,.player img{width:100%;border-radius:var(--radius-control);background:#000;aspect-ratio:16/9;object-fit:contain}

    @media (max-width:1100px){.cols{grid-template-columns:1fr}.instrument{grid-template-columns:minmax(0,1fr) repeat(2,minmax(5rem,1fr))}.instrument .reading.secondary,.instrument .trace-cell{display:none}}
    @media (max-width:760px){.shell{grid-template-columns:minmax(0,1fr)}.side{position:static;height:auto;padding-bottom:.5rem}.nav{grid-auto-flow:column;justify-content:start;overflow-x:auto;scrollbar-width:none}.nav-group h2,.side-foot{display:none}.nav-group{grid-auto-flow:column}.nav-group button{white-space:nowrap}.nav-group button[aria-current=page]::before{display:none}.top{position:static;flex-direction:column;align-items:stretch}.top-actions{justify-content:flex-start}.top-actions .chip{order:3}.row{grid-template-columns:4rem minmax(0,1fr)}.row .tail{grid-column:2}.instrument{grid-template-columns:1fr 1fr}.instrument-name{grid-column:1/-1}.facts div{padding-inline:.85rem}}
    @media (prefers-reduced-motion:reduce){*{transition:none!important}}
  </style>
</head>
<body>
<div class="shell">
  <aside class="side" aria-label="Navegação do Node">
    <div class="brand"><img src="/assets/campex-logo-white.png" alt="CAMPEX" /><span class="brand-tag">Node</span></div>
    <nav class="nav">
      <section class="nav-group" aria-label="Operação">
        <h2>Operação</h2>
        <button type="button" data-page="overview" aria-current="page"><i data-lucide="gauge"></i>Visão geral</button>
        <button type="button" data-page="cameras"><i data-lucide="cctv"></i>Câmeras</button>
        <button type="button" data-page="events"><i data-lucide="siren"></i>Eventos</button>
      </section>
      <section class="nav-group" aria-label="Sistema">
        <h2>Sistema</h2>
        <button type="button" data-page="sync"><i data-lucide="cloud-upload"></i>Sincronização</button>
        <button type="button" data-page="diagnostics"><i data-lucide="activity"></i>Diagnóstico</button>
        <button type="button" data-page="settings"><i data-lucide="settings"></i>Configurações</button>
      </section>
    </nav>
    <div class="side-foot">
      <strong><span class="dot ok" id="service-dot"></span><span id="service-label">Serviço em execução</span></strong>
      <span id="version-label">Versão —</span>
    </div>
  </aside>

  <div class="work">
    <header class="top">
      <h1 id="page-title">Visão geral</h1>
      <div class="top-actions">
        <span class="chip" id="cloud-chip"><span class="dot"></span><span>Verificando Cloud</span></span>
        <button class="btn" type="button" id="sync-button"><i data-lucide="refresh-cw"></i>Sincronizar agora</button>
        <a class="btn primary" href="/app"><i data-lucide="layout-dashboard"></i>Abrir painel CAMPEX</a>
      </div>
    </header>

    <main>
      <section data-view="overview">
        <div style="display:grid;gap:1.25rem">
          <div class="summary"><strong id="summary-count">—</strong><span id="summary-text">Lendo câmeras deste Node…</span></div>
          <section class="panel">
            <div class="panel-head"><div><h2>Câmeras</h2><p>Leituras ao vivo de captura e visão, atualizadas a cada 3 segundos</p></div></div>
            <div class="rack" id="rack-overview"></div>
          </section>
          <div class="cols">
            <section class="panel">
              <div class="panel-head"><div><h2>Eventos recentes</h2><p>Gravados neste computador, com clipe de evidência</p></div><button class="btn" type="button" data-jump="events">Ver todos</button></div>
              <div class="rows" id="events-overview"></div>
            </section>
            <section class="panel">
              <div class="panel-head"><h2>Saúde do Node</h2></div>
              <dl class="facts" id="health-overview"></dl>
            </section>
          </div>
        </div>
      </section>

      <section data-view="cameras" hidden>
        <div class="cols">
          <section class="panel">
            <div class="panel-head"><div><h2>Câmeras deste Node</h2><p>As câmeras da Cloud chegam sozinhas; câmeras locais servem para teste</p></div></div>
            <div class="rack" id="rack-cameras"></div>
          </section>
          <section class="panel">
            <div class="panel-head"><h2>Adicionar câmera RTSP</h2></div>
            <form class="panel-body" id="camera-form">
              <label>Nome<input name="camera_name" placeholder="Portão principal" required /></label>
              <label>Endereço RTSP<input name="rtsp_url" placeholder="rtsp://usuario:senha@192.168.1.10/stream" required /></label>
              <label class="check"><input name="enabled" type="checkbox" checked />Começar a capturar ao salvar</label>
              <div class="actions">
                <button class="btn" type="button" id="test-camera-button"><i data-lucide="plug-zap"></i>Testar conexão</button>
                <button class="btn primary" type="submit"><i data-lucide="plus"></i>Salvar câmera</button>
              </div>
              <p class="note" id="camera-form-status" role="status"></p>
            </form>
          </section>
        </div>
      </section>

      <section data-view="events" hidden>
        <div class="cols">
          <section class="panel">
            <div class="panel-head"><div><h2>Eventos</h2><p>Início, fim e duração de cada ocorrência nas zonas</p></div><button class="btn" type="button" id="events-refresh"><i data-lucide="refresh-cw"></i>Atualizar</button></div>
            <div class="rows" id="events-page"></div>
          </section>
          <section class="panel" id="event-detail">
            <div class="panel-head"><h2>Evidência</h2></div>
            <div class="empty"><strong>Nenhum evento selecionado</strong>Escolha um evento na lista para ver o clipe.</div>
          </section>
        </div>
      </section>

      <section data-view="sync" hidden>
        <div class="cols">
          <section class="panel">
            <div class="panel-head"><div><h2>Fila de envio</h2><p>O que este Node guarda até a Cloud confirmar o recebimento</p></div></div>
            <dl class="facts" id="sync-facts"></dl>
          </section>
          <section class="panel">
            <div class="panel-head"><div><h2>Parear com a CAMPEX Cloud</h2><p>Necessário só na instalação ou ao trocar de empresa</p></div></div>
            <form class="panel-body" id="connect-form">
              <label>Endereço da Cloud<input name="cloud_url" placeholder="Em branco para operar só localmente" /></label>
              <label>Nome deste Node<input name="node_name" placeholder="RBA-NODE-01" /></label>
              <div class="actions">
                <button class="btn primary" type="button" id="start-pairing-button"><i data-lucide="key-round"></i>Gerar código de pareamento</button>
              </div>
              <details>
                <summary class="note" style="cursor:pointer">Tenho um código gerado no painel</summary>
                <div style="display:grid;gap:.85rem;margin-top:.85rem">
                  <label>Código<input name="pairing_code" autocomplete="off" placeholder="CXP-7KQ2-N91P" /></label>
                  <div class="actions"><button class="btn" type="submit"><i data-lucide="link"></i>Parear com este código</button></div>
                </div>
              </details>
              <div id="pairing-box" hidden></div>
              <p class="note" id="form-status" role="status"></p>
            </form>
          </section>
        </div>
      </section>

      <section data-view="diagnostics" hidden>
        <section class="panel">
          <div class="panel-head"><div><h2>Diagnóstico</h2><p>Estado de cada parte do Node, do mais crítico ao menos crítico</p></div><button class="btn" type="button" id="diagnostics-refresh"><i data-lucide="refresh-cw"></i>Verificar agora</button></div>
          <div class="rows" id="diagnostics-rows"></div>
        </section>
      </section>

      <section data-view="settings" hidden>
        <div class="cols">
          <section class="panel">
            <div class="panel-head"><h2>Identidade</h2></div>
            <dl class="facts" id="identity-facts"></dl>
          </section>
          <section class="panel">
            <div class="panel-head"><div><h2>Atualizações</h2><p>O Node baixa, confere a assinatura e instala versões novas sozinho</p></div></div>
            <dl class="facts" id="update-facts"></dl>
          </section>
        </div>
      </section>
    </main>
  </div>
</div>

<script src="/vendor/lucide.min.js"></script>
<script>
  const TITLES = {overview: "Visão geral", cameras: "Câmeras", events: "Eventos", sync: "Sincronização", diagnostics: "Diagnóstico", settings: "Configurações"};
  const EVENT_LABELS = {
    PERSON_MONITORED_ZONE: "Pessoa na zona",
    PERSON_RESTRICTED_ZONE: "Pessoa em zona restrita",
    PERSON_RESTRICTED_ZONE_DWELL: "Permanência em zona restrita",
  };
  const STATE_LABELS = {ONLINE: "Recebendo imagem", CONNECTING: "Conectando", DEGRADED: "Instável", OFFLINE: "Sem imagem", STALE: "Imagem parada"};
  const $ = (selector) => document.querySelector(selector);
  const history = {};
  let status = null;
  let events = [];
  let selectedEvent = null;

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  }
  function icons() { if (window.lucide) window.lucide.createIcons(); }
  function tone(state) { return state === "ONLINE" ? "ok" : state === "OFFLINE" ? "bad" : "warn"; }
  function clock(value) { return value ? new Date(value).toLocaleTimeString("pt-BR", {hour: "2-digit", minute: "2-digit", second: "2-digit"}) : "—"; }
  function dateTime(value) { return value ? new Date(value).toLocaleString("pt-BR") : "nunca"; }
  function ago(value) {
    if (!value) return "nunca";
    const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
    if (seconds < 2) return "agora";
    if (seconds < 60) return `há ${seconds} s`;
    if (seconds < 3600) return `há ${Math.round(seconds / 60)} min`;
    return `há ${Math.round(seconds / 3600)} h`;
  }
  function duration(seconds) {
    if (seconds == null) return "em andamento";
    const s = Math.round(Number(seconds));
    return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s`;
  }
  function uptime(seconds) {
    const s = Number(seconds || 0);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h} h ${m} min` : `${m} min`;
  }
  function percent(value) { return value == null ? "n/d" : `${Math.round(value)}%`; }
  function facts(rows) { return rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${v}</dd></div>`).join(""); }

  async function getJson(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  function trace(values) {
    const points = values.slice(-30);
    if (points.length < 2) return `<svg class="trace" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true"><line x1="0" y1="23" x2="100" y2="23"/></svg>`;
    const max = Math.max(1, ...points);
    const step = 100 / 29;
    const offset = (30 - points.length) * step;
    const line = points.map((v, i) => `${(offset + i * step).toFixed(1)},${(22 - (v / max) * 20).toFixed(1)}`).join(" ");
    return `<svg class="trace" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true"><line x1="0" y1="23" x2="100" y2="23"/><polyline points="${line}"/></svg>`;
  }

  function instrument(camera) {
    const vision = camera.vision || {};
    const metrics = vision.metrics || {};
    const state = camera.status || "OFFLINE";
    const visionOn = vision.status === "RUNNING";
    const visionFps = visionOn ? Number(metrics.vision_fps || 0) : null;
    const cameraFps = metrics.camera_fps ? Number(metrics.camera_fps) : null;
    return `<div class="instrument">
      <div class="instrument-name"><strong title="${esc(camera.name)}">${esc(camera.name)}</strong><span class="state ${tone(state)}"><span class="dot"></span>${esc(STATE_LABELS[state] || state)}</span></div>
      <div class="reading ${cameraFps == null ? "idle" : ""}"><b>${cameraFps == null ? "—" : cameraFps.toFixed(1)}</b><small>FPS da câmera</small></div>
      <div class="reading ${visionFps == null ? "idle" : ""}"><b>${visionFps == null ? "—" : visionFps.toFixed(1)}</b><small>${visionOn ? "FPS da visão" : "Visão desligada"}</small></div>
      <div class="reading secondary"><b>${Number(metrics.frames_processed || 0).toLocaleString("pt-BR")}</b><small>Frames analisados</small></div>
      <div class="reading secondary"><b>${esc(ago(camera.last_frame_at).replace("há ", ""))}</b><small>Último frame</small></div>
      <div class="trace-cell">${trace(history[camera.id] || [])}<small style="color:var(--color-subtle);font-size:.75rem">Visão, últimos 90 s</small></div>
    </div>`;
  }

  function eventRow(event, clickable) {
    const zone = event.metadata?.zone_name || event.zone_id || "Zona";
    const camera = (status?.cameras || []).find((c) => c.id === event.camera_id)?.name || event.camera_id;
    const open = !event.ended_at && event.status === "OPEN";
    const clip = event.metadata?.clip_path ? `<i data-lucide="film" aria-label="Com clipe"></i>` : "";
    return `<div class="row ${clickable ? "clickable" : ""}" ${clickable ? `data-event="${esc(event.id)}" tabindex="0" role="button"` : ""}>
      <time datetime="${esc(event.started_at)}">${clock(event.started_at)}</time>
      <div><strong>${esc(EVENT_LABELS[event.type] || event.type)}</strong><small>${esc(zone)}, ${esc(camera)}</small></div>
      <div class="tail">${clip}${open ? `<span class="badge open">Em andamento</span>` : `<span>${duration(event.duration)}</span>`}</div>
    </div>`;
  }

  function renderRack(target, cameras) {
    $(target).innerHTML = cameras.length
      ? cameras.map(instrument).join("")
      : `<div class="empty"><strong>Nenhuma câmera neste Node</strong>Pareie com a CAMPEX Cloud em Sincronização ou adicione uma câmera RTSP em Câmeras.</div>`;
  }

  function renderEvents() {
    const empty = `<div class="empty"><strong>Nenhum evento gravado</strong>Desenhe uma zona no painel CAMPEX, em Áreas e zonas, e ligue a visão da câmera.</div>`;
    $("#events-overview").innerHTML = events.length ? events.slice(0, 6).map((e) => eventRow(e, false)).join("") : empty;
    $("#events-page").innerHTML = events.length ? events.map((e) => eventRow(e, true)).join("") : empty;
    icons();
  }

  function renderEventDetail(event) {
    const panel = $("#event-detail");
    const clip = event.metadata?.clip_path;
    const snapshot = event.metadata?.overlay_path;
    const media = clip
      ? `<video src="/api/events/${encodeURIComponent(event.id)}/evidence?variant=clip" controls playsinline preload="metadata"></video>`
      : snapshot ? `<img src="/api/events/${encodeURIComponent(event.id)}/evidence?variant=overlay" alt="Imagem do início do evento" />`
      : `<div class="empty"><strong>Sem evidência ainda</strong>O clipe é salvo alguns segundos depois que o evento termina.</div>`;
    panel.innerHTML = `<div class="panel-head"><h2>${esc(EVENT_LABELS[event.type] || event.type)}</h2><span class="badge">${esc(event.id)}</span></div>
      <div class="panel-body player">${media}</div>
      <dl class="facts">${facts([
        ["Zona", esc(event.metadata?.zone_name || event.zone_id || "—")],
        ["Início", esc(dateTime(event.started_at))],
        ["Fim", esc(event.ended_at ? dateTime(event.ended_at) : "em andamento")],
        ["Duração", esc(duration(event.duration))],
        ["Pessoa (track)", esc(event.track_id ?? "—")],
      ])}</dl>`;
  }

  function renderStatus() {
    const s = status;
    const cameras = s.cameras || [];
    const online = cameras.filter((c) => c.status === "ONLINE").length;
    const watching = cameras.filter((c) => c.vision?.status === "RUNNING").length;
    $("#summary-count").textContent = cameras.length ? `${online} de ${cameras.length}` : "0";
    $("#summary-text").textContent = cameras.length
      ? `câmeras recebendo imagem, ${watching} com visão ligada`
      : "câmeras neste Node";
    renderRack("#rack-overview", cameras);
    renderRack("#rack-cameras", cameras);

    const cloudTone = s.paired && s.last_cloud_ok_at ? "ok" : s.paired ? "warn" : "";
    const cloudText = s.paired ? (s.last_cloud_ok_at ? "Conectado à Cloud" : "Cloud sem resposta") : "Operando só localmente";
    $("#cloud-chip").className = `chip ${cloudTone}`;
    $("#cloud-chip").lastElementChild.textContent = cloudText;
    $("#version-label").textContent = `Versão ${s.version || "—"}`;

    $("#health-overview").innerHTML = facts([
      ["Ligado há", esc(uptime(s.uptime_seconds))],
      ["CPU", esc(percent(s.cpu_percent))],
      ["Memória", esc(percent(s.ram_percent))],
      ["Na fila de envio", esc(s.queue_size ?? 0)],
      ["Última sincronização", esc(s.last_sync_at ? ago(s.last_sync_at) : "nunca")],
    ]);
    $("#sync-facts").innerHTML = facts([
      ["Situação", esc(cloudText)],
      ["Itens aguardando envio", esc(s.queue_size ?? 0)],
      ["Última sincronização", esc(dateTime(s.last_sync_at))],
      ["Último heartbeat", esc(dateTime(s.last_heartbeat_at))],
      ["Último erro da Cloud", esc(s.last_cloud_error || "nenhum")],
    ]);
    $("#identity-facts").innerHTML = facts([
      ["ID do Node", `<code>${esc(s.node_id || "—")}</code>`],
      ["Empresa", esc(s.organization_id || "—")],
      ["Cloud", `<code>${esc(s.cloud_url || "não configurada")}</code>`],
      ["Pasta de dados", `<code>${esc(s.data_dir || "—")}</code>`],
      ["Logs", `<code>${esc(s.logs_dir || "—")}</code>`],
    ]);
    const update = s.update || {};
    $("#update-facts").innerHTML = facts([
      ["Versão instalada", esc(s.version || "—")],
      ["Situação", esc(update.message || update.state || "aguardando a primeira verificação")],
      ["Versão disponível", esc(update.available_version || "nenhuma")],
      ["Última verificação", esc(dateTime(update.checked_at))],
    ]);

    const checks = [
      [cameras.length === 0 ? "warn" : online < cameras.length ? "warn" : "ok", "Câmeras", `${online} de ${cameras.length} recebendo imagem`],
      [watching ? "ok" : "warn", "Visão", watching ? `${watching} câmera(s) com detecção ligada` : "Nenhuma câmera com detecção ligada"],
      [s.paired ? (s.last_cloud_ok_at ? "ok" : "bad") : "warn", "Cloud", s.last_cloud_error || cloudText],
      [Number(s.queue_size || 0) > 500 ? "warn" : "ok", "Fila de envio", `${s.queue_size ?? 0} item(ns) aguardando`],
      [Number(s.ram_percent || 0) > 90 ? "bad" : "ok", "Memória", `${percent(s.ram_percent)} em uso`],
      ["ok", "Banco local", "SQLite gravando em " + (s.data_dir || "pasta do usuário")],
    ];
    const order = {bad: 0, warn: 1, ok: 2};
    $("#diagnostics-rows").innerHTML = checks.sort((a, b) => order[a[0]] - order[b[0]]).map(([t, name, detail]) =>
      `<div class="row"><span class="${t}"><span class="dot"></span></span><div><strong>${esc(name)}</strong><small>${esc(detail)}</small></div><div class="tail">${t === "ok" ? "OK" : t === "warn" ? "Atenção" : "Falha"}</div></div>`
    ).join("");
  }

  async function load() {
    try {
      const data = await getJson("/api/status");
      await Promise.all((data.cameras || []).map(async (camera) => {
        try { camera.vision = await getJson(`/api/cameras/${encodeURIComponent(camera.id)}/vision/status`); } catch { camera.vision = null; }
        const fps = camera.vision?.status === "RUNNING" ? Number(camera.vision?.metrics?.vision_fps || 0) : 0;
        (history[camera.id] ||= []).push(fps);
        if (history[camera.id].length > 30) history[camera.id].shift();
      }));
      status = data;
      $("#service-dot").className = "dot ok";
      $("#service-label").textContent = "Serviço em execução";
      renderStatus();
    } catch {
      $("#service-dot").className = "dot bad";
      $("#service-label").textContent = "Node sem resposta";
    }
    try { events = await getJson("/api/events?limit=50"); renderEvents(); } catch { /* the rest of the page still works */ }
    icons();
  }

  function setPage(page) {
    document.querySelectorAll("[data-view]").forEach((el) => { el.hidden = el.dataset.view !== page; });
    document.querySelectorAll(".nav-group button").forEach((b) => {
      if (b.dataset.page === page) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    $("#page-title").textContent = TITLES[page];
  }
  document.querySelectorAll("[data-page],[data-jump]").forEach((b) => b.addEventListener("click", () => setPage(b.dataset.page || b.dataset.jump)));

  function selectEvent(id) {
    const event = events.find((e) => e.id === id);
    if (event) { selectedEvent = id; renderEventDetail(event); }
  }
  $("#events-page").addEventListener("click", (e) => { const row = e.target.closest("[data-event]"); if (row) selectEvent(row.dataset.event); });
  $("#events-page").addEventListener("keydown", (e) => { const row = e.target.closest("[data-event]"); if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); selectEvent(row.dataset.event); } });
  $("#events-refresh").addEventListener("click", load);
  $("#diagnostics-refresh").addEventListener("click", load);

  $("#sync-button").addEventListener("click", async () => {
    const button = $("#sync-button");
    button.disabled = true;
    try {
      const data = await getJson("/api/sync", {method: "POST"});
      button.lastChild.textContent = data.ok ? "Sincronizado" : "Falhou, tente de novo";
    } catch { button.lastChild.textContent = "Falhou, tente de novo"; }
    setTimeout(() => { button.disabled = false; button.lastChild.textContent = "Sincronizar agora"; }, 2500);
    load();
  });

  const cameraNote = $("#camera-form-status");
  $("#test-camera-button").addEventListener("click", async () => {
    const form = $("#camera-form");
    cameraNote.textContent = "Testando a conexão RTSP…";
    try {
      const data = await getJson("/api/cameras/test", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({rtsp_url: form.rtsp_url.value})});
      cameraNote.textContent = data.ok ? `Conexão OK, imagem de ${data.resolution?.width || "?"}×${data.resolution?.height || "?"}.` : (data.error || "Não foi possível abrir a câmera. Confira endereço, usuário e senha.");
    } catch { cameraNote.textContent = "O Node não respondeu. Confira se ele está aberto e tente de novo."; }
  });
  $("#camera-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    cameraNote.textContent = "Salvando câmera…";
    try {
      const response = await fetch("/api/cameras", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name: form.camera_name.value, rtsp_url: form.rtsp_url.value, enabled: form.enabled.checked})});
      const data = await response.json();
      const saved = response.ok && data.ok !== false && (data.ok || data.id);
      cameraNote.textContent = saved ? "Câmera salva." : (data.error || "Não foi possível salvar a câmera.");
      if (saved) form.reset();
      load();
    } catch { cameraNote.textContent = "O Node não respondeu. Confira se ele está aberto e tente de novo."; }
  });

  const pairNote = $("#form-status");
  let pairingTimer = null;
  $("#connect-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    pairNote.textContent = "Pareando…";
    try {
      const data = await getJson("/api/connect", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({cloud_url: form.cloud_url.value, pairing_code: form.pairing_code.value, node_name: form.node_name.value})});
      pairNote.textContent = data.ok ? (data.message || "Pareado. O Node vai buscar as câmeras da Cloud.") : (data.error || "Código não aceito. Confira e tente de novo.");
    } catch { pairNote.textContent = "O Node não respondeu. Tente de novo em alguns segundos."; }
    load();
  });
  $("#start-pairing-button").addEventListener("click", async () => {
    const form = $("#connect-form");
    pairNote.textContent = "Gerando código…";
    try {
      const data = await getJson("/api/pairing/start", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({cloud_url: form.cloud_url.value, node_name: form.node_name.value || "CAMPEX Node"})});
      if (!data.ok) { pairNote.textContent = data.error || "Não foi possível gerar o código."; return; }
      const box = $("#pairing-box");
      box.hidden = false;
      box.innerHTML = `<div class="code">${esc(data.pairing_code)}</div><p class="note">${data.mode === "local" ? "Sem Cloud configurada, o código fica guardado até a Cloud existir." : "No painel CAMPEX, abra Nodes, clique em Conectar Node e digite este código."} Vale até ${esc(dateTime(data.expires_at))}.</p>`;
      pairNote.textContent = data.mode === "local" ? "O Node já opera localmente." : "Aguardando a confirmação no painel…";
      clearInterval(pairingTimer);
      pairingTimer = setInterval(async () => {
        const result = await getJson("/api/pairing/status").catch(() => ({}));
        if (result.status === "authorized") { clearInterval(pairingTimer); box.hidden = true; pairNote.textContent = "Node conectado à CAMPEX Cloud."; load(); }
        else if (result.status === "expired") { clearInterval(pairingTimer); pairNote.textContent = "O código expirou. Gere um novo."; }
      }, 3000);
    } catch { pairNote.textContent = "O Node não respondeu. Tente de novo em alguns segundos."; }
  });

  icons();
  load();
  setInterval(load, 3000);
</script>
</body>
</html>"""
