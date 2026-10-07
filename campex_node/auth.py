from __future__ import annotations

import hashlib
import re
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from backend.auth.service import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    AuthError,
    hash_password,
    normalize_email,
    verify_password,
)
from campex_node.storage.sqlite import connect


# Accounts of the people who use this Node's panel, from this computer or
# from the factory network. Same contract as the Cloud's /auth API, so the
# panel signs in the same way against either.
SESSION_TTL = timedelta(days=30)
SESSION_TOKEN_PREFIX = "cxn_"
ROLES = ("admin", "operator")
NODE_ORGANIZATION = "node"

_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# Verified against when the email does not exist, so a failed login takes the
# same time whether or not the account exists.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL UNIQUE,
        user_agent TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
)


@dataclass(frozen=True)
class NodeUser:
    id: str
    name: str
    email: str
    role: str
    created_at: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "organization_id": NODE_ORGANIZATION,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class NodeSession:
    token: str
    expires_at: str
    user: NodeUser

    def as_dict(self) -> dict:
        return {"token": self.token, "expires_at": self.expires_at, "user": self.user.as_dict()}


class NodeAuthStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(self.database_path)) as connection:
            for statement in SCHEMA:
                connection.execute(statement)
            connection.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (_now().isoformat(),))
            connection.commit()

    def needs_setup(self) -> bool:
        """True until the first account (the administrator) exists."""
        with closing(connect(self.database_path)) as connection:
            return connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None

    def create_user(self, *, name: str, email: str, password: str, role: str = "operator") -> NodeUser:
        name, email = str(name or "").strip(), normalize_email(email)
        _validate(name, email, password)
        if role not in ROLES:
            raise AuthError("Perfil inválido.")
        user = NodeUser(
            id=f"usr_{uuid4().hex[:12]}",
            name=name,
            email=email,
            role=role,
            created_at=_now().isoformat(),
        )
        with closing(connect(self.database_path)) as connection:
            if connection.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                raise AuthError("Já existe uma conta com este email.", 409)
            connection.execute(
                "INSERT INTO users (id, name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user.id, user.name, user.email, hash_password(password), user.role, user.created_at),
            )
            connection.commit()
        return user

    def login(self, *, email: str, password: str, user_agent: str | None = None) -> NodeSession:
        with closing(connect(self.database_path)) as connection:
            row = connection.execute("SELECT * FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
        if row is None:
            verify_password(password or "", _DUMMY_PASSWORD_HASH)
            raise AuthError("Email ou senha inválidos.", 401)
        if not verify_password(password or "", row["password_hash"]):
            raise AuthError("Email ou senha inválidos.", 401)
        return self.create_session(_row_to_user(row), user_agent)

    def create_session(self, user: NodeUser, user_agent: str | None = None) -> NodeSession:
        token = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = _now()
        expires_at = (now + SESSION_TTL).isoformat()
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO user_sessions (id, user_id, token_hash, user_agent, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (f"ses_{uuid4().hex}", user.id, _hash_token(token), (user_agent or "")[:300], now.isoformat(), expires_at),
            )
            connection.commit()
        return NodeSession(token=token, expires_at=expires_at, user=user)

    def authenticate(self, token: str | None) -> NodeUser | None:
        if not token or not token.startswith(SESSION_TOKEN_PREFIX):
            return None
        with closing(connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT u.*, s.expires_at AS session_expires_at
                FROM user_sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                """,
                (_hash_token(token),),
            ).fetchone()
        if row is None or datetime.fromisoformat(row["session_expires_at"]) <= _now():
            return None
        return _row_to_user(row)

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with closing(connect(self.database_path)) as connection:
            connection.execute("DELETE FROM user_sessions WHERE token_hash = ?", (_hash_token(token),))
            connection.commit()

    def list_users(self) -> list[NodeUser]:
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        return [_row_to_user(row) for row in rows]

    def update_user(self, user_id: str, *, role: str | None = None, password: str | None = None) -> NodeUser:
        user = self._get(user_id)
        if role is not None:
            if role not in ROLES:
                raise AuthError("Perfil inválido.")
            if user.is_admin and role != "admin" and self._admin_count() == 1:
                raise AuthError("A fábrica precisa de pelo menos um administrador.")
        if password is not None:
            _validate_password(password)
        with closing(connect(self.database_path)) as connection:
            if role is not None:
                connection.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
            if password is not None:
                connection.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
                # A new password signs the account out everywhere.
                connection.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
            connection.commit()
        return self._get(user_id)

    def change_password(self, user: NodeUser, current: str, new: str) -> None:
        with closing(connect(self.database_path)) as connection:
            row = connection.execute("SELECT password_hash FROM users WHERE id = ?", (user.id,)).fetchone()
        if row is None or not verify_password(current or "", row["password_hash"]):
            raise AuthError("Senha atual incorreta.", 403)
        self.update_user(user.id, password=new)

    def delete_user(self, user_id: str) -> None:
        user = self._get(user_id)
        if user.is_admin and self._admin_count() == 1:
            raise AuthError("A fábrica precisa de pelo menos um administrador.")
        with closing(connect(self.database_path)) as connection:
            connection.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
            connection.commit()

    def _get(self, user_id: str) -> NodeUser:
        with closing(connect(self.database_path)) as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise AuthError("Usuário não encontrado.", 404)
        return _row_to_user(row)

    def _admin_count(self) -> int:
        with closing(connect(self.database_path)) as connection:
            return connection.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]


def _validate(name: str, email: str, password: str) -> None:
    if len(name) < 2:
        raise AuthError("Informe um nome com pelo menos 2 caracteres.")
    if len(name) > 120:
        raise AuthError("O nome pode ter no máximo 120 caracteres.")
    if not _EMAIL_PATTERN.match(email) or len(email) > 254:
        raise AuthError("Informe um email válido.")
    _validate_password(password)


def _validate_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"A senha precisa ter pelo menos {MIN_PASSWORD_LENGTH} caracteres.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError(f"A senha pode ter no máximo {MAX_PASSWORD_LENGTH} caracteres.")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_user(row) -> NodeUser:
    return NodeUser(
        id=row["id"],
        name=row["name"],
        email=row["email"],
        role=row["role"],
        created_at=row["created_at"],
    )
