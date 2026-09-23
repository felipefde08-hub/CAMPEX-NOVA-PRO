import {
  createCamera,
  createInvestigation,
  createMachine,
  createMonitor,
  createCameraRoi,
  createRule,
  createZone,
  cleanupEvidence,
  analyzeVideo,
  cameraSnapshotUrl,
  deleteCamera,
  deleteEvent,
  deleteInvestigation,
  deleteMachine,
  deleteRule,
  deleteZone,
  eventEvidenceUrl,
  cameraStreamUrl,
  cameraVideoUrl,
  getCameraHealth,
  getCameraDiagnostics,
  getCameraProductivity,
  getHealth,
  getMappingPoses,
  getMonitorStatus,
  getOperationsDiagnostics,
  getOperationsSummary,
  getProductivitySummary,
  getRuntimeSettings,
  getStreamInfo,
  getVisionObjects,
  getVisionStatus,
  getVideoAnalysisStatus,
  getNotificationPreferences,
  listNotificationDeliveries,
  listEvents,
  listEvidence,
  listInvestigations,
  listMachines,
  listCameraTemplates,
  listRules,
  listCameras,
  listZones,
  listVideoAnalyses,
  sendNotificationReportNow,
  operationsStreamUrl,
  restartVision,
  setupDemo,
  startMapping,
  startVision,
  stopMapping,
  stopVision,
  simulateRule as simulateRuleRequest,
  testCamera,
  testCameraSource,
  testEmailNotification,
  testTelegramNotification,
  updateCamera,
  updateEvent,
  updateInvestigation,
  updateMachine,
  updateRule,
  updateZone,
  updateNotificationPreferences,
  debugVideoUrl,
  uploadedVideoUrl,
} from "./api.js";
import {
  createLocalAccount,
  getCurrentUser,
  listLocalUsers,
  signInLocal,
  signOutLocal,
} from "./auth.js";
import { currentRoute, routes } from "./state.js";

const VERCEL_SAFE_VIDEO_UPLOAD_BYTES = 4 * 1024 * 1024;

const statusElement = document.querySelector("#backend-status");
const statusText = statusElement.querySelector(".status-text");
const pageTitle = document.querySelector("#page-title");
const appView = document.querySelector("#app-view");
const appShell = document.querySelector("#app-shell");
const accountButton = document.querySelector("#account-button");
const accountName = document.querySelector("#account-name");
const navLinks = document.querySelectorAll("[data-route]");
const sidebarToggle = document.querySelector("#sidebar-toggle");
const toastStack = document.querySelector("#toast-stack");
let activeMediaCameraId = null;
let zoneDrawingPoints = [];
let zonePreviewZones = [];
let machineDrawingPoints = [];
let machinePreviewMachines = [];
let machineClickDetectMode = false;
let liveStatusRefreshInFlight = false;
let backendStatusRefreshInFlight = false;
let operationsStream = null;
let operationsStreamLastSummary = "";
let lastBackendState = "checking";
let backendFailureCount = 0;
const sidebarStorageKey = "campex.sidebar";
let camerasCache = [];
let rulesCache = [];
let editingCameraId = null;
let editingRuleId = null;
let cameraWizard = null;
let backendStatusTimer = null;
let liveStatusTimer = null;
let videoAnalysisTimer = null;
let sidebarReady = false;

function refreshIcons() {
  window.lucide?.createIcons({
    attrs: {
      "aria-hidden": "true",
      focusable: "false",
    },
  });
}

function setSidebarState(state) {
  if (!appShell || !sidebarToggle) return;
  const isCollapsed = state === "collapsed";
  appShell.dataset.sidebar = isCollapsed ? "collapsed" : "expanded";
  sidebarToggle.setAttribute("aria-expanded", String(!isCollapsed));
  sidebarToggle.setAttribute("aria-label", isCollapsed ? "Expandir menu" : "Recolher menu");
  sidebarToggle.innerHTML = `<i data-lucide="${isCollapsed ? "panel-left-open" : "panel-left-close"}" aria-hidden="true"></i>`;
  localStorage.setItem(sidebarStorageKey, appShell.dataset.sidebar);
  refreshIcons();
}

function setupSidebar() {
  if (!appShell || !sidebarToggle || sidebarReady) return;
  sidebarReady = true;
  setSidebarState(localStorage.getItem(sidebarStorageKey) || "expanded");
  sidebarToggle.addEventListener("click", () => {
    const nextState = appShell.dataset.sidebar === "collapsed" ? "expanded" : "collapsed";
    setSidebarState(nextState);
  });
}

function notify(title, detail = "", type = "info", timeout = 4200) {
  if (!toastStack) return;
  const toast = document.createElement("article");
  toast.className = "toast";
  toast.dataset.type = type;
  toast.innerHTML = `<strong>${title}</strong>${detail ? `<span>${detail}</span>` : ""}`;
  toastStack.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(6px)";
    setTimeout(() => toast.remove(), 180);
  }, timeout);
}

function notifyError(title, error) {
  notify(title, error?.message || String(error), "error", 6500);
}

function renderAuthScreen(mode = "login") {
  appShell.hidden = true;
  operationsStream?.close();
  operationsStream = null;
  const existing = document.querySelector("#auth-screen");
  existing?.remove();

  const authScreen = document.createElement("section");
  authScreen.className = "auth-screen";
  authScreen.id = "auth-screen";
  authScreen.innerHTML = `
    <div class="auth-card">
      <div class="auth-logo-block">
        <img src="./assets/campex-logo-white.png" alt="CAMPEX" />
      </div>

      <div class="auth-copy">
        <h1 id="auth-title">Acesse sua conta</h1>
        <p id="auth-subtitle">Entre para acompanhar câmeras, eventos e evidências.</p>
      </div>

      <form class="auth-form" id="login-form">
        <label class="auth-field">
          <span>E-mail</span>
          <div>
            <i data-lucide="mail" aria-hidden="true"></i>
            <input name="email" type="email" autocomplete="email" placeholder="seu@email.com" required />
          </div>
        </label>
        <label class="auth-field">
          <span>Senha</span>
          <div>
            <i data-lucide="lock-keyhole" aria-hidden="true"></i>
            <input name="password" type="password" autocomplete="current-password" placeholder="Sua senha" required />
          </div>
        </label>
        <p class="auth-error" data-auth-error="login"></p>
        <button class="auth-primary" type="submit"><span>Entrar</span><i data-lucide="arrow-right" aria-hidden="true"></i></button>
        <p class="auth-switch"><span>Ainda não tem uma conta?</span><button type="button" data-auth-tab="signup">Criar conta</button></p>
      </form>

      <form class="auth-form" id="signup-form">
        <label class="auth-field">
          <span>Nome</span>
          <div>
            <i data-lucide="user" aria-hidden="true"></i>
            <input name="name" autocomplete="name" placeholder="Seu nome" required minlength="2" />
          </div>
        </label>
        <label class="auth-field">
          <span>E-mail</span>
          <div>
            <i data-lucide="mail" aria-hidden="true"></i>
            <input name="email" type="email" autocomplete="email" placeholder="seu@email.com" required />
          </div>
        </label>
        <label class="auth-field">
          <span>Senha</span>
          <div>
            <i data-lucide="lock-keyhole" aria-hidden="true"></i>
            <input name="password" type="password" autocomplete="new-password" placeholder="Mínimo de 6 caracteres" required minlength="6" />
          </div>
        </label>
        <p class="auth-error" data-auth-error="signup"></p>
        <button class="auth-primary" type="submit"><span>Criar conta</span><i data-lucide="arrow-right" aria-hidden="true"></i></button>
        <p class="auth-switch"><span>Já tem uma conta?</span><button type="button" data-auth-tab="login">Entrar</button></p>
      </form>
    </div>
  `;  document.body.prepend(authScreen);

  authScreen.querySelector("#login-form").addEventListener("submit", handleLoginSubmit);
  authScreen.querySelector("#signup-form").addEventListener("submit", handleSignupSubmit);
  authScreen.querySelectorAll("[data-auth-tab]").forEach((button) => {
    button.addEventListener("click", () => setAuthMode(button.dataset.authTab));
  });
  setAuthMode(mode);
  refreshIcons();
}

function setAuthMode(mode) {
  const nextMode = mode === "signup" ? "signup" : "login";
  document.querySelectorAll("[data-auth-tab]").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.authTab === nextMode));
  });
  const title = document.querySelector("#auth-title");
  const subtitle = document.querySelector("#auth-subtitle");
  if (title) {
    title.textContent = nextMode === "signup" ? "Crie sua conta" : "Acesse sua conta";
  }
  if (subtitle) {
    subtitle.textContent = nextMode === "signup"
      ? "Cadastre seu acesso local em poucos segundos."
      : "Entre para acompanhar câmeras, eventos e evidências.";
  }
  const loginForm = document.querySelector("#login-form");
  const signupForm = document.querySelector("#signup-form");
  if (loginForm) loginForm.hidden = nextMode !== "login";
  if (signupForm) signupForm.hidden = nextMode !== "signup";
}

async function handleLoginSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await submitAuthForm(form, "login", () => signInLocal({
    email: form.elements.email.value,
    password: form.elements.password.value,
  }));
}

async function handleSignupSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await submitAuthForm(form, "signup", () => createLocalAccount({
    name: form.elements.name.value,
    email: form.elements.email.value,
    password: form.elements.password.value,
  }));
}

async function submitAuthForm(form, mode, action) {
  const errorHost = document.querySelector(`[data-auth-error="${mode}"]`);
  const submitButton = form.querySelector("button[type='submit']");
  if (errorHost) errorHost.textContent = "";
  submitButton.disabled = true;
  try {
    const user = await action();
    document.querySelector("#auth-screen")?.remove();
    startAuthenticatedApp(user);
  } catch (error) {
    if (errorHost) errorHost.textContent = error.message || String(error);
  } finally {
    submitButton.disabled = false;
  }
}

function startAuthenticatedApp(user = getCurrentUser()) {
  if (!user) {
    renderAuthScreen();
    return;
  }
  appShell.hidden = false;
  if (accountName) accountName.textContent = user.name;
  setupSidebar();
  applyLocalSettings();
  renderRoute();
  refreshIcons();
  refreshBackendStatus();
  if (!backendStatusTimer) {
    backendStatusTimer = setInterval(refreshBackendStatus, 45000);
  }
  if (!liveStatusTimer) {
    liveStatusTimer = setInterval(() => {
      if (currentRoute() === "live") {
        refreshLiveStatus();
      }
    }, 5000);
  }
}

function handleSignOut() {
  signOutLocal();
  operationsStream?.close();
  operationsStream = null;
  clearInterval(backendStatusTimer);
  clearInterval(liveStatusTimer);
  backendStatusTimer = null;
  liveStatusTimer = null;
  renderAuthScreen();
}

function renderRoute() {
  const routeKey = currentRoute();
  const route = routes[routeKey];
  if (routeKey !== "dashboard" && operationsStream) {
    operationsStream.close();
    operationsStream = null;
  }

  pageTitle.textContent = route.title;
  navLinks.forEach((link) => {
    const isCurrent = link.dataset.route === routeKey;
    if (isCurrent) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  });

  if (routeKey === "cameras") {
    renderCamerasPage();
    return;
  }

  if (routeKey === "dashboard") {
    renderDashboardPage();
    return;
  }

  if (routeKey === "wall") {
    renderVideoWallPage();
    return;
  }

  if (routeKey === "events") {
    renderEventsPage();
    return;
  }

  if (routeKey === "video-analysis") {
    renderVideoAnalysisPage();
    return;
  }

  if (routeKey === "productivity") {
    renderProductivityPage();
    return;
  }

  if (routeKey === "investigations") {
    renderInvestigationsPage();
    return;
  }

  if (routeKey === "zones") {
    renderZonesPage();
    return;
  }

  if (routeKey === "machines") {
    renderMachinesPage();
    return;
  }

  if (routeKey === "rules") {
    renderRulesPage();
    return;
  }

  if (routeKey === "settings") {
    renderSettingsPage();
    return;
  }

  if (routeKey === "evidence") {
    renderEvidencePage();
    return;
  }

  if (routeKey === "diagnostics") {
    renderDiagnosticsPage();
    return;
  }

  if (routeKey === "live") {
    renderLivePage();
    return;
  }

  appView.innerHTML = `
    <div class="foundation-panel">
      <section class="module-notice">
        <h2>${route.title}</h2>
        <p>${route.message}</p>
      </section>
      <section class="system-strip" aria-label="Estado da fundacao">
        <div class="system-cell">
          <span>Frontend</span>
          <strong>Shell carregado</strong>
        </div>
        <div class="system-cell">
          <span>Backend</span>
          <strong id="backend-summary">Aguardando health check</strong>
        </div>
        <div class="system-cell">
          <span>Banco</span>
          <strong>SQLite inicializado pelo backend</strong>
        </div>
      </section>
    </div>
  `;
}

async function renderDashboardPage() {
  appView.innerHTML = `
    <div class="ops-page">
      <section class="ops-toolbar">
        <button type="button" id="dashboard-refresh">Atualizar</button>
      </section>
      <section id="dashboard-kpis" class="settings-grid"></section>
      <section class="ops-grid">
        <div>
          <div class="section-heading"><h2>Eventos recentes</h2><span id="dashboard-event-count">0</span></div>
          <div id="dashboard-events" class="ops-list"></div>
        </div>
        <aside class="ops-detail">
          <h2>Leitura operacional</h2>
          <div id="dashboard-insights" class="object-list"></div>
        </aside>
      </section>
    </div>
  `;
  document.querySelector("#dashboard-refresh").addEventListener("click", loadDashboard);
  startOperationsStream();
  await loadDashboard();
}

function startOperationsStream() {
  if (operationsStream) return;
  try {
    operationsStream = new EventSource(operationsStreamUrl());
    operationsStream.addEventListener("summary", (event) => {
      if (currentRoute() !== "dashboard") return;
      if (event.data === operationsStreamLastSummary) return;
      operationsStreamLastSummary = event.data;
      const summary = JSON.parse(event.data);
      renderDashboardSummary(summary);
    });
    operationsStream.addEventListener("open", () => {});
    operationsStream.addEventListener("error", () => {
      operationsStream?.close();
      operationsStream = null;
    });
  } catch {
    operationsStream = null;
  }
}

async function loadDashboard() {
  try {
    const [summary, cameras, zones] = await Promise.all([
      getOperationsSummary(),
      listCameras(),
      listZones(),
    ]);
    const cameraById = Object.fromEntries(cameras.map((camera) => [camera.id, camera]));
    const zoneById = Object.fromEntries(zones.map((zone) => [zone.id, zone]));
    renderDashboardSummary(summary);
    document.querySelector("#dashboard-event-count").textContent = `${summary.recent_events.length} itens`;
    document.querySelector("#dashboard-events").innerHTML = summary.recent_events.length
      ? summary.recent_events.map((event) => eventRow(event, cameraById, zoneById)).join("")
      : emptyState("Sem eventos recentes", "Quando a visão detectar ocorrências, elas entram aqui.");
    document.querySelectorAll("[data-event-action]").forEach((button) => {
      button.addEventListener("click", handleEventAction);
    });
    renderDashboardInsights(summary);
  } catch (error) {
    document.querySelector("#dashboard-kpis").innerHTML = emptyState("Falha ao carregar painel", error.message);
  }
}

async function renderProductivityPage() {
  appView.innerHTML = `
    <div class="ops-page">
      <section class="ops-toolbar">
        <button type="button" id="productivity-refresh"><i data-lucide="refresh-cw" aria-hidden="true"></i><span>Atualizar</span></button>
      </section>
      <section id="productivity-kpis" class="settings-grid"></section>
      <section class="ops-grid">
        <div>
          <div class="section-heading"><h2>Sinais por câmera</h2><span id="productivity-signal-count">0 sinais</span></div>
          <div id="productivity-cameras" class="ops-list"></div>
        </div>
        <aside class="ops-detail">
          <h2>Máquinas detectadas</h2>
          <div id="productivity-machines" class="object-list"></div>
          <h2>Limitações</h2>
          <div id="productivity-limitations" class="object-list"></div>
        </aside>
      </section>
    </div>
  `;
  document.querySelector("#productivity-refresh").addEventListener("click", loadProductivity);
  refreshIcons();
  await loadProductivity();
}

async function renderVideoAnalysisPage() {
  clearInterval(videoAnalysisTimer);
  appView.innerHTML = `
    <div class="ops-page">
      <section class="ops-grid">
        <div>
          <div class="section-heading">
            <h2>CAMPEX Intelligence por MP4</h2>
            <span>Vídeo como fonte real</span>
          </div>
          <form id="video-analysis-form" class="stack-form">
            <label>Arquivo MP4
              <input name="video" type="file" accept="video/mp4,.mp4" required />
            </label>
            <button type="submit" class="primary-action"><i data-lucide="upload" aria-hidden="true"></i><span>Enviar e analisar</span></button>
          </form>
          <div id="video-analysis-player" class="ops-detail"></div>
        </div>
        <aside class="ops-detail">
          <h2>Status</h2>
          <div id="video-analysis-status" class="object-list"></div>
          <h2>CAMPEX Intelligence</h2>
          <div id="video-analysis-insight" class="object-list"></div>
        </aside>
      </section>
      <section class="ops-grid">
        <div>
          <div class="section-heading"><h2>Eventos</h2><span id="video-event-count">0</span></div>
          <div id="video-analysis-events" class="ops-list"></div>
        </div>
        <aside class="ops-detail">
          <h2>Métricas</h2>
          <div id="video-analysis-metrics" class="settings-grid"></div>
          <h2>Tracks</h2>
          <div id="video-analysis-tracks" class="object-list"></div>
        </aside>
      </section>
    </div>
  `;
  document.querySelector("#video-analysis-form").addEventListener("submit", handleVideoAnalysisSubmit);
  refreshIcons();
  await loadLatestVideoAnalysis();
}

async function handleVideoAnalysisSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const file = form.elements.video.files[0];
  if (!file) return;
  if (file.size > VERCEL_SAFE_VIDEO_UPLOAD_BYTES) {
    const limitMb = Math.floor(VERCEL_SAFE_VIDEO_UPLOAD_BYTES / (1024 * 1024));
    notify(
      "MP4 muito grande",
      `Na Vercel, envie um MP4 com até ${limitMb} MB. O arquivo selecionado tem ${formatBytes(file.size)}.`,
      "error",
      10000,
    );
    document.querySelector("#video-analysis-status").innerHTML = emptyState(
      "Arquivo acima do limite",
      `Reduza o MP4 para até ${limitMb} MB ou use uma infraestrutura de worker/storage para vídeos maiores.`,
    );
    return;
  }
  const button = form.querySelector("button[type='submit']");
  button.disabled = true;
  try {
    const analysis = await analyzeVideo(file);
    notify("Análise criada", "O vídeo entrou na fila de processamento.", "success");
    renderVideoAnalysis(analysis);
    pollVideoAnalysis(analysis.analysis_id);
  } catch (error) {
    notifyError("Falha no upload", error);
  } finally {
    button.disabled = false;
  }
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 MB";
  const mb = bytes / (1024 * 1024);
  return `${mb.toFixed(mb >= 10 ? 0 : 1)} MB`;
}

async function loadLatestVideoAnalysis() {
  try {
    const analyses = await listVideoAnalyses();
    if (!analyses.length) {
      document.querySelector("#video-analysis-status").innerHTML = emptyState("Nenhuma análise", "Envie um MP4 para iniciar.");
      return;
    }
    renderVideoAnalysis(analyses[0]);
    if (!["COMPLETED", "FAILED"].includes(analyses[0].status)) {
      pollVideoAnalysis(analyses[0].analysis_id);
    }
  } catch (error) {
    document.querySelector("#video-analysis-status").innerHTML = emptyState("Falha ao carregar análises", error.message);
  }
}

function pollVideoAnalysis(analysisId) {
  clearInterval(videoAnalysisTimer);
  videoAnalysisTimer = setInterval(async () => {
    if (currentRoute() !== "video-analysis") {
      clearInterval(videoAnalysisTimer);
      return;
    }
    try {
      const analysis = await getVideoAnalysisStatus(analysisId);
      renderVideoAnalysis(analysis);
      if (["COMPLETED", "FAILED"].includes(analysis.status)) {
        clearInterval(videoAnalysisTimer);
      }
    } catch (error) {
      clearInterval(videoAnalysisTimer);
      notifyError("Falha ao acompanhar análise", error);
    }
  }, 1800);
}

function renderVideoAnalysis(analysis) {
  const source = analysis.source || {};
  const metrics = analysis.metrics || {};
  const summary = metrics.summary || {};
  document.querySelector("#video-analysis-status").innerHTML = [
    objectLine(analysis.status, `${analysis.progress || 0}%`, analysis.error || analysis.original_filename),
    objectLine("Fonte", source.duration_seconds ? `${source.duration_seconds.toFixed(1)}s · ${source.width}x${source.height}` : "Aguardando leitura", source.fps ? `${source.fps.toFixed(2)} FPS` : ""),
  ].join("");
  const runtime = analysis.runtime || {};
  const ai = analysis.ai || {};
  document.querySelector("#video-analysis-player").innerHTML = analysis.analysis_id
    ? `
      <video controls playsinline src="${analysis.debug_video ? debugVideoUrl(analysis.analysis_id) : uploadedVideoUrl(analysis.analysis_id)}"></video>
      ${analysis.debug_video ? objectLine("Vídeo debug", "Overlays CAMPEX ativos", "Arquivo original preservado") : objectLine("Vídeo original", "Ative VIDEO_DEBUG_OVERLAY=true para gerar overlays", "")}
    `
    : emptyState("Sem vídeo", "O player aparece após o upload.");
  document.querySelector("#video-analysis-metrics").innerHTML = [
    metricItem("Pessoas únicas", summary.unique_people ?? metrics.people?.detected ?? "-", "IDs temporários de tracking"),
    metricItem("Visíveis", metrics.people?.people_visible ?? "-", `Pico ${metrics.people?.max_simultaneous ?? "-"}`),
    metricItem("Detecções", summary.total_detections ?? "-", "Amostragem de frames"),
    metricItem("Eventos", summary.total_events ?? "-", "Derivados do histórico"),
    metricItem("Moving", `${metrics.activity?.moving_seconds ?? 0}s`, "Tempo acumulado"),
    metricItem("Stationary", `${metrics.activity?.stationary_seconds ?? 0}s`, `${metrics.activity?.stationary_events ?? 0} eventos`),
  ].join("");
  document.querySelector("#video-analysis-status").innerHTML += [
    objectLine("Detector", runtime.detector || "-", `${runtime.model || "-"} · ${runtime.device || "cpu"}${runtime.fallback ? " · fallback" : ""}`),
    objectLine("Tracker", runtime.tracker || "-", `FPS análise ${runtime.analysis_fps ?? "-"}`),
    objectLine("Entrada", runtime.input_resolution ? `${runtime.input_resolution}px` : "-", "Resolução de inferência"),
    objectLine("IA", ai.status || "pendente", ai.fallback_used ? `Fallback: ${ai.reason || ""}` : (ai.model || "")),
  ].join("");
  const events = analysis.events || [];
  document.querySelector("#video-event-count").textContent = `${events.length} eventos`;
  document.querySelector("#video-analysis-events").innerHTML = events.length
    ? events.slice(0, 80).map((event) => `
      <article class="ops-row">
        <div><strong>${event.event_type}</strong><span>${formatEventTime(event)} · track ${event.track_id ?? "-"}</span></div>
      </article>
    `).join("")
    : emptyState("Sem eventos", "Os eventos aparecem durante ou após o processamento.");
  const tracks = analysis.tracks || [];
  document.querySelector("#video-analysis-tracks").innerHTML = tracks.length
    ? tracks.slice(0, 20).map((track) => objectLine(
        `${track.class} #${track.track_id}`,
        `${track.total_visible_seconds ?? track.duration_seconds ?? 0}s observados · ${(track.confidence * 100).toFixed(0)}%`,
        `${track.movement_state || track.state || "UNKNOWN"} · moving ${track.moving_seconds ?? 0}s · stationary ${track.stationary_seconds ?? 0}s`
      )).join("")
    : emptyState("Sem tracks", "O tracker ainda não confirmou objetos.");
  const insight = analysis.insight || {};
  const insightSections = Object.entries(insight.sections || {})
    .map(([label, text]) => objectLine(label, text, ""))
    .join("");
  const aiLabel = ai.fallback_used ? "IA: Fallback local" : (ai.model ? "IA: Nemotron" : "IA: pendente");
  document.querySelector("#video-analysis-insight").innerHTML = insight.summary
    ? objectLine("Análise CAMPEX", insight.summary, aiLabel) + insightSections
    : emptyState("Aguardando inteligência", "O Nemotron é chamado ao final com métricas agregadas.");
  refreshIcons();
}

function formatEventTime(event) {
  if (event.timestamp != null) return formatSeconds(event.timestamp);
  const raw = event.started_at || "";
  if (!raw) return "-";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatSeconds(value) {
  const seconds = Number(value || 0);
  const minutes = Math.floor(seconds / 60);
  const rest = Math.floor(seconds % 60);
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

async function loadProductivity() {
  try {
    const payload = await getProductivitySummary();
    document.querySelector("#productivity-kpis").innerHTML = [
      metricItem("Atividade observada", payload.score == null ? "sem score" : `${payload.score}%`, "Sem calculo de produtividade real"),
      metricItem("Pessoas", payload.counts.people, `${payload.counts.signals} sinais ativos`),
      metricItem("Máquinas", payload.counts.machines, `${payload.mapped_machines_total || 0} mapeadas · ${payload.counts.cameras} câmeras`),
    ].join("");
    document.querySelector("#productivity-signal-count").textContent = `${payload.counts.signals} sinais`;
    document.querySelector("#productivity-cameras").innerHTML = payload.cameras.length
      ? payload.cameras.map(productivityCameraRow).join("")
      : emptyState("Sem câmeras", "Cadastre e ligue Vision nas câmeras para observar atividade.");
    const machines = payload.cameras.flatMap((camera) =>
      (camera.machines || []).map((machine) => ({ ...machine, camera_name: camera.camera_name }))
    );
    const mappedMachines = payload.cameras.flatMap((camera) =>
      (camera.mapped_machines || []).map((machine) => ({ ...machine, camera_name: camera.camera_name }))
    );
    document.querySelector("#productivity-machines").innerHTML = machines.length
      ? machines.map((machine) => objectLine(
          `${machine.class_name} #${machine.track_id}`,
          `${machine.camera_name} · confiança ${(machine.confidence * 100).toFixed(0)}%`,
          "Máquina/veículo reconhecido pelo detector"
        )).join("")
      : mappedMachines.length
        ? mappedMachines.map((machine) => objectLine(
            machine.name,
            `${machine.camera_name} · ${machine.type}`,
            `${machine.state || "UNKNOWN"} · ${machine.requires_operator ? "requer operador" : "operador opcional"} · ativo mapeado`
          )).join("")
        : emptyState("Nenhuma máquina mapeada", "Mapeie máquinas para contextualizar atividade e segurança.");
    document.querySelector("#productivity-limitations").innerHTML = [
      objectLine("Leitura assistiva", "O sistema aponta sinais; a conclusão final deve ser revisada por operador.", "Atividade observada não é produtividade real."),
      objectLine("Máquinas específicas", payload.machine_classes.join(", "), "Classes dependem do detector/fine-tuning."),
      objectLine("Comportamentos", payload.behavior_signals.join(", "), "Sinais disponíveis nesta versão."),
    ].join("");
  } catch (error) {
    document.querySelector("#productivity-kpis").innerHTML = emptyState("Falha ao carregar produtividade", error.message);
  }
}

function productivityCameraRow(camera) {
  const counts = camera.counts || {};
  const signals = camera.signals || [];
  return `
    <article class="ops-row">
      <div>
        <strong>${camera.camera_name}</strong>
        <span>${camera.score == null ? "Sem score" : `Score ${camera.score}%`} · ${counts.people || 0} pessoas · ${counts.machines || 0} máquinas/ativos · ${(camera.mapped_machines || []).length} mapeadas · ${counts.phones || 0} celulares</span>
        <small>${signals.length ? signals.map((signal) => signal.label).join(" · ") : "Sem sinais de atividade no momento"}</small>
      </div>
      <span class="health-badge" data-status="${signals.length ? "DEGRADED" : "ONLINE"}">${signals.length ? "ATENÇÃO" : "OK"}</span>
    </article>
  `;
}

function renderDashboardSummary(summary) {
  const kpis = summary.kpis;
  const intelligence = summary.intelligence || {};
  const triage = intelligence.triage || [];
  const attention = triage.filter((item) => item.decision === "INVESTIGATE");
  const kpiHost = document.querySelector("#dashboard-kpis");
  if (!kpiHost) return;
  kpiHost.innerHTML = [
    metricItem("Câmeras online", `${kpis.cameras_online}/${kpis.cameras_total}`, `${kpis.cameras_active} ativas`),
    metricItem("Eventos abertos", kpis.events_open, `${kpis.events_critical} críticos`),
    metricItem("Recorrências", triage.length, `${attention.length} para investigar`),
    metricItem("Investigações", kpis.investigations_open, "Casos em andamento"),
  ].join("");
  renderDashboardInsights(summary);
}

function renderDashboardInsights(summary) {
  const intelligence = summary.intelligence || {};
  const topGroup = (intelligence.event_groups || [])[0];
  const topTriage = (intelligence.triage || []).find((item) => item.decision === "INVESTIGATE");
  const topFinding = (intelligence.findings || [])[0];
  const topImpact = (intelligence.impacts || [])[0];
  const comparison = intelligence.comparisons?.today_vs_yesterday;
  const normal = intelligence.comparisons?.normal_vs_current;
  const areas = summary.areas.map((area) => `${area.area_id}: ${area.camera_count}`).join(" · ") || "sem áreas";
  const recurrence = topGroup
    ? `${topGroup.location_label}: ${topGroup.frequency}x, ${topGroup.total_duration_label}`
    : "sem recorrência confiável hoje";
  const triage = topTriage
    ? `${topTriage.group.location_label}: ${topTriage.reasons.join(" · ")}`
    : "ruído baixo no momento";
  const finding = topFinding?.statement || "sem finding executivo hoje";
  const comparisonLabel = comparison
    ? `${comparison.direction === "up" ? "+" : comparison.direction === "down" ? "-" : ""}${comparison.duration_delta_label}${comparison.duration_delta_percent !== null ? ` (${comparison.duration_delta_percent}%)` : ""}`
    : "sem base comparativa";
  const normalLabel = normal?.state !== "NO_BASELINE"
    ? `${normal.comparison.direction === "up" ? "+" : normal.comparison.direction === "down" ? "-" : ""}${normal.comparison.duration_delta_label}${normal.comparison.duration_delta_percent !== null ? ` (${normal.comparison.duration_delta_percent}%)` : ""}`
    : "sem baseline recente";
  const impact = topImpact
    ? `${topImpact.lost_time_label} perdidos${topImpact.estimated_cost !== null ? ` · ${formatMoney(topImpact.estimated_cost, topImpact.currency)}` : ""}`
    : "sem impacto acumulado";
  document.querySelector("#dashboard-insights").innerHTML = [
    objectLine("Áreas", areas, "Distribuição das câmeras cadastradas"),
    objectLine("Recorrência", recurrence, "Agrupa eventos confiáveis do dia"),
    objectLine("Triagem", triage, "Sobe frequência, duração ou severidade"),
    objectLine("Finding", finding, "Frase pronta para gerente"),
    objectLine("Hoje vs ontem", comparisonLabel, "Variação de tempo acumulado"),
    objectLine("Atual vs normal", normalLabel, "Média dos dias recentes com amostra"),
    objectLine("Impacto", impact, "Tempo perdido e R$ quando houver taxa configurada"),
  ].join("");
}

async function renderVideoWallPage() {
  appView.innerHTML = `
    <div class="ops-page">
      <section class="ops-toolbar">
        <label>Layout
          <select id="wall-layout">
            <option value="1">1 câmera</option>
            <option value="4" selected>4 câmeras</option>
            <option value="9">9 câmeras</option>
          </select>
        </label>
        <button type="button" id="wall-refresh">Atualizar</button>
      </section>
      <section id="video-wall-grid" class="video-wall-grid" data-layout="4"></section>
    </div>
  `;
  document.querySelector("#wall-layout").addEventListener("change", loadVideoWall);
  document.querySelector("#wall-refresh").addEventListener("click", loadVideoWall);
  await loadVideoWall();
}

async function loadVideoWall() {
  const grid = document.querySelector("#video-wall-grid");
  const layout = Number(document.querySelector("#wall-layout")?.value || 4);
  grid.dataset.layout = String(layout);
  try {
    const cameras = await listCameras();
    const visible = cameras.slice(0, layout);
    grid.innerHTML = visible.length
      ? visible.map(videoWallTile).join("")
      : emptyState("Nenhuma câmera cadastrada", "Cadastre uma câmera para montar o mural operacional.");
  } catch (error) {
    grid.innerHTML = emptyState("Falha ao carregar mural", error.message);
  }
}

function videoWallTile(camera) {
  const media = camera.source_type === "video_file"
    ? `<video class="wall-media" src="${cameraVideoUrl(camera.id)}" autoplay muted playsinline loop></video>`
    : `<img class="wall-media" src="${cameraStreamUrl(camera.id)}" alt="${camera.name}" />`;
  return `
    <article class="wall-tile">
      <header>
        <strong>${camera.name}</strong>
        <span class="health-badge" data-status="${camera.status}">${camera.status}</span>
      </header>
      <div class="wall-media-host">${media}</div>
    </article>
  `;
}

async function renderLivePage() {
  appView.innerHTML = `
    <div class="live-console">
      <section class="live-toolbar" aria-label="Controles ao vivo">
        <label>
          Camera
          <select id="live-camera-select"></select>
        </label>
        <div class="row-actions">
          <button type="button" id="live-toggle-vision" class="mode-toggle" data-active="false">Vision: OFF</button>
          <button type="button" id="live-toggle-detections" class="mode-toggle" data-active="false">Detecção: OFF</button>
          <button type="button" id="live-toggle-mapping" class="mode-toggle" data-active="false">Mapeamento: OFF</button>
          <button type="button" id="live-restart-vision">Reiniciar Vision</button>
          <button type="button" id="live-refresh">Atualizar</button>
        </div>
      </section>
      <section class="live-stage">
        <div class="live-feed">
          <div class="live-feed-header">
            <div>
              <strong id="live-camera-name">Nenhuma camera selecionada</strong>
              <span id="live-detection-summary">Vision parada · 0 objetos</span>
            </div>
            <div class="live-status-badges">
              <span class="health-badge" id="live-camera-status" data-status="OFFLINE">OFFLINE</span>
              <span class="health-badge" id="live-vision-status" data-status="STOPPED">VISION OFF</span>
            </div>
          </div>
          <div id="live-media-host" class="live-media-host" aria-live="polite">
            <div class="media-placeholder">Selecione uma camera</div>
          </div>
          <div class="live-telemetry" id="live-telemetry">
            <span>Camera FPS <strong id="live-camera-fps">0</strong></span>
            <span>Vision FPS <strong id="live-vision-fps">0</strong></span>
            <span>Frames <strong id="live-frame-count">0/0</strong></span>
            <span>Mapeamento <strong id="live-mapping-state">OFF</strong></span>
            <span id="live-error-line">Sem erros</span>
          </div>
        </div>
      </section>
    </div>
  `;

  document.querySelector("#live-camera-select").addEventListener("change", selectLiveCamera);
  document.querySelector("#live-toggle-vision").addEventListener("click", handleToggleVision);
  document.querySelector("#live-toggle-detections").addEventListener("click", handleToggleDetections);
  document.querySelector("#live-toggle-mapping").addEventListener("click", handleToggleMapping);
  document.querySelector("#live-restart-vision").addEventListener("click", handleRestartVision);
  document.querySelector("#live-refresh").addEventListener("click", refreshLiveStatus);
  await loadLiveCameras();
}

async function loadLiveCameras() {
  const select = document.querySelector("#live-camera-select");
  try {
    const cameras = await listCameras();
    select.innerHTML = cameras
      .map((camera) => `<option value="${camera.id}">${camera.name}</option>`)
      .join("");

    if (!cameras.length) {
      select.innerHTML = `<option value="">Cadastre uma camera</option>`;
      renderMediaPlaceholder("Nenhuma câmera cadastrada");
      renderVisionStatus(null, [], null);
      return;
    }

    const savedCameraId = localStorage.getItem("campex.live_camera_id");
    if (savedCameraId && cameras.some((camera) => camera.id === savedCameraId)) {
      select.value = savedCameraId;
    }
    await selectLiveCamera();
  } catch (error) {
    select.innerHTML = `<option value="">Backend indisponivel</option>`;
    renderVisionStatus(null, [], null);
    console.error(error);
  }
}

async function selectLiveCamera() {
  const cameraId = document.querySelector("#live-camera-select").value;
  if (cameraId) {
    localStorage.setItem("campex.live_camera_id", cameraId);
  }
  try {
    const camera = await selectedCamera();
    const nameElement = document.querySelector("#live-camera-name");
    if (nameElement && nameElement.textContent !== (camera?.name || "Camera")) {
      nameElement.textContent = camera?.name || "Camera";
    }
    const status = statusLabel(camera?.status);
    const statusElement = document.querySelector("#live-camera-status");
    if (statusElement) {
      if (statusElement.textContent !== status) statusElement.textContent = status;
      if (statusElement.dataset.status !== status) statusElement.dataset.status = status;
    }
    const visionStatus = await refreshLiveStatus();
    await renderLiveMedia(camera);
  } catch (error) {
    const nameElement = document.querySelector("#live-camera-name");
    if (nameElement && nameElement.textContent !== "Backend indisponivel") {
      nameElement.textContent = "Backend indisponivel";
    }
    const statusElement = document.querySelector("#live-camera-status");
    if (statusElement) {
      if (statusElement.textContent !== "OFFLINE") statusElement.textContent = "OFFLINE";
      if (statusElement.dataset.status !== "OFFLINE") statusElement.dataset.status = "OFFLINE";
    }
    renderMediaPlaceholder("Stream indisponivel");
    renderVisionStatus(null, [], null);
    console.error(error);
  }
}

async function selectedCamera() {
  const cameraId = document.querySelector("#live-camera-select").value;
  if (!cameraId) {
    return null;
  }
  const cameras = await listCameras();
  return cameras.find((item) => item.id === cameraId) || null;
}

async function renderLiveMedia(camera) {
  activeMediaCameraId = camera?.id || null;
  if (!camera?.id) {
    renderMediaPlaceholder("Selecione uma camera");
    return;
  }

  renderMediaPlaceholder("Abrindo stream...");

  if (camera.source_type === "video_file") {
    renderVideoElement(camera);
    return;
  }

  try {
    const info = await getStreamInfo(camera.id);
    if (activeMediaCameraId !== camera.id) {
      return;
    }

    if (info.mode === "file_video") {
      renderVideoElement(camera);
      return;
    }

    renderMjpegElement(camera);
  } catch (error) {
    renderMjpegElement(camera);
  }
}

function renderVideoElement(camera) {
  const mediaHost = document.querySelector("#live-media-host");
  const sources = videoSourcesFor(camera);
  mediaHost.innerHTML = `
    <video
      id="live-video"
      class="live-media"
      src="${sources[0]}"
      controls
      autoplay
      muted
      playsinline
      loop
    ></video>
  `;

  const video = mediaHost.querySelector("video");
  let sourceIndex = 0;
  video.addEventListener("error", () => {
    sourceIndex += 1;
    if (sources[sourceIndex]) {
      video.src = sources[sourceIndex];
      video.load();
      video.play().catch(() => {
        video.controls = true;
      });
      return;
    }
    renderMediaPlaceholder("Nao foi possivel reproduzir o arquivo de video.");
  });
  video.play().catch(() => {
    video.controls = true;
  });
}

function renderMjpegElement(camera) {
  const mediaHost = document.querySelector("#live-media-host");
  const streamUrl = `${cameraStreamUrl(camera.id, { overlay: liveDetectionsVisible() })}${liveDetectionsVisible() ? "&" : "?"}t=${Date.now()}`;
  mediaHost.innerHTML = `
    <img
      id="live-stream"
      class="live-media"
      alt="Video ao vivo da camera selecionada"
      src="${streamUrl}"
    />
  `;

  mediaHost.querySelector("img").addEventListener("error", () => {
    renderMediaPlaceholder("Aguardando frames da camera ao vivo.");
  });
}

function videoSourcesFor(camera) {
  return [cameraVideoUrl(camera.id)];
}

function renderMediaPlaceholder(message) {
  const mediaHost = document.querySelector("#live-media-host");
  if (!mediaHost) {
    return;
  }
  mediaHost.innerHTML = `<div class="media-placeholder">${message}</div>`;
}

async function handleToggleVision() {
  const button = document.querySelector("#live-toggle-vision");
  if (button?.dataset.active === "true") {
    await handleStopVision();
    return;
  }
  await handleStartVision();
}

async function handleStartVision() {
  const cameraId = document.querySelector("#live-camera-select").value;
  if (!cameraId) {
    return;
  }
  // Switch to MJPEG immediately so the user sees the live stream while
  // the backend starts the vision session (model loading can take
  // several seconds and the API call may time out on the client side).
  const camera = await selectedCamera();
  if (camera) {
    renderMjpegElement(camera);
  }
  try {
    await startVision(cameraId);
    notify("Vision ligada", "A análise da câmera foi iniciada.", "success");
  } catch (error) {
    console.error("Start vision failed:", error);
    notifyError("Falha ao ligar Vision", error);
  }
  await refreshLiveStatus();
}

async function handleToggleDetections() {
  const nextVisible = !liveDetectionsVisible();
  localStorage.setItem("campex.live_detections_visible", String(nextVisible));
  updateDetectionToggle();
  const camera = await selectedCamera();
  if (camera) {
    await renderLiveMedia(camera);
  }
  await refreshLiveStatus();
}

async function handleStopVision() {
  const cameraId = document.querySelector("#live-camera-select").value;
  if (!cameraId) {
    return;
  }
  try {
    await stopVision(cameraId);
    notify("Vision desligada", "A análise da câmera foi pausada.", "warning");
    const camera = await selectedCamera();
    await renderLiveMedia(camera);
    await refreshLiveStatus();
  } catch (error) {
    notifyError("Falha ao desligar Vision", error);
  }
}

async function handleToggleMapping() {
  const button = document.querySelector("#live-toggle-mapping");
  if (button?.dataset.active === "true") {
    await handleStopMapping();
    return;
  }
  await handleStartMapping();
}

async function handleStartMapping() {
  const cameraId = document.querySelector("#live-camera-select").value;
  if (!cameraId) {
    return;
  }
  const button = document.querySelector("#live-toggle-mapping");
  if (button) {
    button.textContent = "Mapeamento: carregando";
    button.dataset.loading = "true";
  }
  const camera = await selectedCamera();
  if (camera) {
    renderMjpegElement(camera);
  }
  try {
    await startMapping(cameraId);
    notify("Mapeamento ligado", "O mapeamento corporal foi solicitado.", "success");
  } catch (error) {
    console.error("Start mapping failed:", error);
    notifyError("Falha ao ligar mapeamento", error);
  } finally {
    if (button) {
      delete button.dataset.loading;
    }
  }
  await refreshLiveStatus();
}

async function handleStopMapping() {
  const cameraId = document.querySelector("#live-camera-select").value;
  if (!cameraId) {
    return;
  }
  try {
    await stopMapping(cameraId);
    notify("Mapeamento desligado", "O mapeamento corporal foi pausado.", "warning");
    await refreshLiveStatus();
  } catch (error) {
    notifyError("Falha ao desligar mapeamento", error);
  }
}

async function handleRestartVision() {
  const cameraId = document.querySelector("#live-camera-select").value;
  if (!cameraId) {
    return;
  }
  // Switch to MJPEG immediately so the live stream is visible while the
  // backend restarts the vision session (model loading may take several
  // seconds and the API call can time out on the client side).
  const camera = await selectedCamera();
  if (camera) {
    renderMjpegElement(camera);
  }
  try {
    await restartVision(cameraId);
    notify("Vision reiniciada", "A sessão de análise foi reiniciada.", "success");
  } catch (error) {
    console.error("Restart vision failed:", error);
    notifyError("Falha ao reiniciar Vision", error);
  }
  await refreshLiveStatus();
}

async function refreshLiveStatus() {
  const cameraId = document.querySelector("#live-camera-select")?.value;
  if (!cameraId) {
    return;
  }
  if (liveStatusRefreshInFlight) {
    return;
  }
  liveStatusRefreshInFlight = true;

  try {
    const [health, visionStatus] = await Promise.all([
      getCameraHealth(cameraId),
      getVisionStatus(cameraId),
    ]);
    const showDetections = liveDetectionsVisible();
    const [objects, poses] = showDetections
      ? await Promise.all([getVisionObjects(cameraId), getMappingPoses(cameraId)])
      : [[], []];
    // Apenas atualiza os elementos de status, nunca re-renderiza a mídia
    updateVisionStatusOnly(visionStatus, objects, health, poses);
    return visionStatus;
  } catch (error) {
    updateVisionStatusOnly({ status: "ERROR", error: error.message, metrics: null }, [], null, []);
    return null;
  } finally {
    liveStatusRefreshInFlight = false;
  }
}

function renderVisionStatus(visionStatus, objects, health, poses = []) {
  updateDetectionToggle();
  const metrics = visionStatus?.metrics || {};
  const status = visionStatus?.status || "STOPPED";
  const mapping = visionStatus?.components?.mapping || {};
  updateModeButtons(status, mapping);
  const visionBadge = document.querySelector("#live-vision-status");
  const detectionSummary = document.querySelector("#live-detection-summary");
  if (visionBadge) {
    const newStatus = status === "RUNNING" ? "VISION ON" : status;
    const newDataStatus = status === "RUNNING" ? "ONLINE" : status === "ERROR" ? "OFFLINE" : "DEGRADED";
    if (visionBadge.textContent !== newStatus) {
      visionBadge.textContent = newStatus;
    }
    if (visionBadge.dataset.status !== newDataStatus) {
      visionBadge.dataset.status = newDataStatus;
    }
  }
  if (detectionSummary) {
    const newSummary = liveDetectionsVisible()
      ? `${status} · ${objects.length} objeto(s) · ${poses.length} pose(s)`
      : `${status} · visualização de detecção OFF`;
    if (detectionSummary.textContent !== newSummary) {
      detectionSummary.textContent = newSummary;
    }
  }
  setText("#live-camera-fps", health?.approximate_fps ?? metrics.camera_fps ?? "0");
  setText("#live-vision-fps", metrics.vision_fps ?? "0");
  setText("#live-frame-count", `${metrics.frames_processed ?? 0}/${metrics.frames_received ?? 0}`);
  setText("#live-mapping-state", mappingLabel(mapping));
  const error = visionStatus?.error || mapping.error;
  const errorLine = document.querySelector("#live-error-line");
  if (errorLine) {
    const newError = error || "Sem erros";
    if (errorLine.textContent !== newError) {
      errorLine.textContent = newError;
    }
    const newState = error ? "error" : "ok";
    if (errorLine.dataset.state !== newState) {
      errorLine.dataset.state = newState;
    }
  }
}

function updateVisionStatusOnly(visionStatus, objects, health, poses = []) {
  updateDetectionToggle();
  // Apenas atualiza o status, nunca toca na mídia ou re-renderiza
  const metrics = visionStatus?.metrics || {};
  const status = visionStatus?.status || "STOPPED";
  const mapping = visionStatus?.components?.mapping || {};
  updateModeButtons(status, mapping);
  const visionBadge = document.querySelector("#live-vision-status");
  const detectionSummary = document.querySelector("#live-detection-summary");
  if (visionBadge) {
    const newStatus = status === "RUNNING" ? "VISION ON" : status;
    const newDataStatus = status === "RUNNING" ? "ONLINE" : status === "ERROR" ? "OFFLINE" : "DEGRADED";
    if (visionBadge.textContent !== newStatus) {
      visionBadge.textContent = newStatus;
    }
    if (visionBadge.dataset.status !== newDataStatus) {
      visionBadge.dataset.status = newDataStatus;
    }
  }
  if (detectionSummary) {
    const newSummary = liveDetectionsVisible()
      ? `${status} · ${objects.length} objeto(s) · ${poses.length} pose(s)`
      : `${status} · visualização de detecção OFF`;
    if (detectionSummary.textContent !== newSummary) {
      detectionSummary.textContent = newSummary;
    }
  }
  setText("#live-camera-fps", health?.approximate_fps ?? metrics.camera_fps ?? "0");
  setText("#live-vision-fps", metrics.vision_fps ?? "0");
  setText("#live-frame-count", `${metrics.frames_processed ?? 0}/${metrics.frames_received ?? 0}`);
  setText("#live-mapping-state", mappingLabel(mapping));
  const error = visionStatus?.error || mapping.error;
  const errorLine = document.querySelector("#live-error-line");
  if (errorLine) {
    const newError = error || "Sem erros";
    if (errorLine.textContent !== newError) {
      errorLine.textContent = newError;
    }
    const newState = error ? "error" : "ok";
    if (errorLine.dataset.state !== newState) {
      errorLine.dataset.state = newState;
    }
  }
}

function setText(selector, value) {
  const element = document.querySelector(selector);
  if (element && element.textContent !== String(value)) {
    element.textContent = value;
  }
}

function updateModeButtons(status, mapping) {
  const visionButton = document.querySelector("#live-toggle-vision");
  if (visionButton) {
    const visionOn = ["STARTING", "RUNNING", "RECOVER"].includes(status);
    visionButton.dataset.active = String(visionOn);
    visionButton.textContent = `Vision: ${visionOn ? "ON" : "OFF"}`;
  }

  const mappingButton = document.querySelector("#live-toggle-mapping");
  if (mappingButton && mappingButton.dataset.loading !== "true") {
    const mappingOn = mapping.state === "ACTIVE";
    mappingButton.dataset.active = String(mappingOn);
    mappingButton.textContent = `Mapeamento: ${mappingOn ? "ON" : "OFF"}`;
    mappingButton.dataset.status = mapping.state || "STOPPED";
  }
}

function mappingLabel(mapping) {
  if (mapping.error) {
    return `ERRO: ${mapping.error}`;
  }
  if (mapping.state === "ACTIVE" && !mapping.poses) {
    return "ACTIVE, aguardando corpo";
  }
  return mapping.state || "STOPPED";
}

function mappingEmptyMessage(mapping) {
  if (mapping.error) {
    return mapping.error;
  }
  if (mapping.state === "ACTIVE") {
    return "Modelo ativo. Aguarde uma pessoa visivel no stream.";
  }
  return "Ligue Mapeamento para desenhar os pontos do corpo.";
}

function statusLabel(status) {
  return status || "OFFLINE";
}

function sourceTypeLabel(type) {
  return {
    webcam: "Webcam",
    video_file: "Arquivo",
    rtsp: "RTSP",
    ip_camera: "IP/HTTP",
  }[type] || type;
}

function cameraMeta(camera) {
  const health = camera.health || {};
  const resolution = health.resolution ? `${health.resolution.width}x${health.resolution.height}` : "sem resolução";
  const fps = health.approximate_fps ? `${Number(health.approximate_fps).toFixed(1)} FPS` : "FPS indisponível";
  const lastFrame = health.last_successful_frame ? formatDate(health.last_successful_frame) : "sem frame";
  return `${sourceTypeLabel(camera.source_type)} · ${fps} · ${resolution} · último frame ${lastFrame}`;
}

function cameraRows(cameras) {
  if (!cameras.length) {
    return `
      <div class="empty-state">
        <strong>Nenhuma câmera cadastrada</strong>
        <span>Cadastre webcam, MP4, RTSP ou câmera IP para iniciar a operação.</span>
        <button type="button" data-empty-action="add-camera">Adicionar câmera</button>
      </div>
    `;
  }

  return cameras
    .map(
      (camera) => `
        <article class="camera-row" data-camera-id="${camera.id}">
          <div>
            <strong>${camera.name}</strong>
            <span>${cameraMeta(camera)}</span>
            <small>${camera.source_uri}${camera.health?.last_error ? ` · ${camera.health.last_error}` : ""}</small>
          </div>
          <span class="health-badge" data-status="${statusLabel(camera.health?.status || camera.status)}">${statusLabel(camera.health?.status || camera.status)}</span>
          <label class="inline-toggle">
            <input type="checkbox" data-action="toggle-enabled" ${camera.enabled ? "checked" : ""} />
            Ativa
          </label>
          <div class="row-actions">
            <button type="button" data-action="edit">Editar</button>
            <button type="button" data-action="test">Testar</button>
            <button type="button" data-action="diagnostics">Diagnóstico</button>
            <button type="button" data-action="delete">Excluir</button>
          </div>
          <pre class="camera-result" hidden></pre>
        </article>
      `
    )
    .join("");
}

async function renderCamerasPage() {
  appView.innerHTML = `
    <div class="camera-admin">
      <section class="camera-list-panel">
        <div class="section-heading">
          <h2>Câmeras cadastradas</h2>
          <div class="section-actions">
            <button type="button" id="add-camera"><i data-lucide="plus" aria-hidden="true"></i><span>Adicionar câmera</span></button>
            <button type="button" id="refresh-cameras"><i data-lucide="refresh-cw" aria-hidden="true"></i><span>Atualizar</span></button>
          </div>
        </div>
        <div id="camera-list" class="camera-list">Carregando câmeras...</div>
      </section>
      <section class="modal-layer" id="camera-modal" aria-labelledby="camera-modal-title" aria-modal="true" role="dialog" hidden>
        <div class="modal-backdrop" data-close-modal></div>
        <div class="modal-window camera-wizard-window">
          <header class="modal-header">
            <div>
              <p class="eyebrow" id="wizard-step-count">Etapa 1 de 7</p>
              <h2 id="camera-modal-title">Implantação da câmera</h2>
              <span class="wizard-header-subtitle">Configure sua câmera em algumas etapas, da conexão à ativação operacional.</span>
            </div>
            <div class="wizard-header-actions">
              <button type="button" id="wizard-draft-header"><i data-lucide="save" aria-hidden="true"></i><span>Salvar rascunho</span></button>
              <button type="button" class="icon-button" id="close-camera-modal" aria-label="Fechar janela">
                <i data-lucide="x" aria-hidden="true"></i>
              </button>
            </div>
          </header>
          <div class="camera-wizard">
            <nav class="wizard-steps" id="wizard-steps" aria-label="Etapas do wizard"></nav>
            <div id="wizard-body" class="wizard-body"></div>
            <footer class="modal-actions wizard-actions">
              <button type="button" id="wizard-back">Voltar</button>
              <button type="button" id="wizard-draft">Salvar rascunho</button>
              <div class="wizard-action-spacer"></div>
              <button type="button" id="wizard-cancel">Cancelar</button>
              <button type="button" id="wizard-next" class="primary-action">Avançar</button>
            </footer>
          </div>
        </div>
      </section>
    </div>
  `;

  document.querySelector("#add-camera").addEventListener("click", openCameraModal);
  document.querySelector("#close-camera-modal").addEventListener("click", closeCameraModal);
  document.querySelector("#wizard-cancel").addEventListener("click", closeCameraModal);
  document.querySelector("#wizard-draft").addEventListener("click", saveCameraWizardDraft);
  document.querySelector("#wizard-draft-header").addEventListener("click", saveCameraWizardDraft);
  document.querySelector("#wizard-back").addEventListener("click", () => moveCameraWizard(-1));
  document.querySelector("#wizard-next").addEventListener("click", () => moveCameraWizard(1));
  document.querySelector("#camera-modal [data-close-modal]").addEventListener("click", closeCameraModal);
  document.querySelector("#refresh-cameras").addEventListener("click", loadCameras);
  refreshIcons();
  await loadCameras();
}

function openCameraModal() {
  const modal = document.querySelector("#camera-modal");
  if (!modal) return;
  editingCameraId = null;
  cameraWizard = null;
  renderCameraRegistrationChoice();
  modal.hidden = false;
  document.body.dataset.modalOpen = "true";
  refreshIcons();
}

function openCameraEditModal(cameraId) {
  const camera = camerasCache.find((item) => item.id === cameraId);
  const modal = document.querySelector("#camera-modal");
  if (!camera || !modal) return;
  editingCameraId = camera.id;
  cameraWizard = newCameraWizardState();
  cameraWizard.camera = {
    ...cameraWizard.camera,
    id: camera.id,
    name: camera.name || "",
    source_type: camera.source_type,
    source_uri: camera.source_uri || "",
    area_id: camera.area_id || "",
    enabled: Boolean(camera.enabled),
    vision_enabled: Boolean(camera.vision_enabled),
  };
  cameraWizard.createdCamera = camera;
  document.querySelector("#camera-modal-title").textContent = "Configurar câmera";
  renderCameraWizard();
  modal.hidden = false;
  document.body.dataset.modalOpen = "true";
  refreshIcons();
}

function closeCameraModal() {
  const modal = document.querySelector("#camera-modal");
  if (!modal) return;
  modal.hidden = true;
  editingCameraId = null;
  cameraWizard = null;
  delete document.body.dataset.modalOpen;
}

function renderCameraRegistrationChoice() {
  document.querySelector("#camera-modal-title").textContent = "Adicionar câmera";
  document.querySelector("#wizard-step-count").textContent = "Escolha o tipo de cadastro";
  const subtitle = document.querySelector(".wizard-header-subtitle");
  if (subtitle) {
    subtitle.textContent = "Comece rápido com um cadastro simples ou configure uma implantação operacional completa.";
  }
  document.querySelector("#wizard-steps").innerHTML = "";
  document.querySelector("#wizard-body").innerHTML = `
    <section class="camera-register-choice">
      <button type="button" data-camera-register-mode="simple">
        <i data-lucide="video" aria-hidden="true"></i>
        <strong>Cadastro simples</strong>
        <span>Registre nome, fonte, status e teste de conexão. Ideal para colocar uma câmera no ar rapidamente.</span>
      </button>
      <button type="button" data-camera-register-mode="advanced">
        <i data-lucide="workflow" aria-hidden="true"></i>
        <strong>Configuração avançada</strong>
        <span>Configure câmera, cena, áreas, monitores, regras, saídas e validação operacional.</span>
      </button>
    </section>
  `;
  document.querySelector(".wizard-actions").hidden = true;
  document.querySelectorAll("[data-camera-register-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.cameraRegisterMode === "simple") {
        renderSimpleCameraRegistration();
        return;
      }
      cameraWizard = newCameraWizardState();
      loadCameraWizardDraft();
      renderCameraWizard();
      document.querySelector(".wizard-actions").hidden = false;
      requestAnimationFrame(() => document.querySelector("[data-wizard-field='name']")?.focus());
    });
  });
  refreshIcons();
}

function renderSimpleCameraRegistration() {
  document.querySelector("#camera-modal-title").textContent = "Cadastro simples";
  document.querySelector("#wizard-step-count").textContent = "Cadastro rápido";
  const subtitle = document.querySelector(".wizard-header-subtitle");
  if (subtitle) {
    subtitle.textContent = "Informe a fonte da câmera, teste a conexão e salve.";
  }
  document.querySelector("#wizard-steps").innerHTML = "";
  document.querySelector(".wizard-actions").hidden = true;
  document.querySelector("#wizard-body").innerHTML = `
    <form id="simple-camera-form" class="simple-camera-form">
      <section class="simple-camera-hero">
        <div>
          <span><i data-lucide="video" aria-hidden="true"></i></span>
          <div>
            <h3>Cadastro rápido</h3>
            <p>Adicione uma câmera com os dados essenciais. Você poderá configurar áreas e monitores depois.</p>
          </div>
        </div>
      </section>

      <section class="simple-camera-grid">
        <label class="simple-field simple-field-wide">
          <span>Nome da câmera</span>
          <input name="name" required maxlength="120" placeholder="Entrada principal" />
        </label>
        <label class="simple-field">
          <span>Área opcional</span>
          <input name="area_id" maxlength="80" placeholder="entrada" />
        </label>
        <label class="simple-field">
          <span>Tipo da fonte</span>
          <select name="source_type">
            <option value="webcam">Webcam</option>
            <option value="video_file">Arquivo de vídeo</option>
            <option value="rtsp">RTSP</option>
            <option value="ip_camera">Câmera IP / HTTP</option>
          </select>
        </label>
        <label class="simple-field simple-field-wide">
          <span>Fonte</span>
          <input name="source_uri" required maxlength="1000" placeholder="0, /caminho/video.mp4, rtsp://usuario:senha@ip/stream ou http://ip/stream" />
        </label>
      </section>

      <section class="simple-camera-options">
        <label>
          <input name="enabled" type="checkbox" checked />
          <span><strong>Câmera ativa</strong><small>Inicia disponível para uso após salvar.</small></span>
        </label>
        <label>
          <input name="vision_enabled" type="checkbox" />
          <span><strong>Vision habilitada</strong><small>Ativa processamento visual quando o backend suportar.</small></span>
        </label>
      </section>

      <section class="simple-camera-test">
        <div class="simple-test-header">
          <div>
            <span><i data-lucide="radio" aria-hidden="true"></i></span>
            <div>
              <h3>Teste de conexão</h3>
              <p>Verifique se a CAMPEX consegue receber um frame dessa fonte.</p>
            </div>
          </div>
          <button type="button" data-simple-action="test"><i data-lucide="radio" aria-hidden="true"></i><span>Testar conexão</span></button>
        </div>
        <div id="simple-camera-test-result" class="modal-test-result" hidden></div>
        <img id="simple-camera-preview" class="camera-preview" alt="Preview da câmera" hidden />
      </section>
      <footer class="simple-camera-actions">
        <button type="button" data-simple-action="back">Voltar</button>
        <button type="button" data-simple-action="cancel">Cancelar</button>
        <button type="submit" class="primary-action">Salvar câmera</button>
      </footer>
    </form>
  `;
  document.querySelector("#simple-camera-form").addEventListener("submit", handleSimpleCameraSubmit);
  document.querySelector("[data-simple-action='back']").addEventListener("click", renderCameraRegistrationChoice);
  document.querySelector("[data-simple-action='cancel']").addEventListener("click", closeCameraModal);
  document.querySelector("[data-simple-action='test']").addEventListener("click", handleSimpleCameraTest);
  refreshIcons();
  requestAnimationFrame(() => document.querySelector("#simple-camera-form input[name='name']")?.focus());
}

function clearCameraTestResult() {
  const result = document.querySelector("#camera-test-result");
  const preview = document.querySelector("#camera-preview");
  if (result) {
    result.hidden = true;
    result.textContent = "";
    result.dataset.state = "";
  }
  if (preview) {
    preview.hidden = true;
    preview.removeAttribute("src");
  }
}

function cameraPayloadFromForm(form) {
  const data = new FormData(form);
  return {
    name: data.get("name").trim(),
    source_type: data.get("source_type"),
    source_uri: data.get("source_uri").trim(),
    area_id: data.get("area_id").trim() || null,
    enabled: data.get("enabled") === "on",
    vision_enabled: data.get("vision_enabled") === "on",
  };
}

async function loadCameras() {
  const listElement = document.querySelector("#camera-list");
  try {
    const cameras = await listCameras();
    camerasCache = await Promise.all(
      cameras.map(async (camera) => {
        try {
          return { ...camera, health: await getCameraHealth(camera.id) };
        } catch {
          return { ...camera, health: { status: camera.status || "OFFLINE" } };
        }
      })
    );
    listElement.innerHTML = cameraRows(camerasCache);
    listElement.querySelector("[data-empty-action='add-camera']")?.addEventListener("click", openCameraModal);
    listElement.querySelectorAll(".camera-row").forEach((row) => {
      row.addEventListener("click", handleCameraAction);
      row
        .querySelector("[data-action='toggle-enabled']")
        .addEventListener("change", handleEnabledChange);
    });
  } catch (error) {
    listElement.innerHTML = `<div class="empty-state"><strong>Falha ao carregar câmeras</strong><span>${error.message}</span></div>`;
  }
}

async function handleCameraSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = cameraPayloadFromForm(form);

  try {
    const camera = editingCameraId
      ? await updateCamera(editingCameraId, payload)
      : await createCamera(payload);
    notify(
      editingCameraId ? "Câmera atualizada" : "Câmera cadastrada",
      `${camera.name} está pronta para operação.`,
      "success"
    );
    form.reset();
    form.elements.enabled.checked = true;
    closeCameraModal();
    await loadCameras();
  } catch (error) {
    notifyError("Falha ao cadastrar câmera", error);
  }
}

async function handleTestCameraSource() {
  const form = document.querySelector("#camera-form");
  const result = document.querySelector("#camera-test-result");
  const preview = document.querySelector("#camera-preview");
  if (!form || !result) return;
  result.hidden = false;
  result.dataset.state = "loading";
  result.textContent = "Testando conexão e aguardando frame válido...";
  if (preview) {
    preview.hidden = true;
    preview.removeAttribute("src");
  }
  try {
    const payload = cameraPayloadFromForm(form);
    const response = await testCameraSource(payload);
    result.dataset.state = response.success ? "success" : "error";
    const resolution = response.resolution ? `${response.resolution.width}x${response.resolution.height}` : "sem resolução";
    result.textContent = response.success
      ? `Conexão OK · ${resolution}`
      : `Falha no teste: ${response.error || response.status}`;
    if (preview && response.preview_data_url) {
      preview.src = response.preview_data_url;
      preview.hidden = false;
    }
  } catch (error) {
    result.dataset.state = "error";
    result.textContent = error.message;
  }
}

function simpleCameraPayloadFromForm(form) {
  const data = new FormData(form);
  return {
    name: String(data.get("name") || "").trim(),
    source_type: data.get("source_type"),
    source_uri: String(data.get("source_uri") || "").trim(),
    area_id: String(data.get("area_id") || "").trim() || null,
    enabled: data.get("enabled") === "on",
    vision_enabled: data.get("vision_enabled") === "on",
  };
}

async function handleSimpleCameraTest() {
  const form = document.querySelector("#simple-camera-form");
  const result = document.querySelector("#simple-camera-test-result");
  const preview = document.querySelector("#simple-camera-preview");
  if (!form || !result) return;
  result.hidden = false;
  result.dataset.state = "loading";
  result.textContent = "Testando conexão e aguardando frame válido...";
  if (preview) {
    preview.hidden = true;
    preview.removeAttribute("src");
  }
  try {
    const response = await testCameraSource(simpleCameraPayloadFromForm(form));
    result.dataset.state = response.success ? "success" : "error";
    const resolution = response.resolution ? `${response.resolution.width}x${response.resolution.height}` : "sem resolução";
    result.textContent = response.success
      ? `Conexão OK · ${resolution}`
      : `Falha no teste: ${response.error || response.status}`;
    if (preview && response.preview_data_url) {
      preview.src = response.preview_data_url;
      preview.hidden = false;
    }
  } catch (error) {
    result.dataset.state = "error";
    result.textContent = error.message;
  }
}

async function handleSimpleCameraSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    const camera = await createCamera(simpleCameraPayloadFromForm(form));
    notify("Câmera cadastrada", `${camera.name} está pronta para operação.`, "success");
    closeCameraModal();
    await loadCameras();
  } catch (error) {
    notifyError("Falha ao cadastrar câmera", error);
  }
}

function newCameraWizardState() {
  return {
    step: 0,
    createdCamera: null,
    createdRoi: null,
    createdMonitor: null,
    previewDataUrl: "",
    camera: {
      name: "",
      description: "",
      organization: "default",
      site: "",
      sector: "",
      source_type: "rtsp",
      ip: "",
      port: "",
      source_uri: "",
      username: "",
      password: "",
      resolution: "",
      fps: "5",
      area_id: "",
      enabled: true,
      vision_enabled: false,
    },
    scene: {
      template: "machine",
      observes: ["Máquina / equipamento"],
    },
    roi: {
      name: "LED Máquina 01",
      type: "LED Máquina 01",
      description: "Indicador visual do estado do equipamento",
      coordinates: { x: 0.62, y: 0.21, width: 0.08, height: 0.07 },
    },
    monitor: {
      name: "Indicador visual",
      type: "visual_indicator",
      debounce_seconds: 2,
      sample_fps: 3,
    },
    states: [
      stateDraft("Verde", "Operando", "#31c46b", { h: 60, s: 190, v: 170 }, false),
      stateDraft("Vermelho", "Parada", "#ef5b5b", { h: 0, s: 190, v: 170 }, true),
      stateDraft("Amarelo", "Aguardando", "#e2b245", { h: 30, s: 170, v: 180 }, false),
      stateDraft("Apagado", "Desligada", "#222222", { h: 0, s: 0, v: 35 }, true),
    ],
    outputs: {
      dashboard: true,
      create_event: true,
      daily_report: true,
      telegram: false,
      email: false,
    },
    validation: null,
    connection: {
      state: "idle",
      message: "Nenhum teste executado ainda",
      latency_ms: null,
    },
  };
}

function stateDraft(name, meaning, color, hsv, isStop) {
  return {
    name,
    operational_meaning: meaning,
    color,
    hsv_target: hsv,
    tolerance: { h: 14, s: 90, v: 90 },
    is_stop_state: isStop,
  };
}

function cameraWizardSteps() {
  return ["Câmera", "Cena", "Áreas", "Monitores", "Regras", "Saídas", "Validar"];
}

function renderCameraWizard() {
  if (!cameraWizard) return;
  const steps = cameraWizardSteps();
  document.querySelector("#wizard-step-count").textContent = `Etapa ${cameraWizard.step + 1} de ${steps.length}`;
  document.querySelector("#wizard-steps").innerHTML = steps.map((step, index) => `
    <button type="button" class="${index === cameraWizard.step ? "is-active" : ""} ${index < cameraWizard.step ? "is-done" : ""}" data-wizard-step="${index}" ${index > cameraWizard.step ? "disabled" : ""}>
      <span>${index < cameraWizard.step ? "✓" : index + 1}</span><strong>${step}</strong>
    </button>
  `).join("");
  document.querySelectorAll("[data-wizard-step]").forEach((button) => {
    button.addEventListener("click", () => {
      syncWizardFields();
      const step = Number(button.dataset.wizardStep);
      if (step > cameraWizard.step) return;
      cameraWizard.step = step;
      renderCameraWizard();
    });
  });
  const body = document.querySelector("#wizard-body");
  body.innerHTML = [
    renderWizardCameraStep,
    renderWizardSceneStep,
    renderWizardRoiStep,
    renderWizardMonitorStep,
    renderWizardRulesStep,
    renderWizardOutputsStep,
    renderWizardValidateStep,
  ][cameraWizard.step]();
  bindWizardStep();
  document.querySelector("#wizard-back").disabled = cameraWizard.step === 0;
  document.querySelector("#wizard-next").textContent = cameraWizard.step === steps.length - 1 ? "Ativar monitoramento" : "Avançar";
  refreshIcons();
}

function renderWizardCameraStep() {
  const c = cameraWizard.camera;
  const connection = cameraWizard.connection || {};
  const isRtsp = c.source_type === "rtsp" || c.source_type === "ip_camera";
  return `
    <section class="wizard-panel">
      ${wizardIntro("Qual câmera estamos configurando?", "Informe o essencial para identificar a câmera e conectar a fonte de vídeo.")}
      <div class="wizard-two-column">
        <section class="wizard-section wizard-section-flat">
          <div class="wizard-section-head">
            <span><i data-lucide="building-2" aria-hidden="true"></i></span>
            <div>
              <h3>Informações básicas</h3>
              <p>Organização, local e identificação operacional.</p>
            </div>
          </div>
          <div class="wizard-form-grid wizard-form-grid-two">
            ${wizardInput("Nome da câmera", "name", c.name, "Produção 01")}
            ${wizardInput("Empresa", "organization", c.organization, "RBA Elevadores")}
            ${wizardInput("Unidade", "site", c.site, "Fábrica 01")}
            ${wizardInput("Setor", "sector", c.sector, "Usinagem")}
            ${wizardInput("Descrição opcional", "description", c.description, "Ex.: visão geral da CNC 01")}
          </div>
        </section>

        <section class="wizard-section wizard-section-flat">
          <div class="wizard-section-head">
            <span><i data-lucide="radio" aria-hidden="true"></i></span>
            <div>
              <h3>Fonte da câmera</h3>
              <p>Escolha como a CAMPEX deve acessar o vídeo.</p>
            </div>
          </div>
          <div class="wizard-source-options">
            ${wizardSourceOption("rtsp", "RTSP / IP", "Câmeras industriais, NVR e streams de rede.", c.source_type)}
            ${wizardSourceOption("video_file", "Arquivo / vídeo", "Use MP4 para teste sem câmera física.", c.source_type)}
            ${wizardSourceOption("webcam", "Outra fonte", "Webcam local ou fonte simples.", c.source_type)}
          </div>
          <div class="wizard-form-grid ${isRtsp ? "wizard-form-grid-two" : ""}">
            ${wizardInput(isRtsp ? "URL RTSP / Fonte principal" : "Fonte", "source_uri", c.source_uri, isRtsp ? "rtsp://usuario:senha@ip/stream" : "0 ou caminho/video.mp4")}
            ${isRtsp ? wizardInput("Endereço IP", "ip", c.ip, "192.168.0.10") : ""}
            ${isRtsp ? wizardInput("Porta", "port", c.port, "554") : ""}
            ${isRtsp ? wizardInput("Usuário", "username", c.username, "Opcional") : ""}
            ${isRtsp ? wizardInput("Senha", "password", c.password, "Não será exibida após salvar", "password") : ""}
          </div>
          <details class="wizard-advanced">
            <summary>Configurações de vídeo</summary>
            <div class="wizard-form-grid wizard-form-grid-three">
              ${wizardInput("Resolução esperada", "resolution", c.resolution, "1920x1080")}
              ${wizardInput("FPS esperado", "fps", c.fps, "5")}
              <label>Status inicial<select data-wizard-field="enabled">
                <option value="true" ${c.enabled ? "selected" : ""}>Ativa</option>
                <option value="false" ${!c.enabled ? "selected" : ""}>Inativa</option>
              </select></label>
            </div>
          </details>
        </section>
      </div>

      <section class="wizard-connection-card" data-state="${connection.state || "idle"}">
        <div>
          <span class="wizard-status-dot"></span>
          <div>
            <strong>${connection.state === "success" ? "Câmera conectada" : connection.state === "error" ? "Não foi possível conectar" : connection.state === "loading" ? "Testando conexão..." : "Teste de conexão"}</strong>
            <p>${connection.message || "Verifique se a câmera está acessível antes de avançar."}</p>
          </div>
        </div>
        <dl>
          <div><dt>Resolução</dt><dd>${c.resolution || "-"}</dd></div>
          <div><dt>Latência</dt><dd>${connection.latency_ms ? `${connection.latency_ms} ms` : "-"}</dd></div>
          <div><dt>FPS</dt><dd>${c.fps || "5"}</dd></div>
        </dl>
        <button type="button" id="wizard-test-camera"><i data-lucide="radio"></i><span>${connection.state === "loading" ? "Testando..." : connection.state === "error" ? "Tentar novamente" : "Testar conexão"}</span></button>
      </section>
    </section>
  `;
}

function renderWizardSceneStep() {
  const options = [
    ["Máquina / equipamento", "factory", "Estados, produção, paradas e operação."],
    ["Linha de produção", "workflow", "Fluxo contínuo, gargalos e atividades."],
    ["Entrada / saída", "log-in", "Passagens, acessos e contagem."],
    ["Pessoas", "users", "Presença, permanência e segurança."],
    ["Estoque", "boxes", "Áreas de armazenagem e disponibilidade."],
    ["Doca", "warehouse", "Carga, descarga e ocupação."],
    ["Veículos", "truck", "Movimentação e presença de veículos."],
    ["Área de carga", "package-check", "Operações logísticas e filas."],
    ["Área de segurança", "shield-alert", "Risco, invasão e permanência."],
    ["Outro", "more-horizontal", "Configuração personalizada."],
  ];
  return `
    <section class="wizard-panel">
      ${wizardIntro("O que esta câmera observa?", "Isso ajuda a CAMPEX a recomendar os melhores monitores para esta câmera.")}
      <div class="wizard-scene-layout">
        <div class="wizard-selection-grid">
          ${options.map(([name, icon, description]) => `
            <label class="wizard-select-card ${cameraWizard.scene.observes.includes(name) ? "is-selected" : ""}">
              <input type="checkbox" data-scene-option value="${name}" ${cameraWizard.scene.observes.includes(name) ? "checked" : ""} />
              <i data-lucide="${icon}" aria-hidden="true"></i>
              <strong>${name}</strong>
              <span>${description}</span>
            </label>
          `).join("")}
        </div>
        <aside class="wizard-preview-panel">
          ${cameraWizard.previewDataUrl ? `<img src="${cameraWizard.previewDataUrl}" alt="Preview da câmera" />` : `<div><i data-lucide="image"></i><span>O preview aparecerá depois do teste de conexão.</span></div>`}
          <label>Template recomendado<select data-wizard-field="template">
            ${["machine:Monitorar máquina", "entry:Monitorar entrada", "people_flow:Fluxo de pessoas", "stock:Monitorar estoque", "dock_vehicle:Doca e veículos", "custom:Configuração personalizada"].map((raw) => {
              const [value, label] = raw.split(":");
              return `<option value="${value}" ${cameraWizard.scene.template === value ? "selected" : ""}>${label}</option>`;
            }).join("")}
          </select></label>
        </aside>
      </div>
    </section>
  `;
}

function renderWizardRoiStep() {
  const r = cameraWizard.roi;
  return `
    <section class="wizard-panel">
      ${wizardIntro("Onde devemos observar?", "Desenhe uma área de interesse sobre o frame. A CAMPEX usa essa região para processar apenas o que importa.")}
      <div class="wizard-roi-layout">
        <div class="wizard-frame-shell">
          <div class="wizard-toolstrip" aria-label="Ferramentas de desenho">
            <button type="button" class="is-active"><i data-lucide="mouse-pointer-2"></i><span>Selecionar</span></button>
            <button type="button" class="is-active"><i data-lucide="square"></i><span>Retângulo</span></button>
            <button type="button" disabled><i data-lucide="pentagon"></i><span>Polígono</span></button>
            <button type="button" disabled><i data-lucide="trash-2"></i><span>Excluir</span></button>
          </div>
          <div class="wizard-frame-host" id="wizard-roi-frame">
          ${cameraWizard.previewDataUrl ? `<img src="${cameraWizard.previewDataUrl}" alt="Frame da câmera" />` : `<span>Teste a conexão para capturar um frame.</span>`}
            <div class="wizard-roi-box" style="${roiBoxStyle(r.coordinates)}"><span>${escapeHtml(r.name)}</span></div>
          </div>
        </div>
        <aside class="wizard-side-form wizard-side-panel">
          <h3>Área selecionada</h3>
          ${wizardInput("Nome", "roi_name", r.name, "LED Máquina 01")}
          ${wizardInput("Tipo", "roi_type", r.type, "Indicador visual")}
          ${wizardInput("Descrição", "roi_description", r.description, "Opcional")}
          <details class="wizard-advanced">
            <summary>Configurações avançadas</summary>
            <p class="wizard-muted">Coordenadas normalizadas usadas internamente pela CAMPEX.</p>
            <pre>${JSON.stringify(r.coordinates, null, 2)}</pre>
          </details>
        </aside>
      </div>
    </section>
  `;
}

function renderWizardMonitorStep() {
  const m = cameraWizard.monitor;
  const monitorTypes = [
    ["visual_indicator", "Indicador visual / LED", "Detecta estados através de cores e luzes.", "traffic-cone"],
    ["motion", "Movimento", "Detecta atividade ou ausência de movimento.", "activity"],
    ["presence", "Pessoas", "Presença, contagem e permanência.", "users"],
    ["entry_exit", "Entrada e saída", "Fluxo através de uma linha virtual.", "arrow-left-right"],
    ["vehicle", "Veículos", "Detecção e acompanhamento.", "truck"],
    ["object", "Objetos", "Detecta objetos específicos.", "box"],
    ["custom", "Monitor personalizado", "Configuração avançada para casos especiais.", "sliders-horizontal"],
  ];
  return `
    <section class="wizard-panel">
      ${wizardIntro("O que você deseja monitorar nesta área?", "Escolha o tipo de inteligência visual. Para LED, configure o significado de cada cor.")}
      <div class="wizard-monitor-layout">
        <div class="wizard-selection-grid wizard-monitor-grid">
          ${monitorTypes.map(([value, name, description, icon]) => `
            <label class="wizard-select-card ${m.type === value ? "is-selected" : ""}">
              <input type="radio" name="monitor_type_choice" data-monitor-type value="${value}" ${m.type === value ? "checked" : ""} />
              <i data-lucide="${icon}" aria-hidden="true"></i>
              <strong>${name}</strong>
              <span>${description}</span>
            </label>
          `).join("")}
        </div>
        <aside class="wizard-side-panel">
          <h3>Estados do indicador</h3>
          <p class="wizard-muted">Diga à CAMPEX o que cada cor significa na operação.</p>
          <div class="wizard-state-list">
            ${cameraWizard.states.map((state, index) => `
              <article class="wizard-state-card">
                <span class="wizard-color-dot" style="background:${state.color}"></span>
                <div>
                  <input data-state-field="name" data-state-index="${index}" value="${state.name}" aria-label="Cor do estado" />
                  <input data-state-field="operational_meaning" data-state-index="${index}" value="${state.operational_meaning}" aria-label="Significado operacional" />
                </div>
                <label><input type="checkbox" data-state-field="is_stop_state" data-state-index="${index}" ${state.is_stop_state ? "checked" : ""} />Estado de parada</label>
              </article>
            `).join("")}
          </div>
          <button type="button" class="wizard-secondary" disabled><i data-lucide="plus"></i><span>Adicionar estado</span></button>
          <details class="wizard-advanced">
            <summary>Configurações avançadas</summary>
            <div class="wizard-form-grid">
              ${wizardInput("Tempo para confirmar mudança (segundos)", "debounce_seconds", m.debounce_seconds, "2", "number")}
              ${wizardInput("Frequência de análise (vezes por segundo)", "sample_fps", m.sample_fps, "3", "number")}
            </div>
          </details>
        </aside>
      </div>
    </section>
  `;
}

function renderWizardRulesStep() {
  return `
    <section class="wizard-panel">
      ${wizardIntro("O que fazer quando algo acontecer?", "As regras ficam em forma de frases para representar decisões operacionais sem termos técnicos.")}
      <div class="wizard-rule-builder">
        <article>
          <div class="wizard-rule-line"><strong>Quando</strong><span>Máquina</span><span>mudar para</span><b>PARADA</b></div>
          <div class="wizard-rule-actions">
            <label><input type="checkbox" checked disabled />Registrar evento</label>
            <label><input type="checkbox" checked disabled />Iniciar cronômetro</label>
            <label><input type="checkbox" ${cameraWizard.outputs.telegram ? "checked" : ""} disabled />Enviar Telegram</label>
            <label><input type="checkbox" ${cameraWizard.outputs.email ? "checked" : ""} disabled />Enviar e-mail</label>
          </div>
        </article>
        <article>
          <div class="wizard-rule-line"><strong>Quando</strong><b>PARADA</b><span>durar mais de</span><b>15 minutos</b></div>
          <div class="wizard-rule-actions">
            <label><input type="checkbox" checked disabled />Criar alerta</label>
            <label><input type="checkbox" ${cameraWizard.outputs.telegram ? "checked" : ""} disabled />Enviar Telegram</label>
          </div>
        </article>
      </div>
      <button type="button" class="wizard-secondary" disabled><i data-lucide="plus"></i><span>Adicionar regra</span></button>
    </section>
  `;
}

function renderWizardOutputsStep() {
  const groups = [
    ["Registro", [["dashboard", "Dashboard"], ["create_event", "Eventos"], ["timeline", "Timeline"]]],
    ["Relatórios", [["daily_report", "Relatório diário"], ["weekly_report", "Relatório semanal"], ["monthly_report", "Relatório mensal"]]],
    ["Notificações", [["telegram", "Telegram imediato"], ["email", "E-mail imediato"]]],
  ];
  return `
    <section class="wizard-panel">
      ${wizardIntro("O que a CAMPEX deve fazer com essas informações?", "Escolha onde os dados devem aparecer e quais canais devem receber alertas.")}
      <div class="wizard-output-grid">
        ${groups.map(([title, items]) => `
          <section class="wizard-section wizard-section-flat">
            <h3>${title}</h3>
            <div class="wizard-toggle-list">
              ${items.map(([key, label]) => `
                <label>
                  <span>${label}</span>
                  <input type="checkbox" data-output-key="${key}" ${cameraWizard.outputs[key] ? "checked" : ""} />
                </label>
              `).join("")}
            </div>
          </section>
        `).join("")}
      </div>
    </section>
  `;
}

function renderWizardValidateStep() {
  const status = cameraWizard.validation;
  return `
    <section class="wizard-panel">
      ${wizardIntro("Tudo pronto para começar", "Revise a configuração e execute um teste antes de ativar o monitoramento.")}
      <div class="wizard-validation-layout">
        <section class="wizard-section wizard-section-flat">
          <div class="wizard-summary-grid">
            ${wizardSummaryItem("Câmera", cameraWizard.createdCamera?.name || cameraWizard.camera.name || "-")}
            ${wizardSummaryItem("Local", `${cameraWizard.camera.site || "-"} / ${cameraWizard.camera.sector || "-"}`)}
            ${wizardSummaryItem("Conexão", cameraWizard.connection?.state === "success" ? "Online" : "Pendente")}
            ${wizardSummaryItem("Áreas", cameraWizard.createdRoi ? "1 configurada" : "Pendente")}
            ${wizardSummaryItem("Monitores", cameraWizard.createdMonitor ? "1 ativo" : "Pendente")}
            ${wizardSummaryItem("Saídas", activeOutputsLabel())}
          </div>
        </section>
        <section class="wizard-live-test">
          <div class="wizard-frame-host">${cameraWizard.createdCamera ? `<img src="${cameraSnapshotUrl(cameraWizard.createdCamera.id)}" alt="Validação da câmera" />` : "Salve a câmera para validar."}<div class="wizard-roi-box" style="${roiBoxStyle(cameraWizard.roi.coordinates)}"><span>${escapeHtml(cameraWizard.roi.name)}</span></div></div>
          <aside class="wizard-side-panel">
            <h3>Teste ao vivo</h3>
            <button type="button" id="wizard-run-validation">Executar teste</button>
            <dl class="wizard-status">
              <dt>Área</dt><dd>${cameraWizard.createdRoi?.name || cameraWizard.roi.name}</dd>
              <dt>Estado</dt><dd>${status?.detected_state?.operational_meaning || "Aguardando teste"}</dd>
              <dt>Indicador</dt><dd>${status?.detected_state?.name || "-"}</dd>
              <dt>Confiança</dt><dd>${status?.confidence ? `${Math.round(status.confidence * 100)}%` : "-"}</dd>
              <dt>Estável</dt><dd>${status?.stable ? "Sim" : "Não"}</dd>
            </dl>
            <div class="wizard-checklist">
              ${["Câmera", "Áreas", "Monitor", "Estados", "Regras", "Saídas"].map((item) => `<span><i data-lucide="check"></i>${item}</span>`).join("")}
            </div>
          </aside>
        </section>
      </div>
    </section>
  `;
}

function wizardInput(label, field, value, placeholder = "", type = "text") {
  return `<label>${label}<input data-wizard-field="${field}" type="${type}" value="${escapeHtml(value ?? "")}" placeholder="${placeholder}" /></label>`;
}

function wizardIntro(title, description) {
  return `<header class="wizard-intro"><h3>${title}</h3><p>${description}</p></header>`;
}

function wizardSourceOption(value, title, description, current) {
  return `
    <label class="wizard-select-card wizard-source-card ${current === value || (value === "rtsp" && current === "ip_camera") ? "is-selected" : ""}">
      <input type="radio" name="source_type_choice" data-source-type value="${value}" ${current === value || (value === "rtsp" && current === "ip_camera") ? "checked" : ""} />
      <strong>${title}</strong>
      <span>${description}</span>
    </label>
  `;
}

function wizardSummaryItem(label, value) {
  return `<article><span>${label}</span><strong>${escapeHtml(value)}</strong></article>`;
}

function activeOutputsLabel() {
  const labels = [];
  if (cameraWizard.outputs.dashboard) labels.push("Dashboard");
  if (cameraWizard.outputs.create_event) labels.push("Eventos");
  if (cameraWizard.outputs.daily_report) labels.push("Relatório diário");
  if (cameraWizard.outputs.telegram) labels.push("Telegram");
  if (cameraWizard.outputs.email) labels.push("E-mail");
  return labels.join(", ") || "Nenhuma saída";
}

function bindWizardStep() {
  document.querySelectorAll("[data-wizard-field]").forEach((input) => {
    input.addEventListener("input", syncWizardFields);
    input.addEventListener("change", syncWizardFields);
  });
  document.querySelectorAll("[data-scene-option]").forEach((input) => input.addEventListener("change", syncWizardFields));
  document.querySelectorAll("[data-source-type]").forEach((input) => input.addEventListener("change", syncWizardFields));
  document.querySelectorAll("[data-monitor-type]").forEach((input) => input.addEventListener("change", syncWizardFields));
  document.querySelectorAll("[data-output-key]").forEach((input) => input.addEventListener("change", syncWizardFields));
  document.querySelectorAll("[data-state-field]").forEach((input) => input.addEventListener("input", syncWizardFields));
  document.querySelectorAll("[data-state-field][type='checkbox']").forEach((input) => input.addEventListener("change", syncWizardFields));
  document.querySelector("#wizard-test-camera")?.addEventListener("click", wizardTestCamera);
  document.querySelector("#wizard-run-validation")?.addEventListener("click", wizardRunValidation);
  bindRoiDrawing();
}

function syncWizardFields() {
  if (!cameraWizard) return;
  document.querySelectorAll("[data-wizard-field]").forEach((input) => {
    const key = input.dataset.wizardField;
    if (key.startsWith("roi_")) cameraWizard.roi[key.replace("roi_", "")] = input.value;
    else if (key.startsWith("monitor_")) cameraWizard.monitor[key.replace("monitor_", "")] = input.value;
    else if (key in cameraWizard.scene) cameraWizard.scene[key] = input.value;
    else if (key in cameraWizard.monitor) cameraWizard.monitor[key] = input.value;
    else if (key === "enabled") cameraWizard.camera[key] = input.value === "true" || input.checked;
    else cameraWizard.camera[key] = input.type === "checkbox" ? input.checked : input.value;
  });
  cameraWizard.scene.observes = Array.from(document.querySelectorAll("[data-scene-option]:checked")).map((item) => item.value);
  const selectedSource = document.querySelector("[data-source-type]:checked");
  if (selectedSource) cameraWizard.camera.source_type = selectedSource.value;
  const selectedMonitorType = document.querySelector("[data-monitor-type]:checked");
  if (selectedMonitorType) cameraWizard.monitor.type = selectedMonitorType.value;
  document.querySelectorAll("[data-output-key]").forEach((input) => {
    cameraWizard.outputs[input.dataset.outputKey] = input.checked;
  });
  document.querySelectorAll("[data-state-field]").forEach((input) => {
    const state = cameraWizard.states[Number(input.dataset.stateIndex)];
    state[input.dataset.stateField] = input.type === "checkbox" ? input.checked : input.value;
  });
}

async function moveCameraWizard(direction) {
  syncWizardFields();
  if (direction > 0) {
    const ok = await persistWizardStep();
    if (!ok) return;
  }
  cameraWizard.step = Math.max(0, Math.min(cameraWizardSteps().length - 1, cameraWizard.step + direction));
  renderCameraWizard();
}

async function persistWizardStep() {
  try {
    if (cameraWizard.step === 0 && !cameraWizard.createdCamera) {
      const payload = {
        name: cameraWizard.camera.name || "Câmera sem nome",
        area_id: cameraWizard.camera.sector || cameraWizard.camera.area_id || null,
        source_type: cameraWizard.camera.source_type,
        source_uri: cameraWizard.camera.source_uri || "0",
        enabled: true,
        vision_enabled: false,
      };
      cameraWizard.createdCamera = await createCamera(payload);
      await loadCameras();
    }
    if (cameraWizard.step === 2 && cameraWizard.createdCamera && !cameraWizard.createdRoi) {
      cameraWizard.createdRoi = await createCameraRoi(cameraWizard.createdCamera.id, {
        name: cameraWizard.roi.name,
        type: cameraWizard.roi.type,
        shape: "rect",
        coordinates: cameraWizard.roi.coordinates,
        description: cameraWizard.roi.description,
        enabled: true,
      });
    }
    if (cameraWizard.step === 3 && cameraWizard.createdCamera && cameraWizard.createdRoi && !cameraWizard.createdMonitor) {
      cameraWizard.createdMonitor = await createMonitor({
        camera_id: cameraWizard.createdCamera.id,
        roi_id: cameraWizard.createdRoi.id,
        type: cameraWizard.monitor.type,
        name: cameraWizard.monitor.name,
        configuration: {
          debounce_seconds: Number(cameraWizard.monitor.debounce_seconds || 2),
          sample_fps: Number(cameraWizard.monitor.sample_fps || 3),
          outputs: cameraWizard.outputs,
        },
        enabled: true,
        states: cameraWizard.states,
      });
    }
    if (cameraWizard.step === 6) {
      notify("Monitoramento ativado", "Configuração operacional salva para a câmera.", "success");
      closeCameraModal();
      await loadCameras();
      return false;
    }
    return true;
  } catch (error) {
    notifyError("Falha no wizard", error);
    return false;
  }
}

async function wizardTestCamera() {
  syncWizardFields();
  cameraWizard.connection = {
    state: "loading",
    message: "Aguardando um frame válido da fonte de vídeo...",
    latency_ms: null,
  };
  renderCameraWizard();
  const startedAt = performance.now();
  try {
    const response = await testCameraSource({
      name: cameraWizard.camera.name || "Teste de câmera",
      area_id: cameraWizard.camera.sector || null,
      source_type: cameraWizard.camera.source_type,
      source_uri: cameraWizard.camera.source_uri || "0",
      enabled: false,
      vision_enabled: false,
    });
    if (!response.success) throw new Error(response.error || "Falha na conexão.");
    cameraWizard.previewDataUrl = response.preview_data_url || "";
    if (response.resolution) cameraWizard.camera.resolution = `${response.resolution.width}x${response.resolution.height}`;
    cameraWizard.connection = {
      state: "success",
      message: "Conexão funcionando. A CAMPEX recebeu um frame da câmera.",
      latency_ms: Math.max(1, Math.round(performance.now() - startedAt)),
    };
    renderCameraWizard();
  } catch (error) {
    cameraWizard.connection = {
      state: "error",
      message: error.message,
      latency_ms: null,
    };
    renderCameraWizard();
  }
}

async function wizardRunValidation() {
  if (!cameraWizard.createdMonitor?.id) {
    notify("Validação pendente", "Avance pelas etapas para salvar ROI e monitor antes de validar.", "warning");
    return;
  }
  try {
    cameraWizard.validation = await getMonitorStatus(cameraWizard.createdMonitor.id);
    renderCameraWizard();
  } catch (error) {
    notifyError("Falha na validação", error);
  }
}

function saveCameraWizardDraft() {
  syncWizardFields();
  localStorage.setItem("campex.camera_wizard_draft", JSON.stringify(cameraWizard));
  notify("Rascunho salvo", "Você pode continuar a configuração posteriormente.", "success");
}

function loadCameraWizardDraft() {
  try {
    const draft = JSON.parse(localStorage.getItem("campex.camera_wizard_draft") || "null");
    if (draft?.camera && !cameraWizard.createdCamera) cameraWizard = { ...cameraWizard, ...draft, step: draft.step || 0 };
  } catch {
    return;
  }
}

function bindRoiDrawing() {
  const host = document.querySelector("#wizard-roi-frame");
  if (!host) return;
  let start = null;
  host.addEventListener("pointerdown", (event) => {
    const rect = host.getBoundingClientRect();
    start = { x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height };
  });
  host.addEventListener("pointerup", (event) => {
    if (!start) return;
    const rect = host.getBoundingClientRect();
    const end = { x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height };
    const x = Math.max(0, Math.min(start.x, end.x));
    const y = Math.max(0, Math.min(start.y, end.y));
    const width = Math.min(1 - x, Math.abs(end.x - start.x));
    const height = Math.min(1 - y, Math.abs(end.y - start.y));
    if (width > 0.01 && height > 0.01) {
      cameraWizard.roi.coordinates = { x: Number(x.toFixed(4)), y: Number(y.toFixed(4)), width: Number(width.toFixed(4)), height: Number(height.toFixed(4)) };
      renderCameraWizard();
    }
    start = null;
  });
}

function roiBoxStyle(rect) {
  return `left:${rect.x * 100}%;top:${rect.y * 100}%;width:${rect.width * 100}%;height:${rect.height * 100}%;`;
}

async function handleEnabledChange(event) {
  const row = event.currentTarget.closest(".camera-row");
  const enabled = event.currentTarget.checked;
  try {
    await updateCamera(row.dataset.cameraId, { enabled });
    notify(enabled ? "Câmera ativada" : "Câmera pausada", "Estado atualizado com sucesso.", enabled ? "success" : "warning");
    await loadCameras();
  } catch (error) {
    notifyError("Falha ao atualizar câmera", error);
  }
}

async function handleCameraAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) {
    return;
  }

  const row = button.closest(".camera-row");
  const result = row.querySelector(".camera-result");
  const cameraId = row.dataset.cameraId;
  const action = button.dataset.action;

  try {
    if (action === "edit") {
      openCameraEditModal(cameraId);
      return;
    }

    if (action === "delete") {
      await deleteCamera(cameraId);
      notify("Câmera removida", "A fonte foi excluída do sistema.", "warning");
      await loadCameras();
      return;
    }

    const response =
      action === "test"
        ? await testCamera(cameraId)
        : action === "diagnostics"
          ? await getCameraDiagnostics(cameraId)
          : await getCameraHealth(cameraId);
    result.hidden = false;
    result.textContent = JSON.stringify(response, null, 2);
    if (action === "test") {
      notify("Teste de câmera concluído", response.status || "Conexão verificada.", "success");
    } else if (action === "diagnostics") {
      notify("Diagnóstico carregado", "Health, frames e Vision foram consultados.", "info");
    } else {
      notify("Saúde da câmera", response.status || "Status atualizado.", response.status === "ONLINE" ? "success" : "warning");
    }
  } catch (error) {
    result.hidden = false;
    result.textContent = error.message;
    notifyError("Falha na ação da câmera", error);
  }
}

async function renderEventsPage() {
  appView.innerHTML = `
    <div class="ops-page">
      <section class="ops-toolbar">
        <label>Status
          <select id="event-status-filter">
            <option value="">Todos</option>
            <option value="OPEN">Abertos</option>
            <option value="REVIEWED">Revisados</option>
            <option value="CLOSED">Fechados</option>
          </select>
        </label>
        <label>Câmera
          <select id="event-camera-filter"><option value="">Todas</option></select>
        </label>
        <label>Tipo
          <input id="event-type-filter" placeholder="PERSON_RESTRICTED_ZONE" />
        </label>
        <button type="button" id="event-refresh">Atualizar</button>
      </section>
      <section class="ops-grid">
        <div>
          <div class="section-heading"><h2>Fila de eventos</h2><span id="event-count">0</span></div>
          <div id="event-list" class="ops-list"></div>
        </div>
        <aside class="ops-detail">
          <h2>Evento selecionado</h2>
          <div id="event-detail">${emptyState("Selecione um evento", "Abra detalhes para revisar contexto e evidência.")}</div>
          <h2>Resumo</h2>
          <dl id="event-summary" class="metric-list"></dl>
        </aside>
      </section>
    </div>
  `;
  document.querySelector("#event-refresh").addEventListener("click", loadEvents);
  ["#event-status-filter", "#event-camera-filter", "#event-type-filter"].forEach((selector) => {
    document.querySelector(selector).addEventListener("change", loadEvents);
    document.querySelector(selector).addEventListener("input", debounce(loadEvents, 250));
  });
  await populateCameraSelect("#event-camera-filter", true);
  await loadEvents();
}

async function loadEvents() {
  const list = document.querySelector("#event-list");
  const params = {
    status: document.querySelector("#event-status-filter")?.value,
    camera_id: document.querySelector("#event-camera-filter")?.value,
    event_type: document.querySelector("#event-type-filter")?.value?.trim(),
    limit: 300,
  };
  try {
    const [events, cameras, zones] = await Promise.all([listEvents(params), listCameras(), listZones()]);
    const cameraById = Object.fromEntries(cameras.map((camera) => [camera.id, camera]));
    const zoneById = Object.fromEntries(zones.map((zone) => [zone.id, zone]));
    document.querySelector("#event-count").textContent = `${events.length} eventos`;
    list.innerHTML = events.length
      ? events.map((event) => eventRow(event, cameraById, zoneById)).join("")
      : emptyState("Nenhum evento encontrado", "Ajuste os filtros ou gere eventos entrando em uma zona.");
    list.querySelectorAll("[data-event-action]").forEach((button) => {
      button.addEventListener("click", handleEventAction);
    });
    renderEventSummary(events);
  } catch (error) {
    list.innerHTML = emptyState("Falha ao carregar eventos", error.message);
  }
}

function eventRow(event, cameraById, zoneById) {
  const camera = cameraById[event.camera_id];
  const zone = event.zone_id ? zoneById[event.zone_id] : null;
  const category = event.metadata?.facts?.activity?.category || "other";
  return `
    <article class="ops-row" data-event-id="${event.id}">
      <div>
        <strong>${event.type}</strong>
        <span>${category} · ${camera?.name || event.camera_id} · ${zone?.name || event.zone_id || "sem zona"} · track ${event.track_id ?? "-"}</span>
        <small>${formatDate(event.started_at)} · ${event.confidence ? Math.round(event.confidence * 100) : "-"}%</small>
      </div>
      <span class="severity-badge" data-severity="${event.severity}">${event.severity}</span>
      <span class="health-badge" data-status="${event.status}">${event.status}</span>
      <div class="row-actions">
        <button type="button" data-event-action="review">Revisar</button>
        <button type="button" data-event-action="close">Fechar</button>
        <button type="button" data-event-action="details">Detalhes</button>
        <button type="button" data-event-action="investigate">Investigar</button>
        <button type="button" data-event-action="delete">Excluir</button>
      </div>
    </article>
  `;
}

async function handleEventAction(event) {
  const button = event.currentTarget;
  const row = button.closest("[data-event-id]");
  const eventId = row.dataset.eventId;
  const action = button.dataset.eventAction;
  if (action === "review") {
    await updateEvent(eventId, { status: "REVIEWED" });
  } else if (action === "close") {
    await updateEvent(eventId, { status: "CLOSED" });
  } else if (action === "investigate") {
    await createInvestigationFromEvent(eventId);
    window.location.hash = "investigations";
    return;
  } else if (action === "details") {
    await showEventDetail(eventId);
    return;
  } else if (action === "delete") {
    await deleteEvent(eventId);
  }
  await loadEvents();
}

async function showEventDetail(eventId) {
  const [events, cameras, zones] = await Promise.all([
    listEvents({ limit: 1000 }),
    listCameras(),
    listZones(),
  ]);
  const event = events.find((item) => item.id === eventId);
  if (!event) return;
  const camera = cameras.find((item) => item.id === event.camera_id);
  const zone = zones.find((item) => item.id === event.zone_id);
  const hasEvidence = Boolean(event.metadata?.overlay_path);
  const category = event.metadata?.facts?.activity?.category || "other";
  document.querySelector("#event-detail").innerHTML = `
    <div class="event-detail-panel">
      ${hasEvidence ? `<img class="event-evidence" src="${eventEvidenceUrl(event.id)}" alt="Evidência do evento ${event.id}" />` : ""}
      <dl class="metric-list">
        <dt>ID</dt><dd>${event.id}</dd>
        <dt>Tipo</dt><dd>${event.type}</dd>
        <dt>Categoria</dt><dd>${category}</dd>
        <dt>Câmera</dt><dd>${camera?.name || event.camera_id}</dd>
        <dt>Zona</dt><dd>${zone?.name || event.zone_id || "-"}</dd>
        <dt>Track</dt><dd>${event.track_id ?? "-"}</dd>
        <dt>Status</dt><dd>${event.status}</dd>
        <dt>Início</dt><dd>${formatDate(event.started_at)}</dd>
        <dt>Evidência</dt><dd>${hasEvidence ? "salva" : "não disponível"}</dd>
      </dl>
      <pre class="code-block">${JSON.stringify(event.metadata || {}, null, 2)}</pre>
    </div>
  `;
}

function renderEventSummary(events) {
  const open = events.filter((event) => event.status === "OPEN").length;
  const critical = events.filter((event) => event.severity === "critical").length;
  const types = new Set(events.map((event) => event.type)).size;
  document.querySelector("#event-summary").innerHTML = `
    <dt>Total</dt><dd>${events.length}</dd>
    <dt>Abertos</dt><dd>${open}</dd>
    <dt>Críticos</dt><dd>${critical}</dd>
    <dt>Tipos</dt><dd>${types}</dd>
  `;
}

async function renderInvestigationsPage() {
  appView.innerHTML = `
    <div class="ops-page">
      <section class="ops-toolbar">
        <button type="button" id="new-investigation">Nova investigação</button>
        <button type="button" id="clear-closed-investigations">Limpar fechadas</button>
      </section>
      <section class="ops-grid">
        <div>
          <div class="section-heading"><h2>Investigações</h2><span id="investigation-count">0</span></div>
          <div id="investigation-list" class="ops-list"></div>
        </div>
        <aside class="ops-detail">
          <h2>Workspace</h2>
          <form id="investigation-form" class="stack-form">
            <label>Título<input name="title" required placeholder="Pessoa em zona restrita" /></label>
            <label>Status
              <select name="status">
                <option value="OPEN">Aberta</option>
                <option value="REVIEWING">Em revisão</option>
                <option value="CLOSED">Fechada</option>
              </select>
            </label>
            <label>Evento relacionado<input name="event_id" placeholder="evt_..." /></label>
            <label>Notas<textarea name="notes" rows="8" placeholder="Hipótese, contexto, próximos passos..."></textarea></label>
            <button type="submit">Salvar investigação</button>
          </form>
        </aside>
      </section>
    </div>
  `;
  document.querySelector("#new-investigation").addEventListener("click", () => fillInvestigationForm());
  document.querySelector("#clear-closed-investigations").addEventListener("click", clearClosedInvestigations);
  document.querySelector("#investigation-form").addEventListener("submit", saveInvestigation);
  await renderInvestigations();
}

async function createInvestigationFromEvent(eventId) {
  const items = await listInvestigations();
  if (items.some((item) => item.event_id === eventId)) {
    return;
  }
  await createInvestigation({
    title: `Investigação ${eventId}`,
    status: "OPEN",
    event_id: eventId,
    notes: "",
  });
}

async function renderInvestigations() {
  const items = await listInvestigations();
  document.querySelector("#investigation-count").textContent = `${items.length} casos`;
  document.querySelector("#investigation-list").innerHTML = items.length
    ? items.map((item) => `
      <article class="ops-row" data-investigation-id="${item.id}">
        <div>
          <strong>${item.title}</strong>
          <span>${item.event_id || "sem evento"} · ${formatDate(item.updated_at)}</span>
          <small>${item.notes ? item.notes.slice(0, 120) : "sem notas"}</small>
        </div>
        <span class="health-badge" data-status="${item.status}">${item.status}</span>
        <div class="row-actions">
          <button type="button" data-investigation-action="edit">Abrir</button>
          <button type="button" data-investigation-action="close">Fechar</button>
          <button type="button" data-investigation-action="delete">Excluir</button>
        </div>
      </article>
    `).join("")
    : emptyState("Nenhuma investigação", "Crie manualmente ou envie um evento para investigação.");
  document.querySelectorAll("[data-investigation-action]").forEach((button) => {
    button.addEventListener("click", handleInvestigationAction);
  });
}

function fillInvestigationForm(item = null) {
  const form = document.querySelector("#investigation-form");
  form.dataset.investigationId = item?.id || "";
  form.elements.title.value = item?.title || "";
  form.elements.status.value = item?.status || "OPEN";
  form.elements.event_id.value = item?.event_id || "";
  form.elements.notes.value = item?.notes || "";
}

async function saveInvestigation(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const id = form.dataset.investigationId;
  const payload = {
    title: form.elements.title.value.trim(),
    status: form.elements.status.value,
    event_id: form.elements.event_id.value.trim(),
    notes: form.elements.notes.value.trim(),
  };
  if (id) {
    await updateInvestigation(id, payload);
  } else {
    await createInvestigation(payload);
  }
  fillInvestigationForm();
  await renderInvestigations();
}

async function handleInvestigationAction(event) {
  const row = event.currentTarget.closest("[data-investigation-id]");
  const id = row.dataset.investigationId;
  const action = event.currentTarget.dataset.investigationAction;
  const items = await listInvestigations();
  const item = items.find((entry) => entry.id === id);
  if (!item) return;
  if (action === "edit") {
    fillInvestigationForm(item);
  } else if (action === "close") {
    await updateInvestigation(id, { status: "CLOSED" });
  } else if (action === "delete") {
    await deleteInvestigation(id);
  }
  await renderInvestigations();
}

async function clearClosedInvestigations() {
  const items = await listInvestigations({ status: "CLOSED" });
  await Promise.all(items.map((item) => deleteInvestigation(item.id)));
  await renderInvestigations();
}

async function renderZonesPage() {
  appView.innerHTML = `
    <div class="ops-page">
      <section class="ops-toolbar">
        <label>Câmera<select id="zone-camera-select"></select></label>
        <button type="button" id="zone-refresh">Atualizar</button>
      </section>
      <section class="ops-grid">
        <div>
          <div class="section-heading"><h2>Áreas & zonas</h2><span id="zone-count">0</span></div>
          <div id="area-summary" class="system-strip compact-strip"></div>
          <div class="zone-designer">
            <div class="zone-designer-header">
              <strong id="zone-designer-title">Desenho da câmera</strong>
              <span id="zone-draw-status">Clique na imagem para adicionar pontos</span>
            </div>
            <div id="zone-media-host" class="zone-media-host">
              <div class="media-placeholder">Selecione uma câmera</div>
            </div>
            <div class="zone-draw-tools">
              <button type="button" id="zone-undo-point">Desfazer ponto</button>
              <button type="button" id="zone-clear-points">Limpar desenho</button>
              <button type="button" id="zone-use-full-frame">Quadro inteiro</button>
            </div>
          </div>
          <div id="zone-list" class="ops-list"></div>
        </div>
        <aside class="ops-detail">
          <h2>Nova zona</h2>
          <form id="zone-form" class="stack-form">
            <label>Nome<input name="name" required placeholder="Porta principal" /></label>
            <label>Tipo
              <select name="type">
                <option value="monitored">Monitorada</option>
                <option value="restricted">Restrita</option>
              </select>
            </label>
            <label>Pontos normalizados
              <textarea name="points" rows="6">[]</textarea>
            </label>
            <label class="inline-toggle"><input name="enabled" type="checkbox" checked /> Ativa</label>
            <button type="submit">Salvar zona</button>
          </form>
        </aside>
      </section>
    </div>
  `;
  await populateCameraSelect("#zone-camera-select", false);
  document.querySelector("#zone-camera-select").addEventListener("change", loadZonesPageData);
  document.querySelector("#zone-refresh").addEventListener("click", loadZonesPageData);
  document.querySelector("#zone-form").addEventListener("submit", saveZone);
  document.querySelector("#zone-form").elements.points.addEventListener("input", syncZonePointsFromTextarea);
  document.querySelector("#zone-form").elements.type.addEventListener("change", drawZoneCanvas);
  document.querySelector("#zone-undo-point").addEventListener("click", undoZonePoint);
  document.querySelector("#zone-clear-points").addEventListener("click", clearZoneDrawing);
  document.querySelector("#zone-use-full-frame").addEventListener("click", useFullFrameZone);
  await loadZonesPageData();
}

async function loadZonesPageData() {
  const cameraId = document.querySelector("#zone-camera-select")?.value;
  const [cameras, zones] = await Promise.all([listCameras(), listZones(cameraId)]);
  const selectedCamera = cameras.find((camera) => camera.id === cameraId);
  zonePreviewZones = zones;
  renderAreaSummary(cameras, zones);
  renderZoneMedia(selectedCamera);
  document.querySelector("#zone-count").textContent = `${zones.length} zonas`;
  document.querySelector("#zone-list").innerHTML = zones.length
    ? zones.map((zone) => zoneRow(zone, selectedCamera)).join("")
    : emptyState("Nenhuma zona cadastrada", "Crie uma zona com pontos normalizados entre 0 e 1.");
  document.querySelectorAll("[data-zone-action]").forEach((button) => {
    button.addEventListener("click", handleZoneAction);
  });
  drawZoneCanvas();
}

function renderAreaSummary(cameras, zones) {
  const areaIds = [...new Set(cameras.map((camera) => camera.area_id || "sem_area"))];
  document.querySelector("#area-summary").innerHTML = areaIds.map((areaId) => {
    const areaCameras = cameras.filter((camera) => (camera.area_id || "sem_area") === areaId);
    return `
      <div class="system-cell">
        <span>${areaId === "sem_area" ? "Sem área" : areaId}</span>
        <strong>${areaCameras.length} câmeras · ${zones.length} zonas visíveis</strong>
      </div>
    `;
  }).join("");
}

function zoneRow(zone, camera) {
  return `
    <article class="ops-row" data-zone-id="${zone.id}">
      <div>
        <strong>${zone.name}</strong>
        <span>${camera?.name || zone.camera_id} · ${zone.points.length} pontos</span>
        <small>${zone.points.map((point) => `[${point.join(", ")}]`).join(" ")}</small>
      </div>
      <span class="severity-badge" data-severity="${zone.type === "restricted" ? "critical" : "attention"}">${zone.type}</span>
      <label class="inline-toggle"><input type="checkbox" data-zone-action="toggle" ${zone.enabled ? "checked" : ""} /> Ativa</label>
      <div class="row-actions"><button type="button" data-zone-action="delete">Excluir</button></div>
    </article>
  `;
}

function renderZoneMedia(camera) {
  const host = document.querySelector("#zone-media-host");
  document.querySelector("#zone-designer-title").textContent = camera?.name || "Desenho da câmera";
  if (!camera?.id) {
    host.innerHTML = `<div class="media-placeholder">Cadastre ou selecione uma câmera</div>`;
    return;
  }

  const source = camera.source_type === "video_file" ? cameraVideoUrl(camera.id) : cameraStreamUrl(camera.id);
  const media = camera.source_type === "video_file"
    ? `<video id="zone-media" class="zone-media" src="${source}" autoplay muted playsinline loop controls></video>`
    : `<img id="zone-media" class="zone-media" src="${source}" alt="Preview da câmera" />`;

  host.innerHTML = `
    ${media}
    <canvas id="zone-draw-canvas" class="zone-draw-canvas" aria-label="Desenho de zona"></canvas>
  `;
  const canvas = document.querySelector("#zone-draw-canvas");
  canvas.addEventListener("pointerdown", addZonePoint);
  window.addEventListener("resize", drawZoneCanvas, { once: true });
  setTimeout(drawZoneCanvas, 80);
}

function addZonePoint(event) {
  const canvas = event.currentTarget;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const x = clamp01((event.clientX - rect.left) / rect.width);
  const y = clamp01((event.clientY - rect.top) / rect.height);
  zoneDrawingPoints.push([roundPoint(x), roundPoint(y)]);
  updateZonePointsField();
  drawZoneCanvas();
}

function syncZonePointsFromTextarea() {
  const form = document.querySelector("#zone-form");
  try {
    const parsed = JSON.parse(form.elements.points.value);
    if (Array.isArray(parsed)) {
      zoneDrawingPoints = parsed
        .filter((point) => Array.isArray(point) && point.length === 2)
        .map((point) => [clamp01(Number(point[0])), clamp01(Number(point[1]))]);
      drawZoneCanvas();
    }
  } catch {
    updateZoneDrawStatus("JSON inválido nos pontos");
  }
}

function updateZonePointsField() {
  const form = document.querySelector("#zone-form");
  form.elements.points.value = JSON.stringify(zoneDrawingPoints);
  updateZoneDrawStatus();
}

function undoZonePoint() {
  zoneDrawingPoints.pop();
  updateZonePointsField();
  drawZoneCanvas();
}

function clearZoneDrawing() {
  zoneDrawingPoints = [];
  updateZonePointsField();
  drawZoneCanvas();
}

function useFullFrameZone() {
  zoneDrawingPoints = [[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]];
  updateZonePointsField();
  drawZoneCanvas();
}

function updateZoneDrawStatus(message = "") {
  const status = document.querySelector("#zone-draw-status");
  if (!status) return;
  status.textContent = message || (
    zoneDrawingPoints.length < 3
      ? `${zoneDrawingPoints.length} ponto(s). Marque pelo menos 3.`
      : `${zoneDrawingPoints.length} pontos prontos para salvar.`
  );
}

function drawZoneCanvas() {
  const canvas = document.querySelector("#zone-draw-canvas");
  const host = document.querySelector("#zone-media-host");
  if (!canvas || !host) return;
  const rect = host.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  canvas.style.width = `${rect.width}px`;
  canvas.style.height = `${rect.height}px`;

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);
  zonePreviewZones.forEach((zone) => drawZonePolygon(ctx, zone.points, rect, zone.type, false));
  drawZonePolygon(ctx, zoneDrawingPoints, rect, document.querySelector("#zone-form")?.elements.type.value || "restricted", true);
  updateZoneDrawStatus();
}

function drawZonePolygon(ctx, points, rect, type, editing) {
  if (!points?.length) return;
  const color = type === "restricted" ? "#ff4d4d" : "#f0c94a";
  ctx.lineWidth = editing ? 3 : 2;
  ctx.strokeStyle = color;
  ctx.fillStyle = editing ? "rgba(255, 77, 77, 0.22)" : "rgba(240, 201, 74, 0.12)";
  ctx.beginPath();
  points.forEach(([x, y], index) => {
    const px = Number(x) * rect.width;
    const py = Number(y) * rect.height;
    if (index === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  });
  if (points.length >= 3) {
    ctx.closePath();
    ctx.fill();
  }
  ctx.stroke();
  points.forEach(([x, y], index) => {
    const px = Number(x) * rect.width;
    const py = Number(y) * rect.height;
    ctx.beginPath();
    ctx.arc(px, py, editing ? 5 : 3, 0, Math.PI * 2);
    ctx.fillStyle = editing ? "#ffffff" : color;
    ctx.fill();
    if (editing) {
      ctx.fillStyle = "#050505";
      ctx.font = "10px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(index + 1), px, py);
    }
  });
}

function clamp01(value) {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

function roundPoint(value) {
  return Math.round(value * 10000) / 10000;
}

async function saveZone(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const cameraId = document.querySelector("#zone-camera-select")?.value;
  if (!cameraId) return;
  let points;
  try {
    points = JSON.parse(form.elements.points.value);
  } catch {
    notify("Pontos inválidos", "Use JSON como [[0.1,0.1],[0.9,0.1],[0.9,0.9]].", "error");
    return;
  }
  if (!Array.isArray(points) || points.length < 3) {
    notify("Zona incompleta", "Desenhe pelo menos 3 pontos na imagem.", "warning");
    return;
  }
  await createZone({
    camera_id: cameraId,
    name: form.elements.name.value.trim(),
    type: form.elements.type.value,
    enabled: form.elements.enabled.checked,
    points,
  });
  form.reset();
  zoneDrawingPoints = [];
  form.elements.points.value = "[]";
  form.elements.enabled.checked = true;
  await loadZonesPageData();
  notify("Zona salva", "A zona foi cadastrada para essa câmera.", "success");
}

async function handleZoneAction(event) {
  const row = event.currentTarget.closest("[data-zone-id]");
  const zoneId = row.dataset.zoneId;
  const action = event.currentTarget.dataset.zoneAction;
  if (action === "delete") {
    await deleteZone(zoneId);
  } else if (action === "toggle") {
    await updateZone(zoneId, { enabled: event.currentTarget.checked });
  }
  await loadZonesPageData();
}

async function renderMachinesPage() {
  appView.innerHTML = `
    <div class="ops-page">
      <section class="ops-toolbar">
        <label>Câmera<select id="machine-camera-select"></select></label>
        <button type="button" id="machine-refresh">Atualizar</button>
      </section>
      <section class="ops-grid">
        <div>
          <div class="section-heading">
            <h2>Máquinas mapeadas</h2>
            <div class="section-actions">
              <button type="button" id="add-machine"><i data-lucide="plus" aria-hidden="true"></i><span>Adicionar máquina</span></button>
              <span id="machine-count">0</span>
            </div>
          </div>
          <div class="zone-designer">
            <div class="zone-designer-header">
              <strong id="machine-designer-title">Desenho da câmera</strong>
              <span id="machine-draw-status">Clique na imagem para marcar a máquina</span>
            </div>
            <div id="machine-media-host" class="zone-media-host">
              <div class="media-placeholder">Selecione uma câmera</div>
            </div>
            <div class="zone-draw-tools">
              <button type="button" id="machine-detect-click" class="mode-toggle" data-active="false">Detectar pelo clique</button>
              <button type="button" id="machine-undo-point">Desfazer ponto</button>
              <button type="button" id="machine-clear-points">Limpar desenho</button>
              <button type="button" id="machine-use-full-frame">Quadro inteiro</button>
            </div>
          </div>
          <div id="machine-list" class="ops-list"></div>
        </div>
        <aside class="ops-detail">
          <h2>Mapeamento</h2>
          <div class="object-list">
            ${objectLine("Desenho ativo", "Clique no vídeo para marcar pontos", "Depois use Adicionar máquina para salvar como ativo operacional.")}
            ${objectLine("Máquina", "Ativo com identidade", "Diferente de zona: tem tipo, operador esperado e regra de proximidade.")}
          </div>
        </aside>
      </section>
      <section class="modal-layer" id="machine-modal" aria-labelledby="machine-modal-title" aria-modal="true" role="dialog" hidden>
        <div class="modal-backdrop" data-close-modal></div>
        <div class="modal-window">
          <header class="modal-header">
            <div>
              <p class="eyebrow">Ativo operacional</p>
              <h2 id="machine-modal-title">Adicionar máquina</h2>
            </div>
            <button type="button" class="icon-button" id="close-machine-modal" aria-label="Fechar janela">
              <i data-lucide="x" aria-hidden="true"></i>
            </button>
          </header>
          <form id="machine-form" class="modal-form">
            <label>Nome<input name="name" required placeholder="Prensa 01" /></label>
            <label>Tipo
              <select name="type">
                <option value="fixed">Fixa</option>
                <option value="mobile">Móvel</option>
                <option value="vehicle">Veículo</option>
                <option value="conveyor">Esteira</option>
                <option value="robot">Robô</option>
                <option value="other">Outra</option>
              </select>
            </label>
            <label>Pontos normalizados<textarea name="points" rows="6">[]</textarea></label>
            <label>Distância mínima de pessoa<input name="min_person_distance" type="number" min="0" max="1" step="0.01" value="0.08" /></label>
            <label class="inline-toggle"><input name="enabled" type="checkbox" checked /> Ativa</label>
            <label class="inline-toggle"><input name="requires_operator" type="checkbox" checked /> Requer operador</label>
            <label class="inline-toggle"><input name="allow_idle" type="checkbox" /> Pode ficar parada</label>
            <footer class="modal-actions">
              <button type="button" id="cancel-machine-modal">Cancelar</button>
              <button type="submit" class="primary-action">Salvar máquina</button>
            </footer>
          </form>
        </div>
      </section>
    </div>
  `;
  await populateCameraSelect("#machine-camera-select", false);
  document.querySelector("#add-machine").addEventListener("click", openMachineModal);
  document.querySelector("#close-machine-modal").addEventListener("click", closeMachineModal);
  document.querySelector("#cancel-machine-modal").addEventListener("click", closeMachineModal);
  document.querySelector("#machine-modal [data-close-modal]").addEventListener("click", closeMachineModal);
  document.querySelector("#machine-camera-select").addEventListener("change", loadMachinesPageData);
  document.querySelector("#machine-refresh").addEventListener("click", loadMachinesPageData);
  document.querySelector("#machine-form").addEventListener("submit", saveMachine);
  document.querySelector("#machine-form").elements.points.addEventListener("input", syncMachinePointsFromTextarea);
  document.querySelector("#machine-detect-click").addEventListener("click", toggleMachineClickDetect);
  document.querySelector("#machine-undo-point").addEventListener("click", undoMachinePoint);
  document.querySelector("#machine-clear-points").addEventListener("click", clearMachineDrawing);
  document.querySelector("#machine-use-full-frame").addEventListener("click", useFullFrameMachine);
  refreshIcons();
  await loadMachinesPageData();
}

function openMachineModal() {
  const modal = document.querySelector("#machine-modal");
  const form = document.querySelector("#machine-form");
  if (!modal || !form) return;
  form.reset();
  form.elements.points.value = JSON.stringify(machineDrawingPoints);
  form.elements.enabled.checked = true;
  form.elements.requires_operator.checked = true;
  form.elements.allow_idle.checked = false;
  form.elements.min_person_distance.value = 0.08;
  modal.hidden = false;
  document.body.dataset.modalOpen = "true";
  refreshIcons();
  requestAnimationFrame(() => form.elements.name.focus());
}

function closeMachineModal() {
  const modal = document.querySelector("#machine-modal");
  if (!modal) return;
  modal.hidden = true;
  delete document.body.dataset.modalOpen;
}

async function loadMachinesPageData() {
  const cameraId = document.querySelector("#machine-camera-select")?.value;
  const [cameras, machines] = await Promise.all([listCameras(), listMachines(cameraId)]);
  const selectedCamera = cameras.find((camera) => camera.id === cameraId);
  machinePreviewMachines = machines;
  renderMachineMedia(selectedCamera);
  document.querySelector("#machine-count").textContent = `${machines.length} máquinas`;
  document.querySelector("#machine-list").innerHTML = machines.length
    ? machines.map((machine) => machineRow(machine, selectedCamera)).join("")
    : emptyState("Nenhuma máquina mapeada", "Desenhe a área da máquina e salve como ativo operacional.");
  document.querySelectorAll("[data-machine-action]").forEach((button) => {
    button.addEventListener("click", handleMachineAction);
  });
  drawMachineCanvas();
}

function machineRow(machine, camera) {
  return `
    <article class="ops-row" data-machine-id="${machine.id}">
      <div>
        <strong>${machine.name}</strong>
        <span>${camera?.name || machine.camera_id} · ${machine.type} · ${machine.points.length} pontos</span>
        <small>${machine.requires_operator ? "requer operador" : "operador opcional"} · ${machine.allow_idle ? "parada permitida" : "parada monitorada"} · distância ${machine.min_person_distance}</small>
      </div>
      <span class="health-badge" data-status="${machine.enabled ? "ONLINE" : "OFFLINE"}">${machine.enabled ? "ATIVA" : "INATIVA"}</span>
      <label class="inline-toggle"><input type="checkbox" data-machine-action="toggle" ${machine.enabled ? "checked" : ""} /> Ativa</label>
      <div class="row-actions"><button type="button" data-machine-action="delete">Excluir</button></div>
    </article>
  `;
}

function renderMachineMedia(camera) {
  const host = document.querySelector("#machine-media-host");
  document.querySelector("#machine-designer-title").textContent = camera?.name || "Desenho da câmera";
  if (!camera?.id) {
    host.innerHTML = `<div class="media-placeholder">Cadastre ou selecione uma câmera</div>`;
    return;
  }
  const source = camera.source_type === "video_file" ? cameraVideoUrl(camera.id) : cameraStreamUrl(camera.id);
  const media = camera.source_type === "video_file"
    ? `<video id="machine-media" class="zone-media" src="${source}" autoplay muted playsinline loop controls></video>`
    : `<img id="machine-media" class="zone-media" src="${source}" alt="Preview da câmera" />`;
  host.innerHTML = `${media}<canvas id="machine-draw-canvas" class="zone-draw-canvas" aria-label="Desenho de máquina"></canvas>`;
  const canvas = document.querySelector("#machine-draw-canvas");
  canvas.addEventListener("pointerdown", addMachinePoint);
  window.addEventListener("resize", drawMachineCanvas, { once: true });
  setTimeout(drawMachineCanvas, 80);
}

function toggleMachineClickDetect() {
  machineClickDetectMode = !machineClickDetectMode;
  const button = document.querySelector("#machine-detect-click");
  if (button) {
    button.dataset.active = String(machineClickDetectMode);
    button.textContent = machineClickDetectMode ? "Clique para detectar" : "Detectar pelo clique";
  }
  updateMachineDrawStatus(
    machineClickDetectMode
      ? "Clique sobre a máquina para gerar o contorno automaticamente."
      : ""
  );
}

async function addMachinePoint(event) {
  const rect = event.currentTarget.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  if (machineClickDetectMode) {
    await detectMachineFromClick(event, rect);
    return;
  }
  machineDrawingPoints.push([
    roundPoint(clamp01((event.clientX - rect.left) / rect.width)),
    roundPoint(clamp01((event.clientY - rect.top) / rect.height)),
  ]);
  updateMachinePointsField();
  drawMachineCanvas();
}

async function detectMachineFromClick(event, rect) {
  const cameraId = document.querySelector("#machine-camera-select")?.value;
  if (!cameraId) return;
  const click = {
    x: clamp01((event.clientX - rect.left) / rect.width),
    y: clamp01((event.clientY - rect.top) / rect.height),
  };
  updateMachineDrawStatus("Consultando objetos detectados pela Vision...");
  try {
    const objects = await getVisionObjects(cameraId);
    const match = nearestMachineObject(objects, click, rect);
    if (!match) {
      updateMachineDrawStatus("Nenhuma máquina/objeto detectado perto do clique. Ligue Vision e tente novamente.");
      notify("Nenhum objeto encontrado", "A Vision precisa estar ativa e detectar algo próximo do clique.", "warning");
      return;
    }
    machineDrawingPoints = bboxToNormalizedPolygon(match.bounding_box, rect);
    updateMachinePointsField();
    drawMachineCanvas();
    machineClickDetectMode = false;
    const button = document.querySelector("#machine-detect-click");
    if (button) {
      button.dataset.active = "false";
      button.textContent = "Detectar pelo clique";
    }
    notify("Máquina contornada", `${match.class_name} #${match.track_id} foi usado como base.`, "success");
  } catch (error) {
    updateMachineDrawStatus("Falha ao consultar objetos da Vision.");
    notifyError("Falha ao detectar pelo clique", error);
  }
}

function nearestMachineObject(objects, click, rect) {
  const candidates = objects
    .filter((object) => Array.isArray(object.bounding_box) && object.bounding_box.length >= 4)
    .map((object) => {
      const [x1, y1, x2, y2] = object.bounding_box.map(Number);
      const normalized = Math.max(x1, y1, x2, y2) <= 1.5;
      const center = { x: (x1 + x2) / 2, y: (y1 + y2) / 2 };
      const normalizedCenter = {
        x: normalized ? center.x : center.x / rect.width,
        y: normalized ? center.y : center.y / rect.height,
      };
      const className = String(object.class_name || "").toLowerCase();
      const priority = machineClassPriority(className);
      return {
        ...object,
        _priority: priority,
        _distance: Math.hypot(normalizedCenter.x - click.x, normalizedCenter.y - click.y),
      };
    })
    .filter((object) => object._distance <= 0.35);
  candidates.sort((a, b) => b._priority - a._priority || a._distance - b._distance);
  return candidates[0] || null;
}

function machineClassPriority(className) {
  if (["forklift", "truck", "car", "bus", "motorcycle", "tractor", "excavator", "crane", "machine", "industrial machine"].includes(className)) {
    return 3;
  }
  if (className !== "person" && className !== "cell phone") {
    return 2;
  }
  return 1;
}

function bboxToNormalizedPolygon(boundingBox, rect) {
  const [rawX1, rawY1, rawX2, rawY2] = boundingBox.map(Number);
  const normalized = Math.max(rawX1, rawY1, rawX2, rawY2) <= 1.5;
  const x1 = normalized ? rawX1 : rawX1 / rect.width;
  const y1 = normalized ? rawY1 : rawY1 / rect.height;
  const x2 = normalized ? rawX2 : rawX2 / rect.width;
  const y2 = normalized ? rawY2 : rawY2 / rect.height;
  const padding = 0.015;
  return [
    [roundPoint(clamp01(Math.min(x1, x2) - padding)), roundPoint(clamp01(Math.min(y1, y2) - padding))],
    [roundPoint(clamp01(Math.max(x1, x2) + padding)), roundPoint(clamp01(Math.min(y1, y2) - padding))],
    [roundPoint(clamp01(Math.max(x1, x2) + padding)), roundPoint(clamp01(Math.max(y1, y2) + padding))],
    [roundPoint(clamp01(Math.min(x1, x2) - padding)), roundPoint(clamp01(Math.max(y1, y2) + padding))],
  ];
}

function syncMachinePointsFromTextarea() {
  const form = document.querySelector("#machine-form");
  try {
    const parsed = JSON.parse(form.elements.points.value);
    if (Array.isArray(parsed)) {
      machineDrawingPoints = parsed
        .filter((point) => Array.isArray(point) && point.length === 2)
        .map((point) => [clamp01(Number(point[0])), clamp01(Number(point[1]))]);
      drawMachineCanvas();
    }
  } catch {
    updateMachineDrawStatus("JSON inválido nos pontos");
  }
}

function updateMachinePointsField() {
  const form = document.querySelector("#machine-form");
  form.elements.points.value = JSON.stringify(machineDrawingPoints);
  updateMachineDrawStatus();
}

function undoMachinePoint() {
  machineDrawingPoints.pop();
  updateMachinePointsField();
  drawMachineCanvas();
}

function clearMachineDrawing() {
  machineDrawingPoints = [];
  updateMachinePointsField();
  drawMachineCanvas();
}

function useFullFrameMachine() {
  machineDrawingPoints = [[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]];
  updateMachinePointsField();
  drawMachineCanvas();
}

function updateMachineDrawStatus(message = "") {
  const status = document.querySelector("#machine-draw-status");
  if (!status) return;
  status.textContent = message || (
    machineDrawingPoints.length < 3
      ? `${machineDrawingPoints.length} ponto(s). Marque pelo menos 3.`
      : `${machineDrawingPoints.length} pontos prontos para salvar.`
  );
}

function drawMachineCanvas() {
  const canvas = document.querySelector("#machine-draw-canvas");
  const host = document.querySelector("#machine-media-host");
  if (!canvas || !host) return;
  const rect = host.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  canvas.style.width = `${rect.width}px`;
  canvas.style.height = `${rect.height}px`;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);
  machinePreviewMachines.forEach((machine) => drawMachinePolygon(ctx, machine.points, rect, false));
  drawMachinePolygon(ctx, machineDrawingPoints, rect, true);
  updateMachineDrawStatus();
}

function drawMachinePolygon(ctx, points, rect, editing) {
  if (!points?.length) return;
  const color = "#7db3ff";
  ctx.lineWidth = editing ? 3 : 2;
  ctx.strokeStyle = color;
  ctx.fillStyle = editing ? "rgba(125, 179, 255, 0.2)" : "rgba(125, 179, 255, 0.1)";
  ctx.beginPath();
  points.forEach(([x, y], index) => {
    const px = Number(x) * rect.width;
    const py = Number(y) * rect.height;
    if (index === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  });
  if (points.length >= 3) {
    ctx.closePath();
    ctx.fill();
  }
  ctx.stroke();
  points.forEach(([x, y], index) => {
    const px = Number(x) * rect.width;
    const py = Number(y) * rect.height;
    ctx.beginPath();
    ctx.arc(px, py, editing ? 5 : 3, 0, Math.PI * 2);
    ctx.fillStyle = editing ? "#ffffff" : color;
    ctx.fill();
    if (editing) {
      ctx.fillStyle = "#050505";
      ctx.font = "10px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(index + 1), px, py);
    }
  });
}

async function saveMachine(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const cameraId = document.querySelector("#machine-camera-select")?.value;
  if (!cameraId) return;
  let points;
  try {
    points = JSON.parse(form.elements.points.value);
  } catch {
    notify("Pontos inválidos", "Use JSON como [[0.1,0.1],[0.9,0.1],[0.9,0.9]].", "error");
    return;
  }
  if (!Array.isArray(points) || points.length < 3) {
    notify("Máquina incompleta", "Desenhe pelo menos 3 pontos na imagem.", "warning");
    return;
  }
  await createMachine({
    camera_id: cameraId,
    name: form.elements.name.value.trim(),
    type: form.elements.type.value,
    points,
    enabled: form.elements.enabled.checked,
    requires_operator: form.elements.requires_operator.checked,
    allow_idle: form.elements.allow_idle.checked,
    min_person_distance: Number(form.elements.min_person_distance.value || 0.08),
  });
  form.reset();
  machineDrawingPoints = [];
  form.elements.points.value = "[]";
  form.elements.enabled.checked = true;
  form.elements.requires_operator.checked = true;
  form.elements.min_person_distance.value = 0.08;
  closeMachineModal();
  await loadMachinesPageData();
  notify("Máquina salva", "O ativo operacional foi mapeado para essa câmera.", "success");
}

async function handleMachineAction(event) {
  const row = event.currentTarget.closest("[data-machine-id]");
  const machineId = row.dataset.machineId;
  const action = event.currentTarget.dataset.machineAction;
  if (action === "delete") {
    await deleteMachine(machineId);
  } else if (action === "toggle") {
    await updateMachine(machineId, { enabled: event.currentTarget.checked });
  }
  await loadMachinesPageData();
}

async function renderEvidencePage() {
  appView.innerHTML = `
    <div class="ops-page">
      <section class="ops-toolbar">
        <button type="button" id="evidence-refresh">Atualizar</button>
      </section>
      <section class="ops-grid">
        <div>
          <div class="section-heading"><h2>Evidências</h2><span id="evidence-count">0</span></div>
          <div id="evidence-list" class="ops-list"></div>
        </div>
        <aside class="ops-detail">
          <h2>Retenção</h2>
          <div class="object-list">
            ${objectLine("Origem", "Eventos com mídia", "Lê media_path, snapshot_path ou evidence_path no metadata do evento")}
            ${objectLine("Status", "Somente leitura", "A criação acontece quando o motor salva evidência no evento")}
          </div>
        </aside>
      </section>
    </div>
  `;
  document.querySelector("#evidence-refresh").addEventListener("click", loadEvidence);
  await loadEvidence();
}

async function loadEvidence() {
  const list = document.querySelector("#evidence-list");
  try {
    const [items, cameras, zones] = await Promise.all([listEvidence(), listCameras(), listZones()]);
    const cameraById = Object.fromEntries(cameras.map((camera) => [camera.id, camera]));
    const zoneById = Object.fromEntries(zones.map((zone) => [zone.id, zone]));
    document.querySelector("#evidence-count").textContent = `${items.length} arquivos`;
    list.innerHTML = items.length
      ? items.map((item) => evidenceRow(item, cameraById, zoneById)).join("")
      : emptyState("Nenhuma evidência salva", "Eventos sem snapshot continuam aparecendo na fila de Eventos.");
  } catch (error) {
    list.innerHTML = emptyState("Falha ao carregar evidências", error.message);
  }
}

function evidenceRow(item, cameraById, zoneById) {
  const camera = cameraById[item.camera_id];
  const zone = item.zone_id ? zoneById[item.zone_id] : null;
  return `
    <article class="ops-row">
      <div>
        <strong>${item.type}</strong>
        <span>${camera?.name || item.camera_id} · ${zone?.name || item.zone_id || "sem zona"}</span>
        <small>${formatDate(item.started_at)} · ${item.media_path}</small>
      </div>
      <span class="severity-badge" data-severity="${item.severity}">${item.severity}</span>
      <span class="health-badge" data-status="${item.status}">${item.status}</span>
    </article>
  `;
}

async function renderDiagnosticsPage() {
  appView.innerHTML = `
    <div class="ops-page">
      <section class="ops-toolbar">
        <button type="button" id="diagnostics-refresh">Atualizar</button>
        <button type="button" id="demo-setup">Preparar demo</button>
        <label>Retenção evidências (dias)<input id="retention-days" type="number" min="0" max="365" value="7" /></label>
        <button type="button" id="cleanup-evidence">Limpar evidências</button>
      </section>
      <section id="diagnostics-grid" class="settings-grid"></section>
      <section class="ops-detail">
        <h2>Último evento</h2>
        <pre id="diagnostics-last" class="code-block">Carregando...</pre>
      </section>
    </div>
  `;
  document.querySelector("#diagnostics-refresh").addEventListener("click", loadDiagnostics);
  document.querySelector("#demo-setup").addEventListener("click", prepareDemo);
  document.querySelector("#cleanup-evidence").addEventListener("click", handleCleanupEvidence);
  await loadDiagnostics();
}

async function prepareDemo() {
  const button = document.querySelector("#demo-setup");
  button.textContent = "Preparando...";
  try {
    await setupDemo();
    button.textContent = "Demo pronta";
    notify("Demo pronta", "Vídeo, zona e Vision foram preparados.", "success");
    await loadDiagnostics();
  } catch (error) {
    button.textContent = "Erro na demo";
    notifyError("Falha ao preparar demo", error);
  } finally {
    setTimeout(() => (button.textContent = "Preparar demo"), 1800);
  }
}

async function handleCleanupEvidence() {
  const days = Number(document.querySelector("#retention-days").value || 7);
  const result = await cleanupEvidence(days);
  notify("Limpeza concluída", `${result.deleted_files} arquivos removidos.`, "success");
  await loadDiagnostics();
}

async function loadDiagnostics() {
  const grid = document.querySelector("#diagnostics-grid");
  try {
    const payload = await getOperationsDiagnostics();
    grid.innerHTML = [
      metricItem("Banco local", payload.sqlite ? "OK" : "Não encontrado", payload.database_path),
      metricItem("Câmeras online", payload.cameras_online, "Streams ativos agora"),
      metricItem("IA ativa", payload.ia_ativa, "Câmeras com vision ligada"),
      metricItem("Zonas ativas", payload.zonas_ativas, "Polígonos monitorados"),
      metricItem("Regras", payload.regras_ativas, "Regras operacionais carregadas"),
      metricItem("Eventos abertos", payload.eventos_abertos, "Ocorrências em andamento"),
      metricItem("Investigações", payload.investigacoes_abertas, "Casos não fechados"),
      metricItem("Disco livre", `${payload.disco_livre_percentual}%`, "Partição do banco local"),
      metricItem("Evidências", `${payload.evidence_mb} MB`, "Armazenamento local"),
    ].join("");
    document.querySelector("#diagnostics-last").textContent = payload.ultima_entrega
      ? JSON.stringify(payload.ultima_entrega, null, 2)
      : "Nenhum evento registrado.";
  } catch (error) {
    grid.innerHTML = emptyState("Falha no diagnóstico", error.message);
  }
}

async function renderRulesPage() {
  appView.innerHTML = `
    <div class="ops-page">
      <section class="ops-grid">
        <div>
          <div class="section-heading">
            <h2>Regras ativas</h2>
            <div class="section-actions">
              <button type="button" id="add-rule"><i data-lucide="plus" aria-hidden="true"></i><span>Adicionar regra</span></button>
              <span>Motor determinístico</span>
            </div>
          </div>
          <div id="rules-list" class="ops-list"></div>
        </div>
        <aside class="ops-detail">
          <h2>Simulador</h2>
          <form id="rule-sim-form" class="stack-form">
            <label>Observação
              <select name="observation">
                <option value="person_entered_zone">Pessoa entrou na zona</option>
                <option value="person_exited_zone">Pessoa saiu da zona</option>
                <option value="person_presence">Pessoa permaneceu na zona</option>
              </select>
            </label>
            <label>Tipo de zona
              <select name="zone_type">
                <option value="restricted">Restrita</option>
                <option value="monitored">Monitorada</option>
              </select>
            </label>
            <button type="submit">Simular</button>
          </form>
          <div id="rule-sim-result" class="object-list"></div>
        </aside>
      </section>
      <section class="modal-layer" id="rule-modal" aria-labelledby="rule-modal-title" aria-modal="true" role="dialog" hidden>
        <div class="modal-backdrop" data-close-modal></div>
        <div class="modal-window">
          <header class="modal-header">
            <div>
              <p class="eyebrow">Motor de eventos</p>
              <h2 id="rule-modal-title">Adicionar regra</h2>
            </div>
            <button type="button" class="icon-button" id="close-rule-modal" aria-label="Fechar janela">
              <i data-lucide="x" aria-hidden="true"></i>
            </button>
          </header>
          <form id="rule-form" class="modal-form">
            <input type="hidden" name="rule_id" />
            <label>Nome<input name="name" required placeholder="Permanência em zona restrita" /></label>
            <label>Observação
              <select name="observation_type">
                <option value="person_entered_zone">Pessoa entrou</option>
                <option value="person_exited_zone">Pessoa saiu</option>
                <option value="person_presence">Pessoa permaneceu</option>
              </select>
            </label>
            <label>Tipo de zona
              <select name="zone_type">
                <option value="restricted">Restrita</option>
                <option value="monitored">Monitorada</option>
              </select>
            </label>
            <label>Tipo de evento<input name="event_type" value="PERSON_RESTRICTED_ZONE" required /></label>
            <label>Severidade
              <select name="severity">
                <option value="critical">Crítica</option>
                <option value="attention">Atenção</option>
                <option value="info">Info</option>
              </select>
            </label>
            <label>Tempo mínimo (s)<input name="duration_threshold_seconds" type="number" min="0" step="1" value="0" /></label>
            <label>Cooldown (s)<input name="cooldown_seconds" type="number" min="0" step="1" value="10" /></label>
            <label class="inline-toggle"><input name="enabled" type="checkbox" checked /> Ativa</label>
            <footer class="modal-actions">
              <button type="button" id="cancel-rule-modal">Cancelar</button>
              <button type="submit" class="primary-action">Salvar regra</button>
            </footer>
          </form>
        </div>
      </section>
    </div>
  `;
  document.querySelector("#add-rule").addEventListener("click", openRuleModal);
  document.querySelector("#close-rule-modal").addEventListener("click", closeRuleModal);
  document.querySelector("#cancel-rule-modal").addEventListener("click", closeRuleModal);
  document.querySelector("#rule-modal [data-close-modal]").addEventListener("click", closeRuleModal);
  document.querySelector("#rule-form").addEventListener("submit", saveRule);
  document.querySelector("#rule-sim-form").addEventListener("submit", simulateRule);
  refreshIcons();
  await loadRules();
}

function resetRuleForm(form) {
  form.reset();
  form.elements.rule_id.value = "";
  form.elements.event_type.value = "PERSON_RESTRICTED_ZONE";
  form.elements.duration_threshold_seconds.value = 0;
  form.elements.cooldown_seconds.value = 10;
  form.elements.enabled.checked = true;
}

function openRuleModal() {
  const modal = document.querySelector("#rule-modal");
  const form = document.querySelector("#rule-form");
  if (!modal || !form) return;
  editingRuleId = null;
  resetRuleForm(form);
  document.querySelector("#rule-modal-title").textContent = "Adicionar regra";
  modal.hidden = false;
  document.body.dataset.modalOpen = "true";
  refreshIcons();
  requestAnimationFrame(() => form.elements.name.focus());
}

function openRuleEditModal(ruleId) {
  const rule = rulesCache.find((item) => item.id === ruleId);
  const modal = document.querySelector("#rule-modal");
  const form = document.querySelector("#rule-form");
  if (!rule || !modal || !form) return;
  editingRuleId = rule.id;
  form.elements.rule_id.value = rule.id;
  form.elements.name.value = rule.name || "";
  form.elements.observation_type.value = rule.observation_type || rule.observation;
  form.elements.zone_type.value = rule.zone_type || rule.zone || "restricted";
  form.elements.event_type.value = rule.event_type || rule.event || "PERSON_RESTRICTED_ZONE";
  form.elements.severity.value = rule.severity || "attention";
  form.elements.duration_threshold_seconds.value = rule.duration_threshold_seconds ?? 0;
  form.elements.cooldown_seconds.value = rule.cooldown_seconds ?? 10;
  form.elements.enabled.checked = Boolean(rule.enabled);
  document.querySelector("#rule-modal-title").textContent = "Editar regra";
  modal.hidden = false;
  document.body.dataset.modalOpen = "true";
  refreshIcons();
  requestAnimationFrame(() => form.elements.name.focus());
}

function closeRuleModal() {
  const modal = document.querySelector("#rule-modal");
  if (!modal) return;
  modal.hidden = true;
  editingRuleId = null;
  delete document.body.dataset.modalOpen;
}

async function saveRule(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    name: form.elements.name.value.trim(),
    observation_type: form.elements.observation_type.value,
    zone_type: form.elements.zone_type.value,
    event_type: form.elements.event_type.value.trim(),
    severity: form.elements.severity.value,
    duration_threshold_seconds: Number(form.elements.duration_threshold_seconds.value || 0),
    cooldown_seconds: Number(form.elements.cooldown_seconds.value || 10),
    enabled: form.elements.enabled.checked,
  };
  const wasEditing = Boolean(editingRuleId);
  await (editingRuleId ? updateRule(editingRuleId, payload) : createRule(payload));
  resetRuleForm(form);
  closeRuleModal();
  notify(wasEditing ? "Regra atualizada" : "Regra cadastrada", "O motor de eventos foi atualizado.", "success");
  await loadRules();
}

async function loadRules() {
  const host = document.querySelector("#rules-list");
  try {
    const rules = await listRules();
    rulesCache = rules;
    host.innerHTML = rules.length
      ? rules.map(ruleCard).join("")
      : `<div class="empty-state"><strong>Nenhuma regra ativa</strong><span>Crie uma regra para transformar observações em eventos.</span><button type="button" data-empty-action="add-rule">Adicionar regra</button></div>`;
    host.querySelector("[data-empty-action='add-rule']")?.addEventListener("click", openRuleModal);
    host.querySelectorAll("[data-rule-action]").forEach((button) => {
      button.addEventListener("click", handleRuleAction);
    });
  } catch (error) {
    host.innerHTML = emptyState("Falha ao carregar regras", error.message);
  }
}

function ruleCard(rule) {
  const observation = rule.observation || rule.observation_type;
  const zone = rule.zone || rule.zone_type;
  const event = rule.event || rule.event_type;
  const delay = rule.delay || (rule.duration_threshold_seconds ? `${rule.duration_threshold_seconds}s` : "imediata");
  return `
    <article class="ops-row" data-rule-id="${rule.id || ""}">
      <div>
        <strong>${rule.name}</strong>
        <span>${observation} · zone=${zone || "qualquer"} · cooldown ${rule.cooldown_seconds ?? 10}s</span>
        <small>${event} · ${delay}</small>
      </div>
      <span class="severity-badge" data-severity="${rule.severity}">${rule.severity}</span>
      <span class="health-badge" data-status="${rule.enabled ? "ONLINE" : "OFFLINE"}">${rule.enabled ? "ATIVA" : "INATIVA"}</span>
      <div class="row-actions">
        <button type="button" data-rule-action="edit">Editar</button>
        <button type="button" data-rule-action="toggle">${rule.enabled ? "Desativar" : "Ativar"}</button>
        <button type="button" data-rule-action="delete">Excluir</button>
      </div>
    </article>
  `;
}

async function handleRuleAction(event) {
  const row = event.currentTarget.closest("[data-rule-id]");
  const ruleId = row.dataset.ruleId;
  if (!ruleId) return;
  const action = event.currentTarget.dataset.ruleAction;
  if (action === "edit") {
    openRuleEditModal(ruleId);
    return;
  }
  if (action === "delete") {
    await deleteRule(ruleId);
  } else if (action === "toggle") {
    const active = row.querySelector(".health-badge")?.textContent === "ATIVA";
    await updateRule(ruleId, { enabled: !active });
  }
  await loadRules();
}

async function simulateRule(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const observation = form.elements.observation.value;
  const zone = form.elements.zone_type.value;
  const result = await simulateRuleRequest({ observation_type: observation, zone_type: zone });
  document.querySelector("#rule-sim-result").innerHTML = result.rules.length
    ? result.rules.map(ruleCard).join("")
    : emptyState("Nenhuma regra disparada", "Essa combinação não gera evento no motor atual.");
}

async function renderSettingsPage() {
  appView.innerHTML = `
    <div class="ops-page settings-page settings-reference">
      <section class="settings-reference-head">
        <div>
          <h2>Configurações</h2>
          <p>Gerencie as configurações do sistema, integrações e preferências da sua empresa.</p>
        </div>
        <label class="settings-search">
          <i data-lucide="search"></i>
          <input type="search" placeholder="Buscar configurações..." />
          <kbd>Ctrl K</kbd>
        </label>
      </section>

      <nav class="settings-tabs" aria-label="Categorias de configurações">
        ${settingsTabs().map((tab) => `
          <button type="button" data-settings-tab="${tab.id}">
            <i data-lucide="${tab.icon}"></i>${tab.label}
          </button>
        `).join("")}
      </nav>

      <section class="settings-reference-grid" id="settings-tab-panel"></section>
    </div>
  `;
  setupSettingsInteractions();
  await renderSettingsTab("general");
}

function liveDetectionsVisible() {
  return localStorage.getItem("campex.live_detections_visible") === "true";
}

function updateDetectionToggle() {
  const button = document.querySelector("#live-toggle-detections");
  if (!button) {
    return;
  }
  const visible = liveDetectionsVisible();
  button.dataset.active = String(visible);
  button.textContent = `Detecção: ${visible ? "ON" : "OFF"}`;
}

function settingsTabs() {
  return [
    { id: "general", label: "Geral", icon: "settings" },
    { id: "cameras", label: "Câmeras", icon: "camera" },
    { id: "notifications", label: "Notificações", icon: "bell" },
    { id: "integrations", label: "Integrações", icon: "wrench" },
    { id: "security", label: "Segurança", icon: "shield" },
    { id: "users", label: "Usuários", icon: "users" },
    { id: "appearance", label: "Aparência", icon: "palette" },
  ];
}

function readStoredSettings(key, fallback) {
  try {
    return { ...fallback, ...JSON.parse(localStorage.getItem(key) || "{}") };
  } catch {
    return { ...fallback };
  }
}

function writeStoredSettings(key, value) {
  localStorage.setItem(key, JSON.stringify(value));
}

function companySettings() {
  return readStoredSettings("campex.company", {
    name: "Facchini S.A.",
    document: "03.509.978/0001-00",
    address: "Av. Presidente Juscelino Kubitschek, 1900",
    city: "São José do Rio Preto",
    state: "SP",
    timezone: "America/Sao_Paulo",
  });
}

function securitySettings() {
  return readStoredSettings("campex.security", {
    session_timeout: "8",
    audit_log: true,
    strict_token: Boolean(localStorage.getItem("campex.api_token")),
    evidence_retention_days: "7",
  });
}

function appearanceSettings() {
  return readStoredSettings("campex.appearance", {
    density: document.body.dataset.density || "comfortable",
    accent: "blue",
    motion: "full",
    sidebar: appShell.dataset.sidebar || "expanded",
  });
}

function settingsCard(title, detail, icon, body, actions = "") {
  return `
    <article class="settings-card" data-settings-card>
      <div class="settings-card-header">
        <div>
          <h3>${title}</h3>
          <p>${detail}</p>
        </div>
        ${icon ? `<i data-lucide="${icon}"></i>` : ""}
      </div>
      ${body}
      ${actions ? `<div class="settings-card-actions">${actions}</div>` : ""}
    </article>
  `;
}

async function renderSettingsTab(tabId) {
  const panel = document.querySelector("#settings-tab-panel");
  if (!panel) return;
  document.querySelectorAll("[data-settings-tab]").forEach((button) => {
    const active = button.dataset.settingsTab === tabId;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
  });
  panel.dataset.settingsTab = tabId;
  panel.innerHTML = settingsTabMarkup(tabId);
  wireSettingsTab(tabId);
  refreshIcons();
  await hydrateSettingsTab(tabId);
  filterSettingsCards(document.querySelector(".settings-search input")?.value || "");
}

function settingsTabMarkup(tabId) {
  if (tabId === "cameras") return camerasSettingsMarkup();
  if (tabId === "notifications") return notificationsSettingsMarkup();
  if (tabId === "integrations") return integrationsSettingsMarkup();
  if (tabId === "security") return securitySettingsMarkup();
  if (tabId === "users") return usersSettingsMarkup();
  if (tabId === "appearance") return appearanceSettingsMarkup();
  return generalSettingsMarkup();
}

function generalSettingsMarkup() {
  const company = companySettings();
  return `
    ${settingsCard("Informações da Empresa", "Dados básicos da sua empresa no sistema.", "building-2", `
      <form id="company-settings-form" class="settings-form">
        <div class="settings-form-grid">
          <label>Nome da empresa<input name="name" value="${escapeHtml(company.name)}" /></label>
          <label>CNPJ<input name="document" value="${escapeHtml(company.document)}" /></label>
        </div>
        <label>Endereço<input name="address" value="${escapeHtml(company.address)}" /></label>
        <div class="settings-form-grid settings-form-grid-compact">
          <label>Cidade<input name="city" value="${escapeHtml(company.city)}" /></label>
          <label>Estado
            <select name="state">${["SP", "RJ", "MG", "PR", "SC", "RS"].map((state) => `<option ${state === company.state ? "selected" : ""}>${state}</option>`).join("")}</select>
          </label>
          <label>Fuso horário<input name="timezone" value="${escapeHtml(company.timezone)}" /></label>
        </div>
        <div class="settings-action-bar"><button type="submit" class="primary-action">Salvar alterações</button></div>
      </form>
    `)}
    ${settingsCard("Sistema", "Configurações gerais de funcionamento.", "settings", `
      <form id="local-settings-form" class="settings-form settings-system-form">
        <input name="api_token" type="hidden" />
        <label class="settings-switch-row">
          <span><strong>Modo de operação</strong><small>Ativar ou desativar o modo compacto de operação.</small></span>
          <span class="settings-switch-control"><input name="dense_lists" type="checkbox" /><span>Ativo</span></span>
        </label>
        <label class="settings-split-row">
          <span><strong>Intervalo de análise</strong><small>Frequência de atualização da interface.</small></span>
          <select name="refresh_ms">
            <option value="60000">1 minuto</option>
            <option value="30000">30 segundos</option>
            <option value="2000">2 segundos</option>
          </select>
        </label>
        <label class="settings-split-row">
          <span><strong>Modo do operador</strong><small>Define a experiência operacional local.</small></span>
          <select name="operator_mode">
            <option value="standard">Padrão</option>
            <option value="review">Revisão</option>
            <option value="debug">Debug</option>
          </select>
        </label>
        <label class="settings-split-row">
          <span><strong>Token API local</strong><small>Usado em chamadas protegidas por X-CAMPEX-Token.</small></span>
          <input name="api_token_visible" type="password" autocomplete="off" placeholder="Opcional" />
        </label>
        <div class="settings-action-bar"><button type="submit" class="primary-action">Salvar alterações</button></div>
      </form>
    `)}
  `;
}

function camerasSettingsMarkup() {
  return `
    ${settingsCard("Câmeras", "Gerencie disponibilidade, visão computacional e testes de conexão.", "camera", `
      <div id="settings-camera-list" class="settings-integration-list">${emptyState("Carregando câmeras", "Buscando câmeras cadastradas.")}</div>
    `, `<button type="button" class="secondary-action" data-settings-action="refresh-cameras"><i data-lucide="refresh-cw"></i>Atualizar</button>`)}
    ${settingsCard("Operação visual", "Resumo operacional calculado a partir do backend.", "activity", `
      <div id="settings-camera-summary" class="settings-runtime-grid"></div>
    `)}
  `;
}

function notificationsSettingsMarkup() {
  return `
    <form id="notification-settings-form" class="settings-card settings-form" data-settings-card>
      <div class="settings-card-header">
        <div>
          <h3>Notificações</h3>
          <p>Configure como e quando receber os relatórios.</p>
        </div>
        <i data-lucide="send"></i>
      </div>
      <label class="settings-switch-row">
        <span><strong>Notificações ativas</strong><small>Controla todos os envios automáticos e manuais do CAMPEX.</small></span>
        <span class="settings-switch-control"><input name="enabled" type="checkbox" /><span>Ativo</span></span>
      </label>
      <label class="settings-switch-row">
        <span><strong>Enviar por e-mail</strong><small>Receber relatórios e alertas por e-mail.</small></span>
        <span class="settings-switch-control"><input name="email_enabled" type="checkbox" /><span>Ativo</span></span>
      </label>
      <label class="settings-split-row"><span><strong>E-mail de destino</strong></span><input name="email_recipients" value="operacoes@facchini.com.br" /></label>
      <label class="settings-split-row">
        <span><strong>Frequência de relatórios</strong></span>
        <select name="report_frequency">
          <option value="DAILY">Diário</option>
          <option value="WEEKLY">Semanal</option>
          <option value="MONTHLY">Mensal</option>
        </select>
      </label>
      <div class="settings-form-grid">
        <label>Horário<input name="report_time" type="time" value="18:00" /></label>
        <label>Timezone<input name="timezone" value="America/Sao_Paulo" /></label>
      </div>
      <label class="settings-switch-row">
        <span><strong>Enviar por Telegram</strong><small>Receber alertas em tempo real pelo Telegram.</small></span>
        <span class="settings-switch-control"><input name="telegram_enabled" type="checkbox" /><span>Ativo</span></span>
      </label>
      <label class="settings-split-row"><span><strong>Chat ID do Telegram</strong></span><input name="telegram_chat_id" value="123456789" /></label>
      <label class="settings-switch-row">
        <span><strong>Alertas imediatos</strong><small>Dispara alertas assim que eventos críticos forem registrados.</small></span>
        <span class="settings-switch-control"><input name="immediate_alerts_enabled" type="checkbox" /><span>Ativo</span></span>
      </label>
      <div class="settings-toggle-grid">
        <label class="toggle-row"><input name="camera_offline" type="checkbox" /><span>Câmera offline</span></label>
        <label class="toggle-row"><input name="camera_online" type="checkbox" /><span>Câmera online</span></label>
        <label class="toggle-row"><input name="zone_idle" type="checkbox" /><span>Área sem atividade</span></label>
        <label class="toggle-row"><input name="zone_activity_resumed" type="checkbox" /><span>Atividade retomada</span></label>
        <label class="toggle-row"><input name="crowding_started" type="checkbox" /><span>Aglomeração</span></label>
        <label class="toggle-row"><input name="crowding_ended" type="checkbox" /><span>Aglomeração encerrada</span></label>
        <label class="toggle-row"><input name="long_presence" type="checkbox" /><span>Permanência prolongada</span></label>
      </div>
      <label class="settings-switch-row">
        <span><strong>Relatórios automáticos</strong><small>Agenda relatórios recorrentes conforme frequência e horário.</small></span>
        <span class="settings-switch-control"><input name="reports_enabled" type="checkbox" /><span>Ativo</span></span>
      </label>
      <div class="settings-card-actions">
        <button type="button" id="notification-test-email" class="secondary-action"><i data-lucide="mail-check"></i>Testar e-mail</button>
        <button type="button" id="notification-test-telegram" class="secondary-action"><i data-lucide="message-circle"></i>Testar Telegram</button>
        <button type="button" id="notification-report-now" class="secondary-action"><i data-lucide="send"></i>Enviar relatório agora</button>
        <button type="submit" class="primary-action">Salvar alterações</button>
      </div>
    </form>
    ${settingsCard("Status de entrega", "Última leitura das integrações de notificação.", "radio", `<div id="notification-settings-status" class="settings-integration-list"></div>`)}
    ${settingsCard("Histórico de envios", "Entregas reais registradas pelo backend.", "history", `<div id="notification-delivery-history" class="settings-integration-list"></div>`)}
  `;
}

function integrationsSettingsMarkup() {
  return `
    ${settingsCard("Integrações", "Gerencie as integrações com serviços externos.", "share-2", `
      <div id="notification-settings-status" class="settings-integration-list">
        ${integrationLine("NVIDIA Nemotron", "Análise de vídeo com IA.", "Conectado", "runtime")}
        ${integrationLine("E-mail transacional (Resend)", "Envio de relatórios e alertas.", "Conectado", "email")}
        ${integrationLine("Bot do Telegram", "Notificações em tempo real.", "Conectado", "telegram")}
      </div>
      <div class="settings-info-callout">
        <i data-lucide="info"></i>
        <div>
          <strong>Monitoramento de integrações ativo.</strong>
          <span id="settings-integration-check">Aguardando verificação do backend.</span>
        </div>
      </div>
    `, `
      <button type="button" id="settings-refresh" class="secondary-action"><i data-lucide="refresh-cw"></i>Verificar</button>
      <button type="button" id="notification-report-now" class="secondary-action"><i data-lucide="send"></i>Enviar relatório agora</button>
    `)}
    ${settingsCard("Runtime", "Informações técnicas retornadas pelo backend.", "server", `<div id="settings-runtime" class="settings-runtime-grid"></div>`)}
  `;
}

function securitySettingsMarkup() {
  const settings = securitySettings();
  return `
    ${settingsCard("Segurança", "Controle de sessão, token e retenção de evidências.", "shield", `
      <form id="security-settings-form" class="settings-form">
        <label class="settings-switch-row">
          <span><strong>Exigir token local</strong><small>Salva o token usado nas chamadas protegidas do backend.</small></span>
          <span class="settings-switch-control"><input name="strict_token" type="checkbox" ${settings.strict_token ? "checked" : ""} /><span>Ativo</span></span>
        </label>
        <label class="settings-split-row"><span><strong>Token API</strong><small>X-CAMPEX-Token do ambiente.</small></span><input name="api_token" type="password" value="${escapeHtml(localStorage.getItem("campex.api_token") || "")}" /></label>
        <label class="settings-split-row"><span><strong>Tempo de sessão</strong><small>Preferência local para auditoria operacional.</small></span><select name="session_timeout"><option value="4">4 horas</option><option value="8">8 horas</option><option value="12">12 horas</option></select></label>
        <label class="settings-split-row"><span><strong>Retenção de evidências</strong><small>Executa limpeza pelo backend quando solicitado.</small></span><select name="evidence_retention_days"><option value="7">7 dias</option><option value="15">15 dias</option><option value="30">30 dias</option></select></label>
        <label class="settings-switch-row">
          <span><strong>Log de auditoria</strong><small>Mantém trilha local de alterações nas configurações.</small></span>
          <span class="settings-switch-control"><input name="audit_log" type="checkbox" ${settings.audit_log ? "checked" : ""} /><span>Ativo</span></span>
        </label>
        <div class="settings-card-actions">
          <button type="button" class="secondary-action" data-settings-action="cleanup-evidence"><i data-lucide="trash-2"></i>Limpar evidências antigas</button>
          <button type="submit" class="primary-action">Salvar alterações</button>
        </div>
      </form>
    `)}
    ${settingsCard("Auditoria", "Histórico local de alterações feitas nesta tela.", "list-checks", `<div id="settings-audit-log" class="settings-integration-list">${settingsAuditMarkup()}</div>`)}
  `;
}

function usersSettingsMarkup() {
  const users = listLocalUsers();
  const invites = readStoredSettings("campex.user_invites", { items: [] }).items || [];
  return `
    ${settingsCard("Usuários", "Gerencie usuários locais e convites operacionais.", "users", `
      <div class="settings-integration-list">
        ${users.map((user) => settingsUserLine(user.name, user.email, user.role || "operator", "Ativo")).join("") || emptyState("Nenhum usuário local", "Crie uma conta na tela de acesso.")}
      </div>
    `)}
    ${settingsCard("Convidar usuário", "Registre convites para liberação operacional.", "user-plus", `
      <form id="user-invite-form" class="settings-form">
        <div class="settings-form-grid">
          <label>Nome<input name="name" required placeholder="Nome do operador" /></label>
          <label>E-mail<input name="email" type="email" required placeholder="operador@empresa.com" /></label>
        </div>
        <label>Perfil<select name="role"><option value="operator">Operador</option><option value="admin">Administrador</option><option value="viewer">Visualizador</option></select></label>
        <div class="settings-action-bar"><button type="submit" class="primary-action">Registrar convite</button></div>
      </form>
      <div class="settings-integration-list">${invites.map((invite) => settingsUserLine(invite.name, invite.email, invite.role, "Convite pendente")).join("")}</div>
    `)}
  `;
}

function appearanceSettingsMarkup() {
  const settings = appearanceSettings();
  return `
    ${settingsCard("Aparência", "Personalize densidade, navegação e feedback visual.", "palette", `
      <form id="appearance-settings-form" class="settings-form">
        <label class="settings-split-row"><span><strong>Densidade</strong><small>Controla espaçamento das listas e cards.</small></span><select name="density"><option value="comfortable">Confortável</option><option value="compact">Compacta</option></select></label>
        <label class="settings-split-row"><span><strong>Menu lateral</strong><small>Define estado inicial do menu.</small></span><select name="sidebar"><option value="expanded">Expandido</option><option value="collapsed">Recolhido</option></select></label>
        <label class="settings-split-row"><span><strong>Cor de destaque</strong><small>Aplica destaque visual nos controles.</small></span><select name="accent"><option value="blue">Azul CAMPEX</option><option value="green">Verde operação</option><option value="white">Neutro claro</option></select></label>
        <label class="settings-split-row"><span><strong>Movimento</strong><small>Reduz animações para operações longas.</small></span><select name="motion"><option value="full">Completo</option><option value="reduced">Reduzido</option></select></label>
        <div class="settings-action-bar"><button type="submit" class="primary-action">Salvar aparência</button></div>
      </form>
    `)}
    ${settingsCard("Pré-visualização", "Amostra dos componentes principais.", "sparkles", `
      <div class="settings-preview">
        <button type="button" class="primary-action">Ação primária</button>
        <button type="button" class="secondary-action">Ação secundária</button>
        <span class="health-badge" data-status="ONLINE">ONLINE</span>
      </div>
    `)}
  `;
}

function setupSettingsInteractions() {
  document.querySelectorAll("[data-settings-tab]").forEach((button) => {
    button.addEventListener("click", () => renderSettingsTab(button.dataset.settingsTab));
  });
  const searchInput = document.querySelector(".settings-search input");
  searchInput?.addEventListener("input", (event) => filterSettingsCards(event.currentTarget.value));
  window.addEventListener("keydown", focusSettingsSearch);
}

function focusSettingsSearch(event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && currentRoute() === "settings") {
    event.preventDefault();
    document.querySelector(".settings-search input")?.focus();
  }
}

function wireSettingsTab(tabId) {
  document.querySelector("#company-settings-form")?.addEventListener("submit", saveCompanySettings);
  document.querySelector("#local-settings-form")?.addEventListener("submit", saveLocalSettings);
  document.querySelector("#notification-settings-form")?.addEventListener("submit", saveNotificationSettings);
  document.querySelector("#notification-test-telegram")?.addEventListener("click", testTelegramSettings);
  document.querySelector("#notification-test-email")?.addEventListener("click", testEmailSettings);
  document.querySelector("#notification-report-now")?.addEventListener("click", sendReportNow);
  document.querySelector("#settings-refresh")?.addEventListener("click", () => hydrateSettingsTab(tabId));
  document.querySelector("#security-settings-form")?.addEventListener("submit", saveSecuritySettings);
  document.querySelector("#user-invite-form")?.addEventListener("submit", saveUserInvite);
  document.querySelector("#appearance-settings-form")?.addEventListener("submit", saveAppearanceSettings);
  document.querySelectorAll("[data-settings-action]").forEach((button) => {
    button.addEventListener("click", handleSettingsAction);
  });
  document.querySelectorAll("[data-camera-action]").forEach((button) => {
    button.addEventListener("click", handleSettingsCameraAction);
  });
}

async function hydrateSettingsTab(tabId) {
  if (tabId === "general") {
    fillLocalSettings();
    return;
  }
  if (tabId === "cameras") {
    await loadSettingsCameras();
    return;
  }
  if (tabId === "notifications") {
    const loaded = await loadNotificationSettings();
    if (loaded) {
      await loadNotificationDeliveries();
    }
    return;
  }
  if (tabId === "integrations") {
    await Promise.allSettled([loadNotificationSettings(), loadSettings()]);
    const check = document.querySelector("#settings-integration-check");
    if (check) check.textContent = `Última verificação: ${new Date().toLocaleString("pt-BR")}`;
    return;
  }
  if (tabId === "security") {
    const form = document.querySelector("#security-settings-form");
    const settings = securitySettings();
    if (form) {
      form.elements.session_timeout.value = settings.session_timeout;
      form.elements.evidence_retention_days.value = settings.evidence_retention_days;
    }
    return;
  }
  if (tabId === "appearance") {
    const form = document.querySelector("#appearance-settings-form");
    const settings = appearanceSettings();
    if (form) {
      form.elements.density.value = settings.density;
      form.elements.sidebar.value = settings.sidebar;
      form.elements.accent.value = settings.accent;
      form.elements.motion.value = settings.motion;
    }
  }
}

function filterSettingsCards(query) {
  const normalized = String(query || "").trim().toLowerCase();
  document.querySelectorAll("[data-settings-card]").forEach((card) => {
    card.hidden = normalized ? !card.textContent.toLowerCase().includes(normalized) : false;
  });
}

function saveCompanySettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    name: form.elements.name.value.trim(),
    document: form.elements.document.value.trim(),
    address: form.elements.address.value.trim(),
    city: form.elements.city.value.trim(),
    state: form.elements.state.value,
    timezone: form.elements.timezone.value.trim() || "America/Sao_Paulo",
  };
  writeStoredSettings("campex.company", payload);
  appendSettingsAudit("Empresa atualizada");
  notify("Empresa salva", "Dados da empresa atualizados neste navegador.", "success");
}

async function loadSettingsCameras() {
  const listHost = document.querySelector("#settings-camera-list");
  const summaryHost = document.querySelector("#settings-camera-summary");
  if (!listHost) return;
  try {
    const [cameras, zones, events] = await Promise.all([
      listCameras(),
      listZones(),
      listEvents({ limit: 20 }),
    ]);
    listHost.innerHTML = cameras.length
      ? cameras.map(settingsCameraLine).join("")
      : emptyState("Nenhuma câmera cadastrada", "Cadastre câmeras na área Câmeras.");
    if (summaryHost) {
      const online = cameras.filter((camera) => camera.status === "ONLINE").length;
      const enabled = cameras.filter((camera) => camera.enabled).length;
      summaryHost.innerHTML = [
        metricItem("Câmeras", `${cameras.length} cadastradas`, `${enabled} habilitadas`),
        metricItem("Online", `${online} online`, `${cameras.length - online} offline/degradadas`),
        metricItem("Zonas", `${zones.length} áreas`, "Polígonos operacionais"),
        metricItem("Eventos", `${events.length} recentes`, "Última leitura operacional"),
      ].join("");
    }
    refreshIcons();
    document.querySelectorAll("[data-camera-action]").forEach((button) => {
      button.addEventListener("click", handleSettingsCameraAction);
    });
  } catch (error) {
    listHost.innerHTML = emptyState("Falha ao carregar câmeras", error.message);
    if (summaryHost) summaryHost.innerHTML = "";
  }
}

function settingsCameraLine(camera) {
  const status = camera.status || (camera.enabled ? "ONLINE" : "OFFLINE");
  return `
    <article class="settings-integration-line" data-camera-id="${camera.id}">
      <div>
        <strong>${escapeHtml(camera.name || camera.id)}</strong>
        <span>${escapeHtml(camera.source_type || "camera")} · ${escapeHtml(camera.area_id || "sem área")}</span>
      </div>
      <small data-state="${status === "ONLINE" ? "connected" : "pending"}">${status}</small>
      <div class="settings-row-actions">
        <button type="button" data-camera-action="test">Testar</button>
        <button type="button" data-camera-action="toggle">${camera.enabled ? "Desativar" : "Ativar"}</button>
      </div>
    </article>
  `;
}

async function handleSettingsCameraAction(event) {
  const row = event.currentTarget.closest("[data-camera-id]");
  const cameraId = row?.dataset.cameraId;
  if (!cameraId) return;
  const action = event.currentTarget.dataset.cameraAction;
  try {
    if (action === "test") {
      await testCamera(cameraId);
      notify("Câmera testada", "Conexão validada pelo backend.", "success");
    }
    if (action === "toggle") {
      const enabled = event.currentTarget.textContent === "Desativar";
      await updateCamera(cameraId, { enabled: !enabled });
      notify("Câmera atualizada", `Câmera ${enabled ? "desativada" : "ativada"}.`, "success");
    }
    await loadSettingsCameras();
  } catch (error) {
    notify("Falha na câmera", error.message, "error");
  }
}

async function handleSettingsAction(event) {
  const action = event.currentTarget.dataset.settingsAction;
  if (action === "refresh-cameras") {
    await loadSettingsCameras();
    return;
  }
  if (action === "cleanup-evidence") {
    const days = Number(document.querySelector("#security-settings-form")?.elements.evidence_retention_days.value || 7);
    try {
      await cleanupEvidence(days);
      appendSettingsAudit(`Evidências antigas limpas (${days} dias)`);
      notify("Limpeza solicitada", `Evidências com mais de ${days} dias foram processadas.`, "success");
    } catch (error) {
      notify("Falha na limpeza", error.message, "error");
    }
  }
}

function saveSecuritySettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    strict_token: form.elements.strict_token.checked,
    audit_log: form.elements.audit_log.checked,
    session_timeout: form.elements.session_timeout.value,
    evidence_retention_days: form.elements.evidence_retention_days.value,
  };
  const token = form.elements.api_token.value.trim();
  if (payload.strict_token && token) {
    localStorage.setItem("campex.api_token", token);
  } else if (!payload.strict_token) {
    localStorage.removeItem("campex.api_token");
  }
  writeStoredSettings("campex.security", payload);
  appendSettingsAudit("Segurança atualizada");
  notify("Segurança salva", "Preferências de segurança atualizadas.", "success");
}

function saveUserInvite(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const store = readStoredSettings("campex.user_invites", { items: [] });
  const invite = {
    id: `invite_${Date.now()}`,
    name: form.elements.name.value.trim(),
    email: form.elements.email.value.trim(),
    role: form.elements.role.value,
    created_at: new Date().toISOString(),
  };
  writeStoredSettings("campex.user_invites", { items: [invite, ...(store.items || [])] });
  appendSettingsAudit(`Convite registrado para ${invite.email}`);
  notify("Convite registrado", "O convite ficou salvo nesta instalação.", "success");
  renderSettingsTab("users");
}

function saveAppearanceSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    density: form.elements.density.value,
    sidebar: form.elements.sidebar.value,
    accent: form.elements.accent.value,
    motion: form.elements.motion.value,
  };
  writeStoredSettings("campex.appearance", payload);
  document.body.dataset.density = payload.density;
  document.body.dataset.accent = payload.accent;
  document.body.dataset.motion = payload.motion;
  appShell.dataset.sidebar = payload.sidebar;
  localStorage.setItem(sidebarStorageKey, payload.sidebar);
  appendSettingsAudit("Aparência atualizada");
  notify("Aparência salva", "Preferências visuais aplicadas.", "success");
}

function appendSettingsAudit(message) {
  const security = securitySettings();
  if (!security.audit_log) return;
  const store = readStoredSettings("campex.settings_audit", { items: [] });
  const items = [{ message, at: new Date().toISOString() }, ...(store.items || [])].slice(0, 20);
  writeStoredSettings("campex.settings_audit", { items });
  const host = document.querySelector("#settings-audit-log");
  if (host) host.innerHTML = settingsAuditMarkup();
}

function settingsAuditMarkup() {
  const items = readStoredSettings("campex.settings_audit", { items: [] }).items || [];
  return items.length
    ? items.map((item) => objectLine(item.message, formatDate(item.at), "Configurações")).join("")
    : emptyState("Sem alterações registradas", "As próximas ações salvas aparecerão aqui.");
}

function settingsUserLine(name, email, role, status) {
  return `
    <article class="settings-integration-line">
      <div>
        <strong>${escapeHtml(name || email)}</strong>
        <span>${escapeHtml(email || "")} · ${escapeHtml(role || "operator")}</span>
      </div>
      <small data-state="${status === "Ativo" ? "connected" : "pending"}">${status}</small>
      <button type="button" disabled>Perfil</button>
    </article>
  `;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function loadSettings() {
  const host = document.querySelector("#settings-runtime");
  if (!host) return;
  try {
    const [runtime, cameras, zones, events] = await Promise.all([
      getRuntimeSettings(),
      listCameras(),
      listZones(),
      listEvents({ limit: 50 }),
    ]);
    host.innerHTML = [
      metricItem("Serviço", `${runtime.service_name} ${runtime.version}`, runtime.environment),
      metricItem("Banco", runtime.database_url, `${cameras.length} câmeras`),
      metricItem("Vision", `${runtime.vision.detector} · ${runtime.vision.device}`, `${runtime.vision.fps} FPS · conf ${runtime.vision.confidence}`),
      metricItem("Câmeras", `${runtime.camera.stale_seconds}s stale`, `${runtime.camera.read_failure_limit} falhas até reconectar`),
      metricItem("Zonas", `${zones.length} cadastradas`, `${events.length} eventos recentes`),
      metricItem("CORS", runtime.frontend_origins.join(", "), runtime.log_level),
    ].join("");
  } catch (error) {
    host.innerHTML = emptyState("Falha ao carregar configurações", error.message);
  }
}

async function loadNotificationSettings() {
  const form = document.querySelector("#notification-settings-form");
  const status = document.querySelector("#notification-settings-status");
  try {
    const prefs = await getNotificationPreferences();
    if (form) {
      form.elements.enabled.checked = Boolean(prefs.enabled);
      form.elements.telegram_enabled.checked = Boolean(prefs.telegram_enabled);
      form.elements.telegram_chat_id.value = prefs.telegram_chat_id || "";
      form.elements.email_enabled.checked = Boolean(prefs.email_enabled);
      form.elements.email_recipients.value = (prefs.email_recipients || []).join(", ");
      form.elements.reports_enabled.checked = Boolean(prefs.reports_enabled);
      form.elements.report_frequency.value = prefs.report_frequency || "DAILY";
      form.elements.report_time.value = prefs.report_time || "18:00";
      form.elements.timezone.value = prefs.timezone || "America/Sao_Paulo";
      form.elements.immediate_alerts_enabled.checked = Boolean(prefs.immediate_alerts_enabled);
      const types = new Set(prefs.alert_types || []);
      notificationAlertTypes().forEach((name) => {
        form.elements[name].checked = types.has(name);
      });
      const emailReady = Boolean(prefs.email_configured && prefs.email_recipients?.length);
      const telegramReady = Boolean(prefs.telegram_configured && prefs.telegram_chat_id);
      const emailButton = document.querySelector("#notification-test-email");
      const telegramButton = document.querySelector("#notification-test-telegram");
      if (emailButton) {
        emailButton.disabled = !emailReady;
        emailButton.title = emailReady ? "Enviar teste real de e-mail" : "Configure RESEND_API_KEY no backend e informe destinatários.";
      }
      if (telegramButton) {
        telegramButton.disabled = !telegramReady;
        telegramButton.title = telegramReady ? "Enviar teste real no Telegram" : "Configure TELEGRAM_BOT_TOKEN no backend e informe Chat ID.";
      }
    }
    if (status) {
      status.innerHTML = [
        notificationStatusLine("Preferências", prefs.enabled ? "Notificações ativas" : "Notificações desativadas", prefs.enabled ? "Ativo" : "Inativo"),
        notificationStatusLine("E-mail transacional (Resend)", `${(prefs.email_recipients || []).length} destinatário(s)`, prefs.email_configured ? "Configurado" : "Não configurado"),
        notificationStatusLine("Bot do Telegram", prefs.telegram_chat_id || "Chat ID ausente", prefs.telegram_configured ? "Configurado" : "Não configurado"),
        notificationStatusLine("Relatórios", `${prefs.report_frequency || "DAILY"} às ${prefs.report_time || "18:00"}`, prefs.reports_enabled ? "Ativo" : "Inativo"),
      ].join("");
    }
    return true;
  } catch (error) {
    if (status) {
      status.innerHTML = emptyState("Falha ao carregar notificações", error.message);
    }
    return false;
  }
}

async function saveNotificationSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submitButton = event.submitter || form.querySelector('button[type="submit"]');
  const originalText = submitButton?.textContent;
  if (submitButton) {
    submitButton.disabled = true;
    submitButton.textContent = "Salvando...";
  }
  try {
    const alertTypes = notificationAlertTypes()
      .filter((name) => form.elements[name]?.checked);
    const payload = {
      enabled: form.elements.enabled.checked,
      telegram_enabled: form.elements.telegram_enabled.checked,
      telegram_chat_id: form.elements.telegram_chat_id.value.trim() || null,
      email_enabled: form.elements.email_enabled.checked,
      email_recipients: form.elements.email_recipients.value.split(",").map((item) => item.trim()).filter(Boolean),
      reports_enabled: form.elements.reports_enabled.checked,
      report_frequency: form.elements.report_frequency.value,
      report_time: form.elements.report_time.value || "18:00",
      timezone: form.elements.timezone.value || "America/Sao_Paulo",
      immediate_alerts_enabled: form.elements.immediate_alerts_enabled.checked,
      alert_types: alertTypes,
    };
    await updateNotificationPreferences(payload);
    notify("Notificações salvas", "Preferências de entrega atualizadas.", "success");
    await loadNotificationSettings();
    await loadNotificationDeliveries();
  } catch (error) {
    notify("Falha ao salvar notificações", notificationErrorMessage(error), "error", 8000);
  } finally {
    if (submitButton) {
      submitButton.disabled = false;
      submitButton.textContent = originalText || "Salvar alterações";
    }
  }
}

function notificationErrorMessage(error) {
  const message = error?.message || "Não foi possível salvar as preferências.";
  if (message.toLowerCase().includes("token de api")) {
    return `${message} Configure o Token API nas configurações gerais antes de salvar.`;
  }
  return message;
}

async function testTelegramSettings() {
  try {
    await testTelegramNotification();
    notify("Telegram testado", "Mensagem de teste enviada.", "success");
    await loadNotificationDeliveries();
  } catch (error) {
    notify("Falha no Telegram", error.message, "error");
    if (!isApiAuthError(error)) await loadNotificationDeliveries();
  }
}

async function testEmailSettings() {
  try {
    await testEmailNotification();
    notify("E-mail testado", "Mensagem de teste enviada.", "success");
    await loadNotificationDeliveries();
  } catch (error) {
    notify("Falha no e-mail", error.message, "error");
    if (!isApiAuthError(error)) await loadNotificationDeliveries();
  }
}

async function sendReportNow() {
  try {
    const result = await sendNotificationReportNow();
    notify("Relatório solicitado", JSON.stringify(result.channels || {}), "success");
    await loadNotificationDeliveries();
  } catch (error) {
    notify("Falha ao enviar relatório", error.message, "error");
    if (!isApiAuthError(error)) await loadNotificationDeliveries();
  }
}

function isApiAuthError(error) {
  return String(error?.message || "").toLowerCase().includes("token de api");
}

async function loadNotificationDeliveries() {
  const host = document.querySelector("#notification-delivery-history");
  if (!host) return;
  try {
    const deliveries = await listNotificationDeliveries();
    host.innerHTML = deliveries.length
      ? deliveries.slice(0, 12).map(notificationDeliveryLine).join("")
      : emptyState("Nenhum envio registrado", "Testes e relatórios aparecerão aqui após o backend entregar ou falhar.");
  } catch (error) {
    host.innerHTML = emptyState("Falha ao carregar histórico", error.message);
  }
}

function notificationAlertTypes() {
  return [
    "camera_offline",
    "camera_online",
    "zone_idle",
    "zone_activity_resumed",
    "crowding_started",
    "crowding_ended",
    "long_presence",
  ];
}

function notificationStatusLine(title, detail, status) {
  const connected = ["Configurado", "Ativo"].includes(status);
  return `
    <article class="settings-integration-line">
      <div>
        <strong>${title}</strong>
        <span>${detail}</span>
      </div>
      <small data-state="${connected ? "connected" : "pending"}">${status}</small>
      <button type="button" disabled>Real</button>
    </article>
  `;
}

function notificationDeliveryLine(delivery) {
  const status = String(delivery.status || "pending").toLowerCase();
  const ok = status === "sent";
  const detail = [
    delivery.channel,
    delivery.type,
    delivery.recipient,
  ].filter(Boolean).join(" · ");
  return `
    <article class="settings-integration-line">
      <div>
        <strong>${escapeHtml(delivery.reference_id || delivery.id || "Entrega")}</strong>
        <span>${escapeHtml(detail || "Entrega de notificação")} · ${formatDate(delivery.created_at)}</span>
        ${delivery.error ? `<span>${escapeHtml(delivery.error)}</span>` : ""}
      </div>
      <small data-state="${ok ? "connected" : "pending"}">${escapeHtml(delivery.status || "pending")}</small>
      <button type="button" disabled>${delivery.attempts ?? 0} tent.</button>
    </article>
  `;
}

function metricItem(title, value, detail) {
  return `<article class="metric-item"><span>${title}</span><strong>${value}</strong><small>${detail || ""}</small></article>`;
}

function objectLine(title, value, detail) {
  return `
    <article class="insight-line">
      <div>
        <strong>${title}</strong>
        <span>${value}</span>
        <small>${detail || ""}</small>
      </div>
    </article>
  `;
}

function integrationLine(title, detail, status) {
  const isConnected = status === "Conectado";
  return `
    <article class="settings-integration-line">
      <div>
        <strong>${title}</strong>
        <span>${detail}</span>
      </div>
      <small data-state="${isConnected ? "connected" : "pending"}">${status}</small>
      <button type="button">Configurar</button>
    </article>
  `;
}

function formatMoney(value, currency = "BRL") {
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      maximumFractionDigits: 2,
    }).format(value);
  } catch {
    return `${currency} ${value}`;
  }
}

function localSettings() {
  return JSON.parse(localStorage.getItem("campex.settings") || '{"refresh_ms":2000,"operator_mode":"standard","dense_lists":false,"api_token":""}');
}

function applyLocalSettings() {
  const settings = localSettings();
  const appearance = appearanceSettings();
  document.body.dataset.density = settings.dense_lists ? "compact" : "comfortable";
  document.body.dataset.operatorMode = settings.operator_mode || "standard";
  document.body.dataset.accent = appearance.accent;
  document.body.dataset.motion = appearance.motion;
  if (settings.api_token) {
    localStorage.setItem("campex.api_token", settings.api_token);
  }
}

function fillLocalSettings() {
  const settings = localSettings();
  const form = document.querySelector("#local-settings-form");
  if (!form) return;
  form.elements.refresh_ms.value = settings.refresh_ms;
  form.elements.operator_mode.value = settings.operator_mode;
  form.elements.dense_lists.checked = Boolean(settings.dense_lists);
  form.elements.api_token.value = settings.api_token || localStorage.getItem("campex.api_token") || "";
  if (form.elements.api_token_visible) {
    form.elements.api_token_visible.value = form.elements.api_token.value;
  }
}

function saveLocalSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const settings = {
    refresh_ms: Number(form.elements.refresh_ms.value || 2000),
    operator_mode: form.elements.operator_mode.value,
    dense_lists: form.elements.dense_lists.checked,
    api_token: (form.elements.api_token_visible?.value || form.elements.api_token.value || "").trim(),
  };
  localStorage.setItem("campex.settings", JSON.stringify(settings));
  if (settings.api_token) {
    localStorage.setItem("campex.api_token", settings.api_token);
  } else {
    localStorage.removeItem("campex.api_token");
  }
  applyLocalSettings();
  notify("Configurações salvas", "Preferências locais atualizadas.", "success");
}

async function populateCameraSelect(selector, includeAll = false) {
  const select = document.querySelector(selector);
  const cameras = await listCameras();
  select.innerHTML = [
    includeAll ? `<option value="">Todas</option>` : "",
    ...cameras.map((camera) => `<option value="${camera.id}">${camera.name}</option>`),
  ].join("");
}

function emptyState(title, detail) {
  return `<div class="empty-state"><strong>${title}</strong><span>${detail}</span></div>`;
}

function formatDate(value) {
  if (!value) return "-";
  return new Date(value).toLocaleString("pt-BR");
}

function debounce(fn, delay) {
  let timeout;
  return (...args) => {
    clearTimeout(timeout);
    timeout = setTimeout(() => fn(...args), delay);
  };
}

async function refreshBackendStatus() {
  if (backendStatusRefreshInFlight) return;
  backendStatusRefreshInFlight = true;
  const backendSummary = document.querySelector("#backend-summary");

  try {
    if (lastBackendState !== "online") {
      statusElement.dataset.state = "checking";
      statusText.textContent = "Verificando backend";
    }
    const health = await getHealth();
    backendFailureCount = 0;
    const newState = "online";
    if (lastBackendState !== newState) {
      statusElement.dataset.state = "online";
      statusText.textContent = `${health.service} ${health.version} online`;
      notify("Backend conectado", `${health.service} ${health.version} respondeu em /health.`, "success");
    }
    lastBackendState = newState;
    if (backendSummary) {
      backendSummary.textContent = "Conectado via /api/v1/health";
    }
  } catch (error) {
    backendFailureCount += 1;
    if (lastBackendState === "online" && backendFailureCount < 3) {
      console.warn("[CAMPEX] health check transient failure", error);
      return;
    }
    const newState = backendFailureCount < 3 ? "checking" : "offline";
    if (lastBackendState !== newState) {
      statusElement.dataset.state = newState;
      statusText.textContent = backendFailureCount < 3 ? "Verificando backend" : "Backend indisponível";
      if (backendFailureCount >= 3) {
        notify("Backend indisponível", "Confira a URL configurada da API no arquivo frontend/config.js.", "error", 8000);
      }
    }
    if (backendFailureCount >= 3) {
      lastBackendState = "offline";
    }
    if (backendSummary) {
      backendSummary.textContent = backendFailureCount < 3 ? "Tentando reconectar" : "Sem resposta do backend";
    }
    console.error(error);
  } finally {
    backendStatusRefreshInFlight = false;
  }
}

window.addEventListener("hashchange", () => {
  if (!getCurrentUser()) {
    renderAuthScreen();
    return;
  }
  renderRoute();
  refreshIcons();
  refreshBackendStatus();
});

window.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!document.querySelector("#camera-modal")?.hidden) {
    closeCameraModal();
    return;
  }
  if (!document.querySelector("#rule-modal")?.hidden) {
    closeRuleModal();
    return;
  }
  if (!document.querySelector("#machine-modal")?.hidden) {
    closeMachineModal();
  }
});

accountButton?.addEventListener("click", handleSignOut);
startAuthenticatedApp();
