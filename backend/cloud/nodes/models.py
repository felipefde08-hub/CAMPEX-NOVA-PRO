from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PairingCode:
    code: str
    organization_id: str
    expires_at: str


@dataclass(frozen=True)
class ClaimedNode:
    node_id: str
    organization_id: str
    node_token: str
    name: str


@dataclass(frozen=True)
class NodePairingSession:
    id: str
    pairing_code: str
    node_public_id: str
    expires_at: str
    status: str = "pending"
