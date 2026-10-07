from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from campex_node.activity import MACHINE, PERSON_OCCUPANCY, ActivityStore
from campex_node.analytics import FactoryAnalytics, resolve_period
from campex_node.events import NodeEventStore
from campex_node.factory import FactoryStore


DAY = date(2026, 10, 7)  # Wednesday
SQUARE = [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4], [0.1, 0.4]]


def local(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    # São Paulo is UTC-3.
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc) + timedelta(hours=3)


@pytest.fixture
def plant(tmp_path):
    database = tmp_path / "node.sqlite3"
    zones = NodeEventStore(database)
    zones.initialize()
    activity = ActivityStore(database)
    activity.initialize()
    factory = FactoryStore(database)
    factory.initialize()
    factory.create_shift(
        {
            "name": "Manhã",
            "start": "06:00",
            "end": "14:00",
            "days": [0, 1, 2, 3, 4],
            "breaks": [{"name": "Almoço", "start": "11:00", "end": "12:00"}],
        }
    )
    factory.create_shift({"name": "Tarde", "start": "14:00", "end": "22:00", "days": [0, 1, 2, 3, 4]})
    return zones, activity, factory, FactoryAnalytics(zones, activity, factory)


def _interval(activity, kind, zone, state, start, end):
    interval_id = activity.open(kind=kind, zone_id=zone.id, camera_id=zone.camera_id, state=state, started_at=start)
    if end is not None:
        activity.close(interval_id, end)


def _press(zones, activity, name="Prensa 3", cost=600.0):
    press = zones.create_zone(
        camera_id="cam", name=name, zone_type="machine", points=SQUARE, settings={"cost_per_hour": cost, "line": "Linha 2"}
    )
    timeline = [
        ("RUNNING", local(6), local(8)),
        ("STOPPED", local(8), local(8, 30)),  # 30 min, in shift
        ("RUNNING", local(8, 30), local(13, 50)),
        ("STOPPED", local(13, 50), local(14, 5)),
        ("RUNNING", local(14, 5), local(21)),
        ("STOPPED", local(21), local(23)),  # one hour after the last shift
    ]
    for state, start, end in timeline:
        _interval(activity, MACHINE, press, state, start, end)
    activity.add_count(press.id, local(7), "cycles", 600)
    activity.add_count(press.id, local(15), "cycles", 900)
    return press


def test_machine_downtime_and_cost_count_only_inside_shifts(plant):
    zones, activity, _factory, analytics = plant
    _press(zones, activity)

    report = analytics.machines(local(0), local(0, day=DAY + timedelta(days=1)), now=local(23, 59))
    [machine] = report["machines"]
    assert machine["stops"] == 3
    assert machine["stopped_total_seconds"] == (30 + 15 + 120) * 60
    # Only time inside the shifts is downtime: the hour after 22:00 is not lost production.
    assert machine["stopped_seconds"] == (30 + 15 + 60) * 60
    assert machine["lost_cost"] == 1050.0
    assert machine["longest_stop"]["duration_seconds"] == 2 * 3600
    assert machine["longest_stop"]["productive_seconds"] == 3600
    assert machine["cycles"] == 1500
    assert report["currency"] == "BRL"


def test_stops_longer_than_fifteen_minutes_link_to_the_video_before(plant):
    zones, activity, _factory, analytics = plant
    _press(zones, activity)

    stops = analytics.stops(local(0), local(23, 59), min_seconds=15 * 60 + 1, now=local(23, 59))
    assert [stop["duration_seconds"] for stop in stops] == [30 * 60, 2 * 3600]
    assert stops[0]["shift"] == "Manhã"
    assert stops[0]["cost"] == 300.0


def test_shift_report_ranks_production_and_downtime(plant):
    zones, activity, _factory, analytics = plant
    _press(zones, activity)

    report = analytics.shifts(local(0), local(23, 59), now=local(23, 59))
    by_name = {item["shift"]: item for item in report["by_shift"]}
    assert by_name["Manhã"]["cycles"] == 600
    assert by_name["Tarde"]["cycles"] == 900
    # Morning: 30 + 10 min stopped; afternoon: 5 + 60 min (until 22:00).
    assert by_name["Manhã"]["stopped_seconds"] == 40 * 60
    assert by_name["Tarde"]["stopped_seconds"] == 65 * 60
    assert report["most_productive"] == "Tarde"
    assert report["most_downtime"] == "Tarde"


def test_first_arrival_and_lunch_overrun(plant):
    zones, activity, _factory, analytics = plant
    station = zones.create_zone(camera_id="cam", name="Estação 4", zone_type="station", points=SQUARE)
    _interval(activity, PERSON_OCCUPANCY, station, "VACANT", local(5), local(6, 12))
    _interval(activity, PERSON_OCCUPANCY, station, "OCCUPIED", local(6, 12), local(11))
    _interval(activity, PERSON_OCCUPANCY, station, "VACANT", local(11), local(12, 20))
    _interval(activity, PERSON_OCCUPANCY, station, "OCCUPIED", local(12, 20), None)

    arrival = analytics.first_arrival(DAY)
    assert arrival["first"]["local_time"] == "06:12:00"

    [lunch] = analytics.breaks(DAY, now=local(18))
    assert lunch["name"] == "Almoço"
    assert lunch["overrun"] is True
    assert lunch["max_late_seconds"] == 20 * 60


def test_after_hours_lists_only_presence_outside_the_shifts(plant):
    zones, activity, _factory, analytics = plant
    stock = zones.create_zone(camera_id="cam", name="Estoque", zone_type="area", points=SQUARE)
    _interval(activity, PERSON_OCCUPANCY, stock, "OCCUPIED", local(10), local(10, 30))
    _interval(activity, PERSON_OCCUPANCY, stock, "OCCUPIED", local(23, 10), local(23, 40))

    result = analytics.after_hours(local(0), local(0, day=DAY + timedelta(days=1)), now=local(23, 59))
    [presence] = result["presences"]
    assert presence["zone_name"] == "Estoque"
    assert presence["outside_seconds"] == 30 * 60


def test_summary_compares_with_the_previous_period_and_ranks_problems(plant):
    zones, activity, _factory, analytics = plant
    _press(zones, activity)
    other = zones.create_zone(camera_id="cam", name="Torno 1", zone_type="machine", points=SQUARE, settings={"cost_per_hour": 100})
    _interval(activity, MACHINE, other, "STOPPED", local(9), local(9, 30))
    # Yesterday the press stopped only 10 minutes.
    yesterday = DAY - timedelta(days=1)
    press_id = zones.list_zones()[0].id
    activity.close(
        activity.open(kind=MACHINE, zone_id=press_id, camera_id="cam", state="STOPPED", started_at=local(9, day=yesterday)),
        local(9, 10, day=yesterday),
    )

    summary = analytics.summary(local(0), local(0, day=DAY + timedelta(days=1)), now=local(23, 59, day=DAY + timedelta(days=1)))
    assert summary["current"]["stops"] == 4
    assert summary["previous"]["stops"] == 1
    assert summary["changes"]["stops"] == 3.0
    assert [problem["title"] for problem in summary["top_problems"]] == ["Prensa 3 parada 3x", "Torno 1 parada 1x"]


def test_named_periods_follow_plant_time(plant):
    _zones, _activity, factory, _analytics = plant
    now = local(1, 30, day=date(2026, 10, 8))  # 04:30 UTC on Oct 8
    start, end = resolve_period(factory, period="yesterday", now=now)
    assert (start, end) == (local(0), local(0, day=date(2026, 10, 8)))
    start, _end = resolve_period(factory, period="month", now=now)
    assert start == local(0, day=date(2026, 10, 1))
    with pytest.raises(ValueError):
        resolve_period(factory, period="decade", now=now)


def test_breaks_are_planned_time_not_downtime_or_vacancy(plant):
    zones, activity, _factory, analytics = plant
    press = zones.create_zone(camera_id="cam", name="Prensa 3", zone_type="machine", points=SQUARE, settings={"cost_per_hour": 600})
    station = zones.create_zone(camera_id="cam", name="Estação 4", zone_type="station", points=SQUARE)
    # Stopped and empty over lunch (11:00-12:00), 15 min past it.
    _interval(activity, MACHINE, press, "STOPPED", local(11), local(12, 15))
    _interval(activity, PERSON_OCCUPANCY, station, "VACANT", local(11), local(12, 15))

    [machine] = analytics.machines(local(0), local(23, 59), now=local(23, 59))["machines"]
    assert machine["stopped_total_seconds"] == 75 * 60
    assert machine["stopped_seconds"] == 15 * 60
    assert machine["lost_cost"] == 150.0
    [stop] = analytics.stops(local(0), local(23, 59), now=local(23, 59))
    assert (stop["productive_seconds"], stop["cost"]) == (15 * 60, 150.0)

    [zone] = analytics.occupancy(local(0), local(23, 59), now=local(23, 59))
    assert zone["vacant_seconds"] == 15 * 60
    assert [item["duration_seconds"] for item in zone["vacancies"]] == [15 * 60]


def test_a_stop_entirely_inside_a_break_is_not_a_stop(plant):
    zones, activity, _factory, analytics = plant
    lathe = zones.create_zone(camera_id="cam", name="Torno 1", zone_type="machine", points=SQUARE, settings={"cost_per_hour": 100})
    _interval(activity, MACHINE, lathe, "RUNNING", local(10), local(11, 5))
    _interval(activity, MACHINE, lathe, "STOPPED", local(11, 5), local(11, 50))
    _interval(activity, MACHINE, lathe, "RUNNING", local(11, 50), local(13))

    [machine] = analytics.machines(local(0), local(23, 59), now=local(23, 59))["machines"]
    assert (machine["stops"], machine["stopped_seconds"], machine["lost_cost"]) == (0, 0, 0.0)
    assert machine["stopped_total_seconds"] == 45 * 60
