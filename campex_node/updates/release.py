"""Signed release manifests for CAMPEX Node updates.

A release publishes the Node package and a manifest describing it. The
manifest is signed with the publisher's Ed25519 private key; Nodes carry only
the public key, so a replaced download or manifest is rejected even if the
hosting account is compromised.

Manifest file layout:
    {"payload": "<JSON text>", "signature": "<base64 Ed25519 signature of payload>"}
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


PRODUCT = "campex-node"
PUBLIC_KEY_PATH = Path(__file__).with_name("release_public_key.txt")
_VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReleaseError(Exception):
    pass


@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    platform: str
    url: str
    sha256: str
    size: int


def parse_version(value: str) -> tuple[int, ...]:
    text = str(value or "").strip().lstrip("v")
    if not _VERSION_RE.match(text):
        raise ReleaseError(f"Versão inválida: {value!r}")
    return tuple(int(part) for part in text.split("."))


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def load_public_key(path: Path = PUBLIC_KEY_PATH) -> Ed25519PublicKey | None:
    try:
        text = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(text, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise ReleaseError("Chave pública de atualização inválida.") from exc


def public_key_text(private_key: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def sign_manifest(private_key: Ed25519PrivateKey, manifest: ReleaseManifest) -> bytes:
    payload = json.dumps(
        {
            "product": PRODUCT,
            "version": manifest.version,
            "platform": manifest.platform,
            "url": manifest.url,
            "sha256": manifest.sha256,
            "size": manifest.size,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    signature = base64.b64encode(private_key.sign(payload.encode("utf-8"))).decode("ascii")
    return json.dumps({"payload": payload, "signature": signature}, indent=2).encode("utf-8")


def verify_manifest(document: bytes, public_key: Ed25519PublicKey, *, platform: str) -> ReleaseManifest:
    try:
        envelope = json.loads(document.decode("utf-8"))
        payload_text = envelope["payload"]
        signature = base64.b64decode(envelope["signature"], validate=True)
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise ReleaseError("Manifesto de atualização malformado.") from exc
    if not isinstance(payload_text, str):
        raise ReleaseError("Manifesto de atualização malformado.")
    try:
        public_key.verify(signature, payload_text.encode("utf-8"))
    except InvalidSignature as exc:
        raise ReleaseError("Assinatura do manifesto de atualização inválida.") from exc

    # Only signed content is parsed from here on.
    payload = json.loads(payload_text)
    if payload.get("product") != PRODUCT:
        raise ReleaseError("Manifesto não é do CAMPEX Node.")
    if payload.get("platform") != platform:
        raise ReleaseError(f"Manifesto é para {payload.get('platform')!r}, não {platform!r}.")
    url = str(payload.get("url") or "")
    if not url.startswith("https://"):
        raise ReleaseError("O pacote de atualização precisa ser baixado por HTTPS.")
    sha256 = str(payload.get("sha256") or "").lower()
    if not _SHA256_RE.match(sha256):
        raise ReleaseError("Hash SHA-256 do pacote inválido no manifesto.")
    size = payload.get("size")
    if not isinstance(size, int) or size <= 0:
        raise ReleaseError("Tamanho do pacote inválido no manifesto.")
    version = str(payload.get("version") or "")
    parse_version(version)
    return ReleaseManifest(version=version, platform=payload["platform"], url=url, sha256=sha256, size=size)
