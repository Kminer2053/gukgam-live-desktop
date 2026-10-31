"""국회 HTTP 조회의 검증된 CA 선택과 경로를 노출하지 않는 초기화 오류."""
from __future__ import annotations

import os
from pathlib import Path
import ssl
import sys

import certifi
import httpx


class AssemblyTLSConfigurationError(RuntimeError):
    """명시한 CA를 무시하거나 TLS 검증을 끄지 않고 안전하게 실패한다."""

    def __init__(self, code: str, *, step: str | None = None, os_error: int | None = None):
        self.code = code
        self.step = step if step in {"path_check", "context_init", "ca_read", "ca_load"} else None
        self.os_error = os_error if isinstance(os_error, int) and 0 <= os_error <= 65535 else None
        if code.startswith("assembly_tls_environment_"):
            message = "국회 연결에 지정된 인증서 경로와 파일 형식을 확인하세요"
        else:
            message = "국회 연결용 동봉 인증서를 읽지 못했습니다. 앱 설치 파일을 확인하세요"
        detail = code
        if self.step:
            detail += f";step={self.step}"
        if self.os_error is not None:
            detail += f";os={self.os_error}"
        super().__init__(f"{message}({detail})")


def _tls_context() -> ssl.SSLContext:
    """사용자 CA가 우선이며, 설치판의 기본 CA는 작업 폴더와 무관하다."""
    cafile = os.environ.get("SSL_CERT_FILE") or None
    capath = (os.environ.get("SSL_CERT_DIR") or None) if not cafile else None
    source = "environment" if cafile or capath else "bundle"
    step = "path_check"
    try:
        if cafile:
            path = Path(cafile)
            if not path.is_file():
                raise AssemblyTLSConfigurationError("assembly_tls_environment_file_missing")
        elif capath:
            path = Path(capath)
            if not path.is_dir():
                raise AssemblyTLSConfigurationError("assembly_tls_environment_directory_missing")
        else:
            if getattr(sys, "frozen", False):
                root = Path(getattr(sys, "_MEIPASS", ""))
                if not root.is_absolute():
                    raise AssemblyTLSConfigurationError("assembly_tls_bundle_missing")
                path = root / "certifi" / "cacert.pem"
            else:
                path = Path(certifi.where())
            if not path.is_file():
                raise AssemblyTLSConfigurationError("assembly_tls_bundle_missing")
            cafile = str(path)
        # 동봉 Python 3.12의 create_default_context(SERVER_AUTH)와 같은 TLS 기본값이다.
        # 선택적 SSLKEYLOGFILE 기록 단계는 실행하지 않는다. 전역 환경도 변경하지 않는다.
        # httpx에는 완성된 context를 전달하고 프록시는 기존 trust_env 계약을 유지한다.
        step = "context_init"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        if source == "bundle":
            # Python이 읽은 같은 PEM을 전달해 OpenSSL의 별도 파일 경로 해석을 피한다.
            # 사용자 지정 CA/디렉터리는 기존 경로와 검증 실패 정책을 그대로 따른다.
            step = "ca_read"
            certificates = path.read_text(encoding="ascii")
            step = "ca_load"
            context.load_verify_locations(cadata=certificates)
        else:
            step = "ca_load"
            context.load_verify_locations(cafile=cafile, capath=capath)
        return context
    except AssemblyTLSConfigurationError:
        raise
    except (OSError, ValueError) as error:
        raise AssemblyTLSConfigurationError(f"assembly_tls_{source}_invalid", step=step,
                                             os_error=getattr(error, "errno", None)) from None


def create_http_client(*, timeout: float | httpx.Timeout = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, verify=_tls_context())
