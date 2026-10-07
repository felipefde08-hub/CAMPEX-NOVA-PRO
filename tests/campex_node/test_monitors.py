from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from campex_node.activity import MACHINE, PERSON_OCCUPANCY, VEHICLE_OCCUPANCY, ActivityStore
from campex_node.events import NodeEventStore
from campex_node.factory import FactoryStore
from campex_node.monitors import MachineMonitor, OccupancyMonitor, ZoneCache
from campex_node.vision import EdgeDetection


# 2026-10-07 is a Wednesday; 12:00 UTC is 09:00 in São Paulo.
MORNING = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)  # 00:00 local
LEFT = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]
FRAME = np.zeros((360, 640, 3), dtype=np.uint8)


class _Env:
    def __init__(self, tmp_path) -> None:
        database = tmp_path / "node.sqlite3"
        self.zones = NodeEventStore(database)
        self.zones.initialize()
        self.activity = ActivityStore(database)
        self.activity.initialize()
        self.factory = FactoryStore(database)
        self.factory.initialize()
        self.cache = ZoneCache(self.zones)
        self.occupancy = OccupancyMonitor(self.zones, self.activity, self.factory, tmp_path / "evidence", zones=self.cache)

    def run(self, camera_id: str, steps: list[list[EdgeDetection]], start: datetime, every: float = 1.0) -> datetime:
        at = start
        for detections in steps:
            self.occupancy.observe(camera_id, FRAME, detections, at)
            at += timedelta(seconds=every)
        return at

    def intervals(self, kind: str, start: datetime, hours: float = 24):
        return self.activity.intervals(kind=kind, start=start - timedelta(hours=hours), end=start + timedelta(hours=hours))


def _person(x: float, track_id: int | None = 1) -> EdgeDetection:
    return EdgeDetection("person", 0.9, x, 100.0, x + 60.0, 300.0, track_id=track_id)


def _truck(x: float) -> EdgeDetection:
    return EdgeDetection("truck", 0.8, x, 50.0, x + 200.0, 320.0, track_id=7)


def test_station_occupancy_becomes_intervals_and_a_vacancy_alert(tmp_path):
    env = _Env(tmp_path)
    station = env.zones.create_zone(
        camera_id="cam", name="Estação 4", zone_type="station", points=LEFT, settings={"vacant_alert_seconds": 60}
    )

    at = env.run("cam", [[_person(100)]] * 30, MORNING)
    # Detections flicker for a few frames without making the station vacant.
    at = env.run("cam", [[], [_person(100)], []] * 2, at)
    at = env.run("cam", [[]] * 120, at)

    intervals = env.intervals(PERSON_OCCUPANCY, MORNING)
    assert [item.state for item in intervals] == ["OCCUPIED", "VACANT"]
    occupied, vacant = intervals
    assert occupied.started_at == MORNING
    # Vacant from the last frame the operator was seen.
    assert vacant.started_at == MORNING + timedelta(seconds=34)
    [event] = env.zones.list_events(event_type="STATION_VACANT")
    assert event.zone_id == station.id
    assert event.status == "OPEN"
    assert event.started_at == vacant.started_at.isoformat()

    env.run("cam", [[_person(100)]] * 3, at)
    [event] = env.zones.list_events(event_type="STATION_VACANT")
    assert event.status == "CLOSED"
    assert event.duration == (at - vacant.started_at).total_seconds()


def test_presence_outside_the_shifts_raises_an_after_hours_event(tmp_path):
    env = _Env(tmp_path)
    env.factory.create_shift({"name": "Manhã", "start": "06:00", "end": "14:00", "days": [0, 1, 2, 3, 4]})
    env.zones.create_zone(camera_id="cam", name="Estoque", zone_type="area", points=LEFT)

    env.run("cam", [[_person(100)]] * 5 + [[]] * 15, MORNING)
    assert env.zones.list_events(event_type="AFTER_HOURS_PRESENCE") == []

    env.run("cam", [[_person(100)]] * 5 + [[]] * 15, NIGHT)
    [event] = env.zones.list_events(event_type="AFTER_HOURS_PRESENCE")
    assert event.severity == "critical"
    assert event.status == "CLOSED"
    assert event.metadata["subject"] == "person"
    assert event.started_at == NIGHT.isoformat()


def test_truck_parked_at_a_dock_is_a_visit_with_its_dwell_time(tmp_path):
    env = _Env(tmp_path)
    env.zones.create_zone(camera_id="cam", name="Doca 1", zone_type="dock", points=LEFT, settings={"min_visit_seconds": 60})

    # A car passing for a few seconds is not a visit.
    at = env.run("cam", [[_truck(50)]] * 5 + [[]] * 40, MORNING)
    at = env.run("cam", [[_truck(50)]] * 600 + [[]] * 40, at)

    [visit] = env.zones.list_events(event_type="DOCK_VISIT")
    assert visit.status == "CLOSED"
    assert visit.duration == 599.0
    occupied = [item for item in env.intervals(VEHICLE_OCCUPANCY, MORNING) if item.state == "OCCUPIED"]
    assert len(occupied) == 2


def test_tracks_crossing_a_counting_line_are_counted_by_direction(tmp_path):
    env = _Env(tmp_path)
    line = env.zones.create_zone(camera_id="cam", name="Portão", zone_type="line", points=[[0.5, 0.0], [0.5, 1.0]])

    # Person 1 walks left to right, person 2 right to left, person 3 stays left.
    steps = [[_person(x, 1), _person(600 - x, 2), _person(50, 3)] for x in range(100, 500, 50)]
    env.run("cam", steps, MORNING)

    [row] = env.activity.counts([line.id], MORNING, MORNING + timedelta(minutes=1))
    assert (row["forward"], row["backward"]) == (1, 1)


def _press_frame(stroke: bool, person_x: int | None = None) -> np.ndarray:
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    if stroke:
        frame[60:180, 60:260] = 255  # the ram comes down
    if person_x is not None:
        frame[100:300, person_x : person_x + 60] = 200
    return frame


class _Vision:
    def __init__(self) -> None:
        self.boxes: list[tuple[float, float, float, float]] = []

    def __call__(self, camera_id: str):
        return self.boxes


def _machine_env(tmp_path, **settings):
    env = _Env(tmp_path)
    vision = _Vision()
    machine = env.zones.create_zone(
        camera_id="cam",
        name="Prensa 3",
        zone_type="machine",
        points=[[0.05, 0.1], [0.45, 0.1], [0.45, 0.9], [0.05, 0.9]],
        settings={"stop_after_seconds": 30, "cost_per_hour": 600, **settings},
    )
    monitor = MachineMonitor(
        events_store=env.zones,
        activity=env.activity,
        occupancy=env.occupancy,
        evidence_dir=tmp_path / "evidence",
        people=vision,
        zones=env.cache,
    )
    return env, monitor, machine, vision


def _sample(monitor: MachineMonitor, frames: list[np.ndarray], start: datetime, every: float = 0.5) -> datetime:
    at = start
    for frame in frames:
        monitor.sample("cam", frame, at)
        at += timedelta(seconds=every)
    return at


def test_press_strokes_mean_running_and_silence_means_a_stop(tmp_path):
    env, monitor, machine, _vision = _machine_env(tmp_path)
    strokes = [_press_frame(index % 4 == 0) for index in range(80)]  # a stroke every 2 s for 40 s
    at = _sample(monitor, strokes, MORNING)
    at = _sample(monitor, [_press_frame(False)] * 120, at)  # 60 s still

    states = env.intervals(MACHINE, MORNING)
    assert [item.state for item in states] == ["RUNNING", "STOPPED"]
    running, stopped = states
    # The stop starts at the last movement, not when it was confirmed.
    assert stopped.started_at == MORNING + timedelta(seconds=38.5)
    [event] = env.zones.list_events(event_type="MACHINE_STOPPED")
    assert event.status == "OPEN" and event.zone_id == machine.id
    assert event.metadata["cost_per_hour"] == 600
    rows = env.activity.counts([machine.id], MORNING, at)
    # One cycle per stroke (the first stroke only confirms the machine is running).
    assert sum(row["cycles"] for row in rows) == 19

    _sample(monitor, [_press_frame(index % 4 == 0) for index in range(8)], at)
    [event] = env.zones.list_events(event_type="MACHINE_STOPPED")
    assert event.status == "CLOSED"
    assert event.duration == (at - stopped.started_at).total_seconds()


def test_an_operator_walking_in_front_of_a_stopped_machine_is_not_movement(tmp_path):
    env, monitor, _machine, vision = _machine_env(tmp_path)
    at = _sample(monitor, [_press_frame(False)] * 70, MORNING)
    assert [item.state for item in env.intervals(MACHINE, MORNING)] == ["STOPPED"]

    frames = []
    for index in range(20):
        x = 60 + index * 8
        frames.append((x, _press_frame(False, person_x=x)))
    for x, frame in frames:
        vision.boxes = [(x, 100, x + 60, 300)]
        monitor.sample("cam", frame, at)
        at += timedelta(seconds=0.5)

    assert [item.state for item in env.intervals(MACHINE, MORNING)] == ["STOPPED"]


def test_machine_running_without_operator_raises_missing_operator(tmp_path):
    env, monitor, _machine, _vision = _machine_env(tmp_path, requires_operator=True, operator_absent_seconds=20)
    at = MORNING
    # Operator present while the press starts...
    for index in range(20):
        env.occupancy.observe("cam", FRAME, [_person(150)], at)
        monitor.sample("cam", _press_frame(index % 4 == 0), at)
        at += timedelta(seconds=0.5)
    # ...then leaves while it keeps running.
    for index in range(100):
        env.occupancy.observe("cam", FRAME, [], at)
        monitor.sample("cam", _press_frame(index % 4 == 0), at)
        at += timedelta(seconds=0.5)

    [event] = env.zones.list_events(event_type="MISSING_OPERATOR")
    assert event.status == "OPEN"
    # Absent since the last frame the operator was seen.
    assert event.started_at == (MORNING + timedelta(seconds=9.5)).isoformat()


def test_camera_loss_is_unknown_not_downtime(tmp_path):
    env, monitor, _machine, _vision = _machine_env(tmp_path)
    at = _sample(monitor, [_press_frame(index % 4 == 0) for index in range(20)], MORNING)
    monitor.camera_unavailable("cam", "camera_frame_stale")
    _sample(monitor, [_press_frame(False)] * 10, at + timedelta(minutes=10))

    states = env.intervals(MACHINE, MORNING)
    assert [item.state for item in states] == ["RUNNING"]
    assert states[0].ended_at == at - timedelta(seconds=0.5)
    assert env.zones.list_events(event_type="MACHINE_STOPPED") == []
