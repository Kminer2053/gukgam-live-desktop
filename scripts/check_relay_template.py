"""원격에 보관한 릴레이 템플릿이 허용목록 원본과 같은지 검사한다."""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path


def check(root: Path) -> int:
    spec = importlib.util.spec_from_file_location("relay_builder", root / "scripts/build_cloud.py")
    assert spec and spec.loader
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    with tempfile.TemporaryDirectory(prefix="gukgam-relay-check-") as temp:
        expected = Path(temp) / "cloud"
        builder.build_cloud(root, expected, region="icn1")
        actual = root / "cloud"
        names = {str(path.relative_to(expected)) for path in expected.rglob("*") if path.is_file()}
        present = {str(path.relative_to(actual)) for path in actual.rglob("*")
                   if path.is_file() and "__pycache__" not in path.parts and ".vercel" not in path.parts}
        if present != names or any((actual / name).read_bytes() != (expected / name).read_bytes() for name in names):
            raise ValueError("릴레이 템플릿을 scripts/build_cloud.py --region icn1로 다시 준비하세요")
        return len(names)


if __name__ == "__main__":
    count = check(Path(__file__).resolve().parents[1])
    print(f"릴레이 템플릿 {count}개가 원본 허용목록과 같습니다.")
