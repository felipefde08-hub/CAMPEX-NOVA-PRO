from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend.config import Settings
from backend.database.db import connect


SESSION_TTL = timedelta(days=30)
SESSION_TOKEN_PREFIX = "cxs_"
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256

# scrypt cost: ~16 MB and a few dozen ms per hash, which keeps brute force slow
# without blowing the serverless memory budget.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64

_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class AuthError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class AuthUser:
    id: str
    organization_id: str
    name: str
    email: str
    role: str
    created_at: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "organization_id": self.organization_id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class AuthSession:
    token: str
    expires_at: str
    user: AuthUser

    def as_dict(self) -> dict:
        return {"token": self.token, "expires_at": self.expires_at, "user": self.user.as_dict()}


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        if algorithm != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when the email does not exist, so a login attempt takes the
# same time whether or not the account exists.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_user(row) -> AuthUser:
    return AuthUser(
        id=row["id"],
        organization_id=row["organization_id"],
        name=row["name"],
        email=row["email"],
        role=row["role"],
        created_at=str(row["created_at"]),
    )


def _validate_registration(name: str, email: str, password: str) -> None:
    if len(name) < 2:
        raise AuthError("Informe um nome com pelo menos 2 caracteres.")
    if len(name) > 120:
        raise AuthError("O nome pode ter no máximo 120 caracteres.")
    if not _EMAIL_PATTERN.match(email) or len(email) > 254:
        raise AuthError("Informe um email válido.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"A senha precisa ter pelo menos {MIN_PASSWORD_LENGTH} caracteres.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError(f"A senha pode ter no máximo {MAX_PASSWORD_LENGTH} caracteres.")


class AuthService:
    def __init__(self, settings: Settings) -> None:
        self.database_target = settings.database_target

    def register(
        self,
        *,
        name: str,
        email: str,
        password: str,
        organization_name: str | None = None,
        user_agent: str | None = None,
    ) -> AuthSession:
        name = str(name or "").strip()
        email = normalize_email(email)
        password = str(password or "")
        _validate_registration(name, email, password)
        password_hash = hash_password(password)

        # Every self-service account owns a new organization; inviting
        # teammates into an existing one is a separate flow.
        organization_id = f"org_{uuid4().hex[:16]}"
        user_id = f"usr_{uuid4().hex[:16]}"
        organization_label = str(organization_name or "").strip() or f"Organização de {name}"
        with connect(self.database_target) as connection:
            existing = connection.execute(
                "SELECT 1 FROM users WHERE email = ?", (email,)
            ).fetchone()
            if existing is not None:
                raise AuthError("Já existe uma conta com este email.", status_code=409)
            connection.execute(
                "INSERT INTO organizations (id, name) VALUES (?, ?)",
                (organization_id, organization_label[:120]),
            )
            connection.execute(
                """
                INSERT INTO users (id, organization_id, name, email, password_hash, role)
                VALUES (?, ?, ?, ?, ?, 'owner')
                """,
                (user_id, organization_id, name, email, password_hash),
            )
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return self._create_session(connection, _row_to_user(row), user_agent)

    def login(self, *, email: str, password: str, user_agent: str | None = None) -> AuthSession:
        email = normalize_email(email)
        password = str(password or "")
        with connect(self.database_target) as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE email = ? AND disabled_at IS NULL", (email,)
            ).fetchone()
            password_hash = row["password_hash"] if row is not None else _DUMMY_PASSWORD_HASH
            if not verify_password(password, password_hash) or row is None:
                raise AuthError("Email ou senha inválidos.", status_code=401)
            connection.execute(
                "UPDATE users SET last_login_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (_utc_now().isoformat(), row["id"]),
            )
            return self._create_session(connection, _row_to_user(row), user_agent)

    def authenticate(self, token: str | None) -> AuthUser | None:
        if not token or not token.startswith(SESSION_TOKEN_PREFIX):
            return None
        with connect(self.database_target) as connection:
            row = connection.execute(
                """
                SELECT u.*, s.expires_at AS session_expires_at
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.revoked_at IS NULL AND u.disabled_at IS NULL
                """,
                (_hash_token(token),),
            ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["session_expires_at"]) <= _utc_now():
            return None
        return _row_to_user(row)

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with connect(self.database_target) as connection:
            connection.execute(
                "UPDATE user_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (_utc_now().isoformat(), _hash_token(token)),
            )

    def list_organization_users(self, organization_id: str) -> list[AuthUser]:
        with connect(self.database_target) as connection:
            rows = connection.execute(
                """
                SELECT * FROM users
                WHERE organization_id = ? AND disabled_at IS NULL
                ORDER BY created_at ASC
                """,
                (organization_id,),
            ).fetchall()
        return [_row_to_user(row) for row in rows]

    def _create_session(self, connection, user: AuthUser, user_agent: str | None) -> AuthSession:
        token = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
        expires_at = (_utc_now() + SESSION_TTL).isoformat()
        connection.execute(
            """
            INSERT INTO user_sessions (id, user_id, token_hash, user_agent, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (f"ses_{uuid4().hex}", user.id, _hash_token(token), (user_agent or "")[:300], expires_at),
        )
        return AuthSession(token=token, expires_at=expires_at, user=user)
