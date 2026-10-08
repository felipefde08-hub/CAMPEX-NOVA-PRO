"""Limpeza automática de dados operacionais antigos.

Roda sozinha, sem ação manual:
- no modo local, numa thread de fundo iniciada junto com o backend;
- no modo serverless, depois das sincronizações enviadas pelos Nodes;
- no cron da Vercel que já existe para notificações.

Uma marca em ``app_meta`` garante no máximo uma execução a cada
``retention_interval_hours``, mesmo com várias instâncias ou gatilhos.
Funciona em SQLite e em Postgres.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.config import Settings, get_data_dir
from backend.database.db import connect


logger = logging.getLogger("campex.retention")

LAST_RUN_KEY = "retention_last_run_at"
DELETE_BATCH_SIZE = 20_000
SQLITE_VACUUM_AFTER_ROWS = 100_000

# (tabela, coluna de data, campo de Settings com os dias de retenção).
# Eventos não entram: são o histórico do cliente e ocupam pouco.
RETENTION_TABLES = (
    ("node_metrics", "captured_at", "retention_metrics_days"),
    ("node_sync_items", "received_at", "retention_metrics_days"),
    ("notification_deliveries", "created_at", "retention_history_days"),
    ("state_transitions", "occurred_at", "retention_history_days"),
)

_run_lock = threading.Lock()


def run_retention_if_due(settings: Settings, *, now: datetime | None = None) -> dict | None:
    """Executa a limpeza se o intervalo passou. Nunca levanta exceção."""
    if not _run_lock.acquire(blocking=False):
        return None
    try:
        now = now or datetime.now(timezone.utc)
        if not _claim_run(settings, now):
            return None
        return run_retention(settings, now=now)
    except Exception:
        logger.exception("Automatic retention cleanup failed")
        return None
    finally:
        _run_lock.release()


def run_retention(settings: Settings, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    # SQLite identifica linhas por rowid; Postgres, por ctid.
    row_id = "ctid" if settings.uses_postgres else "rowid"
    deleted: dict[str, int] = {}
    with connect(settings.database_target) as connection:
        for table, column, days_field in RETENTION_TABLES:
            days = getattr(settings, days_field)
            if days <= 0:
                continue
            # Prefixo de data compara certo tanto com ISO 8601 quanto com
            # CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS"), nos dois bancos.
            cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
            deleted[table] = _delete_in_batches(connection, table, column, cutoff, row_id)

    total_rows = sum(deleted.values())
    # Postgres recupera o espaço sozinho (autovacuum); SQLite precisa de VACUUM
    # para devolver ao disco o espaço de uma limpeza grande.
    vacuumed = not settings.uses_postgres and total_rows >= SQLITE_VACUUM_AFTER_ROWS
    if vacuumed:
        with connect(settings.database_target) as connection:
            connection.execute("VACUUM")

    evidence_files = 0
    if settings.retention_evidence_days > 0:
        evidence_files = delete_evidence_files(
            get_data_dir() / "evidence",
            older_than=now - timedelta(days=settings.retention_evidence_days),
        )["deleted_files"]

    result = {
        "ran_at": now.isoformat(),
        "deleted_rows": deleted,
        "deleted_evidence_files": evidence_files,
        "vacuumed": vacuumed,
    }
    logger.info("Retention cleanup finished: %s", result)
    return result


def delete_evidence_files(root: Path, *, older_than: datetime | None) -> dict:
    """Apaga arquivos de evidência anteriores a ``older_than`` (None apaga todos)."""
    if not root.exists():
        return {"deleted_files": 0, "deleted_bytes": 0}
    deleted_files = 0
    deleted_bytes = 0
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            if older_than is None or modified < older_than:
                deleted_bytes += stat.st_size
                path.unlink(missing_ok=True)
                deleted_files += 1
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    return {"deleted_files": deleted_files, "deleted_bytes": deleted_bytes}


class RetentionWorker:
    """Thread de fundo do modo local: verifica a cada 30 min se a limpeza venceu."""

    def __init__(self, settings: Settings, check_seconds: float = 1800.0) -> None:
        self.settings = settings
        self.check_seconds = check_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="campex-retention", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            run_retention_if_due(self.settings)
            self._stop.wait(self.check_seconds)


def _claim_run(settings: Settings, now: datetime) -> bool:
    interval = timedelta(hours=settings.retention_interval_hours)
    with connect(settings.database_target) as connection:
        row = connection.execute(
            "SELECT value FROM app_meta WHERE key = ?", (LAST_RUN_KEY,)
        ).fetchone()
        if row:
            try:
                last_run = datetime.fromisoformat(row["value"])
            except ValueError:
                last_run = None
            if last_run is not None and now - last_run < interval:
                return False
        connection.execute(
            """
            INSERT INTO app_meta (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (LAST_RUN_KEY, now.isoformat()),
        )
        connection.commit()
    return True


def _delete_in_batches(connection, table: str, column: str, cutoff: str, row_id: str) -> int:
    total = 0
    while True:
        cursor = connection.execute(
            f"""
            DELETE FROM {table} WHERE {row_id} IN (
                SELECT {row_id} FROM {table} WHERE {column} < ? LIMIT ?
            )
            """,
            (cutoff, DELETE_BATCH_SIZE),
        )
        connection.commit()
        removed = cursor.rowcount or 0
        total += removed
        if removed < DELETE_BATCH_SIZE:
            return total
