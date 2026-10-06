import { createMediaUrl } from "./media-auth.js";
import { getApiToken, getSessionToken } from "./api-token.js";
const queryApiBaseUrl = new URLSearchParams(window.location.search).get("api");
// The Node prefers 8787 but takes the next free port when another program
// holds it; the range matches PORT_RANGE_SIZE in campex_node/desktop_launcher.py.
const LOCAL_NODE_HOST = "http://127.0.0.1";
const LOCAL_NODE_FIRST_PORT = 8787;
const LOCAL_NODE_PORT_COUNT = 20;
const LOCAL_NODE_PORT_KEY = "campex.local_node_port";
const LOCAL_NODE_RESCAN_MS = 60000;
const isLocalFrontend = ["localhost", "127.0.0.1"].includes(window.location.hostname);
let localNodePort = initialLocalNodePort();
let localNodeDiscovery = null;
let lastFailedDiscoveryAt = 0;
// A dev server on localhost (not the Node's own panel): the API base below is
// fixed for the page's lifetime, so find the Node's port first.
if (isLocalFrontend && !queryApiBaseUrl && !window.CAMPEX_API_BASE_URL && !isLocalNodePort(window.location.port)) {
  await discoverLocalNode();
}
const localApiBaseUrl = `${localNodeOrigin()}/api`;
const isHttpFrontend = ["http:", "https:"].includes(window.location.protocol);
const isHostedFrontend = isHttpFrontend && window.location.protocol === "https:" && !isLocalFrontend;
const storedApiBaseUrl = localStorage.getItem("campex.backend_url") || "";
const configuredApiBaseUrl = window.CAMPEX_API_BASE_URL || "";
const usableStoredApiBaseUrl = !isLocalFrontend && isLocalNodeApiBase(storedApiBaseUrl) ? "" : storedApiBaseUrl;
const API_BASE_URL =
  normalizeApiBaseUrl(
    queryApiBaseUrl ||
    configuredApiBaseUrl ||
    (!isLocalFrontend ? usableStoredApiBaseUrl : "") ||
    (isLocalFrontend ? localApiBaseUrl : "")
  );
const isLocalNodeApi = isLocalNodeApiBase(API_BASE_URL);

const mediaUrl = await createMediaUrl(API_BASE_URL, () => ({
  token: getApiToken(),
  session: getSessionToken(),
}));

function normalizeApiBaseUrl(value) {
  const trimmed = String(value || "").trim().replace(/\/$/, "");
  if (!trimmed) return "";
  if (isLocalNodeApiBase(trimmed)) {
    return trimmed;
  }
  return trimmed.endsWith("/api") ? `${trimmed}/v1` : trimmed;
}

function isLocalNodeApiBase(value) {
  const match = /^https?:\/\/(?:127\.0\.0\.1|localhost):(\d+)\/api\/?$/i.exec(String(value || "").trim());
  return Boolean(match) && isLocalNodePort(match[1]);
}

function isLocalNodePort(value) {
  const port = Number(value);
  return Number.isInteger(port) && port >= LOCAL_NODE_FIRST_PORT && port < LOCAL_NODE_FIRST_PORT + LOCAL_NODE_PORT_COUNT;
}

function initialLocalNodePort() {
  // The Node's own panel is served by the Node: same port.
  if (isLocalFrontend && isLocalNodePort(window.location.port)) return Number(window.location.port);
  try {
    const stored = localStorage.getItem(LOCAL_NODE_PORT_KEY);
    if (isLocalNodePort(stored)) return Number(stored);
  } catch {
    // Storage blocked: fall back to the default port.
  }
  return LOCAL_NODE_FIRST_PORT;
}

export function localNodeOrigin() {
  return `${LOCAL_NODE_HOST}:${localNodePort}`;
}

async function isCampexNodeAt(port) {
  try {
    const response = await fetch(`${LOCAL_NODE_HOST}:${port}/api/status`, {
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(1500),
    });
    if (!response.ok) return false;
    const status = await response.json();
    return Boolean(status && "node_id" in status);
  } catch {
    return false;
  }
}

// Finds the port the local Node is on and remembers it. Returns false when no
// Node answers; a failed scan is not repeated for a minute so dashboards on
// computers without a Node do not keep probing.
export function discoverLocalNode() {
  if (localNodeDiscovery) return localNodeDiscovery;
  if (Date.now() - lastFailedDiscoveryAt < LOCAL_NODE_RESCAN_MS) return Promise.resolve(false);
  localNodeDiscovery = (async () => {
    const ports = Array.from({ length: LOCAL_NODE_PORT_COUNT }, (_, index) => LOCAL_NODE_FIRST_PORT + index);
    const found = await Promise.all(ports.map(async (port) => ((await isCampexNodeAt(port)) ? port : null)));
    const available = found.filter((port) => port !== null);
    if (!available.length) {
      lastFailedDiscoveryAt = Date.now();
      return false;
    }
    localNodePort = available.includes(localNodePort) ? localNodePort : available[0];
    try {
      localStorage.setItem(LOCAL_NODE_PORT_KEY, String(localNodePort));
    } catch {
      // Not remembered across reloads; discovery runs again next time.
    }
    return true;
  })().finally(() => {
    localNodeDiscovery = null;
  });
  return localNodeDiscovery;
}

// fetch() against the local Node; when it does not answer, looks for it on
// the other ports and retries once there.
export async function fetchLocalNode(path, { timeoutMs = 10000, ...options } = {}) {
  const attempt = () => fetch(`${localNodeOrigin()}${path}`, {
    ...options,
    signal: options.signal || AbortSignal.timeout(timeoutMs),
  });
  try {
    return await attempt();
  } catch (error) {
    const previousPort = localNodePort;
    if ((await discoverLocalNode()) && localNodePort !== previousPort) return attempt();
    throw error;
  }
}

function shouldUseLocalNodeCameraId(cameraId = "") {
  return String(cameraId || "").startsWith("local_");
}

async function requestLocalNodeJson(path, { timeoutMs = 10000, ...options } = {}) {
  const response = await fetchLocalNode(`/api${path}`, {
    ...options,
    timeoutMs,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      detail = body.detail || body.error || detail;
    } catch {
      detail = response.statusText || detail;
    }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

async function maybeListLocalNodeCameras() {
  if (!isHostedFrontend || isLocalNodeApi) return [];
  try {
    return await requestLocalNodeJson("/cameras", { timeoutMs: 1500 });
  } catch {
    return [];
  }
}

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

function credentialHeaders() {
  const apiToken = getApiToken();
  const sessionToken = getSessionToken();
  return {
    ...(apiToken ? { "X-CAMPEX-Token": apiToken } : {}),
    ...(sessionToken ? { "X-CAMPEX-Session": sessionToken } : {}),
  };
}

function apiHeaders(extra = {}) {
  return {
    Accept: "application/json",
    "Content-Type": "application/json",
    ...credentialHeaders(),
    ...Object.fromEntries(new Headers(extra)),
  };
}

function uploadHeaders(extra = {}) {
  return {
    Accept: "application/json",
    ...credentialHeaders(),
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
    const error = new Error(detail);
    error.status = response.status;
    if (response.status === 401 && getSessionToken() && !path.startsWith("/auth/")) {
      // The server revoked or expired the login; let the app return to the login screen.
      window.dispatchEvent?.(new CustomEvent("campex:session-expired"));
    }
    throw error;
  }

  if (response.status === 204) {
    return null;
  }

  return response.json();
}

export async function getHealth() {
  return requestJson("/health");
}

// The local CAMPEX Node has no account database, so login there stays in the
// browser (see auth.js); every other backend authenticates against the Cloud.
export function usesLocalNodeApi() {
  return isLocalNodeApi;
}

export function authRegister({ name, email, password }) {
  return requestJson("/auth/register", {
    method: "POST",
    body: JSON.stringify({ name, email, password }),
  });
}

export function authLogin({ email, password }) {
  return requestJson("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function authMe() {
  return requestJson("/auth/me");
}

export function authLogout() {
  return requestJson("/auth/logout", { method: "POST" });
}

export function authListUsers() {
  return requestJson("/auth/users");
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

export async function listCameras() {
  const [cloudResult, localResult] = await Promise.all([
    requestJson("/cameras").catch(() => []),
    maybeListLocalNodeCameras(),
  ]);
  const cloudCameras = Array.isArray(cloudResult) ? cloudResult : [];
  const localCameras = Array.isArray(localResult) ? localResult : [];
  const merged = new Map();
  for (const camera of cloudCameras) {
    if (isHostedFrontend && String(camera.id || "").startsWith("local_")) continue;
    merged.set(camera.id, camera);
  }
  // The local Node also lists the Cloud cameras it captures; those must keep
  // the Cloud copy, whose health says the frames come through the Node relay.
  for (const camera of localCameras) {
    if (!shouldUseLocalNodeCameraId(camera.id)) continue;
    merged.set(camera.id, { ...camera, runtime: "local_node" });
  }
  return Array.from(merged.values());
}

// RTSP cameras are registered in the Cloud; the organization's CAMPEX Node
// picks them up from there, captures them and relays status and frames back.
export function createCamera(payload) {
  return requestJson("/cameras", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateCamera(cameraId, payload) {
  if (shouldUseLocalNodeCameraId(cameraId)) {
    return requestLocalNodeJson(`/cameras/${cameraId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  }
  return requestJson(`/cameras/${cameraId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function testCameraSource(payload) {
  const started = await requestJson("/cameras/test-source", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return started?.pending ? waitForNodeCameraTest(started.job_id) : started;
}

// In the Cloud the connection test runs on the CAMPEX Node (the only machine
// on the camera network); the Cloud hands back the result when it arrives.
async function waitForNodeCameraTest(jobId, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    const result = await requestJson(`/cameras/test-source/${encodeURIComponent(jobId)}`);
    if (!result?.pending) return result;
  }
  return {
    ok: false,
    success: false,
    status: "OFFLINE",
    error: "O CAMPEX Node não respondeu ao teste a tempo. Verifique se ele está aberto e conectado.",
  };
}

// Latest frame relayed by the CAMPEX Node, fetched with the login headers.
export async function fetchCameraSnapshot(cameraId, { overlay = false } = {}) {
  assertApiBaseUrl();
  const params = new URLSearchParams({ t: String(Date.now()) });
  if (overlay) params.set("overlay", "true");
  const response = await fetch(`${API_BASE_URL}/cameras/${encodeURIComponent(cameraId)}/snapshot?${params}`, {
    headers: { ...credentialHeaders(), Accept: "image/jpeg" },
    cache: "no-store",
  });
  if (!response.ok) {
    const error = new Error(`HTTP ${response.status}`);
    error.status = response.status;
    if (response.status === 401 && getSessionToken()) {
      window.dispatchEvent?.(new CustomEvent("campex:session-expired"));
    }
    throw error;
  }
  return { blob: await response.blob(), waiting: response.headers.get("X-CAMPEX-Frame") === "waiting" };
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
  if (shouldUseLocalNodeCameraId(cameraId)) {
    await requestLocalNodeJson(`/cameras/${cameraId}`, {
      method: "DELETE",
    });
    return;
  }
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
  if (shouldUseLocalNodeCameraId(cameraId)) {
    return requestLocalNodeJson(`/cameras/${cameraId}/health`);
  }
  return requestJson(`/cameras/${cameraId}/test`, {
    method: "POST",
  });
}

export function getCameraHealth(cameraId) {
  if (shouldUseLocalNodeCameraId(cameraId)) {
    return requestLocalNodeJson(`/cameras/${cameraId}/health`);
  }
  return requestJson(`/cameras/${cameraId}/health`);
}

export function getCameraDiagnostics(cameraId) {
  return requestJson(`/cameras/${cameraId}/diagnostics`);
}

export function startVision(cameraId) {
  if (shouldUseLocalNodeCameraId(cameraId)) {
    return requestLocalNodeJson(`/cameras/${cameraId}/vision/start`, { method: "POST" });
  }
  return requestJson(`/cameras/${cameraId}/vision/start`, {
    method: "POST",
  });
}

export function restartVision(cameraId) {
  if (shouldUseLocalNodeCameraId(cameraId)) {
    return requestLocalNodeJson(`/cameras/${cameraId}/vision/restart`, { method: "POST" });
  }
  return requestJson(`/cameras/${cameraId}/vision/restart`, {
    method: "POST",
  });
}

export function stopVision(cameraId) {
  if (shouldUseLocalNodeCameraId(cameraId)) {
    return requestLocalNodeJson(`/cameras/${cameraId}/vision/stop`, { method: "POST" });
  }
  return requestJson(`/cameras/${cameraId}/vision/stop`, {
    method: "POST",
  });
}

export function startMapping(cameraId) {
  if (shouldUseLocalNodeCameraId(cameraId)) {
    return Promise.reject(new Error("Mapeamento indisponivel para cameras do Node local."));
  }
  return requestJson(`/cameras/${cameraId}/mapping/start`, {
    method: "POST",
  });
}

export function stopMapping(cameraId) {
  if (shouldUseLocalNodeCameraId(cameraId)) {
    return Promise.reject(new Error("Mapeamento indisponivel para cameras do Node local."));
  }
  return requestJson(`/cameras/${cameraId}/mapping/stop`, {
    method: "POST",
  });
}

export function getVisionStatus(cameraId) {
  if (shouldUseLocalNodeCameraId(cameraId)) {
    return requestLocalNodeJson(`/cameras/${cameraId}/vision/status`);
  }
  return requestJson(`/cameras/${cameraId}/vision/status`);
}

export function getVisionObjects(cameraId) {
  if (shouldUseLocalNodeCameraId(cameraId)) {
    return requestLocalNodeJson(`/cameras/${cameraId}/vision/objects`);
  }
  return requestJson(`/cameras/${cameraId}/vision/objects`);
}

export function getMappingPoses(cameraId) {
  if (shouldUseLocalNodeCameraId(cameraId)) return Promise.resolve([]);
  return requestJson(`/cameras/${cameraId}/mapping/poses`);
}

export function getCameraProductivity(cameraId) {
  return requestJson(`/cameras/${cameraId}/productivity`);
}

export function getStreamInfo(cameraId) {
  if (shouldUseLocalNodeCameraId(cameraId)) {
    return Promise.resolve({ mode: "mjpeg", available: true });
  }
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
  if (mode === "local") await discoverLocalNode();
  const base = mode === "local" ? `${localNodeOrigin()}/api` : window.CAMPEX_API_BASE_URL;
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
