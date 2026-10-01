import { getCloudSession, setApiToken, setCloudSession } from "./api-token.js";
import {
  authListUsers,
  authLogin,
  authLogout,
  authMe,
  authRegister,
  usesLocalNodeApi,
} from "./api.js";

// Accounts live in the CAMPEX Cloud database. Only the panel served by the
// local CAMPEX Node (no database, single machine) keeps accounts in this
// browser's localStorage.
const LOCAL_USERS_KEY = "campex.auth.users";
const LOCAL_SESSION_KEY = "campex.auth.session";
const MIN_PASSWORD_LENGTH = 8;

let cloudUsers = null;

function readJson(key, fallback) {
  try {
    const parsed = JSON.parse(localStorage.getItem(key) || "");
    return parsed ?? fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key, value) {
  localStorage.setItem(key, JSON.stringify(value));
}

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
    return new Error("Não foi possível conectar ao servidor CAMPEX. Verifique sua internet e tente novamente.");
  }
  return error;
}

function randomSalt() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function hashPassword(password, salt) {
  const payload = new TextEncoder().encode(`${salt}:${password}`);
  const digest = await crypto.subtle.digest("SHA-256", payload);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function publicUser(user) {
  return {
    id: user.id,
    name: user.name,
    email: user.email,
    role: user.role,
    created_at: user.created_at,
  };
}

function listLocalUsers() {
  const users = readJson(LOCAL_USERS_KEY, []);
  return Array.isArray(users) ? users : [];
}

export function getCurrentUser() {
  if (!usesLocalNodeApi()) {
    return getCloudSession()?.user || null;
  }
  const session = readJson(LOCAL_SESSION_KEY, null);
  if (!session?.user_id) return null;
  const user = listLocalUsers().find((item) => item.id === session.user_id);
  return user ? publicUser(user) : null;
}

export function listUsers() {
  if (!usesLocalNodeApi()) {
    const current = getCurrentUser();
    return cloudUsers || (current ? [current] : []);
  }
  return listLocalUsers();
}

export async function createAccount({ name, email, password }) {
  validateAccount({ name, email, password });
  if (!usesLocalNodeApi()) {
    try {
      const session = await authRegister({ name: String(name).trim(), email: normalizeEmail(email), password });
      setCloudSession(session);
      cloudUsers = [session.user];
      return session.user;
    } catch (error) {
      throw friendlyError(error);
    }
  }

  const normalizedEmail = normalizeEmail(email);
  const users = listLocalUsers();
  if (users.some((user) => user.email === normalizedEmail)) {
    throw new Error("Já existe uma conta com este email.");
  }
  const salt = randomSalt();
  const now = new Date().toISOString();
  const user = {
    id: `usr_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`,
    name: String(name).trim(),
    email: normalizedEmail,
    role: "operator",
    password_salt: salt,
    password_hash: await hashPassword(password, salt),
    created_at: now,
  };
  writeJson(LOCAL_USERS_KEY, [...users, user]);
  writeJson(LOCAL_SESSION_KEY, { user_id: user.id, signed_in_at: now });
  return publicUser(user);
}

export async function signIn({ email, password }) {
  if (!usesLocalNodeApi()) {
    try {
      const session = await authLogin({ email: normalizeEmail(email), password });
      setCloudSession(session);
      cloudUsers = null;
      return session.user;
    } catch (error) {
      throw friendlyError(error);
    }
  }

  const normalizedEmail = normalizeEmail(email);
  const user = listLocalUsers().find((item) => item.email === normalizedEmail);
  if (!user) {
    throw new Error("Email ou senha inválidos.");
  }
  const passwordHash = await hashPassword(password, user.password_salt);
  if (passwordHash !== user.password_hash) {
    throw new Error("Email ou senha inválidos.");
  }
  writeJson(LOCAL_SESSION_KEY, { user_id: user.id, signed_in_at: new Date().toISOString() });
  return publicUser(user);
}

export function signOut() {
  setApiToken("");
  if (!usesLocalNodeApi() && getCloudSession()) {
    // Revoke on the server too; the local session is cleared regardless.
    authLogout().catch(() => {});
  }
  setCloudSession(null);
  cloudUsers = null;
  localStorage.removeItem(LOCAL_SESSION_KEY);
}

// Confirms the stored cloud session is still valid and refreshes the user
// data. Returns false only when the server rejected the session; network
// failures keep the user signed in so a flaky connection does not log them out.
export async function verifySession() {
  if (usesLocalNodeApi() || !getCloudSession()) return true;
  try {
    const { user } = await authMe();
    const session = getCloudSession();
    if (session) setCloudSession({ ...session, user });
    authListUsers()
      .then((users) => {
        cloudUsers = users;
      })
      .catch(() => {});
    return true;
  } catch (error) {
    if (error?.status === 401) {
      setCloudSession(null);
      cloudUsers = null;
      return false;
    }
    return true;
  }
}
