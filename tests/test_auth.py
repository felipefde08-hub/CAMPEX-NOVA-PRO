from __future__ import annotations

from fastapi.testclient import TestClient

from backend.auth.service import hash_password, verify_password
from backend.config import Settings
from backend.database.db import initialize_database
from backend.database.postgres import PG_CURRENT_TIMESTAMP, Row, redact_url, translate_sql
from backend.main import app


ACCOUNT = {"name": "Ana Operadora", "email": "Ana@Empresa.com ", "password": "senha-forte-123"}


def _configure_db(monkeypatch, tmp_path, *, api_token: str | None = None) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'auth.sqlite3'}")
    if api_token is None:
        monkeypatch.delenv("CAMPEX_API_TOKEN", raising=False)
        monkeypatch.delenv("CAMPEXTOKEN", raising=False)
    else:
        monkeypatch.setenv("CAMPEX_API_TOKEN", api_token)
    initialize_database(Settings.from_env())


def _session(token: str) -> dict:
    return {"X-CAMPEX-Session": token}


def test_register_login_me_and_logout(monkeypatch, tmp_path):
    _configure_db(monkeypatch, tmp_path)

    with TestClient(app) as client:
        created = client.post("/api/v1/auth/register", json=ACCOUNT)
        assert created.status_code == 201
        body = created.json()
        assert body["token"].startswith("cxs_")
        assert body["user"]["email"] == "ana@empresa.com"
        assert body["user"]["role"] == "owner"
        assert "password_hash" not in body["user"]

        me = client.get("/api/v1/auth/me", headers=_session(body["token"]))
        assert me.status_code == 200
        assert me.json()["user"]["id"] == body["user"]["id"]

        logged_in = client.post(
            "/api/v1/auth/login",
            json={"email": "ANA@empresa.com", "password": ACCOUNT["password"]},
        )
        assert logged_in.status_code == 200
        second_token = logged_in.json()["token"]
        assert second_token != body["token"]

        assert client.post("/api/v1/auth/logout", headers=_session(second_token)).status_code == 204
        assert client.get("/api/v1/auth/me", headers=_session(second_token)).status_code == 401
        # Logging out one device keeps the other session valid.
        assert client.get("/api/v1/auth/me", headers=_session(body["token"])).status_code == 200


def test_register_rejects_duplicate_email_and_weak_password(monkeypatch, tmp_path):
    _configure_db(monkeypatch, tmp_path)

    with TestClient(app) as client:
        assert client.post("/api/v1/auth/register", json=ACCOUNT).status_code == 201

        duplicate = client.post("/api/v1/auth/register", json={**ACCOUNT, "email": "ana@empresa.com"})
        assert duplicate.status_code == 409

        weak = client.post(
            "/api/v1/auth/register",
            json={"name": "Bruno", "email": "bruno@empresa.com", "password": "123"},
        )
        assert weak.status_code == 400
        assert "8 caracteres" in weak.json()["detail"]


def test_login_with_wrong_password_or_unknown_email_fails(monkeypatch, tmp_path):
    _configure_db(monkeypatch, tmp_path)

    with TestClient(app) as client:
        client.post("/api/v1/auth/register", json=ACCOUNT)

        wrong = client.post("/api/v1/auth/login", json={"email": ACCOUNT["email"], "password": "errada-123"})
        unknown = client.post("/api/v1/auth/login", json={"email": "ninguem@empresa.com", "password": "x" * 10})

        assert wrong.status_code == 401
        assert unknown.status_code == 401
        assert wrong.json()["detail"] == unknown.json()["detail"]


def test_organization_users_lists_only_own_organization(monkeypatch, tmp_path):
    _configure_db(monkeypatch, tmp_path)

    with TestClient(app) as client:
        ana = client.post("/api/v1/auth/register", json=ACCOUNT).json()
        client.post(
            "/api/v1/auth/register",
            json={"name": "Carlos", "email": "carlos@outra.com", "password": "senha-forte-456"},
        )

        users = client.get("/api/v1/auth/users", headers=_session(ana["token"]))

        assert users.status_code == 200
        assert [user["email"] for user in users.json()] == ["ana@empresa.com"]
        assert client.get("/api/v1/auth/users").status_code == 401


def test_user_session_passes_api_token_guard(monkeypatch, tmp_path):
    _configure_db(monkeypatch, tmp_path, api_token="cloud-api-token")

    with TestClient(app) as client:
        # Signing up must work before the browser has any credential.
        created = client.post("/api/v1/auth/register", json=ACCOUNT)
        assert created.status_code == 201
        token = created.json()["token"]

        assert client.get("/api/v1/cameras").status_code == 401
        assert client.get("/api/v1/cameras", headers=_session("cxs_invalid")).status_code == 401
        assert client.get("/api/v1/cameras", headers=_session(token)).status_code == 200


CAMERA = {
    "name": "Doca 1",
    "source_type": "rtsp",
    "source_uri": "rtsp://203.0.113.10:554/stream",
    "enabled": False,
    "vision_enabled": False,
}


def _register(client, name: str, email: str) -> str:
    response = client.post(
        "/api/v1/auth/register",
        json={"name": name, "email": email, "password": "senha-forte-123"},
    )
    assert response.status_code == 201
    return response.json()["token"]


def test_require_login_blocks_anonymous_access_but_keeps_public_routes(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMPEX_REQUIRE_LOGIN", "true")
    _configure_db(monkeypatch, tmp_path)

    with TestClient(app) as client:
        anonymous = client.get("/api/v1/cameras")
        assert anonymous.status_code == 401
        assert anonymous.json()["detail"] == "Faça login para continuar."
        assert client.get("/api/v1/cameras", headers=_session("cxs_forjado")).status_code == 401

        assert client.get("/api/v1/health").status_code == 200
        pairing = client.post(
            "/api/v1/nodes/pairing/start",
            json={"node_public_id": "node_public_1", "node_name": "Node Doca"},
        )
        assert pairing.status_code == 201

        token = _register(client, "Ana", "ana@empresa.com")
        assert client.get("/api/v1/cameras", headers=_session(token)).status_code == 200


def test_require_login_still_accepts_organization_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMPEX_REQUIRE_LOGIN", "true")
    monkeypatch.setenv("CAMPEX_ALLOWED_ORGANIZATION_IDS", "org_a")
    monkeypatch.setenv("CAMPEX_DEFAULT_ORGANIZATION_ID", "org_a")
    monkeypatch.setenv("CAMPEX_ORGANIZATION_TOKENS", "org_a:token-a")
    _configure_db(monkeypatch, tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/v1/cameras", headers={"Authorization": "Bearer token-a"}).status_code == 200
        assert client.get("/api/v1/cameras", headers={"Authorization": "Bearer errado"}).status_code == 401


def test_logged_in_users_only_see_their_own_organization(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMPEX_REQUIRE_LOGIN", "true")
    _configure_db(monkeypatch, tmp_path)

    with TestClient(app) as client:
        ana = _register(client, "Ana", "ana@empresa.com")
        carlos = _register(client, "Carlos", "carlos@outra.com")

        created = client.post("/api/v1/cameras", json=CAMERA, headers=_session(ana))
        assert created.status_code == 201
        camera_id = created.json()["id"]

        assert [camera["id"] for camera in client.get("/api/v1/cameras", headers=_session(ana)).json()] == [camera_id]
        assert client.get("/api/v1/cameras", headers=_session(carlos)).json() == []
        assert client.get(f"/api/v1/cameras/{camera_id}", headers=_session(carlos)).status_code == 404
        assert client.delete(f"/api/v1/cameras/{camera_id}", headers=_session(carlos)).status_code == 404

        # A header cannot move a logged-in user into another tenant.
        spoofed = client.get(
            "/api/v1/cameras",
            headers={**_session(carlos), "X-CAMPEX-Organization-Id": "default"},
        )
        assert spoofed.json() == []


def test_password_hash_round_trip():
    encoded = hash_password("senha-forte-123")

    assert encoded.startswith("scrypt$")
    assert "senha-forte-123" not in encoded
    assert verify_password("senha-forte-123", encoded)
    assert not verify_password("senha-errada", encoded)
    assert not verify_password("senha-forte-123", "formato-invalido")


def test_translate_sql_converts_placeholders_outside_literals():
    sql = "SELECT * FROM events WHERE id = ? AND note LIKE '50% ?' AND updated_at < CURRENT_TIMESTAMP"

    translated = translate_sql(sql, has_params=True)

    assert translated == (
        "SELECT * FROM events WHERE id = %s AND note LIKE '50%% ?' "
        f"AND updated_at < {PG_CURRENT_TIMESTAMP}"
    )
    assert translate_sql("SELECT '100%'", has_params=False) == "SELECT '100%'"


def test_translate_sql_converts_named_placeholders_but_not_casts():
    sql = "INSERT INTO t (id, note) VALUES (:id, ':literal') ON CONFLICT DO NOTHING RETURNING id::text"

    translated = translate_sql(sql, has_params=True, named=True)

    assert translated == (
        "INSERT INTO t (id, note) VALUES (%(id)s, ':literal') ON CONFLICT DO NOTHING RETURNING id::text"
    )


def test_row_supports_name_and_index_access():
    row = Row(["id", "total"], ["cam_1", 3])

    assert row["id"] == "cam_1"
    assert row[1] == 3
    assert dict(row) == {"id": "cam_1", "total": 3}


def test_redact_url_hides_credentials():
    redacted = redact_url("postgresql://owner:s3cret@ep-x-pooler.neon.tech/neondb?sslmode=require")

    assert redacted == "postgresql://***@ep-x-pooler.neon.tech/neondb"
