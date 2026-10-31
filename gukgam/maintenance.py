"""운영 유지보수: 세션 파일 정리, 보존 기간 집행(1시간마다), 백업·복구, 디스크 상태.

- 세션 파일: data/uploads/{sid}/(녹음 파일), data/audio/{sid}/(원음 WAV), data/raw/{sid}.jsonl(국회 자막 원본).
- 보존(settings.retention): audio_days(원음 WAV, 파일 수정 시각 기준), raw_days(자막 원본, 마지막 기록 시각 기준),
  transcript_days(종료된 세션 기록 전체, 종료 시각 기준). 0 이면 그 항목은 기한 없이 둔다.
  수집 중(live)이거나 매니저가 돌리고 있는 세션은 건드리지 않는다. DB 에 없는 세션의 파일(삭제 잔여물)은 지운다.
- 백업: SQLite 온라인 백업 API 로 WAL 까지 반영된 일관된 사본을 만들고 data/raw 등을 함께 tar.gz 로 묶는다.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import KST, Config
from .db import now_ms

log = logging.getLogger(__name__)

SID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
DAY_MS = 86_400_000
ORPHAN_MIN_AGE_SEC = 3600          # DB 에 없는 세션 파일은 이만큼 지난 뒤에 지운다(생성 직후 경합 방지)
TMP_MAX_AGE_SEC = 86_400            # STT 임시파일은 하루가 지나면(또는 다른 프로세스 것이면) 지운다
RETENTION_INTERVAL_SEC = 3600.0
RETENTION_FIRST_DELAY_SEC = 60.0


# ── 경로 ─────────────────────────────────────────────────────────
def raw_dir(config: Config) -> Path:
    """국회 자막 원본(jsonl) 폴더."""
    return Path(config.data_dir) / "raw"


def raw_path(config: Config, sid: str) -> Path:
    return raw_dir(config) / f"{sid}.jsonl"


def tmp_dir(config: Config) -> Path:
    return Path(config.data_dir) / "tmp"


def _inside(base: Path, p: Path) -> bool:
    try:
        return p.resolve().is_relative_to(base.resolve()) and p.resolve() != base.resolve()
    except OSError:
        return False


def _size(p: Path) -> int:
    try:
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return p.stat().st_size
    except OSError:
        return 0


# ── 세션 파일 삭제 ─────────────────────────────────────────────────
def remove_session_files(config: Config, sid: str) -> dict[str, int]:
    """세션 하나의 업로드·원음·자막 원본 파일을 지운다. {"files": n, "bytes": n}."""
    out = {"files": 0, "bytes": 0}
    if not SID_RE.match(sid or ""):
        return out
    for base in (config.upload_dir, config.audio_dir):
        d = Path(base) / sid
        if d.is_dir() and _inside(Path(base), d):
            out["bytes"] += _size(d)
            out["files"] += sum(1 for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d, ignore_errors=True)
    rd = raw_dir(config)
    for name in (f"{sid}.jsonl", f"{sid}.jsonl.gz"):
        f = rd / name
        if f.is_file() and _inside(rd, f):
            out["bytes"] += _size(f)
            out["files"] += 1
            f.unlink(missing_ok=True)
    return out


def forget_session_state(repo: Any, sid: str) -> None:
    """세션에 딸린 settings 행(화자·블록 추적 상태 tracker:{sid})을 지운다."""
    try:
        repo.db.execute("DELETE FROM settings WHERE key=?", (f"tracker:{sid}",))
    except Exception:  # pragma: no cover - 방어
        log.exception("세션 추적 상태 삭제 실패: %s", sid)


# ── 보존 기간 집행 ──────────────────────────────────────────────────
def _active_sids(rt: Any) -> set[str]:
    out = {s["id"] for s in rt.repo.list_sessions(limit=100_000, status="live")}
    ingest = getattr(rt, "ingest", None)
    runs = getattr(ingest, "_runs", None)
    if isinstance(runs, dict):
        out |= set(runs)
    return out


def _int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def purge_stt_tmp(config: Config, now: float | None = None) -> int:
    """비정상 종료로 남은 STT 임시파일(data/tmp/stt-<pid>-<n>.pcm|wav) 정리. 지운 개수."""
    d = tmp_dir(config)
    if not d.is_dir():
        return 0
    now = now if now is not None else time.time()
    me = os.getpid()
    n = 0
    for f in d.glob("stt-*"):
        m = re.match(r"^stt-(\d+)-\d+\.(?:pcm|wav)$", f.name)
        if not m or not f.is_file():
            continue
        try:
            old = now - f.stat().st_mtime > TMP_MAX_AGE_SEC
        except OSError:
            continue
        if int(m.group(1)) != me or old:
            f.unlink(missing_ok=True)
            n += 1
    return n


def purge_expired(rt: Any, *, now: int | None = None,
                  on_session_deleted: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """보존 기간이 지난 원음·자막 원본·종료 세션 기록을 지운다(동기 — asyncio.to_thread 로 부른다).

    반환: {"audio_files","raw_files","sessions","orphans","tmp_files","bytes"}.
    작업 스레드에서는 삭제 알림 콜백으로 앱 루프에 발행을 예약한다.
    """
    from . import settings

    now = now if now is not None else now_ms()
    cfg: Config = rt.config
    repo = rt.repo
    ret = settings.get(repo, "retention") or {}
    audio_days, raw_days, transcript_days = (_int(ret.get("audio_days")), _int(ret.get("raw_days")),
                                             _int(ret.get("transcript_days")))
    active = _active_sids(rt)
    known = {s["id"]: s for s in repo.list_sessions(limit=1_000_000)}
    out: dict[str, Any] = {"audio_files": 0, "raw_files": 0, "sessions": 0, "orphans": 0, "tmp_files": 0, "bytes": 0}

    # 1) 종료된 세션 기록(transcript_days)
    if transcript_days > 0:
        cutoff = now - transcript_days * DAY_MS
        for sid, s in list(known.items()):
            if s.get("status") != "ended" or sid in active:
                continue
            ended = int(s.get("ended_at") or s.get("updated_at") or 0)
            if not ended or ended >= cutoff:
                continue
            fresh = repo.get_session(sid)          # 그사이 다시 시작했으면 건너뛴다
            if fresh is None or fresh.get("status") != "ended":
                continue
            repo.delete_session(sid)
            deleted = {"sid": sid, "source_type": s["source_type"]}
            if on_session_deleted is None:
                rt.bus.publish(sid, "session.deleted", deleted)
            else:
                on_session_deleted(sid, deleted)
            forget_session_state(repo, sid)
            try:
                rt.drop_pipeline(sid)
            except Exception:  # pragma: no cover - 방어
                pass
            r = remove_session_files(cfg, sid)
            out["bytes"] += r["bytes"]
            out["sessions"] += 1
            known.pop(sid, None)
            repo.audit("system", "retention.purge", sid,
                       {"title": s.get("title", ""), "ended_at": ended, "days": transcript_days, **r})

    now_s = now / 1000

    def old_enough(p: Path, days: int) -> bool:
        try:
            return now_s - p.stat().st_mtime > days * 86_400
        except OSError:
            return False

    def orphan(p: Path, sid: str) -> bool:
        if sid in known or sid in active or not SID_RE.match(sid):
            return False
        try:
            return now_s - p.stat().st_mtime > ORPHAN_MIN_AGE_SEC
        except OSError:
            return False

    # 2) DB 에 없는 세션의 남은 파일(삭제 잔여물)
    for base in (cfg.upload_dir, cfg.audio_dir):
        base = Path(base)
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if d.is_dir() and orphan(d, d.name) and _inside(base, d):
                out["bytes"] += _size(d)
                shutil.rmtree(d, ignore_errors=True)
                out["orphans"] += 1
    rd = raw_dir(cfg)
    if rd.is_dir():
        for f in rd.iterdir():
            m = re.match(r"^(.+)\.jsonl(?:\.gz)?$", f.name)
            if m and f.is_file() and orphan(f, m.group(1)):
                out["bytes"] += _size(f)
                f.unlink(missing_ok=True)
                out["orphans"] += 1

    # 3) 원음 WAV(audio_days) — 파일마다 수정 시각 기준
    if audio_days > 0 and Path(cfg.audio_dir).is_dir():
        for d in Path(cfg.audio_dir).iterdir():
            if d.is_symlink() or not d.is_dir() or d.name in active:
                continue
            files, size = 0, 0
            candidates = list(d.glob("*.wav"))
            review = d / "review"
            if review.is_dir() and not review.is_symlink():
                candidates.extend(review.glob("*.wav"))
            for w in candidates:
                if w.is_symlink() or not w.is_file():
                    continue
                if old_enough(w, audio_days):
                    size += _size(w)
                    w.unlink(missing_ok=True)
                    files += 1
            if files:
                out["audio_files"] += files
                out["bytes"] += size
                repo.audit("system", "retention.audio", d.name, {"files": files, "bytes": size, "days": audio_days})
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

    # 4) 국회 자막 원본(raw_days) — 마지막 기록 시각 기준
    if raw_days > 0 and rd.is_dir():
        for f in rd.iterdir():
            m = re.match(r"^(.+)\.jsonl(?:\.gz)?$", f.name)
            if not m or not f.is_file() or m.group(1) in active:
                continue
            if old_enough(f, raw_days):
                size = _size(f)
                f.unlink(missing_ok=True)
                out["raw_files"] += 1
                out["bytes"] += size
                repo.audit("system", "retention.raw", m.group(1), {"bytes": size, "days": raw_days})

    # 5) STT 임시파일
    out["tmp_files"] = purge_stt_tmp(cfg, now_s)
    # 6) WAL 파일이 계속 커지지 않게 주기적으로 본 DB 에 반영하고 비운다(바쁘면 다음 회차에)
    try:
        repo.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:  # pragma: no cover - 방어
        log.debug("WAL 체크포인트 생략", exc_info=True)
    if any(out[k] for k in ("audio_files", "raw_files", "sessions", "orphans", "tmp_files")):
        log.info("보존 기간 정리: %s", out)
    return out


class RetentionWorker:
    """보존 기간 정리를 기동 직후(first_delay 뒤) 한 번, 그 뒤 interval 마다 돌린다."""

    def __init__(self, rt: Any, interval: float = RETENTION_INTERVAL_SEC,
                 first_delay: float = RETENTION_FIRST_DELAY_SEC):
        self.rt = rt
        self.interval = interval
        self.first_delay = first_delay
        self.task: asyncio.Task | None = None
        self.last: dict[str, Any] | None = None     # {"at", "result"|"error"}

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop(), name="retention")

    async def stop(self) -> None:
        t, self.task = self.task, None
        if t is not None and not t.done():
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)

    async def run_once(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()

        def publish_deleted(sid: str, data: dict[str, Any]) -> None:
            # asyncio 구독자의 대기 Future는 작업 스레드에서 직접 깨우지 않는다.
            loop.call_soon_threadsafe(self.rt.bus.publish, sid, "session.deleted", data)

        try:
            res = await asyncio.to_thread(purge_expired, self.rt, on_session_deleted=publish_deleted)
            self.last = {"at": now_ms(), "result": res}
            return res
        except Exception as e:
            log.exception("보존 기간 정리 실패")
            self.last = {"at": now_ms(), "error": f"{type(e).__name__}: {e}"}
            return {}

    async def _loop(self) -> None:
        await asyncio.sleep(self.first_delay)
        while True:
            await self.run_once()
            await asyncio.sleep(self.interval)


# ── 디스크 ───────────────────────────────────────────────────────
def disk_status(path: Path) -> dict[str, int]:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    u = shutil.disk_usage(p)
    return {"free": u.free, "total": u.total, "used": u.used}


def data_usage(config: Config) -> dict[str, int]:
    """data 폴더 주요 하위 폴더 사용량(바이트)."""
    return {"audio": _size(Path(config.audio_dir)), "raw": _size(raw_dir(config)),
            "uploads": _size(Path(config.upload_dir)), "db": _size(Path(config.db_path))}


# ── 백업·복구 ─────────────────────────────────────────────────────
BACKUP_MEMBERS = ("gukgam.sqlite3", "manifest.json", "raw", "uploads", "audio")


def _stamp() -> str:
    return datetime.now(KST).strftime("%Y%m%d-%H%M%S")


def _check_db(path: Path) -> int:
    """백업 DB 검사: 무결성·세션 테이블. 세션 수를 돌려준다. 문제면 ValueError."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise ValueError(f"DB 무결성 검사 실패: {ok}")
        try:
            return int(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        except sqlite3.DatabaseError as e:
            raise ValueError("국감 라이브 DB 가 아닙니다(sessions 테이블 없음)") from e
    finally:
        con.close()


def snapshot_db(db_path: Path, dest: Path) -> None:
    """SQLite 온라인 백업 API 로 일관된 사본을 만든다(서버가 실행 중이어도 WAL 내용까지 반영)."""
    if not Path(db_path).is_file():
        raise FileNotFoundError(f"DB 파일이 없습니다: {db_path}")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")     # 사본은 단일 파일로
        dst.execute("VACUUM")                         # 지운 행의 빈 공간을 걷어 낸 작은 사본(복구하면 DB 도 작아짐)
    finally:
        dst.close()
        src.close()


def backup(config: Config, out_dir: Path | None = None, *, with_raw: bool = True, with_uploads: bool = False,
           with_audio: bool = False) -> dict[str, Any]:
    """data 를 tar.gz 하나로 백업한다. 반환 {"path","bytes","sessions","includes"}.

    기본 위치는 data/backups/. 같은 디스크가 고장 나면 함께 잃으므로 외장 매체로 복사해 두어야 한다.
    """
    from . import __version__
    from .db import SCHEMA_VERSION

    out_dir = Path(out_dir) if out_dir else Path(config.data_dir) / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    target = out_dir / f"gukgam-backup-{stamp}.tar.gz"
    n = 1
    while target.exists():
        target = out_dir / f"gukgam-backup-{stamp}-{n}.tar.gz"
        n += 1
    includes = ["db"]
    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".backup-") as td:
        snap = Path(td) / "gukgam.sqlite3"
        snapshot_db(config.db_path, snap)
        sessions = _check_db(snap)
        part = target.with_name(target.name + ".part")
        try:
            with tarfile.open(part, "w:gz") as tar:
                tar.add(snap, arcname="gukgam.sqlite3")
                for flag, name, path in ((with_raw, "raw", raw_dir(config)),
                                         (with_uploads, "uploads", Path(config.upload_dir)),
                                         (with_audio, "audio", Path(config.audio_dir))):
                    if flag and path.is_dir():
                        tar.add(path, arcname=name)
                        includes.append(name)
                manifest = {"created_at": now_ms(), "created_kst": stamp, "version": __version__,
                            "schema_version": SCHEMA_VERSION, "sessions": sessions, "includes": includes,
                            "db_bytes": snap.stat().st_size}
                data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
                info = tarfile.TarInfo("manifest.json")
                info.size = len(data)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(data))
            part.replace(target)
        finally:
            part.unlink(missing_ok=True)
    return {"path": str(target), "bytes": target.stat().st_size, "sessions": sessions, "includes": includes}


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    out = []
    for m in tar.getmembers():
        name = m.name.lstrip("./")
        top = name.split("/", 1)[0]
        if top not in BACKUP_MEMBERS or ".." in Path(name).parts or Path(name).is_absolute():
            raise ValueError(f"백업 파일에 허용되지 않는 항목이 있습니다: {m.name}")
        if not (m.isfile() or m.isdir()):
            raise ValueError(f"백업 파일에 일반 파일이 아닌 항목이 있습니다: {m.name}")
        out.append(m)
    return out


def restore(config: Config, archive: Path) -> dict[str, Any]:
    """백업 tar.gz 를 data 폴더로 되돌린다. 서버를 끈 상태에서만 부른다(호출자가 확인).

    기존 DB(-wal·-shm 포함)와, 백업에 들어 있는 폴더(raw/uploads/audio)의 현재 내용은
    data/restore-before-<시각>/ 으로 옮겨 둔다(지우지 않음). 반환 {"sessions","moved_to","restored"}.
    """
    archive = Path(archive)
    if not archive.is_file():
        raise FileNotFoundError(f"백업 파일이 없습니다: {archive}")
    data = Path(config.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=data, prefix=".restore-") as td:
        tdp = Path(td)
        with tarfile.open(archive, "r:gz") as tar:
            members = _safe_members(tar)
            tar.extractall(tdp, members=members, filter="data")
        db_new = tdp / "gukgam.sqlite3"
        if not db_new.is_file():
            raise ValueError("백업 파일에 DB(gukgam.sqlite3)가 없습니다")
        sessions = _check_db(db_new)
        keep = data / f"restore-before-{_stamp()}"
        keep.mkdir(parents=True, exist_ok=True)
        db = Path(config.db_path)
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db) + suffix)
            if p.exists():
                shutil.move(str(p), str(keep / p.name))
        shutil.move(str(db_new), str(db))
        restored = ["db"]
        for name, dest in (("raw", raw_dir(config)), ("uploads", Path(config.upload_dir)),
                           ("audio", Path(config.audio_dir))):
            src = tdp / name
            if not src.is_dir():
                continue
            if dest.exists():
                shutil.move(str(dest), str(keep / name))
            shutil.move(str(src), str(dest))
            restored.append(name)
    return {"sessions": sessions, "moved_to": str(keep), "restored": restored}
