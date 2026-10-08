from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from campex_node.core.config import NodeSettings
from campex_node.storage.retention import EvidenceRetentionService


def test_evidence_retention_removes_only_old_event_media(tmp_path):
    settings = replace(NodeSettings.from_env(), data_dir=tmp_path, evidence_retention_days=90)
    old_clip = tmp_path / "evidence" / "evt_old" / "clip.webm"
    new_clip = tmp_path / "evidence" / "evt_new" / "clip.webm"
    for clip in (old_clip, new_clip):
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"webm")
    now = datetime.now(timezone.utc)
    old_time = (now - timedelta(days=120)).timestamp()
    os.utime(old_clip, (old_time, old_time))

    removed = EvidenceRetentionService(settings).purge_once(now=now)

    assert removed == 1
    assert not old_clip.parent.exists()
    assert new_clip.exists()


def test_evidence_retention_zero_days_does_not_start(tmp_path):
    settings = replace(NodeSettings.from_env(), data_dir=tmp_path, evidence_retention_days=0)
    service = EvidenceRetentionService(settings)

    service.start()

    assert not service._thread.is_alive()
