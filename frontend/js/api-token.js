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

export function setApiToken(token) {
  getApiToken(); // Remove credentials persisted by older versions.
  if (token.trim()) sessionStorage.setItem(TOKEN_KEY, token.trim());
  else sessionStorage.removeItem(TOKEN_KEY);
}
