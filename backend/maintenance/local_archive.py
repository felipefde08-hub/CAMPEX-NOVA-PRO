from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.config import Settings, get_data_dir
from backend.database.db import connect


ARCHIVE_TABLES = (
    "events",
    "investigations",
    "video_analyses",
    "notification_deliveries",
    "node_metrics",
    "node_sync_items",
    "state_transitions",
)


@dataclass(frozen=True)
class ArchiveResult:
    period: str
    archive_dir: Path
    database_backup: Path
    report_json: Path
    report_md: Path
    evidence_zip: Path | None
    deleted_rows: dict[str, int]
    dry_run: bool

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "archive_dir": str(self.archive_dir),
            "database_backup": str(self.database_backup),
            "report_json": str(self.report_json),
            "report_md": str(self.report_md),
            "evidence_zip": str(self.evidence_zip) if self.evidence_zip else None,
            "deleted_rows": self.deleted_rows,
            "dry_run": self.dry_run,
        }


def archive_month(
    settings: Settings,
    *,
    year: int,
    month: int,
    purge: bool = False,
) -> ArchiveResult:
    period = f"{year:04d}-{month:02d}"
    start = f"{period}-01T00:00:00"
    end = _next_month_start(year, month)
    archive_dir = get_data_dir() / "archives" / period
    archive_dir.mkdir(parents=True, exist_ok=True)

    database_backup = archive_dir / f"campex-{period}.sqlite3"
    _backup_database(settings.sqlite_path, database_backup)

    summary = _build_summary(settings, start, end, period)
    report_json = archive_dir / f"report-{period}.json"
    report_md = archive_dir / f"report-{period}.md"
    report_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    report_md.write_text(_render_markdown(summary), encoding="utf-8")

    evidence_zip = _archive_evidence(settings, start, end, archive_dir, period)
    deleted_rows = _purge_period(settings, start, end) if purge else {}
    if purge:
        _vacuum(settings.sqlite_path)

    return ArchiveResult(
        period=period,
        archive_dir=archive_dir,
        database_backup=database_backup,
        report_json=report_json,
        report_md=report_md,
        evidence_zip=evidence_zip,
        deleted_rows=deleted_rows,
        dry_run=not purge,
    )


def _backup_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def _build_summary(settings: Settings, start: str, end: str, period: str) -> dict:
    with connect(settings.sqlite_path) as connection:
        event_rows = connection.execute(
            """
            SELECT type, severity, status, COUNT(*) AS total
            FROM events
            WHERE started_at >= ? AND started_at < ?
            GROUP BY type, severity, status
            ORDER BY total DESC
            """,
            (start, end),
        ).fetchall()
        camera_rows = connection.execute(
            """
            SELECT camera_id, COUNT(*) AS total
            FROM events
            WHERE started_at >= ? AND started_at < ?
            GROUP BY camera_id
            ORDER BY total DESC
            """,
            (start, end),
        ).fetchall()
        totals = {
            "events": connection.execute(
                "SELECT COUNT(*) FROM events WHERE started_at >= ? AND started_at < ?",
                (start, end),
            ).fetchone()[0],
            "critical_events": connection.execute(
                "SELECT COUNT(*) FROM events WHERE severity = 'critical' AND started_at >= ? AND started_at < ?",
                (start, end),
            ).fetchone()[0],
            "investigations": connection.execute(
                "SELECT COUNT(*) FROM investigations WHERE created_at >= ? AND created_at < ?",
                (start, end),
            ).fetchone()[0],
            "video_analyses": connection.execute(
                "SELECT COUNT(*) FROM video_analyses WHERE created_at >= ? AND created_at < ?",
                (start, end),
            ).fetchone()[0],
        }

    return {
        "period": period,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": totals,
        "events_by_type": [dict(row) for row in event_rows],
        "events_by_camera": [dict(row) for row in camera_rows],
    }


def _render_markdown(summary: dict) -> str:
    totals = summary["totals"]
    lines = [
        f"# Relatorio CAMPEX {summary['period']}",
        "",
        f"Gerado em: {summary['generated_at']}",
        "",
        "## Resumo",
        "",
        f"- Eventos: {totals['events']}",
        f"- Eventos criticos: {totals['critical_events']}",
        f"- Investigacoes abertas/criadas: {totals['investigations']}",
        f"- Analises de video: {totals['video_analyses']}",
        "",
        "## Eventos por tipo",
        "",
    ]
    for row in summary["events_by_type"]:
        lines.append(f"- {row['type']} / {row['severity']} / {row['status']}: {row['total']}")
    lines.extend(["", "## Eventos por camera", ""])
    for row in summary["events_by_camera"]:
        lines.append(f"- {row['camera_id']}: {row['total']}")
    lines.append("")
    return "\n".join(lines)


def _archive_evidence(settings: Settings, start: str, end: str, archive_dir: Path, period: str) -> Path | None:
    evidence_paths: set[Path] = set()
    with connect(settings.sqlite_path) as connection:
        rows = connection.execute(
            "SELECT metadata FROM events WHERE started_at >= ? AND started_at < ?",
            (start, end),
        ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except json.JSONDecodeError:
            continue
        for key in ("snapshot_path", "overlay_path", "evidence_metadata_path"):
            raw = metadata.get(key)
            if raw:
                path = Path(raw)
                evidence_paths.add(path if path.is_absolute() else Path.cwd() / path)

    existing = [path.resolve() for path in evidence_paths if path.exists()]
    if not existing:
        return None
    target = archive_dir / f"evidence-{period}.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in existing:
            archive.write(path, arcname=path.name if path.is_file() else str(path))
    return target


def _purge_period(settings: Settings, start: str, end: str) -> dict[str, int]:
    deleted: dict[str, int] = {}
    with connect(settings.sqlite_path) as connection:
        for table in ARCHIVE_TABLES:
            timestamp_column = _timestamp_column(table)
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE {timestamp_column} >= ? AND {timestamp_column} < ?",
                (start, end),
            )
            deleted[table] = cursor.rowcount
        connection.commit()
    return deleted


def _timestamp_column(table: str) -> str:
    if table == "events":
        return "started_at"
    if table == "node_metrics":
        return "captured_at"
    if table == "state_transitions":
        return "occurred_at"
    if table == "node_sync_items":
        return "received_at"
    return "created_at"


def _vacuum(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("VACUUM")
