// Credentials stay in the current tab; never inject server environment secrets
// into public JavaScript, HTML or URLs.
const TOKEN_KEY = "campex.api_token";

export function getApiToken() {
  let settings;
  try {
    settings = JSON.parse(localStorage.getItem("campex.settings") || "{}");
  } catch {
    settings = {};
  }
  if (!settings || typeof settings !== "object") settings = {};
  const legacyToken = localStorage.getItem(TOKEN_KEY) || settings.api_token;
  if (!sessionStorage.getItem(TOKEN_KEY) && legacyToken) {
    sessionStorage.setItem(TOKEN_KEY, legacyToken);
  }
  localStorage.removeItem(TOKEN_KEY);
  if ("api_token" in settings) {
    delete settings.api_token;
    localStorage.setItem("campex.settings", JSON.stringify(settings));
  }
  return sessionStorage.getItem(TOKEN_KEY) || "";
}

// Cloud login session ({ token, expires_at, user }). Kept in localStorage so the
// user stays signed in across tabs; the server can revoke it at any time.
const CLOUD_SESSION_KEY = "campex.auth.cloud_session";

export function getCloudSession() {
  try {
    const session = JSON.parse(localStorage.getItem(CLOUD_SESSION_KEY) || "null");
    if (!session?.token || !session?.user) return null;
    if (session.expires_at && Date.parse(session.expires_at) <= Date.now()) return null;
    return session;
  } catch {
    return null;
  }
}

export function setCloudSession(session) {
  if (session?.token) localStorage.setItem(CLOUD_SESSION_KEY, JSON.stringify(session));
  else localStorage.removeItem(CLOUD_SESSION_KEY);
}

export function getSessionToken() {
  return getCloudSession()?.token || "";
}

export function setApiToken(token) {
  getApiToken(); // Remove credentials persisted by older versions.
  if (token.trim()) sessionStorage.setItem(TOKEN_KEY, token.trim());
  else sessionStorage.removeItem(TOKEN_KEY);
}
