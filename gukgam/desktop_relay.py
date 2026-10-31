"""설치 앱이 만든 새 릴레이 DB의 준비. 서버·기록·계정 파일을 열지 않는다."""
from __future__ import annotations

import json
from typing import TextIO
from urllib.parse import urlsplit

from .cloud.store import PostgresStore


def initialize_database(stream: TextIO) -> None:
    """연결 정보는 익명 파이프에서 한 번만 읽고 오류에도 값을 포함하지 않는다."""
    store = None
    try:
        line = stream.readline(16_385)
        if len(line) > 16_384:
            raise ValueError
        data = json.loads(line)
        if not isinstance(data, dict) or set(data) != {"databaseUrl"}:
            raise ValueError
        dsn = data["databaseUrl"]
        if not isinstance(dsn, str) or any(ord(char) < 32 for char in dsn):
            raise ValueError
        parsed = urlsplit(dsn)
        if (parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname
                or not parsed.username or not parsed.password or parsed.path in {"", "/"}):
            raise ValueError
        store = PostgresStore(dsn)
        store.init_schema()
    except Exception:
        raise ValueError("릴레이 DB 준비에 실패했습니다. 브라우저의 DB 연결 상태와 권한을 확인하세요.") from None
    finally:
        if store:
            store.close()
