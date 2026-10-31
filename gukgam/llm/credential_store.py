"""Windows 사용자 DPAPI로 API 키를 보관한다. 구형 평문 읽기는 이행용으로 유지한다."""
from __future__ import annotations

import base64
import ctypes
import sys

PREFIX = "dpapi:v1:"
NOTICE = "저장된 API 키를 사용할 수 없습니다. 같은 Windows 계정에서 키를 다시 입력하세요."


class CredentialError(ValueError):
    """키나 원래 OS 오류를 포함하지 않는 보관 오류."""


def _windows() -> bool:
    return sys.platform == "win32"


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    source_buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    if decrypt:
        call = crypt.CryptUnprotectData
        call.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    else:
        call = crypt.CryptProtectData
        call.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), "Gukgam API credential", None, None, None, 1, ctypes.byref(result))
    call.restype = wintypes.BOOL
    try:
        if not call(*args) or not result.data or not result.size:
            raise CredentialError(NOTICE)
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(source_buffer, 0, ctypes.sizeof(source_buffer))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def _text(value: str) -> str:
    limit = 65536 if isinstance(value, str) and value.startswith("dpapi:") else 8192
    if not isinstance(value, str) or len(value) > limit or any(c in value for c in "\r\n\x00"):
        raise CredentialError(NOTICE)
    return value


def is_protected(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def unprotect_secret(value: str) -> str:
    value = _text(value)
    if not value.startswith("dpapi:"):
        return value
    if not is_protected(value) or not _windows():
        raise CredentialError(NOTICE)
    try:
        data = base64.b64decode(value[len(PREFIX):], validate=True)
        if not data:
            raise CredentialError(NOTICE)
        return _text(_crypt(data, decrypt=True).decode("utf-8"))
    except Exception:
        raise CredentialError(NOTICE) from None


def protect_secret(value: str) -> str:
    value = _text(value)
    if value.startswith("dpapi:"):
        unprotect_secret(value)
        return value
    if not value or not _windows():
        return value  # 기존 비Windows 배포는 평문 계약을 유지한다. DPAPI라고 표시하지 않는다.
    try:
        return PREFIX + base64.b64encode(_crypt(value.encode("utf-8"), decrypt=False)).decode("ascii")
    except Exception:
        raise CredentialError(NOTICE) from None
