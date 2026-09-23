from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from backend.cloud.nodes.repository import NodeRepository
from backend.config import get_settings


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    organization_id: str
    name: str


async def get_node_identity(request: Request) -> NodeIdentity:
    token = _bearer_token(request.headers.get("authorization"))
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Node token is required.",
        )
    node = NodeRepository(get_settings()).get_node_by_token(token)
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked node token.",
        )
    return NodeIdentity(
        node_id=node["id"],
        organization_id=node["organization_id"],
        name=node["name"],
    )


def node_identity_provider(identity: NodeIdentity = Depends(get_node_identity)) -> NodeIdentity:
    return identity


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip()
