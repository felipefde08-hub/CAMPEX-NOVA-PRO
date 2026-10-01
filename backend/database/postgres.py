"""Postgres (Neon) adapter exposing the subset of the sqlite3 API the repositories use.

Repositories are written against sqlite3: ``?`` placeholders, ``row["col"]`` and
``row[0]`` access, ``with connect(...)`` committing on success. This module keeps
that contract so the same SQL runs on SQLite locally and on Postgres in the cloud.
"""

from __future__ import annotations

import queue
import re
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import psycopg


# SQLite's CURRENT_TIMESTAMP yields "YYYY-MM-DD HH:MM:SS" in UTC. Timestamps are
# stored as TEXT, so Postgres must produce the exact same format for ordering
# and parsing to keep working.
PG_CURRENT_TIMESTAMP = "to_char(timezone('UTC', now()), 'YYYY-MM-DD HH24:MI:SS')"

_QUOTED_SEGMENT = re.compile(r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")")
_CURRENT_TIMESTAMP = re.compile(r"\bCURRENT_TIMESTAMP\b", re.IGNORECASE)
# sqlite3 ":name" placeholders; the lookbehind skips Postgres "::type" casts.
_NAMED_PLACEHOLDER = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")

_IDLE_POOL_SIZE = 4
_idle_connections: "queue.LifoQueue[psycopg.Connection]" = queue.LifoQueue(maxsize=_IDLE_POOL_SIZE)


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"***@{host}" if parts.username or parts.password else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def translate_sql(sql: str, has_params: bool, named: bool = False) -> str:
    segments = _QUOTED_SEGMENT.split(sql)
    translated: list[str] = []
    for index, segment in enumerate(segments):
        quoted = index % 2 == 1
        if has_params:
            # psycopg treats % as a placeholder marker only when params are passed.
            segment = segment.replace("%", "%%")
        if not quoted:
            if named:
                segment = _NAMED_PLACEHOLDER.sub(r"%(\1)s", segment)
            elif has_params:
                segment = segment.replace("?", "%s")
            segment = _CURRENT_TIMESTAMP.sub(PG_CURRENT_TIMESTAMP, segment)
        translated.append(segment)
    return "".join(translated)


def _adapt_value(value: Any) -> Any:
    # Flags are INTEGER columns (SQLite has no boolean type); Postgres refuses
    # to store a boolean in them.
    return int(value) if isinstance(value, bool) else value


def _adapt_params(params: Sequence[Any] | Mapping[str, Any] | None):
    if params is None:
        return None
    if isinstance(params, Mapping):
        return {key: _adapt_value(value) for key, value in params.items()}
    return tuple(_adapt_value(value) for value in params)


def _normalize_value(value: Any) -> Any:
    # SUM/AVG return numeric in Postgres; callers expect plain int/float.
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


class Row(dict):
    """Row accessible by column name or position, like sqlite3.Row."""

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        normalized = [_normalize_value(value) for value in values]
        super().__init__(zip(columns, normalized))
        self._values = tuple(normalized)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _row_factory(cursor: psycopg.Cursor):
    columns = [column.name for column in cursor.description or ()]

    def make_row(values: Sequence[Any]) -> Row:
        return Row(columns, values)

    return make_row


def _open(url: str) -> psycopg.Connection:
    return psycopg.connect(url, connect_timeout=10, row_factory=_row_factory)


def _checkout(url: str) -> tuple[psycopg.Connection, bool]:
    while True:
        try:
            connection = _idle_connections.get_nowait()
        except queue.Empty:
            return _open(url), False
        if not connection.closed and not connection.broken:
            return connection, True
        _close_quietly(connection)


def _close_quietly(connection: psycopg.Connection) -> None:
    try:
        connection.close()
    except Exception:
        pass


class PostgresConnection:
    """Unit of work: commits on clean exit, rolls back on error.

    Connections are reused across requests because each new TLS session to
    Neon costs several round trips; a reused one that the server already
    dropped is replaced transparently on its first statement.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._connection, self._reused = _checkout(url)
        self._executed = False

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> psycopg.Cursor:
        query = translate_sql(sql, params is not None, named=isinstance(params, Mapping))
        adapted = _adapt_params(params)
        try:
            cursor = self._connection.execute(query, adapted)
        except psycopg.OperationalError:
            if not self._reused or self._executed:
                raise
            # Idle pooled connection was closed by Neon (scale to zero, pooler
            # timeout): retry once on a fresh connection.
            _close_quietly(self._connection)
            self._connection, self._reused = _open(self._url), False
            cursor = self._connection.execute(query, adapted)
        self._executed = True
        return cursor

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any] | Mapping[str, Any]]) -> None:
        params_list = [_adapt_params(params) for params in seq_of_params]
        named = bool(params_list) and isinstance(params_list[0], Mapping)
        query = translate_sql(sql, True, named=named)
        with self._connection.cursor() as cursor:
            cursor.executemany(query, params_list)
        self._executed = True

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        connection = self._connection
        if connection.closed or connection.broken:
            return
        try:
            connection.rollback()
            _idle_connections.put_nowait(connection)
        except Exception:
            _close_quietly(connection)

    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self.close()


def connect(url: str) -> PostgresConnection:
    return PostgresConnection(url)
