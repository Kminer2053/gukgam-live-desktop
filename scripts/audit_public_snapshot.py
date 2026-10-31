"""공개 후보 소스만 읽어 경로·비밀 패턴을 점검한다. 복사·배포·네트워크 호출은 없다."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
NEW_PUBLIC_FILES = (
    "docs/public-release/manual.html",
    "docs/public-release/RELEASE_OVERVIEW.md",
    "docs/public-release/THIRD_PARTY_NOTICES.md",
    "docs/public-release/RELEASE_MANIFEST.json",
    "docs/public-release/EXPORT_REVIEW.md",
    "scripts/audit_public_snapshot.py",
    "gukgam/llm/compat_config.py",
    "gukgam/llm/connection_test.py",
    "gukgam/llm/credential_store.py",
    "desktop/assets/brand/gukgam-live.svg",
    "desktop/assets/brand/gukgam-live-1024.png",
    "desktop/assets/brand/gukgam-live.ico",
)
PRIVATE_TOP = {".git", ".aws", ".agents", ".codex", ".claude", ".vercel", ".venv",
               "outputs", "records", "data", "models", "logs", "uploads", "captures",
               "build", "dist", "vendor"}
DATA_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".wav", ".mp3", ".m4a", ".mp4", ".webm",
                 ".flac", ".ogg", ".aac", ".gguf", ".onnx", ".safetensors", ".bin",
                 ".exe", ".msi", ".zip", ".7z", ".enc", ".log", ".jsonl"}
TEXT_SUFFIXES = {".py", ".ts", ".js", ".mjs", ".cjs", ".html", ".css", ".svg", ".md",
                 ".json", ".toml", ".txt", ".sql", ".sh", ".ps1", ".spec", ".yml", ".yaml"}
PATTERNS = {
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential_shape": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,})\b"),
    "jwt_shape": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{16,}\b"),
    "personal_windows_path": re.compile(r"(?i)\b[A-Z]:[\\/]Users[\\/][A-Za-z0-9_.가-힣-]+[\\/]"),
    "personal_unix_path": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.가-힣-]+/"),
    "operational_record_id": re.compile(r"\b20[0-9]{6}-[0-9a-f]{6}\b"),
    "person_name_example_candidate": re.compile(r"예[:：]\s*[\"']?[가-힣]{2,4}\s+(?:위원|의원|장관|처장)"),
    "deployment_url_candidate": re.compile(r"https://[^\s<>\"']+\.(?:vercel\.app|neon\.tech)(?:[/\s\"']|$)"),
    "embedded_credential_assignment": re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passcode|secret)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_./+=-]{12,}[\"']"),
}


def classify(name: str) -> tuple[str, str]:
    """내용을 읽기 전에 운영 자료와 기본 비공개 범위를 경로로 제외한다."""
    path = PurePosixPath(name)
    parts = path.parts
    if path.is_absolute() or not parts or ".." in parts or "\\" in name:
        return "excluded", "unsafe_path"
    if parts[0] in PRIVATE_TOP or any(value in {"node_modules", "__pycache__"} for value in parts):
        return "excluded", "runtime_or_private_directory"
    if any(value.lower().startswith(".env") for value in parts):
        return "excluded", "environment_file_not_read"
    if path.suffix.lower() in DATA_SUFFIXES:
        return "excluded", "data_model_recording_or_build_artifact"
    if parts[0] in {"AGENTS.md", "HANDOFF.md"}:
        return "excluded", "internal_handoff_history"
    if parts[0] == "tests" or name.startswith("desktop/tests/"):
        return "excluded", "separate_synthetic_test_pack_review"
    if parts[0] == ".github":
        return "excluded", "workflow_requires_public_test_pack_review"
    if name == "THIRD_PARTY_NOTICES.md":
        return "review_only", "replace_with_public_notice_and_recheck_links"
    if parts[0] == "docs" and not (name.startswith("docs/public-release/") or name == "docs/cloud/schema.sql"):
        return "excluded", "internal_document_or_evidence"
    if parts[0] == "desktop" and not (
        name.startswith(("desktop/src/", "desktop/scripts/", "desktop/LICENSES/", "desktop/assets/brand/"))
        or name in {"desktop/NOTICE.md", "desktop/package.json", "desktop/package-lock.json",
                    "desktop/tsconfig.json", "desktop/relay-cli/package.json", "desktop/relay-cli/package-lock.json"}
    ):
        return "excluded", "desktop_path_not_selected"
    root_files = {"README.md", "LICENSE", ".gitattributes", ".gitignore", "gukgam-engine.spec", "pyproject.toml"}
    allowed = (parts[0] in {"gukgam", "desktop", "cloud", "scripts"}
               or name.startswith("docs/public-release/") or name == "docs/cloud/schema.sql"
               or name in root_files or len(parts) == 1 and name.startswith("requirements") and name.endswith(".txt"))
    return ("candidate", "source_or_public_document") if allowed else ("excluded", "not_in_candidate_scope")


def safe_source_path(name: str) -> Path:
    """링크·재분석 경로를 통해 검사 범위 밖의 자료를 읽지 않는다."""
    path = ROOT.joinpath(*PurePosixPath(name).parts)
    for item in (path, *path.parents):
        if item == ROOT.parent:
            break
        if item.is_symlink() or hasattr(item, "is_junction") and item.is_junction():
            raise ValueError("linked_source_path")
    if not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("source_outside_repository")
    return path


def audit() -> dict:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True)
    names = {name for name in result.stdout.decode("utf-8").split("\0") if name}
    tracked_count = len(names)
    names.update(name for name in NEW_PUBLIC_FILES if ROOT.joinpath(*PurePosixPath(name).parts).is_file())
    entries, findings = [], []
    for name in sorted(names):
        category, reason = classify(name)
        entry = {"path": name, "category": category, "reason": reason}
        entries.append(entry)
        if category != "candidate":
            continue
        try:
            path = safe_source_path(name)
            raw = path.read_bytes()
        except (OSError, ValueError):
            findings.append({"path": name, "line": None, "kind": "candidate_unreadable_or_linked"})
            continue
        entry.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        textual = path.suffix.lower() in TEXT_SUFFIXES or path.name in {"LICENSE", ".gitignore", ".gitattributes", ".python-version", ".vercelignore"}
        if not textual:
            findings.append({"path": name, "line": None, "kind": "binary_or_unknown_asset_needs_visual_review"})
            continue
        try:
            body = raw.decode("utf-8-sig")
        except UnicodeError:
            findings.append({"path": name, "line": None, "kind": "non_utf8_source_needs_review"})
            continue
        for line_number, line in enumerate(body.splitlines(), 1):
            for kind, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append({"path": name, "line": line_number, "kind": kind})
    return {
        "schema_version": 1,
        "purpose": "public_snapshot_candidate_audit_only",
        "publication_ready": False,
        "tracked_file_count": tracked_count,
        "explicit_new_public_files_included": sorted(names.intersection(NEW_PUBLIC_FILES)),
        "counts": dict(Counter(entry["category"] for entry in entries)),
        "finding_counts": dict(Counter(finding["kind"] for finding in findings)),
        "findings": findings,
        "files": entries,
        "excluded_file_bodies_read": False,
        "copied_or_exported_files": 0,
        "network_or_model_calls": 0,
        "limitations": [
            "자동 패턴 검사는 모든 비밀값·실제 발언·실명을 판별하지 못합니다.",
            "후보 목록은 공개 허용 목록이 아닙니다. 코드·이미지·라이선스의 별도 검토가 필요합니다.",
            "Git 이력의 내용은 검사하지 않았으며 새 공개 저장소로 이력을 복사하면 안 됩니다.",
            "제외한 실제 fixture 때문에 기존 전체 시험·CI의 공개 스냅샷 재현을 보장하지 않습니다.",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="새 outputs/*.json 내부 검토 보고서. 기존 파일은 덮어쓰지 않음")
    args = parser.parse_args(argv)
    report_path = None
    if args.report is not None:
        if (args.report.is_absolute() or not args.report.parts or ".." in args.report.parts
                or args.report.parts[0] != "outputs" or args.report.suffix.lower() != ".json"):
            parser.error("보고서는 저장소 안의 새 outputs/*.json 경로만 지정하세요")
        try:
            report_path = safe_source_path(args.report.as_posix())
        except (OSError, ValueError):
            parser.error("보고서 경로를 확인하세요")
        if report_path.exists():
            parser.error("기존 보고서는 덮어쓰지 않습니다")
    report = audit()
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    print(json.dumps({"status": "review_required", "publication_ready": False, "counts": report["counts"],
                      "finding_counts": report["finding_counts"], "report_written": report_path is not None}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
