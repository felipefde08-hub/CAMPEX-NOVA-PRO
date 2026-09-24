# PyInstaller spec for CAMPEX Node on Windows.
#
# Build from repository root:
#   powershell -ExecutionPolicy Bypass -File packaging/windows/build.ps1

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


ROOT = Path.cwd()


a = Analysis(
    [str(ROOT / "campex_node" / "desktop_launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "frontend" / "assets"), "frontend/assets"),
    ],
    hiddenimports=[
        "campex_node.local_app",
        "campex_node.node_ui",
        "backend.cameras.base",
        "backend.cameras.factory",
        "backend.cameras.frame_buffer",
        "backend.cameras.health",
        "backend.cameras.opencv_source",
        "backend.cameras.rtsp",
        "backend.cameras.security",
        "backend.config",
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
    ]
    + collect_submodules("cv2"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "pandas",
        "scipy",
        "torch",
        "ultralytics",
        "pytest",
        "numpy.tests",
        "numpy.f2py.tests",
        "numpy.lib.tests",
        "numpy.linalg.tests",
        "numpy.random.tests",
        "numpy.typing.tests",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CAMPEX-Node",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CAMPEX-Node",
)
