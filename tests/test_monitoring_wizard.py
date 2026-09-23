from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from backend.database.db import initialize_database
from backend.monitoring.repository import MonitoringRepository
from backend.monitoring.roi import normalize_rect
from backend.monitoring.visual_indicator import DebouncedStateMachine, classify_hsv, dominant_hsv
from tests.helpers import make_settings


def test_roi_normalization_from_pixels():
    rect = normalize_rect(x=620, y=210, width=80, height=70, frame_width=1000, frame_height=1000)
    assert rect.as_dict() == {"x": 0.62, "y": 0.21, "width": 0.08, "height": 0.07}


def test_visual_indicator_classifies_simulated_led_colors():
    green_bgr = np.zeros((20, 20, 3), dtype=np.uint8)
    green_bgr[:, :] = (0, 255, 0)
    hsv = dominant_hsv(green_bgr)
    result = classify_hsv(
        hsv,
        [
            {"id": "green", "name": "Verde", "hsv_target": {"h": 60, "s": 255, "v": 255}, "tolerance": {"h": 8, "s": 20, "v": 20}},
            {"id": "red", "name": "Vermelho", "hsv_target": {"h": 0, "s": 255, "v": 255}, "tolerance": {"h": 8, "s": 20, "v": 20}},
        ],
    )
    assert result.state_id == "green"
    assert result.confidence > 0.95


def test_state_machine_debounces_short_noise():
    machine = DebouncedStateMachine(debounce_seconds=2)
    start = datetime.now(timezone.utc)
    assert not machine.observe("green", start)["changed"]
    assert not machine.observe("green", start + timedelta(seconds=1))["changed"]
    confirmed = machine.observe("green", start + timedelta(seconds=2.1))
    assert confirmed["changed"]
    assert confirmed["current_state_id"] == "green"
    assert not machine.observe("red", start + timedelta(seconds=2.2))["changed"]
    assert machine.current_state_id == "green"


def test_monitoring_repository_isolates_organizations(tmp_path):
    settings = make_settings(tmp_path / "campex.sqlite3")
    initialize_database(settings)
    repo = MonitoringRepository(settings)

    roi_a = repo.create_roi(
        organization_id="org_a",
        camera_id="cam_1",
        name="LED A",
        roi_type="led",
        shape="rect",
        coordinates={"x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1},
    )
    repo.create_roi(
        organization_id="org_b",
        camera_id="cam_1",
        name="LED B",
        roi_type="led",
        shape="rect",
        coordinates={"x": 0.2, "y": 0.2, "width": 0.1, "height": 0.1},
    )

    monitor = repo.create_monitor(
        organization_id="org_a",
        camera_id="cam_1",
        roi_id=roi_a.id,
        monitor_type="visual_indicator",
        name="LED",
        configuration={"debounce_seconds": 2},
    )
    states = repo.replace_states(
        organization_id="org_a",
        monitor_id=monitor.id,
        states=[
            {"name": "Verde", "operational_meaning": "Operando", "color": "#00ff00", "hsv_target": {"h": 60, "s": 255, "v": 255}},
            {"name": "Vermelho", "operational_meaning": "Parada", "color": "#ff0000", "hsv_target": {"h": 0, "s": 255, "v": 255}, "is_stop_state": True},
        ],
    )
    transition = repo.record_transition(
        organization_id="org_a",
        camera_id="cam_1",
        monitor_id=monitor.id,
        roi_id=roi_a.id,
        previous_state_id=states[0].id,
        current_state_id=states[1].id,
        confidence=0.99,
    )

    assert [roi.id for roi in repo.list_rois("org_a")] == [roi_a.id]
    assert repo.list_rois("org_b")[0].name == "LED B"
    assert repo.get_monitor(monitor.id, "org_b") is None
    assert transition["organization_id"] == "org_a"

