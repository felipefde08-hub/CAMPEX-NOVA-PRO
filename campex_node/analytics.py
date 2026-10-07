from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from backend.zones.models import Zone
from campex_node.activity import MACHINE, PERSON_OCCUPANCY, VEHICLE_OCCUPANCY, ActivityStore, Interval
from campex_node.events import NodeEventStore
from campex_node.factory import FactoryStore, Window
from campex_node.recording import RecordingStore
from campex_node.storage.sqlite import parse_iso, utc_now


PERIODS = ("today", "yesterday", "week", "last_week", "month", "last_month")
# How much video before a stop is worth watching.
LEAD_IN_SECONDS = 120
BREAK_TOLERANCE_SECONDS = 5 * 60
BASELINE_DAYS = 30


def resolve_period(
    factory: FactoryStore,
    *,
    period: str | None = None,
    start: str | None = None,
    end: str | None = None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """An explicit [start, end) wins; otherwise a named period in plant time."""
    now = now or utc_now()
    if start or end:
        begin = parse_iso(start) if start else now - timedelta(days=1)
        finish = parse_iso(end) if end else now
        if begin is None or finish is None or finish <= begin:
            raise ValueError("end must be after start.")
        return begin, finish
    period = period or "today"
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}.")
    today = factory.local_date(now)
    if period == "today":
        return factory.day_bounds(today)[0], now
    if period == "yesterday":
        return factory.day_bounds(today - timedelta(days=1))
    if period in ("week", "last_week"):
        monday = today - timedelta(days=today.weekday())
        if period == "week":
            return factory.day_bounds(monday)[0], now
        return factory.day_bounds(monday - timedelta(days=7))[0], factory.day_bounds(monday)[0]
    first = today.replace(day=1)
    if period == "month":
        return factory.day_bounds(first)[0], now
    previous = (first - timedelta(days=1)).replace(day=1)
    return factory.day_bounds(previous)[0], factory.day_bounds(first)[0]


class FactoryAnalytics:
    """Answers the plant's questions from the monitors' intervals and counters."""

    def __init__(
        self,
        events_store: NodeEventStore,
        activity: ActivityStore,
        factory: FactoryStore,
        recordings: RecordingStore | None = None,
    ) -> None:
        self.events_store = events_store
        self.activity = activity
        self.factory = factory
        self.recordings = recordings

    # Machines

    def machines(self, start: datetime, end: datetime, now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        zones = self._zones("machine")
        settings = self.events_store.zone_settings()
        intervals = self._by_zone(self.activity.intervals(kind=MACHINE, zone_ids=zones, start=start, end=end))
        cycles = self._cycles(zones, start, end)
        calendar = self._calendar(start, end)
        currency = self.factory.settings()["currency"]
        machines = []
        for zone_id, zone in zones.items():
            zone_settings = settings.get(zone_id, {})
            items = intervals.get(zone_id, [])
            running = _seconds(items, "RUNNING", start, end, now)
            stopped_total = _seconds(items, "STOPPED", start, end, now)
            # Downtime is what should have been producing: in a shift, outside
            # its breaks. A stop entirely inside lunch is not a stop.
            stopped = _productive(items, "STOPPED", calendar, start, end, now)
            productive = {
                item.id: _productive([item], "STOPPED", calendar, start, end, now)
                for item in items
                if item.state == "STOPPED" and item.started_at >= start
            }
            stops = [item for item in items if productive.get(item.id, 0) > 0]
            longest = max(stops, key=lambda item: productive[item.id], default=None)
            cost_per_hour = float(zone_settings.get("cost_per_hour") or 0.0)
            zone_cycles = cycles.get(zone_id, 0)
            machines.append(
                {
                    "zone_id": zone_id,
                    "name": zone.name,
                    "camera_id": zone.camera_id,
                    "line": zone_settings.get("line") or None,
                    "running_seconds": round(running),
                    "stopped_seconds": round(stopped),
                    "stopped_total_seconds": round(stopped_total),
                    "unmonitored_seconds": round(max(0.0, (min(end, now) - start).total_seconds() - running - stopped_total)),
                    "availability": round(running / (running + stopped), 4) if running + stopped else None,
                    "stops": len(stops),
                    "longest_stop": self._stop_dict(longest, zone, zone_settings, start, end, now) if longest else None,
                    "cycles": zone_cycles,
                    "cycles_per_hour": round(zone_cycles / (running / 3600), 1) if running >= 60 else None,
                    "cost_per_hour": cost_per_hour,
                    "lost_cost": round(stopped / 3600 * cost_per_hour, 2),
                }
            )
        machines.sort(key=lambda item: (item["lost_cost"], item["stopped_seconds"]), reverse=True)
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "currency": currency,
            "schedule_configured": self.factory.has_schedule(),
            "machines": machines,
            "totals": {
                "stopped_seconds": sum(item["stopped_seconds"] for item in machines),
                "stops": sum(item["stops"] for item in machines),
                "cycles": sum(item["cycles"] for item in machines),
                "lost_cost": round(sum(item["lost_cost"] for item in machines), 2),
            },
        }

    def stops(
        self,
        start: datetime,
        end: datetime,
        *,
        min_seconds: float = 0,
        zone_id: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        now = now or utc_now()
        zones = self._zones("machine")
        if zone_id:
            zones = {key: value for key, value in zones.items() if key == zone_id}
        settings = self.events_store.zone_settings()
        result = []
        for item in self.activity.intervals(kind=MACHINE, state="STOPPED", zone_ids=zones, start=start, end=end):
            stop = self._stop_dict(item, zones[item.zone_id], settings.get(item.zone_id, {}), start, end, now)
            if stop["duration_seconds"] >= min_seconds:
                result.append(stop)
        result.sort(key=lambda item: item["started_at"])
        return result

    def speed(self, zone_id: str, start: datetime, end: datetime, now: datetime | None = None) -> dict[str, Any]:
        """Cycles per running hour in the range against the previous 30 days."""
        now = now or utc_now()
        baseline_start = start - timedelta(days=BASELINE_DAYS)
        current = self._rate(zone_id, start, end, now)
        baseline = self._rate(zone_id, baseline_start, start, now)
        change = None
        if current["cycles_per_hour"] and baseline["cycles_per_hour"]:
            change = round(current["cycles_per_hour"] / baseline["cycles_per_hour"] - 1, 4)
        return {"zone_id": zone_id, "current": current, "baseline": baseline, "change": change, "baseline_days": BASELINE_DAYS}

    def hourly(self, start: datetime, end: datetime, zone_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        """Production and downtime per hour, plus the hour-of-day average to find when output drops."""
        now = now or utc_now()
        zones = self._zones("machine")
        if zone_id:
            zones = {key: value for key, value in zones.items() if key == zone_id}
        tz = self.factory.tz()
        buckets: dict[datetime, dict[str, float]] = defaultdict(lambda: {"cycles": 0, "stopped_seconds": 0.0, "running_seconds": 0.0})
        for row in self.activity.counts(zones, start, end):
            buckets[_hour(row["minute"])]["cycles"] += row["cycles"] + row["external"]
        for item in self.activity.intervals(kind=MACHINE, zone_ids=zones, start=start, end=end):
            if item.state not in ("RUNNING", "STOPPED"):
                continue
            cursor = max(start, item.started_at)
            stop = min(end, item.end_or(now))
            while cursor < stop:
                hour = _hour(cursor)
                chunk_end = min(stop, hour + timedelta(hours=1))
                buckets[hour][f"{item.state.lower()}_seconds"] += (chunk_end - cursor).total_seconds()
                cursor = chunk_end
        hours = [
            {"hour": hour.isoformat(), "local_hour": hour.astimezone(tz).strftime("%Y-%m-%d %H:00"), **{k: round(v) for k, v in values.items()}}
            for hour, values in sorted(buckets.items())
        ]
        by_hour_of_day: dict[int, list[float]] = defaultdict(list)
        for hour, values in buckets.items():
            if values["running_seconds"] + values["stopped_seconds"] > 0:
                by_hour_of_day[hour.astimezone(tz).hour].append(values["cycles"])
        profile = [
            {"hour_of_day": hour, "avg_cycles": round(sum(values) / len(values), 1), "samples": len(values)}
            for hour, values in sorted(by_hour_of_day.items())
        ]
        lowest = sorted(profile, key=lambda item: item["avg_cycles"])[:3]
        return {"hours": hours, "hour_of_day": profile, "lowest_hours": lowest}

    def shifts(self, start: datetime, end: datetime, now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        zones = self._zones("machine")
        counts = self.activity.counts(zones, start, end)
        intervals = self.activity.intervals(kind=MACHINE, zone_ids=zones, start=start, end=end)
        calendar = self._calendar(start, end)
        windows = []
        for window in self.factory.shift_windows(start, end):
            w_start, w_end = max(start, window.start), min(end, window.end)
            in_window = [item for item in intervals if item.started_at < w_end and item.end_or(now) > w_start]
            cycles = sum(
                row["cycles"] + row["external"] for row in counts if w_start <= row["minute"] < w_end
            )
            stops = [item for item in in_window if item.state == "STOPPED" and w_start <= item.started_at < w_end]
            windows.append(
                {
                    **window.as_dict(),
                    "cycles": cycles,
                    "stops": len(stops),
                    "stopped_seconds": round(_productive(in_window, "STOPPED", calendar, w_start, w_end, now)),
                    "running_seconds": round(_seconds(in_window, "RUNNING", w_start, w_end, now)),
                }
            )
        totals: dict[str, dict[str, Any]] = {}
        for item in windows:
            entry = totals.setdefault(item["shift"], {"shift": item["shift"], "occurrences": 0, "cycles": 0, "stops": 0, "stopped_seconds": 0})
            entry["occurrences"] += 1
            for key in ("cycles", "stops", "stopped_seconds"):
                entry[key] += item[key]
        ranking = sorted(totals.values(), key=lambda item: item["cycles"], reverse=True)
        return {
            "windows": windows,
            "by_shift": ranking,
            "most_productive": ranking[0]["shift"] if ranking and ranking[0]["cycles"] else None,
            "most_downtime": max(ranking, key=lambda item: item["stopped_seconds"])["shift"] if ranking else None,
        }

    # People and areas

    def occupancy(
        self,
        start: datetime,
        end: datetime,
        *,
        zone_id: str | None = None,
        min_vacant_seconds: float = 60,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Occupied/vacant time per zone; inside shifts when a schedule exists."""
        now = now or utc_now()
        zones = {key: zone for key, zone in self._zones().items() if zone.type != "line"}
        if zone_id:
            zones = {key: value for key, value in zones.items() if key == zone_id}
        calendar = self._calendar(start, end)
        intervals = self._by_zone(self.activity.intervals(kind=PERSON_OCCUPANCY, zone_ids=zones, start=start, end=end))
        result = []
        for key, zone in zones.items():
            items = intervals.get(key, [])
            if not items:
                continue
            vacancies = []
            for item in items:
                productive = _productive([item], "VACANT", calendar, start, end, now)
                if item.state == "VACANT" and productive >= min_vacant_seconds:
                    vacancies.append(
                        {
                            **item.as_dict(now),
                            "duration_seconds": round(productive),
                            "recording": self._recording(zone.camera_id, item.started_at),
                        }
                    )
            result.append(
                {
                    "zone_id": key,
                    "name": zone.name,
                    "type": zone.type,
                    "camera_id": zone.camera_id,
                    "occupied_seconds": round(_productive(items, "OCCUPIED", calendar, start, end, now)),
                    "vacant_seconds": round(_productive(items, "VACANT", calendar, start, end, now)),
                    "within_shifts": bool(calendar[0]),
                    "vacancies": vacancies,
                }
            )
        return result

    def first_arrival(self, day: date, zone_ids: Iterable[str] | None = None) -> dict[str, Any]:
        start, end = self.factory.day_bounds(day)
        zones = self._zones()
        wanted = set(zone_ids) if zone_ids else {key for key, zone in zones.items() if zone.type in ("station", "machine")}
        arrivals = []
        for item in self.activity.intervals(kind=PERSON_OCCUPANCY, state="OCCUPIED", zone_ids=wanted, start=start, end=end):
            if item.started_at >= start and not any(entry["zone_id"] == item.zone_id for entry in arrivals):
                zone = zones.get(item.zone_id)
                arrivals.append(
                    {
                        "zone_id": item.zone_id,
                        "name": zone.name if zone else item.zone_id,
                        "at": item.started_at.isoformat(),
                        "local_time": item.started_at.astimezone(self.factory.tz()).strftime("%H:%M:%S"),
                        "recording": self._recording(item.camera_id, item.started_at),
                    }
                )
        return {"date": day.isoformat(), "first": arrivals[0] if arrivals else None, "zones": arrivals}

    def breaks(self, day: date, now: datetime | None = None) -> list[dict[str, Any]]:
        """For each break that day: when each station was occupied again, and by how much it ran over."""
        now = now or utc_now()
        start, end = self.factory.day_bounds(day)
        stations = {key: zone for key, zone in self._zones().items() if zone.type == "station"}
        result = []
        for window in self.factory.break_windows(start, end):
            if window.local_date != day or window.end > now:
                continue
            returns = []
            horizon = window.end + timedelta(hours=2)
            for item in self.activity.intervals(
                kind=PERSON_OCCUPANCY, state="OCCUPIED", zone_ids=stations, start=window.end, end=horizon
            ):
                if any(entry["zone_id"] == item.zone_id for entry in returns):
                    continue
                back_at = max(item.started_at, window.end)
                late = (back_at - window.end).total_seconds()
                returns.append(
                    {
                        "zone_id": item.zone_id,
                        "name": stations[item.zone_id].name,
                        "back_at": back_at.isoformat(),
                        "late_seconds": round(late),
                        "overrun": late > BREAK_TOLERANCE_SECONDS,
                    }
                )
            result.append(
                {
                    **window.as_dict(),
                    "stations": returns,
                    "overrun": any(entry["overrun"] for entry in returns),
                    "max_late_seconds": max((entry["late_seconds"] for entry in returns), default=None),
                }
            )
        return result

    def after_hours(self, start: datetime, end: datetime, now: datetime | None = None) -> dict[str, Any]:
        """Anyone or any vehicle seen in a zone outside the shifts."""
        now = now or utc_now()
        if not self.factory.has_schedule():
            return {"schedule_configured": False, "presences": []}
        zones = self._zones()
        shifts = self.factory.shift_windows(start - timedelta(days=1), end + timedelta(days=1))
        presences = []
        for kind, subject in ((PERSON_OCCUPANCY, "person"), (VEHICLE_OCCUPANCY, "vehicle")):
            for item in self.activity.intervals(kind=kind, state="OCCUPIED", zone_ids=zones, start=start, end=end):
                outside = item.clipped_seconds(start, end, now) - _overlap_seconds(item, shifts, start, end, now)
                if outside <= 0:
                    continue
                zone = zones[item.zone_id]
                presences.append(
                    {
                        **item.as_dict(now),
                        "subject": subject,
                        "zone_name": zone.name,
                        "zone_type": zone.type,
                        "outside_seconds": round(outside),
                        "recording": self._recording(item.camera_id, item.started_at),
                    }
                )
        presences.sort(key=lambda item: item["started_at"])
        return {"schedule_configured": True, "presences": presences}

    def docks(self, start: datetime, end: datetime) -> dict[str, Any]:
        visits = self.events_store.events_between(start, end, "DOCK_VISIT")
        zones = self._zones("dock")
        items = []
        for event in visits:
            started = datetime.fromisoformat(event.started_at)
            items.append(
                {
                    "event_id": event.id,
                    "zone_id": event.zone_id,
                    "dock": zones[event.zone_id].name if event.zone_id in zones else event.metadata.get("zone_name"),
                    "arrived_at": event.started_at,
                    "left_at": event.ended_at,
                    "dwell_seconds": event.duration,
                    "ongoing": event.ended_at is None,
                    "recording": self._recording(event.camera_id, started),
                }
            )
        finished = [item["dwell_seconds"] for item in items if item["dwell_seconds"] is not None]
        return {
            "visits": items,
            "count": len(items),
            "avg_dwell_seconds": round(sum(finished) / len(finished)) if finished else None,
        }

    def lines(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        zones = self._zones("line")
        settings = self.events_store.zone_settings()
        totals: dict[str, dict[str, int]] = defaultdict(lambda: {"forward": 0, "backward": 0})
        for row in self.activity.counts(zones, start, end):
            totals[row["zone_id"]]["forward"] += row["forward"]
            totals[row["zone_id"]]["backward"] += row["backward"]
        result = []
        for key, zone in zones.items():
            zone_settings = settings.get(key, {})
            counts = totals[key]
            direction = zone_settings.get("direction", "any")
            total = {"a_to_b": counts["forward"], "b_to_a": counts["backward"]}.get(direction, counts["forward"] + counts["backward"])
            result.append(
                {"zone_id": key, "name": zone.name, "subject": zone_settings.get("subject"), "direction": direction, **counts, "count": total}
            )
        return result

    # Management

    def summary(self, start: datetime, end: datetime, now: datetime | None = None) -> dict[str, Any]:
        """The period at a glance, compared with the period of the same length before it."""
        now = now or utc_now()
        length = min(end, now) - start
        previous_start, previous_end = start - length, start
        current = self._overview(start, end, now)
        previous = self._overview(previous_start, previous_end, now)
        machines = self.machines(start, end, now)
        problems = []
        for machine in machines["machines"]:
            if machine["stopped_seconds"] > 0:
                problems.append(
                    {
                        "kind": "downtime",
                        "title": f"{machine['name']} parada {machine['stops']}x",
                        "zone_id": machine["zone_id"],
                        "seconds": machine["stopped_seconds"],
                        "cost": machine["lost_cost"],
                    }
                )
        for zone in self.occupancy(start, end, now=now):
            vacant = sum(item["duration_seconds"] for item in zone["vacancies"])
            if zone["type"] == "station" and vacant:
                problems.append({"kind": "vacancy", "title": f"{zone['name']} desocupada", "zone_id": zone["zone_id"], "seconds": vacant, "cost": 0.0})
        problems.sort(key=lambda item: (item["cost"], item["seconds"]), reverse=True)
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "currency": machines["currency"],
            "current": current,
            "previous": {"start": previous_start.isoformat(), "end": previous_end.isoformat(), **previous},
            "changes": {key: _change(current[key], previous[key]) for key in current if isinstance(current[key], (int, float))},
            "top_problems": problems[:3],
            "machines": machines["machines"],
            "docks": self.docks(start, end)["count"],
            "after_hours": len(self.after_hours(start, end, now)["presences"]),
        }

    def _overview(self, start: datetime, end: datetime, now: datetime) -> dict[str, Any]:
        machines = self.machines(start, end, now)
        events = self.events_store.events_between(start, end)
        by_type: dict[str, int] = defaultdict(int)
        for event in events:
            by_type[event.type] += 1
        return {
            **machines["totals"],
            "events": len(events),
            "restricted_entries": by_type.get("PERSON_RESTRICTED_ZONE", 0),
            "station_vacancies": by_type.get("STATION_VACANT", 0),
            "missing_operator": by_type.get("MISSING_OPERATOR", 0),
            "after_hours": by_type.get("AFTER_HOURS_PRESENCE", 0),
            "dock_visits": by_type.get("DOCK_VISIT", 0),
            "events_by_type": dict(by_type),
        }

    # Helpers

    def _zones(self, zone_type: str | None = None) -> dict[str, Zone]:
        return {
            zone.id: zone
            for zone in self.events_store.list_zones()
            if zone_type is None or zone.type == zone_type
        }

    def _cycles(self, zone_ids: Iterable[str], start: datetime, end: datetime) -> dict[str, int]:
        totals: dict[str, int] = defaultdict(int)
        for row in self.activity.counts(zone_ids, start, end):
            totals[row["zone_id"]] += row["cycles"] + row["external"]
        return totals

    def _rate(self, zone_id: str, start: datetime, end: datetime, now: datetime) -> dict[str, Any]:
        running = _seconds(
            self.activity.intervals(kind=MACHINE, zone_ids=[zone_id], start=start, end=end), "RUNNING", start, end, now
        )
        cycles = self._cycles([zone_id], start, end).get(zone_id, 0)
        return {
            "cycles": cycles,
            "running_seconds": round(running),
            "cycles_per_hour": round(cycles / (running / 3600), 1) if running >= 60 else None,
        }

    def _stop_dict(
        self, item: Interval, zone: Zone, settings: dict[str, Any], start: datetime, end: datetime, now: datetime
    ) -> dict[str, Any]:
        duration = item.clipped_seconds(start, end, now)
        productive = _productive([item], item.state, self._calendar(start, end), start, end, now)
        shift = self.factory.shift_at(item.started_at)
        cost_per_hour = float(settings.get("cost_per_hour") or 0.0)
        return {
            **item.as_dict(now),
            "name": zone.name,
            "line": settings.get("line") or None,
            "duration_seconds": round(duration),
            # Time that should have been producing: inside a shift, outside breaks.
            "productive_seconds": round(productive),
            "shift": shift.name if shift else None,
            "cost": round(productive / 3600 * cost_per_hour, 2),
            # Watch from a little before the machine stopped.
            "recording": self._recording(item.camera_id, item.started_at - timedelta(seconds=LEAD_IN_SECONDS)),
        }

    def _calendar(self, start: datetime, end: datetime) -> tuple[list[Window], list[Window]]:
        """Shift and break windows covering [start, end]."""
        return self.factory.shift_windows(start, end), self.factory.break_windows(start, end)

    def _recording(self, camera_id: str, at: datetime) -> dict[str, Any] | None:
        if self.recordings is None:
            return None
        found = self.recordings.at(camera_id, at)
        if found is None:
            return None
        segment, offset = found
        return {"segment_id": segment.id, "offset_seconds": round(offset, 1), "at": at.isoformat()}

    @staticmethod
    def _by_zone(items: list[Interval]) -> dict[str, list[Interval]]:
        grouped: dict[str, list[Interval]] = defaultdict(list)
        for item in items:
            grouped[item.zone_id].append(item)
        return grouped


def _seconds(items: Iterable[Interval], state: str, start: datetime, end: datetime, now: datetime) -> float:
    return sum(item.clipped_seconds(start, end, now) for item in items if item.state == state)


def _overlap_seconds(item: Interval, windows: list[Window], start: datetime, end: datetime, now: datetime) -> float:
    total = 0.0
    for window in windows:
        total += item.clipped_seconds(max(start, window.start), min(end, window.end), now)
    return total


def _seconds_in_windows(
    items: Iterable[Interval], state: str, windows: list[Window], start: datetime, end: datetime, now: datetime
) -> float:
    return sum(_overlap_seconds(item, windows, start, end, now) for item in items if item.state == state)


def _productive(
    items: Iterable[Interval],
    state: str,
    calendar: tuple[list[Window], list[Window]],
    start: datetime,
    end: datetime,
    now: datetime,
) -> float:
    """Time in ``state`` inside the shifts and outside their breaks.

    Without a schedule every moment counts: the Node cannot tell planned time
    from idle time.
    """
    shifts, breaks = calendar
    if not shifts:
        return _seconds(items, state, start, end, now)
    return _seconds_in_windows(items, state, shifts, start, end, now) - _seconds_in_windows(
        items, state, breaks, start, end, now
    )


def _hour(at: datetime) -> datetime:
    return at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _change(current: float, previous: float) -> float | None:
    if not previous:
        return None
    return round(current / previous - 1, 4)
