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


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    _bootstrap_log("launcher starting")
    parser = argparse.ArgumentParser(description="Run CAMPEX Node desktop launcher.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    os.environ.setdefault("CAMPEX_DESKTOP_MODE", "1")
    port = _select_port(args.host, args.port)
    url = f"http://{args.host}:{port}"
    _bootstrap_log(f"selected url {url}")
    if _is_campex_node_running(url):
        _bootstrap_log("existing local app detected")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

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
    for port in range(preferred_port, preferred_port + 20):
        url = f"http://{host}:{port}"
        if _is_campex_node_running(url):
            return port
        if not _port_is_busy(host, port):
            return port
    raise SystemExit(f"Nenhuma porta local disponivel entre {preferred_port} e {preferred_port + 19}.")


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
