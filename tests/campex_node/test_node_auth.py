from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from campex_node.core.config import NodeCameraConfig
from campex_node.local_app import create_app


SQUARE = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.9], [0.1, 0.9]]
ADMIN = {"name": "Felipe", "email": "felipe@fabrica.com", "password": "senha-forte-1"}
OPERATOR = {"name": "Ana", "email": "ana@fabrica.com", "password": "senha-forte-2"}


@contextmanager
def _node(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMPEX_NODE_DATA_DIR", str(tmp_path / "node"))
    monkeypatch.setenv("CAMPEX_NODE_RECORDING_ENABLED", "false")
    app = create_app()
    # The Node's own computer, and a supervisor's PC on the factory network.
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as local, TestClient(
        app, base_url="http://192.168.0.20:8787", client=("192.168.0.31", 50001)
    ) as network:
        yield app, local, network


def _admin_session(local) -> dict:
    response = local.post("/api/auth/register", json=ADMIN)
    assert response.status_code == 201
    return response.json()


def test_first_account_is_the_admin_and_only_the_node_computer_creates_it(monkeypatch, tmp_path):
    with _node(monkeypatch, tmp_path) as (_app, local, network):
        assert network.get("/api/auth/setup").json() == {"needs_setup": True, "local": False}
        refused = network.post("/api/auth/register", json=ADMIN)
        assert refused.status_code == 403

        session = _admin_session(local)
        assert session["user"]["role"] == "admin" and session["token"].startswith("cxn_")
        assert local.get("/api/auth/setup").json()["needs_setup"] is False
        # Nobody signs themselves up after that.
        assert network.post("/api/auth/register", json=OPERATOR).status_code == 403


def test_the_network_needs_a_login_and_the_session_works_as_header_or_cookie(monkeypatch, tmp_path):
    with _node(monkeypatch, tmp_path) as (_app, local, network):
        _admin_session(local)
        assert network.get("/api/zones").status_code == 401
        assert network.get("/api/status").status_code == 200
        assert local.get("/api/zones").status_code == 200

        assert network.post("/api/auth/login", json={**ADMIN, "password": "errada-123"}).status_code == 401
        login = network.post("/api/auth/login", json={"email": "FELIPE@fabrica.com ", "password": ADMIN["password"]})
        assert login.status_code == 200
        token = login.json()["token"]
        # The cookie carries the session for images and videos.
        assert network.get("/api/zones").status_code == 200
        network.cookies.clear()
        assert network.get("/api/zones").status_code == 401
        assert network.get("/api/zones", headers={"X-CAMPEX-Session": token}).status_code == 200
        assert network.get("/api/auth/me", headers={"X-CAMPEX-Session": token}).json()["user"]["email"] == ADMIN["email"]

        network.post("/api/auth/logout", headers={"X-CAMPEX-Session": token})
        assert network.get("/api/zones", headers={"X-CAMPEX-Session": token}).status_code == 401


def test_a_page_rebinding_its_domain_to_this_computer_is_not_local(monkeypatch, tmp_path):
    with _node(monkeypatch, tmp_path) as (app, _local, _network):
        with TestClient(app, base_url="http://evil.example:8787", client=("127.0.0.1", 50002)) as rebinding:
            assert rebinding.get("/api/zones").status_code == 401
            assert rebinding.post("/api/auth/register", json=ADMIN).status_code == 403


def test_only_admins_manage_accounts(monkeypatch, tmp_path):
    with _node(monkeypatch, tmp_path) as (_app, local, network):
        admin = {"X-CAMPEX-Session": _admin_session(local)["token"]}
        created = network.post("/api/auth/register", json=OPERATOR, headers=admin)
        assert created.status_code == 201 and created.json()["user"]["role"] == "operator"
        operator_id = created.json()["user"]["id"]

        operator = {"X-CAMPEX-Session": network.post("/api/auth/login", json=OPERATOR).json()["token"]}
        network.cookies.clear()
        other = {"name": "Bia", "email": "bia@fabrica.com", "password": "senha-forte-3"}
        assert network.post("/api/auth/register", json=other, headers=operator).status_code == 403
        assert network.patch(f"/api/auth/users/{operator_id}", json={"role": "admin"}, headers=operator).status_code == 403
        assert network.get("/api/backup", headers=operator).status_code == 403
        assert len(network.get("/api/auth/users", headers=operator).json()) == 2

        admin_id = network.get("/api/auth/me", headers=admin).json()["user"]["id"]
        assert network.delete(f"/api/auth/users/{admin_id}", headers=admin).status_code == 400
        assert network.patch(f"/api/auth/users/{admin_id}", json={"role": "operator"}, headers=admin).status_code == 400
        assert network.delete(f"/api/auth/users/{operator_id}", headers=admin).status_code == 204
        assert network.get("/api/zones", headers=operator).status_code == 401


def test_changing_the_password_signs_out_everywhere(monkeypatch, tmp_path):
    with _node(monkeypatch, tmp_path) as (_app, local, network):
        _admin_session(local)
        token = network.post("/api/auth/login", json=ADMIN).json()["token"]
        network.cookies.clear()
        headers = {"X-CAMPEX-Session": token}
        wrong = network.post("/api/auth/password", json={"current_password": "x", "new_password": "nova-senha-1"}, headers=headers)
        assert wrong.status_code == 403
        changed = network.post(
            "/api/auth/password", json={"current_password": ADMIN["password"], "new_password": "nova-senha-1"}, headers=headers
        )
        assert changed.status_code == 204
        assert network.get("/api/zones", headers=headers).status_code == 401
        assert network.post("/api/auth/login", json={**ADMIN, "password": "nova-senha-1"}).status_code == 200


def test_backup_download_and_restore_bring_the_data_back(monkeypatch, tmp_path):
    with _node(monkeypatch, tmp_path) as (app, local, _network):
        camera = NodeCameraConfig(id="cam-1", name="Prensas", rtsp_url="rtsp://camera/1")
        monkeypatch.setattr(app.state.runtime.lifecycle.camera_manager, "configs", lambda: [camera])
        _admin_session(local)
        zone = {"camera_id": "cam-1", "name": "Prensa 3", "type": "machine", "points": SQUARE}
        assert local.post("/api/zones", json=zone).status_code == 201

        backup = local.get("/api/backup")
        assert backup.status_code == 200
        assert backup.content.startswith(b"SQLite format 3")
        assert "campex-node-backup-" in backup.headers["content-disposition"]

        zone_id = local.get("/api/zones").json()[0]["id"]
        assert local.delete(f"/api/zones/{zone_id}").status_code == 204
        assert local.get("/api/zones").json() == []

        assert local.post("/api/backup/restore", content=b"not a database").status_code == 400
        restored = local.post("/api/backup/restore", content=backup.content)
        assert restored.status_code == 200
        assert restored.json()["previous_database"].startswith("antes-da-restauracao-")
        assert [item["name"] for item in local.get("/api/zones").json()] == ["Prensa 3"]
        # The accounts came back with it.
        assert local.post("/api/auth/login", json=ADMIN).status_code == 200


def test_daily_backup_keeps_one_copy_per_day(tmp_path):
    from datetime import datetime, timedelta, timezone

    from campex_node.backup import BackupService
    from campex_node.events import NodeEventStore

    database = tmp_path / "node.sqlite3"
    NodeEventStore(database).initialize()
    service = BackupService(database, tmp_path / "backups", keep=2)
    day = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
    assert service.backup_today(day) is not None
    assert service.backup_today(day) is None  # already done today
    service.backup_today(day + timedelta(days=1))
    service.backup_today(day + timedelta(days=2))
    assert [path.name for path in service.daily_backups()] == ["campex-node-20261008.sqlite3", "campex-node-20261009.sqlite3"]
