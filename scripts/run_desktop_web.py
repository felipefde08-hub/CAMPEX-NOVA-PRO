"""Run CAMPEX as a local desktop-style web app.

This launcher is intentionally simple: it starts the local FastAPI backend,
serves the static frontend from this repository, and opens the browser. It is
the development target for a future Windows .exe wrapper.
"""
from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
FRONTEND_DIR = ROOT / "frontend"
ENV_PATH = ROOT / ".env"


class NoCacheFrontendHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run CAMPEX local web app.")
    parser.add_argument("--backend-host", default="127.0.0.1")
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-host", default="127.0.0.1")
    parser.add_argument("--frontend-port", type=int, default=5174)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    _prepare_local_environment(args)
    _initialize_database()

    backend = _start_backend(args)
    frontend = _start_frontend(args)
    frontend_url = f"http://{args.frontend_host}:{args.frontend_port}"

    print(f"CAMPEX backend:  http://{args.backend_host}:{args.backend_port}/api/v1")
    print(f"CAMPEX frontend: {frontend_url}")
    print("Pressione Ctrl+C para encerrar.")

    if not args.no_browser:
        _open_when_ready(frontend_url)

    try:
        while not backend.should_exit:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nEncerrando CAMPEX...")
    finally:
        frontend.shutdown()
        backend.should_exit = True
    return 0


def _prepare_local_environment(args: argparse.Namespace) -> None:
    os.environ["CAMPEX_DESKTOP_MODE"] = "1"
    os.environ["CAMPEX_RUNTIME"] = "local"
    os.environ.setdefault("CAMPEX_ENV", "development")
    os.environ.setdefault(
        "CAMPEX_FRONTEND_ORIGINS",
        f"http://{args.frontend_host}:{args.frontend_port},http://localhost:{args.frontend_port}",
    )
    os.environ.setdefault("VISION_VIDEO_LOOP", "true")
    os.environ.setdefault("CAMPEX_RATE_LIMIT_UNAUTHORIZED_ONLY", "true")

    if not _env_has_token():
        token = secrets.token_urlsafe(32)
        with ENV_PATH.open("a", encoding="utf-8") as env_file:
            env_file.write(f"\nCAMPEX_API_TOKEN={token}\n")
        os.environ["CAMPEX_API_TOKEN"] = token


def _env_has_token() -> bool:
    if os.getenv("CAMPEXTOKEN") or os.getenv("CAMPEX_API_TOKEN"):
        return True
    if not ENV_PATH.exists():
        return False
    try:
        contents = ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return False
    return "CAMPEXTOKEN=" in contents or "CAMPEX_API_TOKEN=" in contents


def _initialize_database() -> None:
    sys.path.insert(0, str(ROOT))
    try:
        from backend.config import get_settings
        from backend.database.db import initialize_database

        initialize_database(get_settings())
    except Exception as exc:
        print(f"Erro ao inicializar banco: {exc}")
        raise SystemExit("Falha ao inicializar o banco local.")


def _start_backend(args: argparse.Namespace):
    if _port_is_busy(args.backend_host, args.backend_port):
        raise SystemExit(f"A porta do backend ja esta em uso: {args.backend_host}:{args.backend_port}")
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        if exc.name == "uvicorn":
            raise SystemExit(
                "Dependencias ausentes. Instale com: python -m pip install -r requirements.txt"
            ) from exc
        raise

    config = uvicorn.Config(
        "backend.main:app",
        host=args.backend_host,
        port=args.backend_port,
        log_level=os.getenv("CAMPEX_LOG_LEVEL", "info").lower(),
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="campex-backend", daemon=True)
    thread.start()
    return server


def _start_frontend(args: argparse.Namespace) -> ThreadingHTTPServer:
    if _port_is_busy(args.frontend_host, args.frontend_port):
        raise SystemExit(f"A porta do frontend ja esta em uso: {args.frontend_host}:{args.frontend_port}")
    handler = partial(NoCacheFrontendHandler, directory=str(FRONTEND_DIR))
    server = ThreadingHTTPServer((args.frontend_host, args.frontend_port), handler)
    thread = threading.Thread(target=server.serve_forever, name="campex-frontend", daemon=True)
    thread.start()
    return server


def _open_when_ready(url: str) -> None:
    def worker() -> None:
        for _ in range(40):
            try:
                with urlopen(url, timeout=0.5):
                    webbrowser.open(url)
                    return
            except OSError:
                time.sleep(0.25)

    threading.Thread(target=worker, name="campex-open-browser", daemon=True).start()


def _port_is_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


if __name__ == "__main__":
    raise SystemExit(main())
