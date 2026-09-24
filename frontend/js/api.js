import { createMediaUrl } from "./media-auth.js";
import { getApiToken } from "./api-token.js";
const queryApiBaseUrl = new URLSearchParams(window.location.search).get("api");
const localApiBaseUrl = "http://127.0.0.1:8000/api/v1";
const isLocalFrontend = ["localhost", "127.0.0.1"].includes(window.location.hostname);
const API_BASE_URL =
  (queryApiBaseUrl || localStorage.getItem("campex.backend_url") || window.CAMPEX_API_BASE_URL || (isLocalFrontend ? localApiBaseUrl : "")).replace(/\/$/, "");

const mediaUrl = await createMediaUrl(API_BASE_URL, getApiToken);

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
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: apiHeaders(options.headers || {}),
  });

  if (!response.ok) {
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
  const base = mode === "local" ? "http://127.0.0.1:8000/api/v1" : window.CAMPEX_API_BASE_URL;
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
