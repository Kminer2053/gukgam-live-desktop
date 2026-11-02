"""잠금 목록이 플랫폼 기능을 유지하고 비고정 전이 버전을 허용하지 않는지 검사한다."""
from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
FILES = ("requirements.txt", "requirements-julia-cpu.txt", "requirements-desktop.txt", "requirements-ai.txt")


def _rows(name):
    result = []
    for raw in (ROOT / name).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            included = line[3:].strip()
            assert included in FILES
            result.extend(_rows(included))
        else:
            result.append(Requirement(line))
    return result


def _active(name, platform, machine):
    env = {**default_environment(), "sys_platform": platform, "platform_machine": machine, "python_version": "3.12", "python_full_version": "3.12.14", "extra": ""}
    return {canonicalize_name(row.name): row for row in _rows(name) if row.marker is None or row.marker.evaluate(env)}


@pytest.mark.parametrize("name", FILES)
def test_every_dependency_is_an_exact_public_package_pin(name):
    for row in _rows(name):
        assert row.url is None
        specifiers = list(row.specifier)
        assert len(specifiers) == 1
        assert specifiers[0].operator == "=="
        assert "*" not in specifiers[0].version


def test_desktop_preserves_includes_and_original_optional_features():
    includes = [line.strip() for line in (ROOT / "requirements-desktop.txt").read_text(encoding="utf-8").splitlines() if line.startswith("-r ")]
    assert includes == ["-r requirements.txt", "-r requirements-julia-cpu.txt"]
    rows = _rows("requirements-desktop.txt")
    versions = {}
    for row in rows:
        key = canonicalize_name(row.name)
        version = next(iter(row.specifier)).version
        assert versions.setdefault(key, version) == version
    assert {"pyinstaller", "tzdata", "onnxruntime", "tokenizers"} <= set(versions)
    assert any(row.name == "uvicorn" and row.extras == {"standard"} for row in rows)
    assert any(row.name == "psycopg" and row.extras == {"binary"} for row in rows)


def test_windows_and_linux_dependencies_keep_their_platform_boundaries():
    windows = _active("requirements-desktop.txt", "win32", "AMD64")
    linux = _active("requirements-desktop.txt", "linux", "x86_64")
    assert {"colorama", "pefile", "pywin32-ctypes"} <= set(windows)
    assert "uvloop" not in windows
    assert "uvloop" in linux
    assert not ({"colorama", "pefile", "pywin32-ctypes"} & set(linux))


@pytest.mark.parametrize("platform,machine", [("win32", "AMD64"), ("linux", "x86_64"), ("darwin", "x86_64")])
def test_apple_ai_lock_does_not_install_on_unsupported_platforms(platform, machine):
    assert not _active("requirements-ai.txt", platform, machine)


def test_apple_ai_preserves_both_features_and_known_security_floors():
    apple = _active("requirements-ai.txt", "darwin", "arm64")
    assert {"mlx-whisper", "mlx-lm", "mlx", "mlx-metal", "torch"} <= set(apple)
    assert Version(next(iter(apple["mlx"].specifier)).version) >= Version("0.29.4")
    assert Version(next(iter(apple["torch"].specifier)).version) >= Version("2.13.0")
    assert "macOS 14" in (ROOT / "requirements-ai.txt").read_text(encoding="utf-8")


def test_generated_files_do_not_publish_workstation_paths_or_resolution_flags():
    for name in FILES:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert ":\\" not in text
        assert ":/Users/" not in text
        assert "outputs/" not in text
        assert "--index" not in text
        assert "--trusted-host" not in text
