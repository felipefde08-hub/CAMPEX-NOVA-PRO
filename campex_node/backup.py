from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


logger = logging.getLogger("campex.node.backup")

# A backup is a copy of the Node's SQLite file: zones, shifts, events, machine
# history, counters, users and the recording index. Videos are files of their
# own and are not part of it.
REQUIRED_TABLES = frozenset({"zones", "events"})
DAILY_PREFIX = "campex-node-"
CHECK_SECONDS = 60 * 60


def backup_database(database_path: Path, target: Path) -> Path:
    """A consistent copy of the live database, safe while the Node writes to it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".partial")
    with closing(sqlite3.connect(database_path, timeout=10)) as source, closing(sqlite3.connect(partial)) as copy:
        source.backup(copy)
    partial.replace(target)
    return target


def restore_database(backup: Path, database_path: Path) -> None:
    """Overwrites the database with the backup's content, page by page.

    Copying into the open file (instead of replacing it) works on Windows even
    while another handle is open, and SQLite applies it atomically.
    """
    with closing(sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)) as source, closing(
        sqlite3.connect(database_path, timeout=30)
    ) as target:
        source.backup(target)


def validate_backup(path: Path) -> None:
    """Raises ValueError unless ``path`` is an intact CAMPEX Node database."""
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("O arquivo de backup está corrompido.")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.DatabaseError as exc:
        raise ValueError("O arquivo não é um backup do CAMPEX Node.") from exc
    if not REQUIRED_TABLES <= tables:
        raise ValueError("O arquivo não é um backup do CAMPEX Node.")


def backup_filename(at: datetime | None = None) -> str:
    stamp = (at or datetime.now(timezone.utc)).astimezone().strftime("%Y%m%d-%H%M")
    return f"campex-node-backup-{stamp}.sqlite3"


class BackupService:
    """Keeps one copy of the database per day in the backup folder."""

    def __init__(self, database_path: Path, backup_dir: Path, keep: int = 14) -> None:
        self.database_path = database_path
        self.backup_dir = backup_dir
        self.keep = max(1, keep)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="campex-node-backup", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def daily_backups(self) -> list[Path]:
        if not self.backup_dir.is_dir():
            return []
        return sorted(self.backup_dir.glob(f"{DAILY_PREFIX}????????.sqlite3"))

    def backup_today(self, today: datetime | None = None) -> Path | None:
        """Copies the database unless today's copy exists; prunes old copies."""
        day = (today or datetime.now(timezone.utc)).astimezone().strftime("%Y%m%d")
        target = self.backup_dir / f"{DAILY_PREFIX}{day}.sqlite3"
        created = None
        if not target.exists() and self.database_path.exists():
            created = backup_database(self.database_path, target)
            logger.info("Daily backup written to %s", target)
        for old in self.daily_backups()[: -self.keep]:
            try:
                old.unlink()
            except OSError:
                logger.warning("Could not delete old backup %s", old)
        return created

    def status(self) -> dict:
        backups = self.daily_backups()
        return {
            "path": str(self.backup_dir),
            "keep": self.keep,
            "count": len(backups),
            "latest": backups[-1].name if backups else None,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.backup_today()
            except Exception:
                logger.exception("Daily backup failed")
            self._stop.wait(CHECK_SECONDS)
