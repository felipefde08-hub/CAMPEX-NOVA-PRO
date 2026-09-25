import { createMediaUrl } from "./media-auth.js";
import { getApiToken } from "./api-token.js";
const queryApiBaseUrl = new URLSearchParams(window.location.search).get("api");
const localApiBaseUrl = "http://127.0.0.1:8787/api";
const isLocalFrontend = ["localhost", "127.0.0.1"].includes(window.location.hostname);
const storedApiBaseUrl = localStorage.getItem("campex.backend_url") || "";
const configuredApiBaseUrl = window.CAMPEX_API_BASE_URL || "";
const API_BASE_URL =
  (queryApiBaseUrl || configuredApiBaseUrl || (!isLocalFrontend ? storedApiBaseUrl : "") || (isLocalFrontend ? localApiBaseUrl : "")).replace(/\/$/, "");
const isLocalNodeApi = /^https?:\/\/(127\.0\.0\.1|localhost):8787\/api$/i.test(API_BASE_URL);

const mediaUrl = await createMediaUrl(API_BASE_URL, getApiToken);

function localNodeFallback(path) {
  const cleanPath = path.split("?")[0];
  const emptySummary = {
    kpis: {
      cameras_online: 0,
      cameras_total: 0,
      cameras_active: 0,
      events_open: 0,
      events_critical: 0,
      investigations_open: 0,
    },
    recent_events: [],
    areas: [],
    intelligence: {
      event_groups: [],
      triage: [],
      findings: [],
      impacts: [],
      comparisons: {},
    },
  };
  const fallbacks = {
    "/settings/runtime": {
      service_name: "CAMPEX Node Local",
      version: "local",
      environment: "offline/local",
      database_url: "SQLite local do Node",
      vision: { detector: "Edge Vision", device: "CPU", fps: 0, confidence: 0 },
      camera: { stale_seconds: 30, read_failure_limit: 3 },
      frontend_origins: ["Frontend local"],
      log_level: "INFO",
    },
    "/operations/summary": emptySummary,
    "/operations/diagnostics": {
      sqlite: true,
      database_path: "Banco local do Node",
      cameras_online: 0,
      ia_ativa: 0,
      zonas_ativas: 0,
      regras_ativas: 0,
      eventos_abertos: 0,
      investigacoes_abertas: 0,
      disco_livre_percentual: 0,
      evidence_mb: 0,
      ultima_entrega: null,
    },
    "/operations/productivity": {
      score: null,
      counts: { people: 0, signals: 0, machines: 0, cameras: 0, phones: 0 },
      mapped_machines_total: 0,
      cameras: [],
      machine_classes: ["pessoa"],
      behavior_signals: ["Edge Vision local"],
    },
    "/operations/evidence": [],
    "/operations/rules": [],
    "/operations/taxonomy": { event_types: [], severities: [], zones: [], actions: [] },
    "/investigations": [],
    "/camera-templates": [],
    "/monitors": [],
    "/events": [],
    "/zones": [],
    "/machines": [],
    "/videos": [],
    "/notifications/preferences": {
      enabled: false,
      telegram_enabled: false,
      telegram_chat_id: "",
      email_enabled: false,
      email_recipients: [],
      reports_enabled: false,
      report_frequency: "DAILY",
      report_time: "18:00",
      timezone: "America/Sao_Paulo",
      immediate_alerts_enabled: false,
      alert_types: [],
      email_configured: false,
      telegram_configured: false,
    },
    "/notifications/deliveries": [],
  };
  if (cleanPath.endsWith("/diagnostics")) {
    return { status: "UNKNOWN", checks: [], message: "Diagnostico detalhado indisponivel neste Node." };
  }
  if (cleanPath.endsWith("/mapping/poses")) {
    return [];
  }
  if (cleanPath.endsWith("/productivity")) {
    return { score: null, counts: {}, signals: [], machines: [] };
  }
  if (cleanPath.endsWith("/stream/info")) {
    return { mode: "mjpeg", available: true };
  }
  return Object.prototype.hasOwnProperty.call(fallbacks, cleanPath) ? fallbacks[cleanPath] : undefined;
}

function assertApiBaseUrl() {
  if (!API_BASE_URL) {
    throw new Error(
      "API_BASE_URL não configurada. Defina window.CAMPEX_API_BASE_URL em frontend/config.js."
    );
  }
}

function apiHeaders(extra = {}) {
  const apiToken = getApiToken();
  return {
    Accept: "application/json",
    "Content-Type": "application/json",
    ...(apiToken ? { "X-CAMPEX-Token": apiToken } : {}),
    ...Object.fromEntries(new Headers(extra)),
  };
}

function uploadHeaders(extra = {}) {
  const apiToken = getApiToken();
  return {
    Accept: "application/json",
    ...(apiToken ? { "X-CAMPEX-Token": apiToken } : {}),
    ...Object.fromEntries(new Headers(extra)),
  };
}

async function requestJson(path, options = {}) {
  assertApiBaseUrl();
  const method = String(options.method || "GET").toUpperCase();
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: apiHeaders(options.headers || {}),
  });

  if (!response.ok) {
    const fallback = method === "GET" && response.status === 404 && isLocalNodeApi
      ? localNodeFallback(path)
      : undefined;
    if (fallback !== undefined) {
      return typeof structuredClone === "function"
        ? structuredClone(fallback)
        : JSON.parse(JSON.stringify(fallback));
    }
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch {
      detail = response.status === 413
        ? "Arquivo grande demais para upload direto na Vercel. Use MP4 de até 4 MB."
        : response.statusText || detail;
    }
    throw new Error(detail);
  }

  if (response.status === 204) {
    return null;
  }

  return response.json();
}

export async function getHealth() {
  return requestJson("/health");
}

export function getRuntimeSettings() {
  return requestJson("/settings/runtime");
}

export function getOperationsSummary() {
  return requestJson("/operations/summary");
}

export function getOperationsDiagnostics() {
  return requestJson("/operations/diagnostics");
}

export function getProductivitySummary() {
  return requestJson("/operations/productivity");
}

export function listEvidence() {
  return requestJson("/operations/evidence");
}

export function listRules() {
  return requestJson("/operations/rules");
}

export function getOperationalTaxonomy() {
  return requestJson("/operations/taxonomy");
}

export function createRule(payload) {
  return requestJson("/operations/rules", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateRule(ruleId, payload) {
  return requestJson(`/operations/rules/${ruleId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteRule(ruleId) {
  assertApiBaseUrl();
  const response = await fetch(`${API_BASE_URL}/operations/rules/${ruleId}`, {
    method: "DELETE",
    headers: apiHeaders(),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
}

export function cleanupEvidence(days = 7) {
  return requestJson("/operations/evidence/cleanup", {
    method: "POST",
    body: JSON.stringify({ days }),
  });
}

export function setupDemo() {
  return requestJson("/operations/demo/setup", { method: "POST" });
}

export function operationsStreamUrl() {
  assertApiBaseUrl();
  return mediaUrl(`${API_BASE_URL}/operations/stream`);
}

export function simulateRule(payload) {
  return requestJson("/operations/rules/simulate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listInvestigations(params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, value);
    }
  });
  return requestJson(`/investigations${query.toString() ? `?${query}` : ""}`);
}

export function createInvestigation(payload) {
  return requestJson("/investigations", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateInvestigation(investigationId, payload) {
  return requestJson(`/investigations/${investigationId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteInvestigation(investigationId) {
  assertApiBaseUrl();
  const response = await fetch(`${API_BASE_URL}/investigations/${investigationId}`, {
    method: "DELETE",
    headers: apiHeaders(),
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
}

export function listCameras() {
  return requestJson("/cameras");
}

export function createCamera(payload) {
  return requestJson("/cameras", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateCamera(cameraId, payload) {
  return requestJson(`/cameras/${cameraId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function testCameraSource(payload) {
  return requestJson("/cameras/test-source", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listCameraTemplates() {
  return requestJson("/camera-templates");
}

export function createCameraRoi(cameraId, payload) {
  return requestJson(`/cameras/${cameraId}/rois`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listCameraRois(cameraId) {
  return requestJson(`/cameras/${cameraId}/rois`);
}

export function createMonitor(payload) {
  return requestJson("/monitors", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listMonitors(cameraId = "") {
  const query = cameraId ? `?camera_id=${encodeURIComponent(cameraId)}` : "";
  return requestJson(`/monitors${query}`);
}

export function getMonitorStatus(monitorId) {
  return requestJson(`/monitors/${monitorId}/status`);
}

export function listEvents(params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, value);
    }
  });
  return requestJson(`/events${query.toString() ? `?${query}` : ""}`);
}

export function updateEvent(eventId, payload) {
  return requestJson(`/events/${eventId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function eventEvidenceUrl(eventId, variant = "overlay") {
  assertApiBaseUrl();
  return mediaUrl(`${API_BASE_URL}/events/${eventId}/evidence?variant=${encodeURIComponent(variant)}`);
}

export async function deleteEvent(eventId) {
  assertApiBaseUrl();
  const response = await fetch(`${API_BASE_URL}/events/${eventId}`, {
    method: "DELETE",
    headers: apiHeaders(),
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
}

export function listZones(cameraId = "") {
  const query = cameraId ? `?camera_id=${encodeURIComponent(cameraId)}` : "";
  return requestJson(`/zones${query}`);
}

export function listMachines(cameraId = "") {
  const query = cameraId ? `?camera_id=${encodeURIComponent(cameraId)}` : "";
  return requestJson(`/machines${query}`);
}

export function createMachine(payload) {
  return requestJson("/machines", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateMachine(machineId, payload) {
  return requestJson(`/machines/${machineId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteMachine(machineId) {
  assertApiBaseUrl();
  const response = await fetch(`${API_BASE_URL}/machines/${machineId}`, {
    method: "DELETE",
    headers: apiHeaders(),
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
}

export function createZone(payload) {
  return requestJson("/zones", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateZone(zoneId, payload) {
  return requestJson(`/zones/${zoneId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteZone(zoneId) {
  assertApiBaseUrl();
  const response = await fetch(`${API_BASE_URL}/zones/${zoneId}`, {
    method: "DELETE",
    headers: apiHeaders(),
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
}

export async function deleteCamera(cameraId) {
  assertApiBaseUrl();
  const response = await fetch(`${API_BASE_URL}/cameras/${cameraId}`, {
    method: "DELETE",
    headers: apiHeaders(),
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
}

export function testCamera(cameraId) {
  return requestJson(`/cameras/${cameraId}/test`, {
    method: "POST",
  });
}

export function getCameraHealth(cameraId) {
  return requestJson(`/cameras/${cameraId}/health`);
}

export function getCameraDiagnostics(cameraId) {
  return requestJson(`/cameras/${cameraId}/diagnostics`);
}

export function startVision(cameraId) {
  return requestJson(`/cameras/${cameraId}/vision/start`, {
    method: "POST",
  });
}

export function restartVision(cameraId) {
  return requestJson(`/cameras/${cameraId}/vision/restart`, {
    method: "POST",
  });
}

export function stopVision(cameraId) {
  return requestJson(`/cameras/${cameraId}/vision/stop`, {
    method: "POST",
  });
}

export function startMapping(cameraId) {
  return requestJson(`/cameras/${cameraId}/mapping/start`, {
    method: "POST",
  });
}

export function stopMapping(cameraId) {
  return requestJson(`/cameras/${cameraId}/mapping/stop`, {
    method: "POST",
  });
}

export function getVisionStatus(cameraId) {
  return requestJson(`/cameras/${cameraId}/vision/status`);
}

export function getVisionObjects(cameraId) {
  return requestJson(`/cameras/${cameraId}/vision/objects`);
}

export function getMappingPoses(cameraId) {
  return requestJson(`/cameras/${cameraId}/mapping/poses`);
}

export function getCameraProductivity(cameraId) {
  return requestJson(`/cameras/${cameraId}/productivity`);
}

export function getStreamInfo(cameraId) {
  return requestJson(`/cameras/${cameraId}/stream/info`);
}

export function cameraStreamUrl(cameraId, options = {}) {
  assertApiBaseUrl();
  const query = options.overlay ? "?overlay=true" : "";
  return mediaUrl(`${API_BASE_URL}/cameras/${cameraId}/stream${query}`);
}

export function cameraVideoUrl(cameraId) {
  assertApiBaseUrl();
  return mediaUrl(`${API_BASE_URL}/cameras/${cameraId}/video`);
}

export function cameraSnapshotUrl(cameraId, options = {}) {
  assertApiBaseUrl();
  const params = new URLSearchParams({ t: String(Date.now()) });
  if (options.overlay) {
    params.set("overlay", "true");
  }
  return mediaUrl(`${API_BASE_URL}/cameras/${cameraId}/snapshot?${params}`);
}

export async function analyzeVideo(file) {
  assertApiBaseUrl();
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(`${API_BASE_URL}/videos/analyze`, {
    method: "POST",
    headers: uploadHeaders(),
    body: formData,
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch {
      detail = response.statusText || detail;
    }
    throw new Error(detail);
  }
  return response.json();
}

export function listVideoAnalyses() {
  return requestJson("/videos");
}

export function getVideoAnalysisStatus(analysisId) {
  return requestJson(`/videos/${analysisId}/status`);
}

export function uploadedVideoUrl(analysisId) {
  assertApiBaseUrl();
  return mediaUrl(`${API_BASE_URL}/videos/${analysisId}/video`);
}

export function debugVideoUrl(analysisId) {
  assertApiBaseUrl();
  return mediaUrl(`${API_BASE_URL}/videos/${analysisId}/debug-video`);
}

export function getNotificationPreferences() {
  return requestJson("/notifications/preferences");
}

export function updateNotificationPreferences(payload) {
  return requestJson("/notifications/preferences", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function testTelegramNotification() {
  return requestJson("/notifications/test/telegram", { method: "POST" });
}

export function testEmailNotification() {
  return requestJson("/notifications/test/email", { method: "POST" });
}

export function sendNotificationReportNow() {
  return requestJson("/notifications/send-report", { method: "POST" });
}

export function listNotificationDeliveries() {
  return requestJson("/notifications/deliveries");
}

export function listNodes() {
  return requestJson("/nodes");
}

export function getNodeTelemetry(nodeId) {
  return requestJson(`/nodes/${nodeId}/telemetry`);
}

export function requestNodePairingCode() {
  return requestJson("/nodes/pair/request", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function lookupNodePairingCode(code) {
  return requestJson("/nodes/pairing/lookup", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
}

export function authorizeNodePairingCode(code, nodeName = "") {
  return requestJson("/nodes/pairing/authorize", {
    method: "POST",
    body: JSON.stringify({ code, node_name: nodeName || null }),
  });
}

export function renameNode(nodeId, name) {
  return requestJson(`/nodes/${nodeId}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}

export async function revokeNode(nodeId) {
  assertApiBaseUrl();
  const response = await fetch(`${API_BASE_URL}/nodes/${nodeId}`, {
    method: "DELETE",
    headers: apiHeaders(),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
}

export function getBackendUrl() { return API_BASE_URL; }

export async function connectBackend(mode, token) {
  const base = mode === "local" ? "http://127.0.0.1:8787/api" : window.CAMPEX_API_BASE_URL;
  if (!base) throw new Error("Endereço do backend não configurado.");
  const response = await fetch(`${base}/cameras`, {
    headers: { Accept: "application/json", ...(token ? { "X-CAMPEX-Token": token.trim() } : {}) },
    signal: AbortSignal.timeout(8000),
    redirect: "error",
  });
  if (!response.ok) {
    throw new Error(response.status === 401
      ? "Token inválido para o backend selecionado."
      : `O backend respondeu HTTP ${response.status}.`);
  }
  localStorage.setItem("campex.backend_url", base);
  return base;
}
