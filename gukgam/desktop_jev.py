"""설치 앱 부모 파이프에서 받은 Jev 키를 프로세스 메모리에만 보관한다."""
from __future__ import annotations

from typing import Any
import re

from .llm.decision import JevDecisionProvider


def _key(bridge: Any) -> str:
    value = getattr(bridge, "_desktop_jev_key", "")
    return value if isinstance(value, str) else ""


def configure_from_parent(bridge: Any, message: Any) -> bool:
    """HTTP·SQLite 설정 경로가 아닌 소유 부모의 익명 stdin에서만 호출한다."""
    if not isinstance(message, dict) or message.get("type") != "jev.configure":
        return False
    value = message.get("key")
    revision = message.get("revision")
    if (set(message) != {"type", "key", "revision"} or not isinstance(value, str) or len(value) > 4096
            or not isinstance(revision, str) or not (revision == "unconfigured" or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", revision))
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        return True
    bridge._desktop_jev_key = value
    bridge._desktop_jev_revision = revision
    return True


def clear(bridge: Any) -> None:
    bridge._desktop_jev_key = ""
    bridge._desktop_jev_revision = "unconfigured"


def credential_revision(bridge: Any) -> str:
    """키·키 해시와 무관하게 main이 생성해 암호화 보관한 변경 세대다."""
    return getattr(bridge, "_desktop_jev_revision", "unconfigured")


class DesktopJevDecisionProvider(JevDecisionProvider):
    """기존 호출·검증·시간 제한을 유지하며 전역 환경의 키를 읽지 않는다."""

    def __init__(self, bridge: Any, **kwargs):
        super().__init__(api_key="", **kwargs)
        self._desktop_bridge = bridge

    @property
    def available(self) -> bool:
        return bool(_key(self._desktop_bridge))

    @property
    def provenance(self) -> dict[str, Any]:
        return {**super().provenance, "credential_revision": credential_revision(self._desktop_bridge)}

    async def _request(self, state: Any, questions: dict[str, Any]) -> Any:
        self._key = _key(self._desktop_bridge)
        try:
            return await super()._request(state, questions)
        finally:
            self._key = ""

    async def close(self) -> None:
        self._key = ""
        await super().close()
