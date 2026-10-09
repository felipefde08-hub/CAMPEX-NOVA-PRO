"""MachineMonitor with people boxes that are older than the sampled frame."""

from __future__ import annotations

from datetime import timedelta

from campex_node import monitors
from campex_node.activity import MACHINE

from tests.campex_node.test_monitors import MORNING, _machine_env, _press_frame, _sample

STEP = 0.5  # seconds between machine samples


class _TimedVision:
    """people_observation(): boxes plus the time of the frame they came from."""

    def __init__(self) -> None:
        self.observation = (None, [])

    def __call__(self, camera_id: str):
        return self.observation


def _timed_env(tmp_path, **settings):
    env, monitor, machine, _vision = _machine_env(tmp_path, **settings)
    vision = _TimedVision()
    monitor.people = vision
    return env, monitor, machine, vision


def _walk_past(monitor, vision, at, *, lag: float, speed_px: float = 40.0, steps: int = 6):
    """An operator walks in front of the machine; vision reports them `lag` s late."""
    for index in range(steps):
        x = 40 + index * speed_px * STEP
        seen_x = x - speed_px * lag  # where the person was when vision saw them
        vision.observation = (at - timedelta(seconds=lag), [(seen_x, 100, seen_x + 60, 300)])
        monitor.sample("cam", _press_frame(False, person_x=int(x)), at)
        at += timedelta(seconds=STEP)
    return at


def _states(env):
    return [item.state for item in env.intervals(MACHINE, MORNING)]


def test_late_boxes_of_a_walking_operator_do_not_start_a_stopped_machine(tmp_path, monkeypatch):
    env, monitor, _machine, vision = _timed_env(tmp_path)
    at = _sample(monitor, [_press_frame(False)] * 70, MORNING)
    assert _states(env) == ["STOPPED"]

    _walk_past(monitor, vision, at, lag=0.5)

    assert _states(env) == ["STOPPED"]


def test_without_accounting_for_the_lag_the_same_walk_looked_like_a_running_machine(tmp_path, monkeypatch):
    # Control: masking the late boxes where they were reported (the previous
    # behaviour) leaves the walking operator visible as machine movement.
    monkeypatch.setattr(monitors, "PERSON_MAX_SPEED_BOX_WIDTHS", 0.0)
    env, monitor, _machine, vision = _timed_env(tmp_path)
    at = _sample(monitor, [_press_frame(False)] * 70, MORNING)

    _walk_past(monitor, vision, at, lag=0.5)

    assert _states(env) == ["STOPPED", "RUNNING"]


def test_boxes_too_old_to_place_people_make_movement_inconclusive(tmp_path):
    env, monitor, _machine, vision = _timed_env(tmp_path)
    at = _sample(monitor, [_press_frame(False)] * 70, MORNING)

    _walk_past(monitor, vision, at, lag=3.0)

    assert _states(env) == ["STOPPED"]
    [snapshot] = monitor.snapshot("cam")
    assert snapshot["conclusive"] is False
    assert snapshot["people_observation_age_ms"] == 3000


def test_a_running_press_is_still_seen_with_people_elsewhere(tmp_path):
    env, monitor, _machine, vision = _timed_env(tmp_path)
    at = MORNING
    for index in range(20):
        # Fresh boxes of someone far from the machine.
        vision.observation = (at - timedelta(seconds=0.2), [(500, 100, 560, 300)])
        monitor.sample("cam", _press_frame(index % 4 == 0), at)
        at += timedelta(seconds=STEP)

    assert _states(env) == ["RUNNING"]
    assert monitor.snapshot("cam")[0]["conclusive"] is True


def test_a_machine_hidden_behind_people_is_not_declared_stopped(tmp_path):
    env, monitor, _machine, vision = _timed_env(tmp_path)
    at = _sample(monitor, [_press_frame(index % 4 == 0) for index in range(20)], MORNING)
    assert _states(env) == ["RUNNING"]
    last_motion = MORNING + timedelta(seconds=8.5)  # the ram going back up after the last stroke

    # 40 s with the machine area covered by people: it cannot be seen to stop.
    for _ in range(80):
        vision.observation = (at, [(0, 0, 320, 360)])
        monitor.sample("cam", _press_frame(False), at)
        at += timedelta(seconds=STEP)
    assert _states(env) == ["RUNNING"]

    # Once visible again, 30 s of stillness confirm the stop, dated from the last movement.
    vision.observation = (None, [])
    _sample(monitor, [_press_frame(False)] * 62, at)
    states = env.intervals(MACHINE, MORNING)
    assert [item.state for item in states] == ["RUNNING", "STOPPED"]
    assert states[1].started_at == last_motion


def test_a_provider_without_timestamps_keeps_the_previous_behaviour(tmp_path):
    env, monitor, _machine, vision = _machine_env(tmp_path)
    at = _sample(monitor, [_press_frame(False)] * 70, MORNING)
    for index in range(20):
        x = 60 + index * 8
        vision.boxes = [(x, 100, x + 60, 300)]
        monitor.sample("cam", _press_frame(False, person_x=x), at)
        at += timedelta(seconds=STEP)

    assert _states(env) == ["STOPPED"]
