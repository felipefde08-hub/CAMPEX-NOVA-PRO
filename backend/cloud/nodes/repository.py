from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.config import Settings
from backend.database.db import connect

from .models import ClaimedNode, NodePairingSession, PairingCode


PAIRING_CODE_TTL_MINUTES = 15
NODE_INITIATED_PAIRING_TTL_MINUTES = 10
NODE_OFFLINE_AFTER_SECONDS = 90


class NodeRepository:
    def __init__(self, settings: Settings) -> None:
        self.database_path: Path = settings.sqlite_path

    def create_pairing_code(
        self,
        *,
        organization_id: str,
        requested_by: str | None = None,
        ttl_minutes: int = PAIRING_CODE_TTL_MINUTES,
    ) -> PairingCode:
        code = _generate_pairing_code()
        expires_at = _utc_now() + timedelta(minutes=ttl_minutes)
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO node_pairing_codes (
                    code, organization_id, requested_by, expires_at, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    code,
                    organization_id,
                    requested_by,
                    expires_at.isoformat(),
                    _utc_now().isoformat(),
                ),
            )
            connection.commit()
        return PairingCode(code=code, organization_id=organization_id, expires_at=expires_at.isoformat())

    def claim_pairing_code(
        self,
        *,
        code: str,
        node_name: str,
        platform: str,
        hostname: str,
        version: str,
    ) -> ClaimedNode | None:
        normalized_code = code.strip().upper()
        now = _utc_now()
        node_token = secrets.token_urlsafe(40)
        node_id = f"node_{secrets.token_hex(8)}"
        token_hash = _hash_token(node_token)
        with connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT * FROM node_pairing_codes
                WHERE code = ? AND claimed_at IS NULL AND expires_at > ?
                """,
                (normalized_code, now.isoformat()),
            ).fetchone()
            if row is None:
                return None

            organization_id = row["organization_id"]
            connection.execute(
                """
                INSERT INTO campex_nodes (
                    id, organization_id, name, token_hash, status, version,
                    platform, hostname, last_seen_at, revoked_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'online', ?, ?, ?, ?, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    node_id,
                    organization_id,
                    node_name,
                    token_hash,
                    version,
                    platform,
                    hostname,
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE node_pairing_codes
                SET claimed_at = ?, claimed_node_id = ?
                WHERE code = ?
                """,
                (now.isoformat(), node_id, normalized_code),
            )
            connection.commit()
        return ClaimedNode(
            node_id=node_id,
            organization_id=organization_id,
            node_token=node_token,
            name=node_name,
        )

    def start_pairing_session(
        self,
        *,
        node_public_id: str,
        node_name: str,
        hostname: str | None,
        platform: str | None,
        architecture: str | None,
        version: str,
        ttl_minutes: int = NODE_INITIATED_PAIRING_TTL_MINUTES,
    ) -> NodePairingSession:
        now = _utc_now()
        expires_at = now + timedelta(minutes=ttl_minutes)
        session_id = f"nps_{secrets.token_hex(12)}"
        pairing_code = _generate_short_pairing_code()
        code_hash = _hash_pairing_code(pairing_code)
        with connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE node_pairing_sessions
                SET status = 'expired', updated_at = ?
                WHERE node_public_id = ? AND status = 'pending'
                """,
                (now.isoformat(), node_public_id),
            )
            connection.execute(
                """
                INSERT INTO node_pairing_sessions (
                    id, pairing_code_hash, node_public_id, node_name, hostname,
                    platform, architecture, version, status, expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    session_id,
                    code_hash,
                    node_public_id,
                    node_name,
                    hostname,
                    platform,
                    architecture,
                    version,
                    expires_at.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.commit()
        return NodePairingSession(
            id=session_id,
            pairing_code=pairing_code,
            node_public_id=node_public_id,
            expires_at=expires_at.isoformat(),
        )

    def get_pairing_session_status(self, session_id: str, node_public_id: str) -> dict | None:
        now = _utc_now().isoformat()
        with connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE node_pairing_sessions
                SET status = 'expired', updated_at = ?
                WHERE status = 'pending' AND expires_at <= ?
                """,
                (now, now),
            )
            row = connection.execute(
                """
                SELECT session.*, node.token_hash
                FROM node_pairing_sessions session
                LEFT JOIN campex_nodes node ON node.id = session.authorized_node_id
                WHERE session.id = ? AND session.node_public_id = ?
                """,
                (session_id, node_public_id),
            ).fetchone()
            connection.commit()
        return dict(row) if row else None

    def find_pending_pairing_session_by_code(self, code: str) -> dict | None:
        now = _utc_now().isoformat()
        with connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT * FROM node_pairing_sessions
                WHERE pairing_code_hash = ?
                  AND status = 'pending'
                  AND expires_at > ?
                """,
                (_hash_pairing_code(code), now),
            ).fetchone()
        return dict(row) if row else None

    def authorize_pairing_session(
        self,
        *,
        code: str,
        organization_id: str,
        node_name: str | None = None,
    ) -> ClaimedNode | None:
        normalized_code = _normalize_short_code(code)
        now = _utc_now()
        node_token = secrets.token_urlsafe(40)
        node_id = f"node_{secrets.token_hex(8)}"
        token_hash = _hash_token(node_token)
        with connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT * FROM node_pairing_sessions
                WHERE pairing_code_hash = ?
                  AND status = 'pending'
                  AND expires_at > ?
                """,
                (_hash_pairing_code(normalized_code), now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            display_name = (node_name or row["node_name"] or row["hostname"] or "CAMPEX Node").strip()
            connection.execute(
                """
                INSERT INTO campex_nodes (
                    id, organization_id, name, token_hash, status, version,
                    platform, hostname, last_seen_at, revoked_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'online', ?, ?, ?, ?, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    node_id,
                    organization_id,
                    display_name,
                    token_hash,
                    row["version"],
                    row["platform"],
                    row["hostname"],
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE node_pairing_sessions
                SET status = 'authorized',
                    organization_id = ?,
                    authorized_node_id = ?,
                    node_token_once = ?,
                    authorized_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (organization_id, node_id, node_token, now.isoformat(), now.isoformat(), row["id"]),
            )
            connection.commit()
        return ClaimedNode(
            node_id=node_id,
            organization_id=organization_id,
            node_token=node_token,
            name=display_name,
        )

    def consume_authorized_pairing_session(self, session_id: str, node_public_id: str) -> ClaimedNode | None:
        now = _utc_now().isoformat()
        with connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT session.*, node.name
                FROM node_pairing_sessions session
                JOIN campex_nodes node ON node.id = session.authorized_node_id
                WHERE session.id = ?
                  AND session.node_public_id = ?
                  AND session.status = 'authorized'
                  AND session.node_token_once IS NOT NULL
                """,
                (session_id, node_public_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE node_pairing_sessions
                SET node_token_once = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, session_id),
            )
            connection.commit()
        return ClaimedNode(
            node_id=row["authorized_node_id"],
            organization_id=row["organization_id"],
            node_token=row["node_token_once"],
            name=row["name"],
        )

    def get_node_by_token(self, token: str) -> dict | None:
        token_hash = _hash_token(token)
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM campex_nodes
                WHERE token_hash IS NOT NULL AND revoked_at IS NULL
                """
            ).fetchall()
        for row in rows:
            if hmac.compare_digest(str(row["token_hash"]), token_hash):
                return dict(row)
        return None

    def list_nodes(self, organization_id: str) -> list[dict]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT id, organization_id, name, status, version, platform, hostname,
                       cameras_total, cameras_online, vision_status, queue_size,
                       last_seen_at, revoked_at,
                       created_at, updated_at
                FROM campex_nodes
                WHERE organization_id = ?
                ORDER BY created_at DESC
                """,
                (organization_id,),
            ).fetchall()
        return [_with_computed_status(dict(row)) for row in rows]

    def get_node(self, node_id: str, organization_id: str | None = None) -> dict | None:
        with connect(self.database_path) as connection:
            if organization_id is None:
                row = connection.execute(
                    "SELECT * FROM campex_nodes WHERE id = ?",
                    (node_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM campex_nodes WHERE id = ? AND organization_id = ?",
                    (node_id, organization_id),
                ).fetchone()
        return _with_computed_status(dict(row)) if row else None

    def update_heartbeat(
        self,
        *,
        node_id: str,
        status: str,
        version: str,
        platform: str | None,
        hostname: str | None,
        cameras_total: int,
        cameras_online: int,
        vision_status: str | None,
        queue_size: int,
    ) -> None:
        with connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE campex_nodes
                SET status = ?,
                    version = ?,
                    platform = COALESCE(?, platform),
                    hostname = COALESCE(?, hostname),
                    cameras_total = ?,
                    cameras_online = ?,
                    vision_status = ?,
                    queue_size = ?,
                    last_seen_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND revoked_at IS NULL
                """,
                (
                    status,
                    version,
                    platform,
                    hostname,
                    cameras_total,
                    cameras_online,
                    vision_status,
                    queue_size,
                    _utc_now().isoformat(),
                    node_id,
                ),
            )
            connection.commit()

    def rename_node(self, node_id: str, organization_id: str, name: str) -> dict | None:
        with connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE campex_nodes
                SET name = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND organization_id = ?
                """,
                (name, node_id, organization_id),
            )
            connection.commit()
        return self.get_node(node_id, organization_id)

    def revoke_node(self, node_id: str, organization_id: str) -> bool:
        with connect(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE campex_nodes
                SET revoked_at = ?, status = 'offline', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND organization_id = ? AND revoked_at IS NULL
                """,
                (_utc_now().isoformat(), node_id, organization_id),
            )
            connection.commit()
            return cursor.rowcount > 0


def _generate_pairing_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2)]
    return f"CXP-{parts[0]}-{parts[1]}"


def _generate_short_pairing_code() -> str:
    value = secrets.randbelow(1_000_000)
    return f"{value:06d}"


def _normalize_short_code(code: str) -> str:
    digits = "".join(ch for ch in code if ch.isdigit())
    return digits


def _hash_pairing_code(code: str) -> str:
    return hashlib.sha256(_normalize_short_code(code).encode("utf-8")).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _with_computed_status(node: dict) -> dict:
    if node.get("revoked_at"):
        node["status"] = "offline"
        return node
    last_seen_at = node.get("last_seen_at")
    if not last_seen_at:
        node["status"] = "offline"
        return node
    try:
        parsed = datetime.fromisoformat(str(last_seen_at))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        node["status"] = "offline"
        return node
    if (_utc_now() - parsed).total_seconds() > NODE_OFFLINE_AFTER_SECONDS:
        node["status"] = "offline"
    return node
