# PyInstaller spec for the local web launcher.
#
# Build from repository root after installing runtime dependencies:
#   py -3.12 -m pip install -r requirements.txt pyinstaller
#   py -3.12 -m PyInstaller packaging/windows/CAMPEX-Desktop.spec

from pathlib import Path


ROOT = Path.cwd()


a = Analysis(
    [str(ROOT / "scripts" / "run_desktop_web.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "frontend"), "frontend"),
        (str(ROOT / "backend"), "backend"),
        (str(ROOT / "scripts" / "init_db.py"), "scripts"),
        (str(ROOT / "yolo11n.pt"), "."),
    ],
    hiddenimports=[
        "backend.main",
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CAMPEX-Desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
