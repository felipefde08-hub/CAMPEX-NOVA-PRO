from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from campex_node.core.config import NodeSettings
from campex_node.storage.local_store import LocalStore
from campex_node.updates import service as update_module
from campex_node.updates.release import ReleaseError, ReleaseManifest, is_newer, parse_version, sign_manifest
from campex_node.updates.service import UpdateService


PACKAGE_URL = "https://example.com/CampexNode-windows.zip"


def make_settings(tmp_path, **overrides):
    values = dict(
        environment="test",
        version="0.3.0",
        log_level="INFO",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "node.sqlite3",
        node_id_file=tmp_path / "data" / "node_id",
        update_manifest_url="https://example.com/campex-node-manifest.json",
    )
    values.update(overrides)
    return NodeSettings(**values)


def make_package(files: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in (files or {"CampexNode/CampexNode.exe": b"new exe", "CampexNode/_internal/lib.dll": b"x"}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def signed_manifest(key, package: bytes, *, version="0.4.0", sha256=None) -> bytes:
    return sign_manifest(
        key,
        ReleaseManifest(
            version=version,
            platform="windows",
            url=PACKAGE_URL,
            sha256=sha256 or hashlib.sha256(package).hexdigest(),
            size=len(package),
        ),
    )


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def node(tmp_path, monkeypatch):
    """An installed packaged Node whose release is signed by `key`."""
    key = Ed25519PrivateKey.generate()
    install_dir = tmp_path / "CampexNode"
    install_dir.mkdir()
    (install_dir / "CampexNode.exe").write_bytes(b"old exe")
    settings = make_settings(tmp_path)
    settings.data_dir.mkdir(parents=True)
    store = LocalStore(settings.database_path)
    store.initialize()
    served = {}
    monkeypatch.setattr(update_module, "load_public_key", lambda: key.public_key())
    monkeypatch.setattr(update_module, "_http_get", lambda url, limit: served["manifest"])
    monkeypatch.setattr(update_module, "urlopen", lambda request, timeout: FakeResponse(served["package"]))
    launched, exited = [], []
    service = UpdateService(
        settings=settings,
        store=store,
        install_dir=install_dir,
        launch=launched.append,
        exit_process=lambda: exited.append(True),
    )
    return {"key": key, "service": service, "served": served, "launched": launched, "exited": exited, "settings": settings}


def test_newer_signed_release_is_installed_and_node_restarts(node):
    package = make_package()
    node["served"].update(manifest=signed_manifest(node["key"], package), package=package)

    status = node["service"].check_and_apply()

    assert status.state == "restarting"
    assert node["exited"] == [True]
    command = node["launched"][0]
    assert command[command.index("-Version") + 1] == "0.4.0"
    assert command[command.index("-PreviousVersion") + 1] == "0.3.0"
    staging = command[command.index("-Source") + 1]
    assert (update_module.Path(staging) / "CampexNode.exe").read_bytes() == b"new exe"
    assert (node["settings"].data_dir / "updates" / "apply_update.ps1").is_file()
    # The restarted Node reports the port it took, which may not be the current one.
    assert command[command.index("-PortFile") + 1] == str(node["settings"].data_dir / "campex-node.port")


def test_manifest_signed_with_another_key_is_rejected(node):
    package = make_package()
    forged = signed_manifest(Ed25519PrivateKey.generate(), package)
    node["served"].update(manifest=forged, package=package)

    status = node["service"].check_and_apply()

    assert status.state == "error"
    assert "Assinatura" in status.message
    assert node["launched"] == [] and node["exited"] == []


def test_package_that_differs_from_signed_hash_is_rejected(node):
    package = make_package()
    node["served"].update(manifest=signed_manifest(node["key"], package), package=make_package({"CampexNode/CampexNode.exe": b"evil"}))

    status = node["service"].check_and_apply()

    assert status.state == "error"
    assert "não confere" in status.message
    assert node["launched"] == []


def test_package_escaping_the_staging_folder_is_rejected(node):
    package = make_package({"CampexNode/CampexNode.exe": b"x", "../../outside.txt": b"x"})
    node["served"].update(manifest=signed_manifest(node["key"], package), package=package)

    status = node["service"].check_and_apply()

    assert status.state == "error"
    assert node["launched"] == []


def test_same_version_is_not_reinstalled(node):
    package = make_package()
    node["served"].update(manifest=signed_manifest(node["key"], package, version="0.3.0"), package=package)

    assert node["service"].check_and_apply().state == "up_to_date"
    assert node["launched"] == []


def test_version_that_was_rolled_back_is_not_retried(node):
    updates_dir = node["settings"].data_dir / "updates"
    updates_dir.mkdir()
    (updates_dir / "last_update.json").write_text(
        json.dumps({"status": "rolled_back", "version": "0.4.0", "previous_version": "0.3.0"}),
        encoding="utf-8",
    )
    node["service"]._record_previous_attempt()
    assert "não iniciou" in node["service"].status.message

    package = make_package()
    node["served"].update(manifest=signed_manifest(node["key"], package), package=package)
    assert node["service"].check_and_apply().state == "skipped"
    assert node["launched"] == []


def test_updates_stay_off_outside_the_packaged_node(tmp_path):
    settings = make_settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.initialize()
    service = UpdateService(settings=settings, store=store, install_dir=None)

    service.start()

    assert service.status.state == "disabled"


def test_versions_compare_numerically():
    assert is_newer("0.10.0", "0.9.9")
    assert not is_newer("0.3.0", "0.3.0")
    with pytest.raises(ReleaseError):
        parse_version("latest")
