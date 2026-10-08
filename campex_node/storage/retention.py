from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from backend.maintenance.retention import delete_evidence_files

from campex_node.core.config import NodeSettings


logger = logging.getLogger("campex.node.retention")

CHECK_INTERVAL_SECONDS = 6 * 3600


class EvidenceRetentionService:
    """Apaga clipes e imagens de eventos mais antigos que o prazo configurado.

    A fila de envio e as gravações contínuas têm limpeza própria; os eventos
    em si ficam, só a mídia pesada sai.
    """

    def __init__(self, settings: NodeSettings) -> None:
        self.settings = settings
        self.evidence_dir = settings.data_dir / "evidence"
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="campex-node-evidence-retention",
            daemon=True,
        )

    def start(self) -> None:
        if self.settings.evidence_retention_days > 0 and not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def purge_once(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        try:
            result = delete_evidence_files(
                self.evidence_dir,
                older_than=now - timedelta(days=self.settings.evidence_retention_days),
            )
        except Exception:
            logger.exception("Evidence retention failed")
            return 0
        if result["deleted_files"]:
            logger.info(
                "Evidence retention removed %s file(s), %s bytes",
                result["deleted_files"],
                result["deleted_bytes"],
            )
        return result["deleted_files"]

    def _run(self) -> None:
        while not self._stop.is_set():
            self.purge_once()
            self._stop.wait(CHECK_INTERVAL_SECONDS)
