"""통합 정리의 제공자·기관·검토 설정 변경을 값 공개 없이 식별한다."""
from __future__ import annotations

import hashlib
import json

from . import settings


def processing_signature(repo) -> str:
    """설정 원문은 저장하지 않고 처리 중 변경 여부만 대조한다."""
    values = {name: settings.get(repo, name)
              for name in ("llm", "org", "watch_keywords", "transcript_review")}
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
