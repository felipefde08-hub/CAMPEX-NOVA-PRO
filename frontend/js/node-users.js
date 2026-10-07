import { authChangePassword, authCreateUser, authDeleteUser, authUpdateUser } from "./api.js";
import { getCurrentUser, isAdmin, refreshUsers } from "./auth.js";

const ROLE_LABELS = { admin: "Administrador", operator: "Operador" };

// Accounts of the CAMPEX Node: the administrator creates the team's logins
// here; everyone can change their own password.
export async function renderNodeUsers(host, { notify, refreshIcons, settingsCard }) {
  if (!host) return;
  let users = [];
  try {
    users = await refreshUsers();
  } catch (error) {
    host.innerHTML = settingsCard("Usuários", "Contas de acesso a este CAMPEX Node.", "users", `<p>${escapeHtml(error.message)}</p>`);
    return;
  }
  const me = getCurrentUser();
  const admin = isAdmin();
  host.innerHTML = `
    ${settingsCard("Usuários", "Contas de acesso a este CAMPEX Node, inclusive pela rede da fábrica.", "users", `
      <div class="settings-integration-list">
        ${users.map((user) => `
          <article class="settings-integration-line" data-user-id="${user.id}">
            <div>
              <strong>${escapeHtml(user.name)}${user.id === me?.id ? " (você)" : ""}</strong>
              <span>${escapeHtml(user.email)}</span>
            </div>
            ${admin && user.id !== me?.id ? `
              <select data-user-role aria-label="Perfil de ${escapeHtml(user.name)}">
                ${Object.entries(ROLE_LABELS).map(([role, label]) => `<option value="${role}" ${role === user.role ? "selected" : ""}>${label}</option>`).join("")}
              </select>
              <button type="button" data-user-delete>Excluir</button>` : `<small data-state="connected">${ROLE_LABELS[user.role] || user.role}</small>`}
          </article>`).join("")}
      </div>
    `)}
    ${admin ? settingsCard("Nova conta", "A pessoa entra com este e-mail e senha, deste computador ou de outro na rede.", "user-plus", `
      <form id="node-user-form" class="settings-form">
        <div class="settings-form-grid">
          <label>Nome<input name="name" required minlength="2" placeholder="Nome da pessoa" /></label>
          <label>E-mail<input name="email" type="email" required placeholder="pessoa@empresa.com" /></label>
          <label>Senha inicial<input name="password" type="password" required minlength="8" autocomplete="new-password" /></label>
          <label>Perfil<select name="role">${Object.entries(ROLE_LABELS).map(([role, label]) => `<option value="${role}" ${role === "operator" ? "selected" : ""}>${label}</option>`).join("")}</select></label>
        </div>
        <div class="settings-action-bar"><button type="submit" class="primary-action">Criar conta</button></div>
      </form>
    `) : ""}
    ${settingsCard("Minha senha", "Ao trocar a senha, você precisa entrar de novo em todos os aparelhos.", "key-round", `
      <form id="node-password-form" class="settings-form">
        <div class="settings-form-grid">
          <label>Senha atual<input name="current" type="password" required autocomplete="current-password" /></label>
          <label>Nova senha<input name="next" type="password" required minlength="8" autocomplete="new-password" /></label>
        </div>
        <div class="settings-action-bar"><button type="submit" class="primary-action">Trocar senha</button></div>
      </form>
    `)}
  `;
  refreshIcons();

  const rerender = () => renderNodeUsers(host, { notify, refreshIcons, settingsCard });
  host.querySelector("#node-user-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      await authCreateUser({
        name: form.elements.name.value.trim(),
        email: form.elements.email.value.trim(),
        password: form.elements.password.value,
        role: form.elements.role.value,
      });
      notify("Conta criada", `${form.elements.email.value.trim()} já pode entrar.`, "success");
      await rerender();
    } catch (error) {
      notify("Não foi possível criar a conta", error.message, "error");
    }
  });
  host.querySelectorAll("[data-user-role]").forEach((select) => {
    select.addEventListener("change", async () => {
      const userId = select.closest("[data-user-id]").dataset.userId;
      try {
        await authUpdateUser(userId, { role: select.value });
        notify("Perfil atualizado", "", "success");
      } catch (error) {
        notify("Não foi possível alterar o perfil", error.message, "error");
      }
      await rerender();
    });
  });
  host.querySelectorAll("[data-user-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-user-id]");
      const name = row.querySelector("strong").textContent;
      if (!window.confirm(`Excluir a conta de ${name}? A pessoa perde o acesso na hora.`)) return;
      try {
        await authDeleteUser(row.dataset.userId);
        notify("Conta excluída", "", "success");
      } catch (error) {
        notify("Não foi possível excluir", error.message, "error");
      }
      await rerender();
    });
  });
  host.querySelector("#node-password-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      await authChangePassword(form.elements.current.value, form.elements.next.value);
      notify("Senha trocada", "Entre de novo com a nova senha.", "success");
      window.dispatchEvent(new CustomEvent("campex:session-expired"));
    } catch (error) {
      notify("Não foi possível trocar a senha", error.message, "error");
    }
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
