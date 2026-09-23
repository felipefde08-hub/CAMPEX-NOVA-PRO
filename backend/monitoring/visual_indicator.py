from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class ColorClassification:
    state_id: str | None
    state_name: str | None
    confidence: float
    hsv: dict[str, float]


def dominant_hsv(frame) -> dict[str, float]:
    if frame is None or getattr(frame, "size", 0) == 0:
        return {"h": 0.0, "s": 0.0, "v": 0.0}
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    pixels = hsv.reshape((-1, 3))
    median = np.median(pixels, axis=0)
    return {"h": float(median[0]), "s": float(median[1]), "v": float(median[2])}


def classify_hsv(
    hsv: dict[str, float],
    states: list[dict[str, Any]],
) -> ColorClassification:
    best: tuple[float, dict[str, Any]] | None = None
    for state in states:
        target = state.get("hsv_target") or {}
        tolerance = state.get("tolerance") or {}
        score = _match_score(hsv, target, tolerance)
        if score is not None and (best is None or score > best[0]):
            best = (score, state)
    if best is None:
        return ColorClassification(None, None, 0.0, hsv)
    state = best[1]
    return ColorClassification(
        state_id=state.get("id"),
        state_name=state.get("name"),
        confidence=round(best[0], 4),
        hsv=hsv,
    )


def _match_score(hsv: dict[str, float], target: dict[str, float], tolerance: dict[str, float]) -> float | None:
    h_tol = float(tolerance.get("h", 12))
    s_tol = float(tolerance.get("s", 80))
    v_tol = float(tolerance.get("v", 80))
    h_delta = min(abs(float(hsv["h"]) - float(target.get("h", 0))), 180 - abs(float(hsv["h"]) - float(target.get("h", 0))))
    s_delta = abs(float(hsv["s"]) - float(target.get("s", 0)))
    v_delta = abs(float(hsv["v"]) - float(target.get("v", 0)))
    if h_delta > h_tol or s_delta > s_tol or v_delta > v_tol:
        return None
    h_score = 1 - (h_delta / max(h_tol, 1))
    s_score = 1 - (s_delta / max(s_tol, 1))
    v_score = 1 - (v_delta / max(v_tol, 1))
    return max(0.0, min(1.0, (h_score * 0.5) + (s_score * 0.25) + (v_score * 0.25)))


class DebouncedStateMachine:
    def __init__(self, debounce_seconds: float = 2.0) -> None:
        self.debounce_seconds = debounce_seconds
        self.current_state_id: str | None = None
        self.candidate_state_id: str | None = None
        self.candidate_since: datetime | None = None

    def observe(self, state_id: str | None, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        if state_id == self.current_state_id:
            self.candidate_state_id = None
            self.candidate_since = None
            return {"changed": False, "stable": True, "current_state_id": self.current_state_id}
        if state_id != self.candidate_state_id:
            self.candidate_state_id = state_id
            self.candidate_since = now
            return {"changed": False, "stable": False, "current_state_id": self.current_state_id}
        elapsed = (now - (self.candidate_since or now)).total_seconds()
        if elapsed < self.debounce_seconds:
            return {
                "changed": False,
                "stable": False,
                "current_state_id": self.current_state_id,
                "candidate_state_id": state_id,
                "candidate_seconds": elapsed,
            }
        previous = self.current_state_id
        self.current_state_id = state_id
        self.candidate_state_id = None
        self.candidate_since = None
        return {
            "changed": previous != state_id,
            "stable": True,
            "previous_state_id": previous,
            "current_state_id": state_id,
        }

