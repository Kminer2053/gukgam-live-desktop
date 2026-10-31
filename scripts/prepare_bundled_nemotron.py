"""공식 고정 CPU 화자분리 자산만 설치파일용 dist 폴더에 준비한다."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gukgam.stt.nemotron_diarization_assets import prepare, verify_assets, MODEL_REPO, MODEL_REVISION, MODEL_SHA256


async def build(target: Path, *, check=False):
    if not check:
        await prepare(target, confirm=True, accept_license=True)
    assets = await asyncio.to_thread(verify_assets, target)
    assets.close()
    return {"model": MODEL_REPO, "revision": MODEL_REVISION, "sha256": MODEL_SHA256,
            "verified": True, "inference_executed": False, "operating_data_included": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = ROOT / "dist/nemotron-assets"
    result = asyncio.run(build(target, check=args.check))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
