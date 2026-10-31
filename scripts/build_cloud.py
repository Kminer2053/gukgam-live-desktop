"""정해진 소스만 포함하는 Vercel FastAPI 배포 폴더를 만든다."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
from pathlib import Path


MODULES = (
    "gukgam/__init__.py", "gukgam/config.py", "gukgam/models.py", "gukgam/serialize.py", "gukgam/stage_mirror.py",
    "gukgam/nlp/__init__.py", "gukgam/nlp/floor.py", "gukgam/nlp/text.py",
    "gukgam/cloud/__init__.py", "gukgam/cloud/app.py", "gukgam/cloud/mirror.py",
    "gukgam/cloud/stream.py", "gukgam/cloud/tunnel.py", "gukgam/cloud/policy.py",
    "gukgam/cloud/source.py", "gukgam/cloud/source_projection.py",
    "gukgam/cloud/store.py", "gukgam/cloud/mongo_store.py",
    "gukgam/web/__init__.py", "gukgam/web/auth.py",
)
TEMPLATES = (
    "_macros.html", "admin.html", "base.html", "dashboard.html", "items.html", "login.html",
    "report.html", "search.html", "session.html", "session_new.html", "utterance.html",
)
ASSETS = (
    "css/app.css", "css/transcript-stages.css", "css/report.css", "img/favicon.svg", "img/icons.svg", "img/logo.svg",
    "js/admin.js", "js/audio-capture.js", "js/common.js", "js/dashboard.js", "js/items.js",
    "js/pcm-worklet.js", "js/report.js", "js/report-view.js", "js/search.js", "js/session-new.js",
    "js/session.js", "js/question-audio.js", "js/transcript-stages.js", "js/closed-block-view.js", "js/utterance.js",
    "vendor/README.txt", "vendor/hls.min.js",
)
GENERATED_NAMES = {
    "index.py", "requirements.txt", ".python-version", "vercel.json", ".vercelignore",
    "gukgam", "public", "docs", "__pycache__",
}
REQUIREMENTS = (
    "fastapi>=0.115\njinja2>=3.1\npymongo>=4.11,<5\npsycopg[binary]>=3.2,<4\n"
    "python-multipart>=0.0.9\ntzdata\n"
)


class BuildError(Exception):
    """원본 파일과 출력 폴더를 보호하기 위한 빌드 거부 사유."""


def _copies() -> dict[str, str]:
    files = {name: name for name in MODULES}
    files["docs/cloud/schema.sql"] = "docs/cloud/schema.sql"
    files.update({f"gukgam/web/templates/{name}": f"gukgam/web/templates/{name}"
                  for name in TEMPLATES})
    files.update({f"public/static/{name}": f"gukgam/web/static/{name}" for name in ASSETS})
    return files


def _is_link(path: Path) -> bool:
    # Windows 디렉터리 junction은 is_symlink()만으로 확인되지 않는다.
    return path.is_symlink() or path.is_junction()


def _linked_ancestor(path: Path) -> bool:
    # 먼저 상위부터 검사해 연결된 외부 폴더를 따라 내려가지 않는다.
    return any(_is_link(item) for item in reversed((path, *path.parents)))


def _contains_link(folder: Path) -> bool:
    waiting = [folder]
    while waiting:
        for child in waiting.pop().iterdir():
            if _is_link(child):
                return True
            if child.is_dir():
                waiting.append(child)
    return False


def _source(root: Path, relative: str) -> Path:
    file = root / relative
    if any(_is_link(path) for path in (file, *file.parents) if path != root and root in path.parents):
        raise BuildError("소스 허용목록에 심볼릭 링크 또는 junction이 있어 빌드를 중단했습니다")
    if not file.is_file():
        raise BuildError("필수 클라우드 소스가 없어 빌드를 중단했습니다")
    return file


def _validate_output(root: Path, out: Path) -> None:
    if _linked_ancestor(out.absolute()):
        raise BuildError("출력 폴더와 상위 경로는 심볼릭 링크 또는 junction일 수 없습니다")
    target = out.resolve()
    if target == root or target in root.parents:
        raise BuildError("저장소와 그 상위 폴더를 출력 폴더로 사용할 수 없습니다")
    if root in target.parents and target.relative_to(root).parts[0] != "cloud":
        raise BuildError("저장소 안에서는 cloud 폴더만 출력 위치로 사용할 수 있습니다")
    if out.exists() and not out.is_dir():
        raise BuildError("출력 위치가 폴더가 아닙니다")
    if not out.exists():
        return
    if any(child.name not in GENERATED_NAMES | {".vercel"} for child in out.iterdir()):
        raise BuildError("출력 폴더에 생성물이 아닌 파일이 있습니다. 별도 보관 후 다시 실행하세요")
    if _contains_link(out):
        raise BuildError("출력 폴더에 심볼릭 링크 또는 junction이 있어 빌드를 중단했습니다")
    metadata = out / ".vercel"
    if metadata.exists() and not metadata.is_dir():
        raise BuildError("Vercel 연결 정보 위치가 폴더가 아닙니다")


def _ignore(files: dict[str, str]) -> str:
    # 새로 생긴 파일도 자동 업로드되지 않도록 정확한 파일만 다시 허용한다.
    names = set(files) | {"index.py", "requirements.txt", ".python-version", "vercel.json"}
    directories = {parent.as_posix() for name in names for parent in Path(name).parents if parent != Path(".")}
    # CLI는 디렉터리도 끝 슬래시 없이 검사하므로 디렉터리 경로 자체를 허용한다.
    lines = ["**", *(f"!{name}" for name in sorted(directories, key=lambda name: (name.count("/"), name))),
             *(f"!{name}" for name in sorted(names)), ".env*", "**/.env*", ".vercel/",
             "**/__pycache__/", "**/*.pyc"]
    return "\n".join(lines) + "\n"


def build_cloud(root: Path, out: Path, *, region: str = "sin1") -> int:
    """허용목록을 검증한 뒤 생성물만 교체하고 기존 Vercel 연결 정보를 보존한다."""
    if not re.fullmatch(r"[a-z]{3}[1-9]", region):
        raise BuildError("Vercel 리전 코드를 올바르게 지정하세요")
    if _linked_ancestor(root.absolute()):
        raise BuildError("소스 저장소와 상위 경로는 심볼릭 링크 또는 junction일 수 없습니다")
    root = root.resolve()
    _validate_output(root, out)
    copies = _copies()
    sources = {destination: _source(root, source) for destination, source in copies.items()}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cloud-build-", dir=out.parent) as task_stage:
        stage = Path(task_stage)
        for destination, source in sources.items():
            target = stage / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        (stage / "index.py").write_text(
            "from gukgam.cloud.app import create_cloud_app\n\napp = create_cloud_app()\n", encoding="utf-8")
        (stage / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
        (stage / ".python-version").write_text("3.12\n", encoding="utf-8")
        (stage / "vercel.json").write_text(json.dumps({
            "$schema": "https://openapi.vercel.sh/vercel.json", "framework": "fastapi",
            "regions": [region], "functions": {"index.py": {"maxDuration": 60}},
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (stage / ".vercelignore").write_text(_ignore(copies), encoding="utf-8")
        # 복사를 모두 마친 뒤 생성물만 바꾼다. 알 수 없는 파일은 위 검사에서 거부한다.
        _validate_output(root, out)
        out.mkdir(parents=True, exist_ok=True)
        for name in GENERATED_NAMES:
            old = out / name
            if old.is_dir():
                shutil.rmtree(old)
            elif old.exists():
                old.unlink()
        # Windows의 비공개 임시 폴더 DACL을 rename으로 배포물에 옮기지 않는다.
        # 출력 아래 새 항목을 생성해 출력 부모의 접근 규칙을 상속한다. ACL은 변경하지 않는다.
        for child in stage.iterdir():
            destination = out / child.name
            if child.is_dir():
                shutil.copytree(child, destination, copy_function=shutil.copyfile)
            else:
                shutil.copyfile(child, destination)
    return len(copies) + 5


def main() -> int:
    parser = argparse.ArgumentParser(description="비밀 파일을 제외한 클라우드 전용 폴더 생성")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="소스 저장소 위치")
    parser.add_argument("--out", type=Path, help="출력 폴더(기본: 저장소/cloud)")
    parser.add_argument("--region", default="sin1", help="Vercel 함수 리전(실제 DB 리전 확인 후 지정)")
    args = parser.parse_args()
    try:
        count = build_cloud(args.root, args.out or args.root / "cloud", region=args.region)
    except BuildError as exc:
        parser.exit(1, f"클라우드 빌드 실패: {exc}\n")
    except OSError as exc:
        parser.exit(1, f"클라우드 파일 준비 실패({type(exc).__name__})\n")
    print(f"클라우드 전용 파일 {count}개를 준비했습니다. DB 리전 확인 후 배포하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
