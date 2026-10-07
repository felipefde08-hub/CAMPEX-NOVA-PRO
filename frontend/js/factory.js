import {
  addProductionCount,
  createShift,
  deleteShift,
  findRecording,
  getFactoryAnalytics,
  getFactoryLive,
  getFactorySettings,
  getMachineSpeed,
  getRecordingStatus,
  listShifts,
  listZones,
  recordingFileUrl,
  updateFactorySettings,
} from "./api.js";

const PERIODS = [
  ["today", "Hoje"],
  ["yesterday", "Ontem"],
  ["week", "Esta semana"],
  ["last_week", "Semana passada"],
  ["month", "Este mês"],
  ["last_month", "Mês passado"],
];

const TABS = [
  ["summary", "Resumo", "layout-dashboard"],
  ["machines", "Máquinas", "factory"],
  ["people", "Pessoas", "users"],
  ["security", "Segurança", "shield-alert"],
  ["logistics", "Expedição", "truck"],
  ["setup", "Configuração", "settings"],
];

const WEEKDAYS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"];
const STATE_LABELS = {
  RUNNING: ["Rodando", "info"],
  STOPPED: ["Parada", "critical"],
  UNKNOWN: ["Sem imagem", "attention"],
  OCCUPIED: ["Ocupada", "info"],
  VACANT: ["Vazia", "attention"],
};

let ui = { notify: () => {}, refreshIcons: () => {} };
let view = { tab: "summary", period: "today", currency: "BRL" };

export async function renderFactoryPage(appView, helpers) {
  ui = { ...ui, ...helpers };
  appView.innerHTML = `
    <div class="ops-page factory-page">
      <section class="ops-toolbar factory-toolbar">
        <nav class="factory-tabs" aria-label="Seções da fábrica">
          ${TABS.map(([id, label, icon]) => `
            <button type="button" data-factory-tab="${id}" aria-selected="${id === view.tab}">
              <i data-lucide="${icon}" aria-hidden="true"></i><span>${label}</span>
            </button>`).join("")}
        </nav>
        <label>Período
          <select id="factory-period">
            ${PERIODS.map(([id, label]) => `<option value="${id}" ${id === view.period ? "selected" : ""}>${label}</option>`).join("")}
          </select>
        </label>
        <button type="button" id="factory-refresh"><i data-lucide="refresh-cw" aria-hidden="true"></i><span>Atualizar</span></button>
      </section>
      <section id="factory-panel" aria-live="polite"></section>
      <section class="modal-layer" id="factory-video" role="dialog" aria-modal="true" aria-labelledby="factory-video-title" hidden>
        <div class="modal-backdrop" data-close-video></div>
        <div class="modal-window factory-video-window">
          <header class="modal-header">
            <div><h2 id="factory-video-title">Gravação</h2><p id="factory-video-detail"></p></div>
            <button type="button" class="icon-button" data-close-video aria-label="Fechar vídeo"><i data-lucide="x" aria-hidden="true"></i></button>
          </header>
          <video id="factory-video-player" controls autoplay playsinline></video>
        </div>
      </section>
    </div>
  `;
  appView.querySelectorAll("[data-factory-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      view.tab = button.dataset.factoryTab;
      appView.querySelectorAll("[data-factory-tab]").forEach((item) => {
        item.setAttribute("aria-selected", String(item === button));
      });
      loadTab();
    });
  });
  appView.querySelector("#factory-period").addEventListener("change", (event) => {
    view.period = event.currentTarget.value;
    loadTab();
  });
  appView.querySelector("#factory-refresh").addEventListener("click", loadTab);
  appView.querySelectorAll("[data-close-video]").forEach((item) => item.addEventListener("click", closeVideo));
  const panel = appView.querySelector("#factory-panel");
  panel.addEventListener("click", handlePanelClick);
  panel.addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(event.target);
  });
  panel.addEventListener("change", (event) => {
    if (["factory-day", "factory-min-stop"].includes(event.target.id)) loadTab();
  });
  ui.refreshIcons();
  try {
    view.currency = (await getFactorySettings()).currency || "BRL";
  } catch {
    // The panel shows the connection error below.
  }
  await loadTab();
}

async function loadTab() {
  const panel = document.querySelector("#factory-panel");
  if (!panel) return;
  panel.innerHTML = `<div class="empty-state"><strong>Carregando…</strong><span>Consultando o CAMPEX Node.</span></div>`;
  const loaders = {
    summary: loadSummary,
    machines: loadMachines,
    people: loadPeople,
    security: loadSecurity,
    logistics: loadLogistics,
    setup: loadSetup,
  };
  try {
    panel.innerHTML = await loaders[view.tab]();
  } catch (error) {
    panel.innerHTML = empty(
      "Não foi possível falar com o CAMPEX Node",
      `${escapeHtml(error.message)}. Os dados da fábrica ficam no Node instalado na planta: abra este painel pelo Node ou no computador onde ele roda.`,
    );
  }
  ui.refreshIcons();
}

// Summary

async function loadSummary() {
  const [summary, live, shifts] = await Promise.all([
    analytics("summary"),
    getFactoryLive(),
    listShifts(),
  ]);
  const current = summary.current;
  const changes = summary.changes || {};
  const notices = [];
  if (!shifts.length) notices.push("Cadastre os turnos em Configuração para medir custo, fora do expediente e intervalos.");
  if (!summary.machines.length) notices.push("Desenhe zonas do tipo Máquina em Áreas & Zonas para acompanhar paradas e ciclos.");
  return `
    ${notices.map((text) => `<div class="factory-notice"><i data-lucide="info" aria-hidden="true"></i><span>${text}</span></div>`).join("")}
    <section class="settings-grid">
      ${metric("Paradas", current.stops, `${duration(current.stopped_seconds)} parado ${change(changes.stops, true)}`)}
      ${metric("Custo das paradas", money(current.lost_cost), change(changes.lost_cost, true))}
      ${metric("Ciclos", number(current.cycles), change(changes.cycles))}
      ${metric("Eventos", current.events, `${current.restricted_entries} em área restrita · ${current.after_hours} fora do horário`)}
    </section>
    <section class="ops-grid">
      <div>
        ${heading("Os 3 problemas que mais custaram", `${summary.top_problems.length} itens`)}
        <div class="ops-list">
          ${summary.top_problems.length
            ? summary.top_problems.map((item, index) => `
              <article class="ops-row">
                <div><strong>${index + 1}. ${escapeHtml(item.title)}</strong><span>${duration(item.seconds)}</span></div>
                <span class="severity-badge" data-severity="${item.cost ? "critical" : "attention"}">${item.cost ? money(item.cost) : "sem custo cadastrado"}</span>
              </article>`).join("")
            : empty("Nenhum problema no período", "Sem paradas nem estações vazias registradas.")}
        </div>
        ${heading("Máquinas agora", live.shift ? `Turno ${escapeHtml(live.shift.shift)}` : live.working_time === false ? "Fora do expediente" : "")}
        <div class="factory-live">
          ${live.machines.length
            ? live.machines.map((item) => `
              <article class="factory-live-item">
                <strong>${escapeHtml(item.name)}</strong>
                ${stateBadge(item.state)}
                <small>${item.since ? `desde ${time(item.since)}` : ""}</small>
              </article>`).join("")
            : empty("Nenhuma máquina monitorada", "Crie zonas do tipo Máquina.")}
        </div>
      </div>
      <aside class="ops-detail">
        <h2>Comparado ao período anterior</h2>
        <div class="object-list">
          ${line("Paradas", `${current.stops} (antes ${summary.previous.stops})`, change(changes.stops, true))}
          ${line("Tempo parado", `${duration(current.stopped_seconds)} (antes ${duration(summary.previous.stopped_seconds)})`, change(changes.stopped_seconds, true))}
          ${line("Ciclos", `${number(current.cycles)} (antes ${number(summary.previous.cycles)})`, change(changes.cycles))}
          ${line("Estações vazias", `${current.station_vacancies} (antes ${summary.previous.station_vacancies})`, "")}
          ${line("Sem operador", `${current.missing_operator} (antes ${summary.previous.missing_operator})`, "")}
          ${line("Caminhões na doca", `${current.dock_visits} (antes ${summary.previous.dock_visits})`, "")}
        </div>
      </aside>
    </section>
  `;
}

// Machines

async function loadMachines() {
  const minMinutes = Number(document.querySelector("#factory-min-stop")?.value || 15);
  const [report, stops, shifts, hourly, live] = await Promise.all([
    analytics("machines"),
    analytics("stops", { min_minutes: minMinutes }),
    analytics("shifts"),
    analytics("hourly"),
    getFactoryLive(),
  ]);
  const liveById = Object.fromEntries(live.machines.map((item) => [item.zone_id, item]));
  const maxCycles = Math.max(1, ...hourly.hours.map((item) => item.cycles));
  return `
    ${heading("Máquinas", `${report.machines.length} monitoradas`)}
    ${report.machines.length ? `
    <div class="factory-table-wrap">
      <table class="factory-table">
        <thead><tr>
          <th>Máquina</th><th>Agora</th><th>Disponível</th><th>Parada</th><th>Paradas</th>
          <th>Maior parada</th><th>Ciclos</th><th>Ciclos/h</th><th>Custo</th><th></th>
        </tr></thead>
        <tbody>
          ${report.machines.map((item) => `
            <tr>
              <td><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.line || "")}</small></td>
              <td>${stateBadge(liveById[item.zone_id]?.state || "UNKNOWN")}</td>
              <td>${item.availability == null ? "-" : percent(item.availability)}</td>
              <td>${duration(item.stopped_seconds)}</td>
              <td>${item.stops}</td>
              <td>${item.longest_stop ? `${duration(item.longest_stop.duration_seconds)} <small>${time(item.longest_stop.started_at)}</small>` : "-"}</td>
              <td>${number(item.cycles)}</td>
              <td>${item.cycles_per_hour ?? "-"}</td>
              <td>${money(item.lost_cost)}</td>
              <td><button type="button" data-speed="${item.zone_id}" data-name="${escapeHtml(item.name)}">Ritmo</button></td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>
    <div id="factory-speed"></div>` : empty("Nenhuma máquina cadastrada", "Em Áreas & Zonas, desenhe a área de cada máquina com o tipo Máquina.")}
    <section class="ops-grid">
      <div>
        <div class="section-heading">
          <h2>Paradas</h2>
          <label class="factory-inline">acima de <input id="factory-min-stop" type="number" min="0" value="${minMinutes}" /> min</label>
        </div>
        <div class="ops-list">
          ${stops.length ? stops.map((item) => `
            <article class="ops-row">
              <div>
                <strong>${escapeHtml(item.name)} · ${duration(item.duration_seconds)}</strong>
                <span>${dateTime(item.started_at)}${item.ongoing ? " · ainda parada" : ` até ${time(item.ended_at)}`}${item.shift ? ` · ${escapeHtml(item.shift)}` : ""}</span>
              </div>
              <span class="severity-badge" data-severity="critical">${money(item.cost)}</span>
              ${videoButton(item.camera_id, item.recording, "Ver 2 min antes")}
            </article>`).join("") : empty("Nenhuma parada", `Nenhuma parada acima de ${minMinutes} min no período.`)}
        </div>
      </div>
      <aside class="ops-detail">
        <h2>Por turno</h2>
        <div class="object-list">
          ${shifts.by_shift.length ? shifts.by_shift.map((item) => line(
            escapeHtml(item.shift),
            `${number(item.cycles)} ciclos · ${item.stops} paradas`,
            `${duration(item.stopped_seconds)} parado em ${item.occurrences} turno(s)`,
          )).join("") : empty("Sem turnos", "Cadastre os turnos em Configuração.")}
          ${shifts.most_productive ? line("Produz mais", escapeHtml(shifts.most_productive), "") : ""}
          ${shifts.most_downtime ? line("Para mais", escapeHtml(shifts.most_downtime), "") : ""}
        </div>
        <h2>Horários de menor produção</h2>
        <div class="object-list">
          ${hourly.lowest_hours.length ? hourly.lowest_hours.map((item) => line(
            `${String(item.hour_of_day).padStart(2, "0")}h`, `${item.avg_cycles} ciclos/h em média`, `${item.samples} hora(s) observada(s)`,
          )).join("") : empty("Sem dados", "Ainda não há ciclos registrados.")}
        </div>
      </aside>
    </section>
    ${hourly.hours.length ? `
      ${heading("Ciclos por hora", "")}
      <div class="factory-bars" role="img" aria-label="Ciclos por hora">
        ${hourly.hours.map((item) => `
          <div class="factory-bar" title="${item.local_hour}: ${item.cycles} ciclos, ${duration(item.stopped_seconds)} parado">
            <span style="height:${Math.round((item.cycles / maxCycles) * 100)}%"></span>
            <small>${item.local_hour.slice(11, 13)}h</small>
          </div>`).join("")}
      </div>` : ""}
  `;
}

// People

async function loadPeople() {
  const day = document.querySelector("#factory-day")?.value || "";
  const [occupancy, arrival, breaks] = await Promise.all([
    analytics("occupancy", { min_vacant_minutes: 5 }),
    getFactoryAnalytics("first-arrival", { date: day }),
    getFactoryAnalytics("breaks", { date: day }),
  ]);
  const zones = occupancy.filter((item) => ["station", "machine"].includes(item.type));
  return `
    <section class="ops-toolbar"><label>Dia<input id="factory-day" type="date" value="${arrival.date}" /></label></section>
    <section class="settings-grid">
      ${metric("Primeiro a chegar", arrival.first ? arrival.first.local_time : "-", arrival.first ? escapeHtml(arrival.first.name) : "ninguém nas estações")}
      ${metric("Intervalos estourados", breaks.filter((item) => item.overrun).length, `de ${breaks.length} intervalo(s)`)}
      ${metric("Estações monitoradas", zones.length, "estações e postos de máquina")}
    </section>
    <section class="ops-grid">
      <div>
        ${heading("Ocupação das estações", zones.length && zones[0].within_shifts ? "dentro dos turnos" : "período inteiro")}
        <div class="ops-list">
          ${zones.length ? zones.map((zone) => `
            <article class="ops-row factory-occupancy">
              <div>
                <strong>${escapeHtml(zone.name)}</strong>
                <span>Ocupada ${duration(zone.occupied_seconds)} · vazia ${duration(zone.vacant_seconds)}</span>
                ${zone.vacancies.slice(0, 5).map((item) => `
                  <small>Vazia ${time(item.started_at)}–${item.ended_at ? time(item.ended_at) : "agora"} (${duration(item.duration_seconds)})
                    ${videoButton(zone.camera_id, item.recording, "vídeo", true)}</small>`).join("")}
              </div>
              <div class="factory-ratio"><span style="width:${ratio(zone.occupied_seconds, zone.vacant_seconds)}%"></span></div>
            </article>`).join("") : empty("Sem estações", "Desenhe zonas do tipo Estação (ou Máquina) em Áreas & Zonas.")}
        </div>
      </div>
      <aside class="ops-detail">
        <h2>Intervalos</h2>
        <div class="object-list">
          ${breaks.length ? breaks.map((item) => line(
            `${escapeHtml(item.name)} (${escapeHtml(item.shift)})`,
            item.overrun ? `Passou ${duration(item.max_late_seconds)}` : item.max_late_seconds == null ? "Ninguém voltou às estações" : "Dentro do horário",
            item.stations.map((station) => `${escapeHtml(station.name)}: ${time(station.back_at)}`).join(" · "),
          )).join("") : empty("Sem intervalos", "Cadastre intervalos nos turnos.")}
        </div>
        <h2>Chegada por estação</h2>
        <div class="object-list">
          ${arrival.zones.length ? arrival.zones.map((item) => line(escapeHtml(item.name), item.local_time, "")).join("") : empty("Sem chegadas", "Nenhuma presença registrada no dia.")}
        </div>
      </aside>
    </section>
  `;
}

// Security

async function loadSecurity() {
  const [afterHours, summary] = await Promise.all([analytics("after-hours"), analytics("summary")]);
  const presences = afterHours.presences;
  return `
    ${afterHours.schedule_configured ? "" : `<div class="factory-notice"><i data-lucide="info" aria-hidden="true"></i><span>Sem turnos cadastrados o Node não sabe o que é fora do expediente. Cadastre-os em Configuração.</span></div>`}
    <section class="settings-grid">
      ${metric("Fora do expediente", presences.length, `${presences.filter((item) => item.subject === "vehicle").length} com veículo`)}
      ${metric("Área restrita", summary.current.restricted_entries, "entradas no período")}
      ${metric("Sem operador", summary.current.missing_operator, "máquina rodando sem ninguém")}
    </section>
    ${heading("Movimento fora do expediente", `${presences.length} registros`)}
    <div class="ops-list">
      ${presences.length ? presences.map((item) => `
        <article class="ops-row">
          <div>
            <strong>${item.subject === "vehicle" ? "Veículo" : "Pessoa"} em ${escapeHtml(item.zone_name)}</strong>
            <span>${dateTime(item.started_at)}${item.ended_at ? ` até ${time(item.ended_at)}` : " · em andamento"} · ${duration(item.outside_seconds)} fora do horário</span>
          </div>
          <span class="severity-badge" data-severity="critical">${zoneTypeLabel(item.zone_type)}</span>
          ${videoButton(item.camera_id, item.recording, "Ver vídeo")}
        </article>`).join("") : empty("Nada fora do expediente", "Nenhuma pessoa ou veículo nas zonas fora dos turnos.")}
    </div>
    <p class="factory-hint">Para saber quem mexeu numa máquina à noite, desenhe a área da máquina (tipo Máquina) e a área ao redor (tipo Área): qualquer presença fora do turno aparece aqui com o vídeo.</p>
  `;
}

// Logistics

async function loadLogistics() {
  const [docks, lines] = await Promise.all([analytics("docks"), analytics("lines")]);
  return `
    <section class="settings-grid">
      ${metric("Caminhões na doca", docks.count, "visitas acima do tempo mínimo")}
      ${metric("Tempo médio na doca", docks.avg_dwell_seconds == null ? "-" : duration(docks.avg_dwell_seconds), "")}
      ${metric("Linhas de contagem", lines.length, `${number(lines.reduce((total, item) => total + item.count, 0))} passagens`)}
    </section>
    <section class="ops-grid">
      <div>
        ${heading("Visitas às docas", `${docks.count} no período`)}
        <div class="ops-list">
          ${docks.visits.length ? docks.visits.map((item) => `
            <article class="ops-row">
              <div>
                <strong>${escapeHtml(item.dock || "Doca")}</strong>
                <span>Chegou ${dateTime(item.arrived_at)} · ${item.ongoing ? "ainda na doca" : `saiu ${time(item.left_at)}`} · ${duration(item.dwell_seconds || 0)}</span>
              </div>
              ${videoButton(null, item.recording, "Ver carregamento", false, item.event_id)}
            </article>`).join("") : empty("Nenhuma visita", "Desenhe zonas do tipo Doca na câmera da expedição.")}
        </div>
      </div>
      <aside class="ops-detail">
        <h2>Linhas de contagem</h2>
        <div class="object-list">
          ${lines.length ? lines.map((item) => line(
            escapeHtml(item.name),
            `${number(item.count)} ${item.subject === "vehicle" ? "veículos" : item.subject === "any" ? "passagens" : "pessoas"}`,
            `A→B ${item.forward} · B→A ${item.backward}`,
          )).join("") : empty("Sem linhas", "Desenhe uma linha (tipo Linha de contagem) num portão ou esteira.")}
        </div>
      </aside>
    </section>
  `;
}

// Setup

async function loadSetup() {
  const [settings, shifts, recording, zones] = await Promise.all([
    getFactorySettings(),
    listShifts(),
    getRecordingStatus(),
    listZones(),
  ]);
  const countable = zones.filter((zone) => ["machine", "line"].includes(zone.type));
  const cameras = Object.entries(recording.cameras || {});
  return `
    <section class="ops-grid">
      <div>
        ${heading("Turnos", `${shifts.length} cadastrados`)}
        <div class="ops-list">
          ${shifts.length ? shifts.map((shift) => `
            <article class="ops-row">
              <div>
                <strong>${escapeHtml(shift.name)} · ${shift.start}–${shift.end}</strong>
                <span>${shift.days.map((day) => WEEKDAYS[day]).join(", ")}</span>
                ${shift.breaks.length ? `<small>${shift.breaks.map((item) => `${escapeHtml(item.name)} ${item.start}–${item.end}`).join(" · ")}</small>` : ""}
              </div>
              <div class="row-actions"><button type="button" data-delete-shift="${shift.id}">Excluir</button></div>
            </article>`).join("") : empty("Nenhum turno", "Sem turnos não há custo de parada por turno, fora do expediente nem controle de intervalo.")}
        </div>
        <form id="factory-shift-form" class="stack-form factory-shift-form">
          <h3>Novo turno</h3>
          <label>Nome<input name="name" required placeholder="Manhã" /></label>
          <div class="factory-pair">
            <label>Início<input name="start" type="time" required value="06:00" /></label>
            <label>Fim<input name="end" type="time" required value="14:00" /></label>
          </div>
          <fieldset class="factory-days"><legend>Dias</legend>
            ${WEEKDAYS.map((label, index) => `<label class="inline-toggle"><input type="checkbox" name="days" value="${index}" ${index < 5 ? "checked" : ""} /> ${label}</label>`).join("")}
          </fieldset>
          <div class="factory-pair">
            <label>Intervalo<input name="break_name" placeholder="Almoço" /></label>
            <label>De<input name="break_start" type="time" /></label>
            <label>Até<input name="break_end" type="time" /></label>
          </div>
          <button type="submit">Salvar turno</button>
        </form>
      </div>
      <aside class="ops-detail">
        <h2>Fábrica</h2>
        <form id="factory-settings-form" class="stack-form">
          <label>Fuso horário<input name="timezone" value="${escapeHtml(settings.timezone)}" /></label>
          <label>Moeda<input name="currency" maxlength="3" value="${escapeHtml(settings.currency)}" /></label>
          <button type="submit">Salvar</button>
        </form>
        <h2>Gravação contínua</h2>
        <div class="object-list">
          ${line("Situação", recording.enabled ? "Ligada" : "Desligada", escapeHtml(recording.path || ""))}
          ${line("Disco", recording.disk?.free_gb == null ? "-" : `${recording.disk.free_gb} GB livres de ${recording.disk.total_gb} GB`, `mantém ${recording.min_free_gb} GB livres · até ${recording.retention_days} dias`)}
          ${line("Gravado", bytes(recording.total_bytes || 0), "")}
          ${cameras.map(([cameraId, item]) => line(
            escapeHtml(cameraId),
            { RECORDING: "Gravando", STARTING: "Conectando", ERROR: "Falha", STOPPED: "Parada" }[item.status] || "-",
            item.error ? escapeHtml(item.error) : item.first ? `desde ${dateTime(item.first)}` : "",
          )).join("")}
        </div>
        <h2>Produção manual / CLP</h2>
        ${countable.length ? `
        <form id="factory-count-form" class="stack-form">
          <label>Máquina ou linha
            <select name="zone_id">${countable.map((zone) => `<option value="${zone.id}">${escapeHtml(zone.name)}</option>`).join("")}</select>
          </label>
          <label>Peças<input name="count" type="number" min="1" required /></label>
          <button type="submit">Lançar</button>
          <small>Integrações enviam o mesmo dado para POST /api/production/counts.</small>
        </form>` : empty("Sem máquinas", "Crie zonas do tipo Máquina para lançar produção.")}
      </aside>
    </section>
  `;
}

// Interactions (delegated: the panel is re-rendered on every load)

async function handlePanelClick(event) {
  const target = event.target.closest("button");
  if (!target) return;
  if (target.dataset.video !== undefined) {
    await openVideo(target.dataset);
  } else if (target.dataset.speed) {
    await showSpeed(target.dataset.speed, target.dataset.name);
  } else if (target.dataset.deleteShift) {
    await deleteShift(target.dataset.deleteShift);
    ui.notify("Turno excluído", "", "success");
    await loadTab();
  }
}

async function submitForm(form) {
  if (!form || !form.reportValidity()) return;
  const data = new FormData(form);
  try {
    if (form.id === "factory-shift-form") {
      const breaks = data.get("break_start") && data.get("break_end")
        ? [{ name: data.get("break_name") || "Intervalo", start: data.get("break_start"), end: data.get("break_end") }]
        : [];
      await createShift({
        name: data.get("name"),
        start: data.get("start"),
        end: data.get("end"),
        days: data.getAll("days").map(Number),
        breaks,
      });
      ui.notify("Turno salvo", "", "success");
    } else if (form.id === "factory-settings-form") {
      const saved = await updateFactorySettings({ timezone: data.get("timezone"), currency: data.get("currency") });
      view.currency = saved.currency;
      ui.notify("Configuração salva", "", "success");
    } else if (form.id === "factory-count-form") {
      await addProductionCount({ zone_id: data.get("zone_id"), count: Number(data.get("count")) });
      ui.notify("Produção lançada", "", "success");
    }
    await loadTab();
  } catch (error) {
    ui.notify("Não foi possível salvar", error.message, "error");
  }
}

async function showSpeed(zoneId, name) {
  const host = document.querySelector("#factory-speed");
  if (!host) return;
  const speed = await getMachineSpeed(zoneId, { period: view.period });
  const changeText = speed.change == null ? "sem base de comparação" : `${speed.change < 0 ? "mais lenta" : "mais rápida"} (${percent(Math.abs(speed.change))})`;
  host.innerHTML = `
    <div class="factory-notice">
      <i data-lucide="gauge" aria-hidden="true"></i>
      <span><strong>${escapeHtml(name)}</strong>: ${speed.current.cycles_per_hour ?? "-"} ciclos/h no período contra ${speed.baseline.cycles_per_hour ?? "-"} ciclos/h nos ${speed.baseline_days} dias anteriores — ${changeText}.</span>
    </div>`;
  ui.refreshIcons();
}

async function openVideo({ camera, segment, offset, at, event: eventId }) {
  let segmentId = segment;
  let seek = Number(offset || 0);
  try {
    if (!segmentId && camera && at) {
      const found = await findRecording(camera, at);
      segmentId = found.segment.id;
      seek = found.offset_seconds;
    }
  } catch {
    segmentId = "";
  }
  if (!segmentId) {
    ui.notify(
      "Sem gravação desse momento",
      eventId ? "Abra o evento em Eventos para ver o clipe." : "A gravação contínua não cobre esse horário.",
      "warning",
    );
    return;
  }
  const modal = document.querySelector("#factory-video");
  const player = document.querySelector("#factory-video-player");
  document.querySelector("#factory-video-detail").textContent = at ? dateTime(at) : "";
  player.src = recordingFileUrl(segmentId, seek);
  modal.hidden = false;
}

function closeVideo() {
  const player = document.querySelector("#factory-video-player");
  if (player) {
    player.pause();
    player.removeAttribute("src");
    player.load();
  }
  document.querySelector("#factory-video").hidden = true;
}

// Rendering helpers

function analytics(kind, params = {}) {
  return getFactoryAnalytics(kind, { period: view.period, ...params });
}

function videoButton(cameraId, recording, label, compact = false, eventId = "") {
  if (!recording && !cameraId && !eventId) return "";
  const attrs = recording
    ? `data-segment="${recording.segment_id}" data-offset="${recording.offset_seconds}" data-at="${recording.at}"`
    : `data-camera="${escapeHtml(cameraId || "")}" data-event="${escapeHtml(eventId || "")}"`;
  return `<button type="button" class="${compact ? "link-button" : ""}" data-video ${attrs} ${recording ? "" : "title=\"Sem gravação contínua desse momento\""}>
    <i data-lucide="play" aria-hidden="true"></i><span>${label}</span></button>`;
}

function stateBadge(state) {
  const [label, severity] = STATE_LABELS[state] || [state || "-", "attention"];
  return `<span class="severity-badge" data-severity="${severity}">${label}</span>`;
}

function zoneTypeLabel(type) {
  return { machine: "Máquina", station: "Estação", dock: "Doca", area: "Área", monitored: "Monitorada", restricted: "Restrita" }[type] || type;
}

function metric(title, value, detail) {
  return `<article class="metric-item"><span>${title}</span><strong>${value}</strong><small>${detail || ""}</small></article>`;
}

function line(title, value, detail) {
  return `<article class="insight-line"><div><strong>${title}</strong><span>${value}</span><small>${detail || ""}</small></div></article>`;
}

function heading(title, detail) {
  return `<div class="section-heading"><h2>${title}</h2><span>${detail || ""}</span></div>`;
}

function empty(title, detail) {
  return `<div class="empty-state"><strong>${title}</strong><span>${detail}</span></div>`;
}

function change(value, lowerIsBetter = false) {
  if (value == null) return "";
  const better = lowerIsBetter ? value < 0 : value > 0;
  const sign = value > 0 ? "+" : "";
  return `<span class="factory-change" data-better="${better}">${sign}${Math.round(value * 100)}% vs antes</span>`;
}

function duration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours) return `${hours}h${String(minutes).padStart(2, "0")}`;
  if (minutes) return `${minutes} min`;
  return `${total} s`;
}

function money(value) {
  try {
    return new Intl.NumberFormat("pt-BR", { style: "currency", currency: view.currency }).format(value || 0);
  } catch {
    return `${view.currency} ${Number(value || 0).toFixed(2)}`;
  }
}

function number(value) {
  return new Intl.NumberFormat("pt-BR").format(value || 0);
}

function percent(value) {
  return `${Math.round(value * 100)}%`;
}

function ratio(occupied, vacant) {
  const total = occupied + vacant;
  return total ? Math.round((occupied / total) * 100) : 0;
}

function bytes(value) {
  const gb = value / 1024 ** 3;
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.round(value / 1024 ** 2)} MB`;
}

function time(value) {
  return value ? new Date(value).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }) : "-";
}

function dateTime(value) {
  return value ? new Date(value).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "-";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
