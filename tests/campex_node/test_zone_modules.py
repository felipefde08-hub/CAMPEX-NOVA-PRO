"""Zone modules: machines watched by a signal light, occupancy limits and
how long one person stays."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from campex_node.activity import MACHINE
from campex_node.factory import normalize_zone_settings
from campex_node.local_app import create_app
from campex_node.monitors import MachineMonitor

from tests.campex_node.test_monitors import LEFT, MORNING, _Env, _person


# The light sits at x 64-128, y 36-108 of a 640x360 frame.
LIGHT = [[0.1, 0.1], [0.2, 0.1], [0.2, 0.3], [0.1, 0.3]]
RED, DARK_RED, GREEN = (0, 0, 255), (0, 0, 60), (0, 255, 0)


def _frame(color=DARK_RED, lit_share: float = 1.0) -> np.ndarray:
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    frame[36:108, 64:128] = DARK_RED
    width = int(64 * lit_share)
    frame[36:108, 64:64 + width] = color
    return frame


def _light_env(tmp_path, **settings):
    env = _Env(tmp_path)
    people: list = []
    zone = env.zones.create_zone(
        camera_id="cam", name="Injetora 3", zone_type="machine", points=LIGHT,
        settings={"detection": "light", "stop_after_seconds": 10, **settings},
    )
    monitor = MachineMonitor(
        events_store=env.zones, activity=env.activity, occupancy=env.occupancy,
        evidence_dir=tmp_path / "evidence", people=lambda camera_id: list(people), zones=env.cache,
    )
    return env, monitor, zone, people


def _sample(monitor, frames, start, every: float = 0.5):
    at = start
    for frame in frames:
        monitor.sample("cam", frame, at)
        at += timedelta(seconds=every)
    return at


def _states(env):
    return [item.state for item in env.intervals(MACHINE, MORNING)]


def test_a_lit_light_means_running_and_an_unlit_one_a_stop(tmp_path):
    env, monitor, zone, _people = _light_env(tmp_path)
    at = _sample(monitor, [_frame(RED)] * 20, MORNING)  # 10 s lit
    at = _sample(monitor, [_frame()] * 40, at)  # 20 s off
    assert _states(env) == ["RUNNING", "STOPPED"]
    [event] = env.zones.list_events(event_type="MACHINE_STOPPED")
    assert event.zone_id == zone.id and event.status == "OPEN"
    # The stop starts when the light went out (plus the blink hold), not when it was confirmed.
    stopped = env.intervals(MACHINE, MORNING)[1]
    assert stopped.started_at == MORNING + timedelta(seconds=9.5 + 2.0)
    # A light has no strokes: no cycles.
    assert env.activity.counts([zone.id], MORNING, at) == []


def test_a_blinking_light_keeps_the_machine_running(tmp_path):
    env, monitor, _zone, _people = _light_env(tmp_path)
    _sample(monitor, [_frame(RED) if index % 2 == 0 else _frame() for index in range(120)], MORNING)  # 60 s
    assert _states(env) == ["RUNNING"]
    assert env.zones.list_events(event_type="MACHINE_STOPPED") == []


def test_an_andon_light_that_means_stopped(tmp_path):
    env, monitor, _zone, _people = _light_env(tmp_path, light_means="stopped")
    at = _sample(monitor, [_frame()] * 20, MORNING)  # off: running
    _sample(monitor, [_frame(RED)] * 40, at)  # red on: stopped
    assert _states(env) == ["RUNNING", "STOPPED"]


def test_the_light_color_filters_out_other_lights(tmp_path):
    env, monitor, _zone, _people = _light_env(tmp_path, light_color="red")
    _sample(monitor, [_frame(GREEN)] * 40, MORNING)
    assert _states(env) == ["STOPPED"]


def test_a_dim_led_works_once_calibrated(tmp_path):
    env, monitor, zone, _people = _light_env(tmp_path)
    # Only 10% of the drawn area lights up: under the default 15% threshold.
    at = _sample(monitor, [_frame(RED, lit_share=0.1)] * 30, MORNING)
    assert _states(env) == ["STOPPED"]
    on = monitor.light_level(zone.id)
    at = _sample(monitor, [_frame()] * 2, at)
    off = monitor.light_level(zone.id)
    assert on == pytest.approx(0.0938, abs=0.01) and off == 0.0
    env.zones.update_zone(zone.id, {"settings": {"light_on_level": on, "light_off_level": off}})
    _sample(monitor, [_frame(RED, lit_share=0.1)] * 10, at + timedelta(seconds=2.5))
    assert _states(env) == ["STOPPED", "RUNNING"]


def test_a_person_in_front_of_the_light_says_nothing(tmp_path):
    env, monitor, _zone, people = _light_env(tmp_path)
    at = _sample(monitor, [_frame(RED)] * 10, MORNING)
    people.append((40.0, 20.0, 150.0, 340.0))  # covers the whole light
    _sample(monitor, [_frame()] * 60, at)
    assert _states(env) == ["RUNNING"]


def test_too_many_people_for_long_enough_is_an_occupancy_limit(tmp_path):
    env = _Env(tmp_path)
    zone = env.zones.create_zone(
        camera_id="cam", name="Refeitório", zone_type="occupancy", points=LEFT,
        settings={"max_people": 2, "over_limit_seconds": 10},
    )
    crowd = [_person(20, 1), _person(100, 2), _person(180, 3)]
    at = env.run("cam", [crowd] * 5, MORNING)  # 5 s over: not yet
    assert env.zones.list_events(event_type="OCCUPANCY_LIMIT") == []
    at = env.run("cam", [crowd[:2]] * 3 + [crowd] * 10, at)  # a short dip does not reset it
    [event] = env.zones.list_events(event_type="OCCUPANCY_LIMIT")
    assert event.status == "OPEN" and event.zone_id == zone.id
    assert event.started_at == MORNING.isoformat()
    assert event.metadata["max_people"] == 2

    env.run("cam", [crowd[:1]] * 15, at)
    [event] = env.zones.list_events(event_type="OCCUPANCY_LIMIT")
    assert event.status == "CLOSED"
    assert event.metadata["peak"] == 3


def test_one_person_staying_too_long_is_a_long_presence(tmp_path):
    env = _Env(tmp_path)
    env.zones.create_zone(
        camera_id="cam", name="Almoxarifado", zone_type="occupancy", points=LEFT, settings={"max_dwell_seconds": 30}
    )
    at = env.run("cam", [[_person(20, 1), _person(150, 2)]] * 10 + [[_person(20, 1)]] * 30, MORNING)
    [event] = env.zones.list_events(event_type="LONG_PRESENCE")
    assert event.status == "OPEN"
    assert event.metadata["track_id"] == 1  # person 2 left after 10 s
    assert event.started_at == MORNING.isoformat()

    env.run("cam", [[]] * 15, at)  # gone
    [event] = env.zones.list_events(event_type="LONG_PRESENCE")
    assert event.status == "CLOSED"
    assert event.metadata["dwell_seconds"] == 39.0


def test_settings_reject_unknown_light_options():
    assert normalize_zone_settings("machine", {"detection": "light"})["light_means"] == "running"
    with pytest.raises(ValueError):
        normalize_zone_settings("machine", {"detection": "sound"})
    with pytest.raises(ValueError):
        normalize_zone_settings("machine", {"light_color": "purple"})
    with pytest.raises(ValueError):
        normalize_zone_settings("machine", {"light_on_level": 2})


@contextmanager
def _node(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMPEX_NODE_DATA_DIR", str(tmp_path / "node"))
    monkeypatch.setenv("CAMPEX_NODE_RECORDING_ENABLED", "false")
    monkeypatch.setenv("CAMPEX_NODE_CAMERAS_JSON", '[{"id":"cam","name":"Galpão","rtsp_url":"rtsp://10.0.0.9/s","enabled":false}]')
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as local:
        yield app, local


def test_the_panel_calibrates_a_light_from_the_current_reading(monkeypatch, tmp_path):
    with _node(monkeypatch, tmp_path) as (app, local):
        created = local.post(
            "/api/zones",
            json={"camera_id": "cam", "name": "Torre 1", "type": "machine", "points": LIGHT, "settings": {"detection": "light"}},
        )
        assert created.status_code == 201, created.text
        zone_id = created.json()["id"]
        motion = local.post("/api/zones", json={"camera_id": "cam", "name": "Prensa", "type": "machine", "points": LIGHT}).json()

        assert local.post(f"/api/zones/{motion['id']}/calibrate-light", json={"state": "on"}).status_code == 400
        # No camera frames yet: nothing to read.
        assert local.post(f"/api/zones/{zone_id}/calibrate-light", json={"state": "on"}).status_code == 409

        machines = app.state.runtime.lifecycle.machines
        monkeypatch.setattr(machines, "light_level", lambda requested: 0.42 if requested == zone_id else None)
        response = local.post(f"/api/zones/{zone_id}/calibrate-light", json={"state": "on"})
        assert response.status_code == 200
        assert response.json()["settings"]["light_on_level"] == 0.42

        occupancy = local.post(
            "/api/zones",
            json={"camera_id": "cam", "name": "Refeitório", "type": "occupancy", "points": LEFT, "settings": {"max_people": 20}},
        )
        assert occupancy.status_code == 201 and occupancy.json()["settings"]["max_people"] == 20
