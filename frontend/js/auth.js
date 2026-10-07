import { getCloudSession, setApiToken, setCloudSession } from "./api-token.js";
import {
  authListUsers,
  authLogin,
  authLogout,
  authMe,
  authRegister,
  authSetup,
  usesLocalNodeApi,
} from "./api.js";

// Accounts live on the server the panel talks to: the CAMPEX Node's own
// database, or the CAMPEX Cloud. The browser keeps only the session token.
const LEGACY_LOCAL_KEYS = ["campex.auth.users", "campex.auth.session"];
const MIN_PASSWORD_LENGTH = 8;

let serverUsers = null;

// Older Node panels kept accounts in this browser; they are no longer used.
LEGACY_LOCAL_KEYS.forEach((key) => {
  try {
    localStorage.removeItem(key);
  } catch {
    // Storage blocked: nothing to clean up.
  }
});

function normalizeEmail(email) {
  return String(email || "").trim().toLowerCase();
}

function validateAccount({ name, email, password }) {
  if (String(name || "").trim().length < 2) {
    throw new Error("Informe um nome com pelo menos 2 caracteres.");
  }
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalizeEmail(email))) {
    throw new Error("Informe um email válido.");
  }
  if (String(password || "").length < MIN_PASSWORD_LENGTH) {
    throw new Error(`A senha precisa ter pelo menos ${MIN_PASSWORD_LENGTH} caracteres.`);
  }
}

function friendlyError(error) {
  if (error instanceof TypeError) {
    // fetch() rejects with TypeError when the server cannot be reached.
    return new Error(
      usesLocalNodeApi()
        ? "Não foi possível conectar ao CAMPEX Node. Verifique se ele está aberto e se este computador está na rede da fábrica."
        : "Não foi possível conectar ao servidor CAMPEX. Verifique sua internet e tente novamente.",
    );
  }
  return error;
}

export function getCurrentUser() {
  return getCloudSession()?.user || null;
}

export function isAdmin() {
  return getCurrentUser()?.role === "admin";
}

export function listUsers() {
  const current = getCurrentUser();
  return serverUsers || (current ? [current] : []);
}

// On the Node, whether the first (administrator) account still has to be
// created, and whether this browser runs on the Node's computer, the only
// place where that is allowed.
export async function getAuthSetup() {
  if (!usesLocalNodeApi()) return { needs_setup: false, local: false, node: false };
  try {
    return { ...(await authSetup()), node: true };
  } catch {
    return { needs_setup: false, local: false, node: true };
  }
}

export async function createAccount({ name, email, password }) {
  validateAccount({ name, email, password });
  try {
    const session = await authRegister({ name: String(name).trim(), email: normalizeEmail(email), password });
    setCloudSession(session);
    serverUsers = [session.user];
    return session.user;
  } catch (error) {
    throw friendlyError(error);
  }
}

export async function signIn({ email, password }) {
  try {
    const session = await authLogin({ email: normalizeEmail(email), password });
    setCloudSession(session);
    serverUsers = null;
    return session.user;
  } catch (error) {
    throw friendlyError(error);
  }
}

export function signOut() {
  setApiToken("");
  if (getCloudSession()) {
    // Revoke on the server too; the local session is cleared regardless.
    authLogout().catch(() => {});
  }
  setCloudSession(null);
  serverUsers = null;
}

export async function refreshUsers() {
  serverUsers = await authListUsers();
  return serverUsers;
}

// Confirms the stored session is still valid and refreshes the user data.
// Returns false only when the server rejected the session; network failures
// keep the user signed in so a flaky connection does not log them out.
export async function verifySession() {
  if (!getCloudSession()) return true;
  try {
    const { user } = await authMe();
    const session = getCloudSession();
    if (session) setCloudSession({ ...session, user });
    refreshUsers().catch(() => {});
    return true;
  } catch (error) {
    if (error?.status === 401) {
      setCloudSession(null);
      serverUsers = null;
      return false;
    }
    return true;
  }
}
