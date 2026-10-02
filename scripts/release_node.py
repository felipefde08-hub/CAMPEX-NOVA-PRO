"""Signing tool for CAMPEX Node releases.

One time, on the publisher's machine:
    python scripts/release_node.py keygen
creates the private signing key (keep it safe, never commit it) and writes
the matching public key into campex_node/updates/release_public_key.txt,
which is packaged into every Node.

For each release (packaging/windows/build.ps1 runs this):
    python scripts/release_node.py manifest dist/CampexNode-windows.zip
writes dist/campex-node-manifest.json, to be uploaded next to the zip.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campex_node import __version__  # noqa: E402
from campex_node.updates.release import PUBLIC_KEY_PATH, ReleaseManifest, public_key_text, sign_manifest  # noqa: E402

DEFAULT_KEY_PATH = Path.home() / ".campex" / "campex-node-release.key"
RELEASE_URL = "https://github.com/felipefde08-hub/CAMPEX-NOVA-PRO/releases/download/campex-node-v{version}/{name}"


def key_path(value: str | None) -> Path:
    return Path(value or os.getenv("CAMPEX_NODE_RELEASE_KEY") or DEFAULT_KEY_PATH).expanduser()


def keygen(args: argparse.Namespace) -> int:
    path = key_path(args.key)
    if path.exists():
        print(f"A chave já existe em {path}; não foi substituída.", file=sys.stderr)
        return 1
    private_key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    PUBLIC_KEY_PATH.write_text(public_key_text(private_key) + "\n", encoding="ascii")
    print(f"Chave privada: {path}  (faça backup; não coloque no repositório)")
    print(f"Chave pública: {PUBLIC_KEY_PATH}  (vai dentro do Node; faça commit)")
    return 0


def manifest(args: argparse.Namespace) -> int:
    path = key_path(args.key)
    private_key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise SystemExit("A chave de assinatura precisa ser Ed25519.")
    expected_public = PUBLIC_KEY_PATH.read_text(encoding="ascii").strip()
    if public_key_text(private_key) != expected_public:
        raise SystemExit(f"{path} não corresponde à chave pública em {PUBLIC_KEY_PATH}.")

    package = Path(args.package)
    digest = hashlib.sha256()
    with package.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    url = args.url or RELEASE_URL.format(version=__version__, name=package.name)
    document = sign_manifest(
        private_key,
        ReleaseManifest(
            version=__version__,
            platform="windows",
            url=url,
            sha256=digest.hexdigest(),
            size=package.stat().st_size,
        ),
    )
    output = Path(args.output or package.with_name("campex-node-manifest.json"))
    output.write_bytes(document)
    print(f"Manifesto da versão {__version__}: {output}")
    print(f"Publique o zip e o manifesto na release campex-node-v{__version__} (marcada como latest).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key", help=f"Chave privada (padrão: {DEFAULT_KEY_PATH} ou CAMPEX_NODE_RELEASE_KEY).")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("keygen", help="Cria a chave de assinatura (uma única vez).")
    sign = commands.add_parser("manifest", help="Gera o manifesto assinado de um pacote.")
    sign.add_argument("package")
    sign.add_argument("--url", help="URL de download do pacote (padrão: release campex-node-v<versão>).")
    sign.add_argument("--output")
    args = parser.parse_args(argv)
    return keygen(args) if args.command == "keygen" else manifest(args)


if __name__ == "__main__":
    raise SystemExit(main())
