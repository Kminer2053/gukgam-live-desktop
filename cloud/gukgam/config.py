"""런타임 설정.

환경변수(GUKGAM_*)는 기동 시 고정값이고, 기관명·감시 키워드·AI 엔진 같은 운영 설정은
DB settings 테이블에 저장되어 관리자 화면에서 바뀐다(gukgam.settings 참고).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    host: str = field(default_factory=lambda: os.environ.get("GUKGAM_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("GUKGAM_PORT", "8765")))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("GUKGAM_DATA_DIR", ROOT / "data")))
    # 인증: 둘 다 비어 있으면 로컬 개발 모드(인증 없음, 화면에 경고 배너)
    operator_passcode: str = field(default_factory=lambda: os.environ.get("GUKGAM_OPERATOR_PASSCODE", ""))
    viewer_passcode: str = field(default_factory=lambda: os.environ.get("GUKGAM_VIEWER_PASSCODE", ""))
    secret_key: str = field(default_factory=lambda: os.environ.get("GUKGAM_SECRET_KEY", ""))
    # 외부망 호출(국회 의사중계 일정/자막) 허용 여부 — 폐쇄망 배치 시 끈다
    allow_assembly_network: bool = field(default_factory=lambda: _env_bool("GUKGAM_ALLOW_ASSEMBLY_NETWORK", True))
    # 테스트에서 MLX 등 무거운 엔진 로딩을 막는다
    disable_heavy_engines: bool = field(default_factory=lambda: _env_bool("GUKGAM_DISABLE_HEAVY", False))
    # 클라우드 동기화는 두 값이 모두 있을 때만 켜진다(.env는 진입점 전용).
    cloud_url: str = field(default_factory=lambda: os.environ.get("GUKGAM_CLOUD_URL", ""))
    agent_token: str = field(default_factory=lambda: os.environ.get("GUKGAM_AGENT_TOKEN", ""))
    cloud_mirror_sensitive: bool = field(default_factory=lambda: _env_bool("GUKGAM_CLOUD_MIRROR_SENSITIVE", False))
    cloud_read_mode: str = field(default_factory=lambda: os.environ.get("GUKGAM_CLOUD_READ_MODE", "mirror").strip().lower())
    # 설치형 앱의 전용 연결. OAuth 자격증명은 Electron에만 두며 이 객체로 전달하지 않는다.
    desktop_bridge: Any = field(default=None, repr=False, compare=False)
    desktop_organization: str = ""
    desktop_model: str = ""

    def __post_init__(self) -> None:
        if self.cloud_read_mode not in {"mirror", "source"}:
            raise ValueError("클라우드 열람 방식은 mirror 또는 source여야 합니다")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gukgam.sqlite3"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.operator_passcode or self.viewer_passcode)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.audio_dir, self.upload_dir):
            d.mkdir(parents=True, exist_ok=True)
