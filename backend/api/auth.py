from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from backend.auth.service import AuthError, AuthService, AuthUser
from backend.config import get_settings


SESSION_HEADER = "x-campex-session"

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class RegisterPayload(BaseModel):
    name: str
    email: str
    password: str
    organization_name: str | None = None


class LoginPayload(BaseModel):
    email: str
    password: str


def _service(request: Request) -> AuthService:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    return AuthService(settings)


def _session_token(request: Request) -> str | None:
    return request.headers.get(SESSION_HEADER) or None


def require_user(request: Request) -> AuthUser:
    user = _service(request).authenticate(_session_token(request))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão expirada. Entre novamente.")
    return user


def _raise_auth_error(error: AuthError) -> None:
    raise HTTPException(status_code=error.status_code, detail=error.message) from error


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterPayload, request: Request) -> dict:
    try:
        session = _service(request).register(
            name=payload.name,
            email=payload.email,
            password=payload.password,
            organization_name=payload.organization_name,
            user_agent=request.headers.get("user-agent"),
        )
    except AuthError as error:
        _raise_auth_error(error)
    return session.as_dict()


@router.post("/login")
def login(payload: LoginPayload, request: Request) -> dict:
    try:
        session = _service(request).login(
            email=payload.email,
            password=payload.password,
            user_agent=request.headers.get("user-agent"),
        )
    except AuthError as error:
        _raise_auth_error(error)
    return session.as_dict()


@router.get("/me")
def me(user: AuthUser = Depends(require_user)) -> dict:
    return {"user": user.as_dict()}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request) -> Response:
    _service(request).logout(_session_token(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users")
def organization_users(request: Request, user: AuthUser = Depends(require_user)) -> list[dict]:
    return [member.as_dict() for member in _service(request).list_organization_users(user.organization_id)]
