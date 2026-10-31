"""Windows 설치판에 넣을 Python 엔진을 모델 없이 묶는다."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import ssl
import subprocess
import sys
from types import CodeType
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "gukgam-engine.spec"
ENTRY = ROOT / "build" / "desktop-engine-entry" / "entry.py"
DIST = ROOT / "dist" / "engine"
WORK = ROOT / "build" / "desktop-engine"
REQUIRED = (
    "gukgam/desktop_runtime.py",
    "gukgam/web/templates/base.html",
    "gukgam/web/static/js/session.js",
    "gukgam/web/static/js/offline-session.js",
    "docs/cloud/schema.sql",
    "gukgam-engine.spec",
)
ENTRY_SOURCE = (
    '"""설치판의 표준입출력 JSONL 엔진 진입점."""\n'
    "from gukgam.desktop_runtime import main\n\n"
    "if __name__ == '__main__':\n"
    "    raise SystemExit(main())\n"
)


def tls_runtime_data(ssl_module=None) -> list[tuple[str, str]]:
    """동봉 ssl 생성자가 요구하는 공개 보안 자료만 같은 위치에 수집한다."""
    module = ssl if ssl_module is None else ssl_module
    name = "codex-rejected-cas.pem"
    code = getattr(module.SSLContext.__new__, "__code__", None)

    def requires_resource(value) -> bool:
        return isinstance(value, CodeType) and any(
            item == name if isinstance(item, str) else requires_resource(item)
            for item in value.co_consts)

    if not requires_resource(code):
        return []
    resource = Path(module.__file__).resolve().with_name(name)
    if not resource.is_file():
        raise ValueError("동봉 Python의 필수 TLS 보안 자료가 없습니다")
    return [(str(resource), ".")]


def validate_sources(root: Path = ROOT) -> dict:
    """소스를 실행하거나 설정·기록·비밀 파일을 읽지 않고 빌드 입력을 검사한다."""
    missing = [name for name in REQUIRED if not (root / name).is_file()]
    if missing:
        raise ValueError("엔진 빌드 입력이 없습니다: " + ", ".join(missing))
    for name in ("gukgam/web/templates", "gukgam/web/static"):
        source = root / name
        if source.is_symlink() or any(path.is_symlink() for path in source.rglob("*")):
            raise ValueError("엔진 자산에 심볼릭 링크를 포함할 수 없습니다")
    module = ast.parse((root / "gukgam/desktop_runtime.py").read_text(encoding="utf-8"))
    if not any(isinstance(node, ast.FunctionDef) and node.name == "main" for node in module.body):
        raise ValueError("엔진에 동기 main 진입점이 없습니다")
    return {
        "entry_module": "gukgam.desktop_runtime",
        "transport": "stdin/stdout JSONL",
        "target": "windows-x64",
        "engine_relative_path": "engine/gukgam-engine.exe",
        "bundled_models": 0,
        "tls_runtime_resources": [Path(source).name for source, _ in tls_runtime_data()],
        "required_inputs": list(REQUIRED),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="입력만 검사하고 빌드하지 않는다")
    args = parser.parse_args(argv)
    try:
        contract = validate_sources()
    except (ValueError, SyntaxError) as exc:
        parser.exit(2, f"{exc}\n")
    if args.check:
        print(json.dumps(contract, ensure_ascii=False, indent=2))
        return 0
    if sys.platform != "win32":
        parser.exit(2, "Windows 엔진은 Windows 빌드 환경에서 만드세요. 입력 검사는 --check를 사용하세요.\n")
    if sys.version_info < (3, 12):
        parser.exit(2, "엔진 빌드에는 Python 3.12 이상이 필요합니다.\n")
    ENTRY.parent.mkdir(parents=True, exist_ok=True)
    ENTRY.write_text(ENTRY_SOURCE, encoding="utf-8")
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(DIST), "--workpath", str(WORK), str(SPEC),
    ]
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        return result.returncode
    executable = DIST / "gukgam-engine" / "gukgam-engine.exe"
    internal = executable.parent / "_internal"
    if not executable.is_file() or not internal.is_dir():
        parser.exit(2, "엔진 실행 파일과 동봉 라이브러리를 확인하지 못했습니다.\n")
    for source, destination in tls_runtime_data():
        resource = internal / destination / Path(source).name
        if not resource.is_file() or hashlib.sha256(resource.read_bytes()).digest() != hashlib.sha256(Path(source).read_bytes()).digest():
            parser.exit(2, "필수 TLS 보안 자료가 설치 엔진에 동일하게 동봉되지 않았습니다.\n")
    # 실행 파일을 기동하지 않는다. 실제 Windows 기동·계정 연결은 별도 수용 시험이다.
    print(json.dumps({**contract, "built": str(executable), "executed": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
