"""go2rtc sidecar: one connection per camera, restreamed on this computer.

Cheap cameras accept only two or three RTSP sessions. With go2rtc the Node
opens one per camera and the vision worker, the recorder and the live view
all read go2rtc's local copy (rtsp://127.0.0.1:<port>/<stream>).

go2rtc trusts localhost by default and its API can run commands (exec/echo
sources), so the config keeps it on 127.0.0.1, demands a password even
from localhost, loads only the camera modules and exposes only the API paths
the Node uses.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

from campex_node.core.config import ROOT_DIR, NodeCameraConfig, NodeSettings


logger = logging.getLogger("campex.node.go2rtc")

# No exec/echo/expr (they run commands), no webrtc (it opens port 8555 to the
# network) and no ffmpeg (the Node never transcodes).
MODULES = ("api", "ws", "http", "rtsp", "mp4", "onvif", "isapi", "dvrip")
API_PATHS = ("/api", "/api/streams", "/api/ws")
START_TIMEOUT_SECONDS = 10.0
SUPERVISE_SECONDS = 2.0
MAX_RESTART_DELAY_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 3.0
LOG_MAX_BYTES = 5 * 1024**2
USERNAME = "campex"


def stream_name(camera: NodeCameraConfig) -> str:
    """go2rtc stream for a camera; a new camera URL gets a new stream."""
    readable = re.sub(r"[^A-Za-z0-9_-]", "_", camera.id)[:40]
    digest = hashlib.sha256(f"{camera.id}\n{camera.rtsp_url}".encode("utf-8")).hexdigest()[:10]
    return f"{readable}_{digest}"


def find_binary(configured: str | None = None) -> Path | None:
    name = "go2rtc.exe" if os.name == "nt" else "go2rtc"
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.append(Path(bundle_dir) / "go2rtc" / name)
    # scripts/fetch_go2rtc.py puts the pinned release here.
    candidates.append(ROOT_DIR / "campex_node" / "bin" / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    on_path = shutil.which("go2rtc")
    return Path(on_path).resolve() if on_path else None


def render_config(*, api_port: int, rtsp_port: int, password: str, streams: dict[str, str]) -> str:
    # JSON strings are valid YAML double-quoted scalars. Streams stay in block
    # style: go2rtc rewrites that section when the API adds or removes one.
    quote = json.dumps
    lines = [
        "app:",
        f"  modules: {quote(list(MODULES))}",
        "api:",
        f"  listen: {quote(f'127.0.0.1:{api_port}')}",
        f"  username: {quote(USERNAME)}",
        f"  password: {quote(password)}",
        "  local_auth: true",
        f"  allow_paths: {quote(list(API_PATHS))}",
        "rtsp:",
        f"  listen: {quote(f'127.0.0.1:{rtsp_port}')}",
        "streams:",
    ]
    lines.extend(f"  {name}: {quote(url)}" for name, url in sorted(streams.items()))
    return "\n".join(lines) + "\n"


class Go2rtcRelay:
    """Runs go2rtc and keeps its streams in step with the Node's cameras.

    ``source_url`` is what every camera consumer opens. It stays the camera's
    own URL until go2rtc has started once; after that a crash is repaired by
    restarting go2rtc, not by switching the consumers back and forth.
    """

    def __init__(self, settings: NodeSettings, *, binary: Path | None = None) -> None:
        self.settings = settings
        self.binary = binary if binary is not None else (find_binary(settings.go2rtc_path) if settings.go2rtc_enabled else None)
        self.directory = settings.data_dir / "go2rtc"
        self.config_path = self.directory / "go2rtc.yaml"
        self.log_path = self.directory / "go2rtc.log"
        self.status = "DISABLED"
        self.error: str | None = None
        self.version: str | None = None
        self._password = secrets.token_hex(24)
        self._streams: dict[str, str] = {}
        self._in_use = False
        self._process: subprocess.Popen | None = None
        self._log_file = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # Set when go2rtc's streams may differ from the cameras.
        self._restart = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def in_use(self) -> bool:
        return self._in_use

    def source_url(self, camera: NodeCameraConfig) -> str:
        if not self._in_use:
            return camera.rtsp_url
        return f"rtsp://127.0.0.1:{self.settings.go2rtc_rtsp_port}/{stream_name(camera)}"

    def live_upstream(self, camera: NodeCameraConfig) -> tuple[str, dict[str, str]] | None:
        """go2rtc's WebSocket (MSE/MP4) for a camera, with its credentials."""
        if not self._in_use or self.status != "RUNNING":
            return None
        query = urllib.parse.urlencode({"src": stream_name(camera)})
        return (
            f"ws://127.0.0.1:{self.settings.go2rtc_api_port}/api/ws?{query}",
            {"Authorization": self._auth_header()},
        )

    def summary(self) -> dict:
        return {
            "enabled": self.settings.go2rtc_enabled,
            "status": self.status,
            "error": self.error,
            "version": self.version,
            "live_mode": "mse" if self._in_use and self.status == "RUNNING" else "mjpeg",
        }

    def start(self, cameras: Iterable[NodeCameraConfig]) -> None:
        if not self.settings.go2rtc_enabled:
            self.status = "DISABLED"
            return
        if self.binary is None:
            self.status = "MISSING"
            self.error = "Executável do go2rtc não encontrado; as câmeras seguem com conexão direta."
            logger.warning("go2rtc enabled but its binary was not found; cameras use direct RTSP")
            return
        with self._lock:
            self._streams = _wanted_streams(cameras)
        if not self._launch():
            logger.warning("go2rtc did not start (%s); cameras use direct RTSP", self.error)
            self._terminate()
            return
        self._in_use = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="campex-node-go2rtc", daemon=True)
        self._thread.start()

    def sync(self, cameras: Iterable[NodeCameraConfig]) -> None:
        """Adds and removes go2rtc streams to match the cameras."""
        if not self._in_use:
            return
        wanted = _wanted_streams(cameras)
        with self._lock:
            current = dict(self._streams)
            self._streams = wanted
        if current == wanted:
            return
        if self.status != "RUNNING":
            self._restart.set()  # the next launch writes the whole config
            return
        applied = all(
            [self._request("DELETE", {"src": name}) is not None for name in current.keys() - wanted.keys()]
            + [self._request("PUT", {"name": name, "src": wanted[name]}) is not None for name in wanted.keys() - current.keys()]
        )
        if not applied:
            self._restart.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=SUPERVISE_SECONDS + 1)
            self._thread = None
        self._terminate()
        if self.status != "DISABLED":
            self.status = "STOPPED"

    def _supervise(self) -> None:
        delay = SUPERVISE_SECONDS
        while not self._stop.wait(SUPERVISE_SECONDS if self.status == "RUNNING" else delay):
            process = self._process
            alive = process is not None and process.poll() is None
            if self.status == "RUNNING" and alive and not self._restart.is_set():
                continue
            self._restart.clear()
            if self.status == "RUNNING" and not alive:
                self.status = "ERROR"
                self.error = f"go2rtc parou (código {process.returncode if process else '?'}); reiniciando."
                logger.error("go2rtc exited with code %s; restarting", process.returncode if process else None)
            self._terminate()
            if self._launch():
                delay = SUPERVISE_SECONDS
            else:
                self._terminate()
                delay = min(MAX_RESTART_DELAY_SECONDS, delay * 2)

    def _launch(self) -> bool:
        self.status = "STARTING"
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            streams = dict(self._streams)
        self.config_path.write_text(
            render_config(
                api_port=self.settings.go2rtc_api_port,
                rtsp_port=self.settings.go2rtc_rtsp_port,
                password=self._password,
                streams=streams,
            ),
            encoding="utf-8",
        )
        _restrict_permissions(self.config_path)
        _kill_orphans(self.config_path)
        self._open_log()
        try:
            self._process = subprocess.Popen(
                [str(self.binary), "-config", str(self.config_path)],
                cwd=str(self.directory),
                stdin=subprocess.DEVNULL,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self.status, self.error = "ERROR", f"Não foi possível iniciar o go2rtc: {exc}"
            return False
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not self._stop.is_set():
            if self._process.poll() is not None:
                self.status = "ERROR"
                self.error = f"go2rtc encerrou ao iniciar (código {self._process.returncode}); veja {self.log_path}."
                return False
            info = self._request("GET", None, path="/api", quiet=True)
            if info is not None:
                # Another program on the port would not know our password, but
                # an older go2rtc of ours could: check it is this config.
                if Path(info.get("config_path") or "").resolve() != self.config_path.resolve():
                    self.status = "ERROR"
                    self.error = f"A porta {self.settings.go2rtc_api_port} já está em uso por outro go2rtc."
                    return False
                self.version = info.get("version")
                self.status, self.error = "RUNNING", None
                logger.info("go2rtc %s running with %s stream(s)", self.version, len(streams))
                return True
            time.sleep(0.2)
        self.status = "ERROR"
        self.error = (
            f"go2rtc não respondeu em {START_TIMEOUT_SECONDS:.0f}s; verifique se as portas "
            f"{self.settings.go2rtc_api_port}/{self.settings.go2rtc_rtsp_port} estão livres."
        )
        return False

    def _terminate(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    def _open_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
        try:
            if self.log_path.stat().st_size > LOG_MAX_BYTES:
                self.log_path.replace(self.log_path.with_suffix(".log.1"))
        except OSError:
            pass
        self._log_file = open(self.log_path, "ab")

    def _auth_header(self) -> str:
        token = base64.b64encode(f"{USERNAME}:{self._password}".encode("ascii")).decode("ascii")
        return f"Basic {token}"

    def _request(self, method: str, query: dict | None, *, path: str = "/api/streams", quiet: bool = False) -> dict | None:
        url = f"http://127.0.0.1:{self.settings.go2rtc_api_port}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, method=method, headers={"Authorization": self._auth_header()})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = response.read()
        except (urllib.error.URLError, OSError) as exc:
            if not quiet:
                # The query carries camera credentials: log only the method.
                logger.warning("go2rtc %s %s failed: %s", method, path, getattr(exc, "code", type(exc).__name__))
            return None
        try:
            return json.loads(body) if body else {}
        except ValueError:
            return {}


def _wanted_streams(cameras: Iterable[NodeCameraConfig]) -> dict[str, str]:
    return {stream_name(camera): camera.rtsp_url for camera in cameras if camera.enabled and camera.rtsp_url}


def _restrict_permissions(path: Path) -> None:
    # The config holds camera passwords. Windows already limits
    # %LOCALAPPDATA% to the user; elsewhere make it owner-only.
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _kill_orphans(config_path: Path) -> None:
    """Stops a go2rtc left running by a Node that did not shut down cleanly."""
    try:
        import psutil
    except ImportError:
        return
    marker = str(config_path)
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (process.info.get("name") or "").lower()
            if name.startswith("go2rtc") and marker in (process.info.get("cmdline") or []):
                logger.warning("Stopping orphan go2rtc (pid %s)", process.pid)
                process.kill()
                process.wait(timeout=5)
        except (psutil.Error, OSError):
            continue
