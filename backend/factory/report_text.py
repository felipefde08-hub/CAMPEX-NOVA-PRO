"""The factory report as text, for Telegram and e-mail."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


PERIOD_LABELS = {
    "today": "hoje",
    "yesterday": "ontem",
    "week": "esta semana",
    "last_week": "semana passada",
    "month": "este mês",
    "last_month": "mês passado",
}
MAX_MACHINES = 10


def format_factory_report(summaries: list[dict[str, Any]]) -> str:
    sections = [_node_section(item) for item in summaries]
    return "\n\n".join(["CAMPEX - Relatório da fábrica", *sections, "Gerado pela CAMPEX."])


def report_subject(summaries: list[dict[str, Any]]) -> str:
    period = PERIOD_LABELS.get(summaries[0]["period"], summaries[0]["period"]) if summaries else ""
    names = ", ".join(item["node_name"] for item in summaries[:2])
    return f"CAMPEX | Relatório da fábrica | {names} | {period}"


def _node_section(item: dict[str, Any]) -> str:
    summary = item["summary"]
    currency = summary.get("currency") or "BRL"
    current = summary["current"]
    zone = ZoneInfo(item.get("timezone") or "America/Sao_Paulo")
    start = datetime.fromisoformat(summary["start"]).astimezone(zone)
    end = datetime.fromisoformat(summary["end"]).astimezone(zone)
    lines = [
        f"{item['node_name']} · {PERIOD_LABELS.get(item['period'], item['period'])} "
        f"({start:%d/%m %H:%M} a {end:%d/%m %H:%M})",
        "",
    ]
    machines = summary.get("machines") or []
    if machines:
        lines += [
            f"Máquinas paradas: {duration(current['stopped_seconds'])} em {current['stops']} parada(s)"
            + _change(summary, "stopped_seconds"),
            f"Perda estimada: {money(current['lost_cost'], currency)}" + _change(summary, "lost_cost"),
        ]
        if current.get("cycles"):
            lines.append(f"Ciclos: {current['cycles']:,}".replace(",", "."))
    else:
        lines.append("Nenhuma máquina monitorada.")

    problems = summary.get("top_problems") or []
    if problems:
        lines += ["", "Maiores problemas:"]
        for index, problem in enumerate(problems, 1):
            cost = f", {money(problem['cost'], currency)}" if problem.get("cost") else ""
            lines.append(f"{index}. {problem['title']} — {duration(problem['seconds'])}{cost}")

    if machines:
        lines += ["", "Por máquina:"]
        for machine in machines[:MAX_MACHINES]:
            availability = machine.get("availability")
            available = f"{availability * 100:.0f}% disponível" if availability is not None else "sem dados"
            stopped = f", parada {duration(machine['stopped_seconds'])} ({machine['stops']}x)" if machine["stops"] else ""
            lines.append(f"• {machine['name']}: {available}{stopped}")
        if len(machines) > MAX_MACHINES:
            lines.append(f"• e mais {len(machines) - MAX_MACHINES} máquina(s) no painel.")

    others = [
        (current.get("missing_operator"), "máquina(s) sem operador"),
        (current.get("station_vacancies"), "posto(s) vazio(s)"),
        (current.get("restricted_entries"), "entrada(s) em área restrita"),
        (current.get("after_hours"), "presença(s) fora do horário"),
        (current.get("dock_visits"), "visita(s) na doca"),
    ]
    noted = [f"{count} {label}" for count, label in others if count]
    if noted:
        lines += ["", "Também: " + ", ".join(noted) + "."]
    return "\n".join(lines)


def duration(seconds: float | int | None) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60}h{minutes % 60:02d}"


def money(value: float | int | None, currency: str) -> str:
    amount = f"{float(value or 0):,.2f}"
    if currency == "BRL":
        return "R$ " + amount.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{currency} {amount}"


def _change(summary: dict[str, Any], key: str) -> str:
    change = (summary.get("changes") or {}).get(key)
    if change is None or abs(change) < 0.05:
        return ""
    arrow = "▲" if change > 0 else "▼"
    return f" ({arrow} {abs(change) * 100:.0f}% vs. período anterior)"
