from __future__ import annotations

import argparse
import multiprocessing
import os
import socket
import traceback
import threading
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


LOCK_FILE_NAME = "campex-node.lock"
# Read by the updater and by tools to find the local panel.
PORT_FILE_NAME = "campex-node.port"
# The dashboard looks for the Node on the same range.
PORT_RANGE_SIZE = 20
# A lock that could not be created must not stop the Node from starting.
_NO_LOCK = object()


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    _bootstrap_log("launcher starting")
    parser = argparse.ArgumentParser(description="Run CAMPEX Node desktop launcher.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    os.environ.setdefault("CAMPEX_DESKTOP_MODE", "1")
    from campex_node.core.config import _default_data_dir

    data_dir = _default_data_dir()
    port_file = data_dir / PORT_FILE_NAME
    # Held for the whole life of this process: one Node per data folder.
    # Without it a slow Node (Vision on the CPU) looked absent and a second
    # one started on another port with the same token and cameras.
    instance_lock = _acquire_instance_lock(data_dir / LOCK_FILE_NAME)
    if instance_lock is None:
        running_port = _read_port(port_file) or args.port
        url = f"http://{args.host}:{running_port}"
        _bootstrap_log(f"another CAMPEX Node is running at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    port = _select_port(args.host, args.port)
    if port != args.port:
        _bootstrap_log(f"port {args.port} is used by another program; using {port}")
    # The updater checks the restarted Node on this port.
    os.environ["CAMPEX_NODE_PORT"] = str(port)
    _write_port(port_file, port)
    url = f"http://{args.host}:{port}"
    _bootstrap_log(f"selected url {url}")

    try:
        _bootstrap_log("importing uvicorn")
        import uvicorn
        _bootstrap_log("uvicorn imported")
    except Exception:
        _bootstrap_log("uvicorn import failed\n" + traceback.format_exc())
        raise

    _bootstrap_log("creating uvicorn config")
    config = uvicorn.Config(
        "campex_node.local_app:create_app",
        host=args.host,
        port=port,
        factory=True,
        log_level=os.getenv("CAMPEX_NODE_LOG_LEVEL", "info").lower(),
        log_config=None,
        access_log=False,
    )
    _bootstrap_log("creating uvicorn server")
    server = uvicorn.Server(config)
    _bootstrap_log("uvicorn server created")
    def run_server() -> None:
        try:
            server.run()
        except Exception:
            _bootstrap_log("uvicorn server failed\n" + traceback.format_exc())
            raise

    thread = threading.Thread(target=run_server, name="campex-node-local-app", daemon=True)
    thread.start()
    _bootstrap_log("uvicorn thread started")

    if not args.no_browser:
        _open_when_ready(url)

    try:
        while thread.is_alive() and not server.should_exit:
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
    _bootstrap_log("launcher exiting")
    return 0


def _select_port(host: str, preferred_port: int) -> int:
    # This process holds the instance lock, so a busy port belongs to another program.
    for port in range(preferred_port, preferred_port + PORT_RANGE_SIZE):
        if not _port_is_busy(host, port):
            return port
    raise SystemExit(
        f"Nenhuma porta local disponivel entre {preferred_port} e {preferred_port + PORT_RANGE_SIZE - 1}."
    )


def _acquire_instance_lock(path: Path, wait_seconds: float = 5.0):
    """Exclusive lock on ``path``, or None when another Node holds it.

    Waits a little: during an update the previous Node is still exiting. The
    operating system releases the lock when the holder exits, even if killed.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")
    except OSError:
        _bootstrap_log("instance lock unavailable\n" + traceback.format_exc())
        return _NO_LOCK
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(0.25)


def _read_port(path: Path) -> int | None:
    try:
        port = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def _write_port(path: Path, port: int) -> None:
    try:
        path.write_text(str(port), encoding="ascii")
    except OSError:
        _bootstrap_log("could not write port file\n" + traceback.format_exc())


def _is_campex_node_running(url: str) -> bool:
    try:
        with urlopen(f"{url}/api/status", timeout=0.4) as response:
            return response.status == 200 and b"node_id" in response.read(1024)
    except (OSError, URLError):
        return False


def _open_when_ready(url: str) -> None:
    def worker() -> None:
        for _ in range(80):
            if _is_campex_node_running(url):
                webbrowser.open(url)
                return
            time.sleep(0.25)

    threading.Thread(target=worker, name="campex-node-open-browser", daemon=True).start()


def _port_is_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def _bootstrap_log(message: str) -> None:
    try:
        if os.name == "nt":
            base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            log_dir = Path(base) / "CAMPEX" / "node" / "logs"
        else:
            log_dir = Path.home() / ".local" / "share" / "campex" / "node" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "campex-node-bootstrap.log").open("a", encoding="utf-8") as file:
            file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
