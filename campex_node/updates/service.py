"""Automatic updates for the packaged CAMPEX Node (Windows).

The Node periodically reads the signed release manifest, downloads a newer
package, checks its SHA-256, extracts it next to the data directory and hands
the folder swap to apply_update.ps1, which runs after this process exits,
restarts the Node and rolls back if the new version does not start.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from backend.cameras.security import sanitize_error_message

from campex_node.core.config import NodeSettings
from campex_node.updates.release import (
    ReleaseError,
    ReleaseManifest,
    is_newer,
    load_public_key,
    verify_manifest,
)


logger = logging.getLogger("campex.node.updates")

PACKAGE_FOLDER = "CampexNode"
EXE_NAME = "CampexNode.exe"
APPLY_SCRIPT = Path(__file__).with_name("apply_update.ps1")
FIRST_CHECK_DELAY_SECONDS = 120.0
MAX_MANIFEST_BYTES = 64 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
BAD_VERSIONS_META = "update_bad_versions"


@dataclass
class UpdateStatus:
    state: str = "idle"
    message: str | None = None
    available_version: str | None = None
    checked_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "message": self.message,
            "available_version": self.available_version,
            "checked_at": self.checked_at,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def packaged_install_dir() -> Path | None:
    """Folder holding CampexNode.exe when running the packaged Windows build."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


class UpdateService:
    def __init__(
        self,
        *,
        settings: NodeSettings,
        store,
        install_dir: Path | None = None,
        exit_process: Callable[[], None] | None = None,
        launch: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.install_dir = install_dir if install_dir is not None else packaged_install_dir()
        self.updates_dir = settings.data_dir / "updates"
        self.status = UpdateStatus()
        self._exit_process = exit_process or _exit_process
        self._launch = launch or _launch_detached
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="campex-node-updates", daemon=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._record_previous_attempt()
        reason = self._disabled_reason()
        if reason:
            self.status = UpdateStatus(state="disabled", message=reason)
            logger.info("Automatic updates disabled: %s", reason)
            return
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if self._stop.wait(FIRST_CHECK_DELAY_SECONDS):
            return
        while not self._stop.is_set():
            try:
                self.check_and_apply()
            except Exception:
                logger.exception("Update check failed")
            self._stop.wait(self.settings.update_check_interval_seconds)

    def _disabled_reason(self) -> str | None:
        if not self.settings.auto_update_enabled:
            return "Desativada por CAMPEX_NODE_AUTO_UPDATE."
        if self.install_dir is None:
            return "Disponível apenas no CAMPEX Node empacotado para Windows."
        if not self.settings.update_manifest_url:
            return "URL do manifesto de atualização não configurada."
        try:
            if load_public_key() is None:
                return "Este pacote não traz a chave pública de atualização."
        except ReleaseError as exc:
            return str(exc)
        return None

    # ------------------------------------------------------------------
    # Update flow
    # ------------------------------------------------------------------

    def check_and_apply(self) -> UpdateStatus:
        if not self._busy.acquire(blocking=False):
            return self.status
        try:
            manifest = self._fetch_manifest()
            self.status.checked_at = _now()
            if manifest is None:
                return self.status
            package = self._download(manifest)
            staging = self._extract(manifest, package)
            self._hand_over(manifest, staging)
            return self.status
        except ReleaseError as exc:
            self._set("error", str(exc))
            logger.warning("Update not applied: %s", exc)
            return self.status
        except OSError as exc:
            self._set("error", sanitize_error_message(f"Falha na atualização: {exc}"))
            logger.warning("Update not applied: %s", exc)
            return self.status
        finally:
            self._busy.release()

    def _fetch_manifest(self) -> ReleaseManifest | None:
        public_key = load_public_key()
        if public_key is None:
            raise ReleaseError("Este pacote não traz a chave pública de atualização.")
        self._set("checking", None)
        document = _http_get(self.settings.update_manifest_url, limit=MAX_MANIFEST_BYTES)
        manifest = verify_manifest(document, public_key, platform="windows")
        if not is_newer(manifest.version, self.settings.version):
            self._set("up_to_date", None)
            return None
        if manifest.version in self._bad_versions():
            self._set("skipped", f"A versão {manifest.version} falhou ao iniciar antes e não será reinstalada.")
            return None
        self.status.available_version = manifest.version
        return manifest

    def _download(self, manifest: ReleaseManifest) -> Path:
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.updates_dir).free
        # The zip, its extracted copy and the backup of the current folder.
        if free < manifest.size * 4:
            raise ReleaseError("Espaço em disco insuficiente para baixar a atualização.")
        package = self.updates_dir / f"CampexNode-{manifest.version}.zip"
        partial = package.with_suffix(".zip.part")
        self._set("downloading", f"Baixando a versão {manifest.version}.")
        digest = hashlib.sha256()
        received = 0
        request = Request(manifest.url, headers={"User-Agent": f"CAMPEX-Node/{self.settings.version}"})
        with urlopen(request, timeout=60) as response, partial.open("wb") as output:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                received += len(chunk)
                if received > manifest.size:
                    break
                digest.update(chunk)
                output.write(chunk)
                if self._stop.is_set():
                    raise ReleaseError("Download interrompido.")
        if received != manifest.size or digest.hexdigest() != manifest.sha256:
            partial.unlink(missing_ok=True)
            raise ReleaseError("O pacote baixado não confere com o manifesto assinado.")
        partial.replace(package)
        return package

    def _extract(self, manifest: ReleaseManifest, package: Path) -> Path:
        staging_root = self.updates_dir / f"staging-{manifest.version}"
        if staging_root.exists():
            shutil.rmtree(staging_root)
        staging_root.mkdir(parents=True)
        self._set("installing", f"Preparando a versão {manifest.version}.")
        root = staging_root.resolve()
        with zipfile.ZipFile(package) as archive:
            for member in archive.infolist():
                destination = (root / member.filename.replace("\\", "/")).resolve()
                if root != destination and root not in destination.parents:
                    raise ReleaseError("Pacote de atualização com caminho inválido.")
            archive.extractall(root)
        package.unlink(missing_ok=True)
        folder = root / PACKAGE_FOLDER
        if not (folder / EXE_NAME).is_file():
            raise ReleaseError(f"O pacote não contém {PACKAGE_FOLDER}/{EXE_NAME}.")
        return folder

    def _hand_over(self, manifest: ReleaseManifest, staging: Path) -> None:
        assert self.install_dir is not None
        parent = self.install_dir.parent
        if not os.access(parent, os.W_OK) or not os.access(self.install_dir, os.W_OK):
            raise ReleaseError(
                f"Sem permissão para atualizar a pasta {self.install_dir}. "
                "Instale o CAMPEX Node numa pasta do usuário, como C:\\CampexNode."
            )
        script = self.updates_dir / "apply_update.ps1"
        shutil.copyfile(APPLY_SCRIPT, script)
        logs_dir = self.settings.data_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        port = os.getenv("CAMPEX_NODE_PORT", "8787")
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script),
            "-NodeProcessId",
            str(os.getpid()),
            "-Source",
            str(staging),
            "-Target",
            str(self.install_dir),
            "-Version",
            manifest.version,
            "-PreviousVersion",
            self.settings.version,
            "-StatusUrl",
            f"http://127.0.0.1:{port}/api/status",
            "-ResultPath",
            str(self.updates_dir / "last_update.json"),
            "-LogPath",
            str(logs_dir / "campex-node-update.log"),
            # The restarted Node may pick another port if this one got taken.
            "-PortFile",
            str(self.settings.data_dir / "campex-node.port"),
        ]
        self._set("restarting", f"Reiniciando para a versão {manifest.version}.")
        logger.info("Installing CAMPEX Node %s; the Node will restart", manifest.version)
        self._launch(command)
        self._exit_process()

    # ------------------------------------------------------------------
    # Previous attempts
    # ------------------------------------------------------------------

    def _record_previous_attempt(self) -> None:
        result_path = self.updates_dir / "last_update.json"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        status = result.get("status")
        version = str(result.get("version") or "")
        if status == "ok":
            logger.info("Updated to CAMPEX Node %s", version)
        elif status in {"rolled_back", "failed"} and version:
            logger.warning("Update to %s did not complete (%s): %s", version, status, result.get("detail"))
            if status == "rolled_back":
                bad = self._bad_versions()
                bad.add(version)
                self.store.set_meta(BAD_VERSIONS_META, json.dumps(sorted(bad)))
            self.status = UpdateStatus(
                state="error",
                message=(
                    f"A versão {version} não iniciou e a versão anterior foi restaurada."
                    if status == "rolled_back"
                    else f"A atualização para {version} falhou: {sanitize_error_message(result.get('detail'))}"
                ),
            )
        for leftover in self.updates_dir.glob("staging-*"):
            shutil.rmtree(leftover, ignore_errors=True)
        result_path.unlink(missing_ok=True)

    def _bad_versions(self) -> set[str]:
        try:
            return set(json.loads(self.store.get_meta(BAD_VERSIONS_META) or "[]"))
        except ValueError:
            return set()

    def _set(self, state: str, message: str | None) -> None:
        self.status.state = state
        self.status.message = message


def _http_get(url: str, *, limit: int) -> bytes:
    request = Request(url, headers={"User-Agent": "CAMPEX-Node", "Cache-Control": "no-cache"})
    with urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ReleaseError("Manifesto de atualização grande demais.")
    return data


def _launch_detached(command: list[str]) -> None:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        # Leave the Task Scheduler job, which would end the script with the Node.
        subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
    except OSError:
        subprocess.Popen(command, creationflags=flags, close_fds=True)


def _exit_process() -> None:
    def worker() -> None:
        # Let the status response that reported "restarting" go out first.
        time.sleep(2)
        os._exit(0)

    threading.Thread(target=worker, name="campex-node-update-exit", daemon=True).start()
