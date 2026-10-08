"""Downloads the pinned go2rtc release into campex_node/bin/.

    python scripts/fetch_go2rtc.py            # this computer's platform
    python scripts/fetch_go2rtc.py linux_arm64

The packaging scripts run it so the binary ships inside the Node. Every
download is checked against the SHA-256 published with the release; update
VERSION and HASHES together.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import sys
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = ROOT / "campex_node" / "bin"
VERSION = "v1.9.14"
URL = "https://github.com/AlexxIT/go2rtc/releases/download/{version}/{asset}"
# Release asset -> SHA-256, from the GitHub release of VERSION.
HASHES = {
    "go2rtc_win64.zip": "dd4167d75cb04abe618855b7c71f8658bd009f60c1a71835d134d2c11c939907",
    "go2rtc_win_arm64.zip": "814be0f6d8669025c7bccdd1f026ffaf613abae5352239f4ec84de543b94594a",
    "go2rtc_linux_amd64": "32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6",
    "go2rtc_linux_arm64": "359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50",
    "go2rtc_mac_amd64.zip": "9b0b9a27a4dc3a5b8b93376e7e8fc2787c6af624a512842622be84aec0171c7a",
    "go2rtc_mac_arm64.zip": "919b78adc759d6b3883d1e1b2ac915ac0985bb903ff1897b4d228527bd64690c",
}
PLATFORMS = {
    "win64": "go2rtc_win64.zip",
    "win_arm64": "go2rtc_win_arm64.zip",
    "linux_amd64": "go2rtc_linux_amd64",
    "linux_arm64": "go2rtc_linux_arm64",
    "mac_amd64": "go2rtc_mac_amd64.zip",
    "mac_arm64": "go2rtc_mac_arm64.zip",
}


def current_platform() -> str:
    machine = platform.machine().lower()
    arm = machine in {"arm64", "aarch64"}
    if sys.platform == "win32":
        return "win_arm64" if arm else "win64"
    if sys.platform == "darwin":
        return "mac_arm64" if arm else "mac_amd64"
    return "linux_arm64" if arm else "linux_amd64"


def fetch(platform_name: str) -> Path:
    asset = PLATFORMS[platform_name]
    with urllib.request.urlopen(URL.format(version=VERSION, asset=asset), timeout=120) as response:
        payload = response.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != HASHES[asset]:
        raise SystemExit(f"SHA-256 mismatch for {asset}: got {digest}")
    name = "go2rtc.exe" if platform_name.startswith("win") else "go2rtc"
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            payload = archive.read(name)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = BIN_DIR / name
    target.write_bytes(payload)
    if os.name != "nt":
        target.chmod(0o755)
    return target


def main(argv: list[str]) -> int:
    platform_name = argv[0] if argv else current_platform()
    if platform_name not in PLATFORMS:
        raise SystemExit(f"Unknown platform {platform_name}; use one of: {', '.join(PLATFORMS)}")
    print(f"go2rtc {VERSION} -> {fetch(platform_name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
