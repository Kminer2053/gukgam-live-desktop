# -*- mode: python ; coding: utf-8 -*-
"""기록·설정·계정·모델을 제외한 Windows 엔진 묶음."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules
from scripts.build_desktop_engine import tls_runtime_data


ROOT = Path(SPECPATH).resolve()
ENTRY = ROOT / "build" / "desktop-engine-entry" / "entry.py"
if not ENTRY.is_file():
    raise SystemExit("scripts/build_desktop_engine.py로 엔진을 빌드하세요")

EXCLUDED = [
    "mlx", "mlx_lm", "mlx_whisper", "torch", "torchaudio", "tensorflow",
    "pytest", "pytest_asyncio", "mongomock",
    "gukgam.llm.mlx_worker", "gukgam.stt.mlx_worker",
]
hidden = collect_submodules(
    "gukgam",
    filter=lambda name: name not in EXCLUDED,
)
hidden += [
    "gukgam.stt.nemotron_cpu", "gukgam.stt.nemotron_worker", "gukgam.stt.nemotron_assets",
    "gukgam.stt.nemotron_diarization", "gukgam.stt.nemotron_diarization_worker",
    "gukgam.stt.nemotron_diarization_assets",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on", "jinja2.ext",
]
datas = [
    (str(ROOT / "gukgam/web/templates"), "gukgam/web/templates"),
    (str(ROOT / "gukgam/web/static"), "gukgam/web/static"),
    (str(ROOT / "docs/cloud/schema.sql"), "docs/cloud"),
]
datas += collect_data_files("tzdata")
datas += tls_runtime_data()
hidden += collect_submodules("psycopg") + collect_submodules("psycopg_binary")
relay_binaries = collect_dynamic_libs("psycopg_binary")
# 선택 기능 실행기는 포함하고, 사용자 모델·계정·기록은 포함하지 않는다.
hidden += collect_submodules("onnxruntime") + collect_submodules("tokenizers")
relay_binaries += collect_dynamic_libs("onnxruntime") + collect_dynamic_libs("tokenizers")
datas += collect_data_files("onnxruntime") + collect_data_files("tokenizers")

a = Analysis(
    [str(ENTRY)], pathex=[str(ROOT)], binaries=relay_binaries, datas=datas,
    hiddenimports=sorted(set(hidden)), hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=EXCLUDED, noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="gukgam-engine",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=False, name="gukgam-engine",
)
