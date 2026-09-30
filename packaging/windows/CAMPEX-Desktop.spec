# PyInstaller spec for CAMPEX Node on Windows.
#
# Build from repository root:
#   powershell -ExecutionPolicy Bypass -File packaging/windows/build.ps1

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


ROOT = Path.cwd()


def safe_collect_all(package: str):
    try:
        return collect_all(package)
    except Exception:
        return [], [], []


torch_datas, torch_binaries, torch_hiddenimports = safe_collect_all("torch")
torchvision_datas, torchvision_binaries, torchvision_hiddenimports = safe_collect_all("torchvision")
ultralytics_datas, ultralytics_binaries, ultralytics_hiddenimports = safe_collect_all("ultralytics")


a = Analysis(
    [str(ROOT / "campex_node" / "desktop_launcher.py")],
    pathex=[str(ROOT)],
    binaries=[
        *torch_binaries,
        *torchvision_binaries,
        *ultralytics_binaries,
    ],
    datas=[
        (str(ROOT / "frontend" / "assets"), "frontend/assets"),
        (str(ROOT / "yolo11n.pt"), "."),
        *torch_datas,
        *torchvision_datas,
        *ultralytics_datas,
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
    + collect_submodules("cv2")
    + torch_hiddenimports
    + torchvision_hiddenimports
    + ultralytics_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "pandas",
        "scipy",
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
    name="CampexNode",
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
    name="CampexNode",
)
