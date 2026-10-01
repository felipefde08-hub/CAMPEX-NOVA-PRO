from __future__ import annotations

from fastapi import Depends, Request

from backend.config import get_settings
from backend.security.organization_scope import OrganizationScope, resolve_organization_scope


def _request_settings(request: Request):
    return getattr(request.app.state, "settings", None) or get_settings()


async def get_organization_scope(request: Request) -> OrganizationScope:
    # Set by the API guard middleware after validating X-CAMPEX-Session. A
    # logged-in user always works inside their own organization; request
    # headers cannot point them at another tenant.
    user = getattr(request.state, "auth_user", None)
    if user is not None:
        return OrganizationScope(
            organization_id=user.organization_id,
            authenticated=True,
            source="user_session",
        )
    settings = _request_settings(request)
    authorization = request.headers.get("authorization")
    organization_token = request.headers.get("x-campex-organization-token")
    organization_id = request.headers.get("x-campex-organization-id")
    return resolve_organization_scope(
        settings,
        authorization=authorization,
        organization_token=organization_token,
        requested_organization_id=organization_id,
    )


def organization_id_provider(scope: OrganizationScope = Depends(get_organization_scope)) -> str:
    return scope.organization_id
