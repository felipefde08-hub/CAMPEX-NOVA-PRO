"""Benchmark reproduzível do motor de visão do CAMPEX Node.

Roda o EdgeVisionService real (scheduler, EdgeTracker, NodeEventPipeline com
zonas, OccupancyMonitor) contra câmeras simuladas sobre o LatestFrameBuffer real.

Detector:
  --detector synthetic  (padrão) consome CPU de verdade por --inference-ms,
                        calibrado nesta máquina, e devolve as caixas do gabarito
                        com ruído. Mede o ENCANAMENTO, não a precisão do YOLO.
  --detector yolo       usa o detector real do Node (Ultralytics). Exige
                        ultralytics/torch instalados; as caixas vêm do modelo.

Uso (na raiz do repositório):
  python scripts/benchmark_vision.py . --cameras 1 2 4 --inference-ms 50 150 \
      --seconds 15 --zones none --out resultado.json

<repo_root> pode apontar para outra cópia do código, para comparar versões nas
mesmas condições. --zones corridor põe uma zona cruzada pelas pessoas: eventos
e clipes contínuos, o cenário de estresse da evidência.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo")
    parser.add_argument("--cameras", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--inference-ms", type=float, nargs="+", default=[50.0, 150.0])
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--warmup", type=float, default=2.0)
    parser.add_argument("--camera-fps", type=float, default=25.0)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--detector", choices=["synthetic", "yolo"], default="synthetic")
    parser.add_argument("--video", help="com --detector yolo: vídeo reproduzido como câmera")
    parser.add_argument("--zones", choices=["corridor", "none"], default="corridor",
                        help="corridor: zona cruzada pelas pessoas (muitos eventos e clipes); none: só o scheduler")
    parser.add_argument("--label", default="")
    parser.add_argument("--out")
    args = parser.parse_args()
    sys.path.insert(0, args.repo)

    import numpy as np  # noqa: F401  (import tardio: depois do sys.path)
    import psutil

    burn = _Burner() if args.detector == "synthetic" else None
    rows = []
    latencies = args.inference_ms if args.detector == "synthetic" else [0.0]
    for cameras in args.cameras:
        for inference_ms in latencies:
            row = run(args, cameras, inference_ms, burn, psutil)
            row["label"] = args.label
            print(json.dumps(row, ensure_ascii=False), flush=True)
            rows.append(row)
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")


class _Burner:
    """Trabalho de CPU real (OpenCV, multi-thread como o PyTorch) por N ms."""

    def __init__(self) -> None:
        import cv2
        import numpy as np

        self.cv2 = cv2
        self.image = np.random.randint(0, 255, (640, 640, 3), np.uint8)
        started = time.perf_counter()
        for _ in range(20):
            cv2.GaussianBlur(self.image, (15, 15), 0)
        self.ms_per_op = (time.perf_counter() - started) * 1000 / 20

    def burn(self, ms: float) -> None:
        for _ in range(max(1, round(ms / self.ms_per_op))):
            self.cv2.GaussianBlur(self.image, (15, 15), 0)


BOX_W, BOX_H = 70.0, 170.0
WALKERS = 4
SPEED_BW = 2.5  # larguras de corpo por segundo (andando)


def ground_truth(camera: int, t: float, width: int, height: int):
    people = []
    for pid in range(WALKERS):
        direction = 1 if (pid + camera) % 2 == 0 else -1
        span = width - BOX_W
        travel = 150 + pid * 260 + direction * SPEED_BW * BOX_W * t
        lap = int(travel // span)
        x = travel % span
        y = 120 + pid * (height - 320) / max(1, WALKERS - 1)
        people.append((pid * 1000 + lap, (x, y, x + BOX_W, y + BOX_H)))
    return people


def run(args, n_cameras: int, inference_ms: float, burner, psutil) -> dict:
    import numpy as np

    from backend.cameras.frame_buffer import LatestFrameBuffer
    from campex_node.activity import ActivityStore
    from campex_node.core.config import NodeCameraConfig, NodeSettings
    from campex_node.events import NodeEventPipeline, NodeEventStore
    from campex_node.factory import FactoryStore
    from campex_node.monitors import OccupancyMonitor, ZoneCache
    from campex_node.vision import EdgeDetection, EdgeVisionService

    tmp = Path(tempfile.mkdtemp(prefix="campex-bench-"))
    settings = NodeSettings(
        environment="bench", version="bench", log_level="WARNING", data_dir=tmp,
        database_path=tmp / "node.sqlite3", node_id_file=tmp / "node_id",
        cloud_url="https://cloud.invalid/api/v1", cloud_token="x",
    )
    database = tmp / "node.sqlite3"
    store = NodeEventStore(database)
    store.initialize()
    activity = ActivityStore(database)
    activity.initialize()
    factory = FactoryStore(database)
    factory.initialize()
    configs = [
        NodeCameraConfig(id=f"cam{index}", name=f"cam{index}", rtsp_url="rtsp://bench", vision_enabled=True)
        for index in range(n_cameras)
    ]
    for config in configs if args.zones == "corridor" else []:
        store.create_zone(camera_id=config.id, name="Corredor", zone_type="monitored",
                          points=[[0.3, 0.0], [0.7, 0.0], [0.7, 1.0], [0.3, 1.0]])
    events = NodeEventPipeline(store, tmp, fps=1.0 / max(0.05, settings.vision_interval_seconds))
    occupancy = OccupancyMonitor(store, activity, factory, tmp / "evidence", zones=ZoneCache(store))

    t0 = time.monotonic()
    frame_times: dict[tuple[int, int], float] = {}
    base = np.random.randint(0, 255, (args.height, args.width, 3), np.uint8)
    video = None
    if args.video:
        import cv2

        video = cv2.VideoCapture(args.video)

    class Cameras:
        def __init__(self):
            self.buffers = {config.id: LatestFrameBuffer() for config in configs}
            self.counter = 0
            self.stop = threading.Event()

        def configs(self):
            return list(configs)

        def states(self):
            return []

        def latest_frame(self, camera_id):
            return self.buffers[camera_id].latest()

        def latest_snapshot(self, camera_id):
            return self.buffers[camera_id].snapshot()

        def latest_identity(self, camera_id):
            return self.buffers[camera_id].identity()

        def feed(self):
            period = 1.0 / args.camera_fps
            next_at = time.monotonic()
            while not self.stop.is_set():
                self.counter += 1
                now = time.monotonic()
                if video is not None:
                    ok, frame = video.read()
                    if not ok:
                        video.set(1, 0)
                        ok, frame = video.read()
                else:
                    frame = base
                for index, config in enumerate(configs):
                    frame_times[(index, self.counter)] = now - t0
                    tagged = frame.copy()
                    tagged[0, 0] = (index, self.counter & 255, (self.counter >> 8) & 255)
                    tagged[0, 1] = ((self.counter >> 16) & 255, 0, 0)
                    self.buffers[config.id].put(tagged, datetime.now(timezone.utc))
                next_at += period
                self.stop.wait(max(0.0, next_at - time.monotonic()))

    cameras = Cameras()

    def identify(frame):
        p, q = frame[0, 0], frame[0, 1]
        index = int(p[0])
        counter = int(p[1]) | int(p[2]) << 8 | int(q[0]) << 16
        return index, counter

    starts: dict[int, list[float]] = {i: [] for i in range(n_cameras)}
    frame_age_at_start: list[float] = []
    rng = random.Random(7)

    class BenchVision(EdgeVisionService):
        def _detect(self, frame):
            index, counter = identify(frame)
            now = time.monotonic() - t0
            starts[index].append(now)
            frame_age_at_start.append(now - frame_times[(index, counter)])
            if burner is None:
                return super()._detect(frame)
            burner.burn(inference_ms)
            t = frame_times[(index, counter)]
            boxes = []
            for _pid, (x1, y1, x2, y2) in ground_truth(index, t, args.width, args.height):
                j = lambda: rng.gauss(0, 0.03 * BOX_W)  # noqa: E731
                boxes.append(EdgeDetection("person", 0.9, x1 + j(), y1 + j(), x2 + j(), y2 + j()))
            return boxes, "synthetic"

    post_task: list[float] = []
    original_process = events.process

    def timed_process(*a, **k):
        t = time.perf_counter()
        try:
            return original_process(*a, **k)
        finally:
            post_task.append(time.perf_counter() - t)

    events.process = timed_process
    vision = BenchVision(settings, cameras, events, observers=[occupancy])
    process = psutil.Process()
    feeder = threading.Thread(target=cameras.feed, daemon=True)
    feeder.start()
    time.sleep(0.3)
    vision.start()
    time.sleep(args.warmup)

    stop = threading.Event()
    publish_latency: list[float] = []
    overlay_age: list[float] = []
    id_samples: dict[int, list] = {i: [] for i in range(n_cameras)}

    def poll_results():
        seen: dict[int, object] = {}
        while not stop.is_set():
            for index in range(n_cameras):
                result = vision.latest_result(f"cam{index}")
                if result is None or seen.get(index) == result.key:
                    continue
                seen[index] = result.key
                _, counter = identify(result.frame)
                frame_t = frame_times[(index, counter)]
                publish_latency.append(time.monotonic() - t0 - frame_t)
                id_samples[index].append((frame_t, [(d.track_id, d) for d in result.detections]))
            time.sleep(0.002)

    def sample_overlay():
        while not stop.is_set():
            for index in range(n_cameras):
                result = vision.overlay_result(f"cam{index}")
                if result is not None:
                    _, counter = identify(result.frame)
                    overlay_age.append(time.monotonic() - t0 - frame_times[(index, counter)])
            time.sleep(0.15)  # período do MJPEG local

    pollers = [threading.Thread(target=poll_results, daemon=True), threading.Thread(target=sample_overlay, daemon=True)]
    for thread in pollers:
        thread.start()
    window_start = time.monotonic() - t0
    for index in range(n_cameras):
        starts[index] = [s for s in starts[index] if s >= window_start]
    frame_age_at_start.clear()
    process.cpu_percent(None)
    rss_start = process.memory_info().rss
    rss_peak = rss_start
    rss_timeline: list[float] = []
    next_mark = time.monotonic()
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        time.sleep(0.25)
        rss = process.memory_info().rss
        rss_peak = max(rss_peak, rss)
        if time.monotonic() >= next_mark:
            rss_timeline.append(round(rss / 2**20))
            next_mark += 5.0
    cpu = process.cpu_percent(None)
    window_end = time.monotonic() - t0
    stop.set()
    for thread in pollers:
        thread.join()
    status = vision.status("cam0")
    vision.stop()
    cameras.stop.set()
    feeder.join()

    per_camera_fps = [
        len([s for s in starts[i] if window_start <= s <= window_end]) / (window_end - window_start)
        for i in range(n_cameras)
    ]
    switches = 0
    for index, samples in id_samples.items():
        last: dict[int, int] = {}
        for frame_t, items in samples:
            gt = ground_truth(index, frame_t, args.width, args.height)
            for track_id, det in items:
                if track_id is None:
                    continue
                cx = (det.x1 + det.x2) / 2
                gid, _ = min(gt, key=lambda g: abs((g[1][0] + g[1][2]) / 2 - cx) + abs(g[1][1] - det.y1))
                if gid in last and last[gid] != track_id:
                    switches += 1
                last[gid] = track_id
    minutes = (window_end - window_start) / 60

    def pct(values, q):
        values = sorted(values)
        return round(1000 * values[int(q * (len(values) - 1))]) if values else None

    return {
        "zones": args.zones,
        "cameras": n_cameras,
        "inference_ms": inference_ms if burner else "yolo",
        "fps_per_camera_mean": round(statistics.mean(per_camera_fps), 2),
        "fps_per_camera_min": round(min(per_camera_fps), 2),
        "frame_age_at_start_ms_p50": pct(frame_age_at_start, 0.5),
        "frame_age_at_start_ms_p95": pct(frame_age_at_start, 0.95),
        "publish_latency_ms_p50": pct(publish_latency, 0.5),
        "publish_latency_ms_p95": pct(publish_latency, 0.95),
        "overlay_age_ms_p50": pct(overlay_age, 0.5),
        "overlay_age_ms_p95": pct(overlay_age, 0.95),
        "id_switches_per_min_per_camera": round(switches / minutes / n_cameras, 1) if burner else None,
        "cpu_percent_one_core": round(cpu, 1),
        "cpu_percent_machine": round(cpu / psutil.cpu_count(), 1),
        "rss_mb_start": round(rss_start / 2**20, 1),
        "rss_mb_peak": round(rss_peak / 2**20, 1),
        "rss_mb_every_5s": rss_timeline,
        "open_clips_at_end": sum(len(clips) for clips in events.clips._clips.values()),
        "queue_full_waits": (status.get("metrics") or {}).get("queue_full_waits"),
        "post_task_ms_p50": pct(post_task, 0.5) if post_task else None,
        "post_task_ms_p95": pct(post_task, 0.95) if post_task else None,
        "status_keys": sorted((status.get("metrics") or {}).keys()),
    }


if __name__ == "__main__":
    main()
