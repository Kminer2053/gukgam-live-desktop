"""운영 파일 없이 비공개 스트림의 짧은 쓰기 경계를 확인한다."""
import pytest

from gukgam.secure_files import create_private_file, write_all


def test_secure_stream_finishes_short_writes_for_standard_consumers(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.bin"
    raw = "합성 문서 전체".encode("utf-8")
    with create_private_file(path) as stream:
        original = stream._write_once
        calls = []
        def short_write(value):
            calls.append(len(value))
            return original(value[:3])
        monkeypatch.setattr(stream, "_write_once", short_write)
        assert stream.write(raw) == len(raw)
    assert path.read_bytes() == raw
    assert len(calls) > 1


@pytest.mark.parametrize("count", [None, 0, -1, True, 1000])
def test_secure_stream_rejects_invalid_progress(tmp_path, monkeypatch, count):
    path = tmp_path / "synthetic.bin"
    with create_private_file(path) as stream:
        monkeypatch.setattr(stream, "_write_once", lambda raw: count)
        with pytest.raises(OSError):
            stream.write(b"synthetic")
    assert path.read_bytes() == b""


def test_write_all_accepts_empty_input_and_preserves_closed_stream_error(tmp_path):
    with create_private_file(tmp_path / "synthetic.bin") as stream:
        assert write_all(stream, b"") is None
    with pytest.raises(ValueError):
        write_all(stream, b"")
