from __future__ import annotations

import json
import os
import re
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from campex_node.storage.sqlite import connect, utc_now


DEFAULT_TIMEZONE = "America/Sao_Paulo"
DEFAULT_CURRENCY = "BRL"

# Zone types and the attributes each one carries. Event zones (monitored,
# restricted) feed the event engine; the others feed the factory monitors.
ZONE_SETTINGS: dict[str, dict[str, Any]] = {
    "monitored": {},
    "restricted": {},
    "machine": {
        "line": "",
        "cost_per_hour": 0.0,
        "requires_operator": False,
        # The operator area is the machine polygon grown by this fraction of
        # the frame on every side.
        "operator_margin": 0.08,
        "operator_absent_seconds": 60.0,
        # Fraction of the machine area that must change between frames to
        # count as movement.
        "motion_threshold": 0.015,
        "stop_after_seconds": 30.0,
    },
    "station": {"line": "", "vacant_alert_seconds": 300.0},
    "dock": {"min_visit_seconds": 120.0},
    "area": {"after_hours_alert": True},
    "line": {"subject": "person", "direction": "any"},
}
ZONE_TYPES = tuple(ZONE_SETTINGS)
EVENT_ZONE_TYPES = ("monitored", "restricted")
LINE_SUBJECTS = ("person", "vehicle", "any")
LINE_DIRECTIONS = ("any", "a_to_b", "b_to_a")

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS shifts (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        days TEXT NOT NULL,
        breaks TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS factory_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)


def normalize_zone_settings(zone_type: str, settings: dict[str, Any] | None, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Defaults for the type, then ``base``, then ``settings``; unknown keys are dropped."""
    if zone_type not in ZONE_SETTINGS:
        raise ValueError(f"Zone type must be one of {', '.join(ZONE_TYPES)}.")
    defaults = ZONE_SETTINGS[zone_type]
    result = dict(defaults)
    for source in (base or {}, settings or {}):
        for key, value in source.items():
            if key not in defaults:
                continue
            result[key] = _coerce(key, value, defaults[key])
    if zone_type == "line":
        if result["subject"] not in LINE_SUBJECTS:
            raise ValueError(f"Line subject must be one of {', '.join(LINE_SUBJECTS)}.")
        if result["direction"] not in LINE_DIRECTIONS:
            raise ValueError(f"Line direction must be one of {', '.join(LINE_DIRECTIONS)}.")
    return result


def _coerce(key: str, value: Any, default: Any) -> Any:
    try:
        if isinstance(default, bool):
            return bool(value)
        if isinstance(default, float):
            number = float(value)
            if number < 0:
                raise ValueError
            return number
        return str(value).strip()[:120]
    except (TypeError, ValueError):
        raise ValueError(f"Invalid value for zone setting {key}.") from None


@dataclass(frozen=True)
class ShiftBreak:
    name: str
    start: str
    end: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class Shift:
    id: str
    name: str
    start: str
    end: str
    days: tuple[int, ...]  # 0 = Monday
    breaks: tuple[ShiftBreak, ...]
    enabled: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "start": self.start,
            "end": self.end,
            "days": list(self.days),
            "breaks": [item.as_dict() for item in self.breaks],
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class Window:
    """A concrete occurrence of a shift or break, in UTC."""

    shift: Shift
    name: str
    start: datetime
    end: datetime
    local_date: date

    def contains(self, at: datetime) -> bool:
        return self.start <= at < self.end

    def as_dict(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift.id,
            "shift": self.shift.name,
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "date": self.local_date.isoformat(),
        }


class FactoryStore:
    """Shifts and plant-wide settings, in the Node's SQLite file."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._cache: tuple[list[Shift], tzinfo] | None = None

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(self.database_path)) as connection:
            for statement in SCHEMA:
                connection.execute(statement)
            connection.commit()

    # Settings

    def settings(self) -> dict[str, Any]:
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute("SELECT key, value FROM factory_settings").fetchall()
        stored = {row["key"]: json.loads(row["value"]) for row in rows}
        return {
            "timezone": stored.get("timezone") or os.getenv("CAMPEX_NODE_TIMEZONE") or DEFAULT_TIMEZONE,
            "currency": stored.get("currency") or DEFAULT_CURRENCY,
        }

    def update_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if "timezone" in updates:
            name = str(updates["timezone"] or "").strip()
            _zone(name)  # raises on an unknown zone
            values["timezone"] = name
        if "currency" in updates:
            currency = str(updates["currency"] or "").strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("Currency must be a 3-letter code.")
            values["currency"] = currency
        with closing(connect(self.database_path)) as connection:
            for key, value in values.items():
                connection.execute(
                    "INSERT INTO factory_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)),
                )
            connection.commit()
        self._cache = None
        return self.settings()

    def tz(self) -> tzinfo:
        return self._load()[1]

    # Shifts

    def list_shifts(self) -> list[Shift]:
        return list(self._load()[0])

    def create_shift(self, payload: dict[str, Any]) -> Shift:
        fields = _shift_fields(payload)
        shift_id = f"shift_{uuid4().hex[:12]}"
        now = utc_now().isoformat()
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO shifts (id, name, start_time, end_time, days, breaks, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (shift_id, *fields, now, now),
            )
            connection.commit()
        self._cache = None
        return self.get_shift(shift_id)  # type: ignore[return-value]

    def update_shift(self, shift_id: str, payload: dict[str, Any]) -> Shift | None:
        current = self.get_shift(shift_id)
        if current is None:
            return None
        fields = _shift_fields({**current.as_dict(), **payload})
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE shifts SET name = ?, start_time = ?, end_time = ?, days = ?, breaks = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (*fields, utc_now().isoformat(), shift_id),
            )
            connection.commit()
        self._cache = None
        return self.get_shift(shift_id)

    def delete_shift(self, shift_id: str) -> bool:
        with closing(connect(self.database_path)) as connection:
            cursor = connection.execute("DELETE FROM shifts WHERE id = ?", (shift_id,))
            connection.commit()
        self._cache = None
        return cursor.rowcount > 0

    def get_shift(self, shift_id: str) -> Shift | None:
        return next((shift for shift in self._load()[0] if shift.id == shift_id), None)

    # Calendar

    def has_schedule(self) -> bool:
        return any(shift.enabled for shift in self._load()[0])

    def shift_windows(self, start: datetime, end: datetime) -> list[Window]:
        shifts, zone = self._load()
        return sorted(
            (window for window in _windows(shifts, zone, start, end, breaks=False) if window.end > start and window.start < end),
            key=lambda window: window.start,
        )

    def break_windows(self, start: datetime, end: datetime) -> list[Window]:
        shifts, zone = self._load()
        return sorted(
            (window for window in _windows(shifts, zone, start, end, breaks=True) if window.end > start and window.start < end),
            key=lambda window: window.start,
        )

    def shift_at(self, at: datetime) -> Window | None:
        return next((window for window in self.shift_windows(at, at + timedelta(seconds=1)) if window.contains(at)), None)

    def is_working_time(self, at: datetime) -> bool | None:
        """None when no schedule is configured: the Node cannot tell."""
        if not self.has_schedule():
            return None
        return self.shift_at(at) is not None

    def day_bounds(self, day: date) -> tuple[datetime, datetime]:
        zone = self.tz()
        start = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
        return start, end

    def local_date(self, at: datetime) -> date:
        return at.astimezone(self.tz()).date()

    def _load(self) -> tuple[list[Shift], tzinfo]:
        if self._cache is None:
            with closing(connect(self.database_path)) as connection:
                rows = connection.execute("SELECT * FROM shifts ORDER BY start_time ASC").fetchall()
            shifts = [_row_to_shift(row) for row in rows]
            self._cache = (shifts, _zone(self.settings()["timezone"]))
        return self._cache


class SnapshotFactory(FactoryStore):
    """The calendar of a factory snapshot a Node synced (CAMPEX Cloud).

    Same windows, breaks and time zone rules as on the Node; read-only.
    """

    def __init__(self, snapshot: dict[str, Any]) -> None:
        super().__init__(Path(""))  # never opened
        stored = snapshot.get("settings") or {}
        self._settings = {
            "timezone": stored.get("timezone") or DEFAULT_TIMEZONE,
            "currency": stored.get("currency") or DEFAULT_CURRENCY,
        }
        shifts = [shift_from_dict(item) for item in snapshot.get("shifts") or []]
        self._cache = (sorted(shifts, key=lambda shift: shift.start), _zone(self._settings["timezone"]))

    def initialize(self) -> None:
        pass

    def settings(self) -> dict[str, Any]:
        return dict(self._settings)


def shift_from_dict(data: dict[str, Any]) -> Shift:
    return Shift(
        id=str(data["id"]),
        name=str(data.get("name") or ""),
        start=str(data["start"]),
        end=str(data["end"]),
        days=tuple(int(day) for day in data.get("days") or ()),
        breaks=tuple(ShiftBreak(**item) for item in data.get("breaks") or ()),
        enabled=bool(data.get("enabled", True)),
    )


def _windows(shifts: list[Shift], zone: tzinfo, start: datetime, end: datetime, *, breaks: bool) -> Iterator[Window]:
    # A shift that starts the evening before still covers the range.
    first = start.astimezone(zone).date() - timedelta(days=1)
    last = end.astimezone(zone).date()
    day = first
    while day <= last:
        for shift in shifts:
            if not shift.enabled or day.weekday() not in shift.days:
                continue
            shift_start = _local(day, shift.start, zone)
            shift_end = _local(day, shift.end, zone)
            if shift_end <= shift_start:
                shift_end = _local(day + timedelta(days=1), shift.end, zone)
            if not breaks:
                yield Window(shift, shift.name, shift_start, shift_end, day)
                continue
            for item in shift.breaks:
                break_start = _local(day, item.start, zone)
                if break_start < shift_start:
                    break_start = _local(day + timedelta(days=1), item.start, zone)
                break_end = _local(break_start.astimezone(zone).date(), item.end, zone)
                if break_end <= break_start:
                    break_end = _local(break_start.astimezone(zone).date() + timedelta(days=1), item.end, zone)
                yield Window(shift, item.name, break_start, break_end, day)
        day += timedelta(days=1)


def _local(day: date, clock: str, zone: tzinfo) -> datetime:
    hours, minutes = (int(part) for part in clock.split(":"))
    return datetime.combine(day, time(hours, minutes), tzinfo=zone).astimezone(timezone.utc)


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown time zone: {name}.") from None


def _clock(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _TIME_PATTERN.match(text):
        raise ValueError(f"{field} must be HH:MM.")
    return text


def _shift_fields(payload: dict[str, Any]) -> tuple:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("Shift name is required.")
    start = _clock(payload.get("start"), "Shift start")
    end = _clock(payload.get("end"), "Shift end")
    if start == end:
        raise ValueError("Shift start and end must differ.")
    days = sorted({int(day) for day in payload.get("days", range(5))})
    if not days or any(day < 0 or day > 6 for day in days):
        raise ValueError("Shift days must be weekdays from 0 (Monday) to 6 (Sunday).")
    breaks = []
    for item in payload.get("breaks") or []:
        breaks.append(
            {
                "name": str(item.get("name") or "Intervalo").strip()[:60],
                "start": _clock(item.get("start"), "Break start"),
                "end": _clock(item.get("end"), "Break end"),
            }
        )
    return (name[:60], start, end, json.dumps(days), json.dumps(breaks), int(bool(payload.get("enabled", True))))


def _row_to_shift(row) -> Shift:
    return Shift(
        id=row["id"],
        name=row["name"],
        start=row["start_time"],
        end=row["end_time"],
        days=tuple(json.loads(row["days"])),
        breaks=tuple(ShiftBreak(**item) for item in json.loads(row["breaks"])),
        enabled=bool(row["enabled"]),
    )

