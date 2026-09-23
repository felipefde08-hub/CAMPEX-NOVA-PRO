from __future__ import annotations

from backend.monitoring.models import NormalizedRect


def normalize_rect(
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    frame_width: float | None = None,
    frame_height: float | None = None,
) -> NormalizedRect:
    if frame_width and frame_height and (x > 1 or y > 1 or width > 1 or height > 1):
        x = x / frame_width
        width = width / frame_width
        y = y / frame_height
        height = height / frame_height
    rect = NormalizedRect(float(x), float(y), float(width), float(height))
    validate_rect(rect)
    return rect


def validate_rect(rect: NormalizedRect) -> None:
    if rect.width <= 0 or rect.height <= 0:
        raise ValueError("ROI width and height must be greater than zero.")
    if rect.x < 0 or rect.y < 0:
        raise ValueError("ROI x/y must be normalized values from 0.0 to 1.0.")
    if rect.x + rect.width > 1.0 or rect.y + rect.height > 1.0:
        raise ValueError("ROI must stay inside the normalized frame bounds.")


def crop_normalized_rect(frame, rect: NormalizedRect):
    height, width = frame.shape[:2]
    x1 = max(0, min(width - 1, int(round(rect.x * width))))
    y1 = max(0, min(height - 1, int(round(rect.y * height))))
    x2 = max(x1 + 1, min(width, int(round((rect.x + rect.width) * width))))
    y2 = max(y1 + 1, min(height, int(round((rect.y + rect.height) * height))))
    return frame[y1:y2, x1:x2]

