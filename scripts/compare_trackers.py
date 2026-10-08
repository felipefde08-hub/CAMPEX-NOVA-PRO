"""Compares the Node's trackers on a recorded video.

    python scripts/compare_trackers.py palace.mp4
    python scripts/compare_trackers.py gravacao.mp4 --interval 0.35 --out comparacao.mp4

Frames are sampled at the Node's vision interval and detected once with the
Node's YOLO settings; every tracker sees the same detections. Without ground
truth the numbers measure stability: fewer IDs for the same people, longer
tracks and fewer short-lived ones mean fewer identity breaks. --out writes a
side-by-side video to check the IDs by eye.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campex_node.tracking import create_tracker  # noqa: E402
from campex_node.vision import DETECTED_CLASSES, EdgeDetection  # noqa: E402

warnings.filterwarnings("ignore", category=FutureWarning)

TRACKERS = ("edge", "bytetrack")
SHORT_TRACK_SECONDS = 2.0


@dataclass
class Stats:
    first_seen: dict[int, float] = field(default_factory=dict)
    last_seen: dict[int, float] = field(default_factory=dict)
    detections: int = 0
    with_id: int = 0

    def observe(self, results: list[EdgeDetection], at: float) -> None:
        for item in results:
            if item.class_name != "person":
                continue
            self.detections += 1
            if item.track_id is not None:
                self.with_id += 1
                self.first_seen.setdefault(item.track_id, at)
                self.last_seen[item.track_id] = at

    def report(self, people_per_frame: float) -> dict[str, str]:
        durations = [self.last_seen[i] - self.first_seen[i] for i in self.first_seen]
        return {
            "IDs de pessoa": str(len(durations)),
            "IDs por pessoa visível": f"{len(durations) / people_per_frame:.1f}" if people_per_frame else "-",
            "Detecções com ID": f"{100 * self.with_id / self.detections:.0f}%" if self.detections else "-",
            "Duração mediana do track": f"{statistics.median(durations):.1f}s" if durations else "-",
            f"Tracks < {SHORT_TRACK_SECONDS:.0f}s": str(sum(1 for d in durations if d < SHORT_TRACK_SECONDS)),
        }


def detect(model, frame, confidence: float, input_size: int) -> list[EdgeDetection]:
    detections = []
    for result in model.predict(frame, conf=confidence, classes=list(DETECTED_CLASSES), imgsz=input_size, verbose=False):
        for xyxy, score, class_id in zip(result.boxes.xyxy.tolist(), result.boxes.conf.tolist(), result.boxes.cls.tolist()):
            name = DETECTED_CLASSES.get(int(class_id))
            if name:
                detections.append(EdgeDetection(name, round(float(score), 4), *(float(v) for v in xyxy[:4])))
    return detections


def draw(frame, results: list[EdgeDetection], title: str):
    canvas = frame.copy()
    for item in results:
        color = (60, 200, 90) if item.track_id is not None else (120, 120, 120)
        p1, p2 = (int(item.x1), int(item.y1)), (int(item.x2), int(item.y2))
        cv2.rectangle(canvas, p1, p2, color, 2)
        if item.track_id is not None:
            cv2.putText(canvas, f"#{item.track_id}", (p1[0], max(16, p1[1] - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    cv2.putText(canvas, title, (16, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
    return canvas


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video", type=Path)
    parser.add_argument("--interval", type=float, default=0.35, help="seconds between analysed frames (Node default)")
    parser.add_argument("--confidence", type=float, default=0.35)
    parser.add_argument("--input-size", type=int, default=640)
    parser.add_argument("--model", default=str(ROOT / "yolo11n.pt"))
    parser.add_argument("--max-seconds", type=float, default=0.0, help="stop after this much video (0 = all)")
    parser.add_argument("--out", type=Path, help="side-by-side video with each tracker's IDs")
    args = parser.parse_args(argv)

    from ultralytics import YOLO

    model = YOLO(args.model)
    capture = cv2.VideoCapture(str(args.video))
    if not capture.isOpened():
        raise SystemExit(f"Could not open {args.video}")
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    trackers = {name: create_tracker(name) for name in TRACKERS}
    stats = {name: Stats() for name in TRACKERS}
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    writer = None
    people_counts: list[int] = []
    next_sample, index = 0.0, 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        at = index / fps
        index += 1
        if args.max_seconds and at > args.max_seconds:
            break
        if at + 1e-6 < next_sample:
            continue
        next_sample += args.interval
        detections = detect(model, frame, args.confidence, args.input_size)
        people_counts.append(sum(1 for item in detections if item.class_name == "person"))
        panels = []
        for name, tracker in trackers.items():
            results = tracker.update(detections, start + timedelta(seconds=at))
            stats[name].observe(results, at)
            if args.out:
                panels.append(draw(frame, results, name))
        if args.out:
            joined = cv2.hconcat(panels)
            if writer is None:
                height, width = joined.shape[:2]
                writer = cv2.VideoWriter(str(args.out), cv2.VideoWriter_fourcc(*"mp4v"), 1 / args.interval, (width, height))
            writer.write(joined)
    capture.release()
    if writer is not None:
        writer.release()

    people_per_frame = statistics.mean(people_counts) if people_counts else 0.0
    reports = {name: stats[name].report(people_per_frame) for name in TRACKERS}
    print(f"{args.video.name}: {len(people_counts)} quadros analisados a cada {args.interval}s, "
          f"{people_per_frame:.1f} pessoas por quadro em média\n")
    rows = list(next(iter(reports.values())))
    width = max(len(row) for row in rows)
    print(f"{'':<{width}}  " + "  ".join(f"{name:>10}" for name in TRACKERS))
    for row in rows:
        print(f"{row:<{width}}  " + "  ".join(f"{reports[name][row]:>10}" for name in TRACKERS))
    if args.out:
        print(f"\nVídeo lado a lado: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
