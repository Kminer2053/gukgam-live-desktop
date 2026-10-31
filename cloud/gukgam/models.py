"""도메인 상수와 모듈 간 계약(dataclass)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SessionStatus = Literal["idle", "live", "paused", "ended"]
SourceType = Literal["assembly_caption", "assembly_hls", "browser", "file", "replay", "manual"]
SpeakerRole = Literal["chair", "member", "answer", "other", "unknown", "system"]
BlockKind = Literal["question", "chair", "other"]
BlockStatus = Literal["ai_inferred", "needs_name", "confirmed"]
ItemKind = Literal["data_request", "commitment", "issue", "witness_request", "written_question"]
ItemStatus = Literal["open", "in_progress", "done", "dismissed"]

SOURCE_LABELS: dict[str, str] = {
    "assembly_caption": "국회 AI자막 수신",
    "assembly_hls": "국회 중계영상 음성인식",
    "browser": "브라우저 마이크·탭 음성",
    "file": "녹음 파일",
    "replay": "리플레이(시연용)",
    "manual": "수기 입력",
}
STATUS_LABELS: dict[str, str] = {"idle": "대기", "live": "수집 중", "paused": "일시정지", "ended": "종료"}
ROLE_LABELS: dict[str, str] = {
    "chair": "위원장", "member": "위원", "answer": "답변", "other": "기타", "unknown": "미상", "system": "기록",
}
BLOCK_STATUS_LABELS: dict[str, str] = {
    "ai_inferred": "AI 추정", "needs_name": "성명 확인 필요", "confirmed": "확인됨",
}
ITEM_KIND_LABELS: dict[str, str] = {
    "data_request": "요구자료",
    "commitment": "약속·검토사항",
    "issue": "지적사항",
    "witness_request": "증인·참고인 요구",
    "written_question": "서면질의",
}
ITEM_STATUS_LABELS: dict[str, str] = {"open": "미처리", "in_progress": "처리중", "done": "완료", "dismissed": "제외"}


@dataclass
class Turn:
    """한 조각 안의 화자 단위 텍스트.

    speaker_hint: 수집원이 준 화자 정보(예: '가상인물 위원', 'hwa:0'). 없으면 None.
    new_speaker: 수집원이 이 지점에서 화자가 바뀌었다고 표시했는지(국회 자막의 '-' 접두 등).
    """

    text: str
    speaker_hint: str | None = None
    new_speaker: bool = False
    source_speaker_hint: str | None = None  # 호명 분리 전 수집원의 임시 라벨


@dataclass
class SegmentEvent:
    """수집원 → 파이프라인 계약. 같은 key 로 여러 번 오면 갱신(부분→확정)이다."""

    key: str
    ts_start: int                     # epoch ms
    turns: list[Turn]
    is_final: bool
    ts_end: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(t.text for t in self.turns if t.text).strip()


@dataclass
class ExtractedItem:
    kind: str
    text: str
    strength: str = ""
    due_date: str = ""
    quote: str = ""
    speaker: str = ""
    requester: str = ""
    confidence: float = 0.6
    origin: str = "rule"


@dataclass
class Mention:
    keyword: str
    category: str      # agency|person|project|custom
    start: int
    end: int
