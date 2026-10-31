"""명령행: 서버·시연·백업·복구·클라우드 동기화."""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shlex
import sys
from pathlib import Path

from .config import ROOT, Config
from .secure_files import open_regular_file, verify_private_file

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_ENV_BYTES = 1024 * 1024
DEMO_FIXTURE = ROOT / "tests" / "fixtures" / "gukto_20261007_tail.txt"
DEMO_ROSTER = "\n".join([
    "유의동 위원장 국민의힘",
    "강득구 위원 더불어민주당",
    "김남근 위원 더불어민주당",
    "김미애 위원 국민의힘",
    "김종양 위원 국민의힘",
    "복기왕 위원 더불어민주당",
    "김은혜 위원 국민의힘",
    "홍지선 기관 국토교통부장관",
])


def _env_error_message() -> str:
    if os.name == "nt":
        return ".env를 읽을 수 없습니다. 현재 사용자와 SYSTEM만 허용하는 보호 ACL과 KEY=값 형식을 확인하세요."
    return ".env를 읽을 수 없습니다. 권한 600과 KEY=값 형식을 확인하세요."


def _load_env(path: Path | None = None) -> None:
    """비공개 일반 파일을 최대1MiB 읽고 전체 검증 뒤 기존 환경변수를 보존한다."""
    path = path if path is not None else ROOT / ".env"
    try:
        with open_regular_file(path, private=True) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > MAX_ENV_BYTES:
                raise ValueError
            raw_file = stream.read(MAX_ENV_BYTES + 1)
            verify_private_file(stream)
            after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (len(raw_file) > MAX_ENV_BYTES or len(raw_file) != before.st_size or b"\0" in raw_file
                or any(getattr(before, key) != getattr(after, key) for key in fields)):
            raise ValueError
        values = {}
        for line in raw_file.decode("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, raw = line.partition("=")
            key = key.strip()
            if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError
            parts = shlex.split(raw, comments=True, posix=True)
            if len(parts) > 1:
                raise ValueError
            values[key] = parts[0] if parts else ""
    except FileNotFoundError:
        return
    except (OSError, UnicodeError, ValueError):
        raise ValueError(_env_error_message()) from None
    added = []
    try:
        for key, value in values.items():
            existed = key in os.environ
            current = os.environ.setdefault(key, value)
            if not existed and current == value:
                added.append((key, value))
    except (OSError, UnicodeError, ValueError):
        for key, value in reversed(added):
            if os.environ.get(key) == value:
                os.environ.pop(key, None)
        raise ValueError(_env_error_message()) from None


def _config(args: argparse.Namespace) -> Config:
    cfg = Config()
    if getattr(args, "host", None):
        cfg.host = args.host
    if getattr(args, "port", None):
        cfg.port = args.port
    return cfg


def _warn_exposure(cfg: Config) -> None:
    if cfg.host not in ("127.0.0.1", "localhost", "::1") and not cfg.auth_enabled:
        print("[경고] 인증 없이 외부 접속을 허용합니다. GUKGAM_OPERATOR_PASSCODE / GUKGAM_VIEWER_PASSCODE 를 "
              "설정하세요(python -m gukgam init-admin 참고).", file=sys.stderr)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    try:
        _load_env()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    cfg = _config(args)
    _warn_exposure(cfg)
    print(f"국감 라이브 서버: http://{cfg.host}:{cfg.port}  (인증 {'켜짐' if cfg.auth_enabled else '꺼짐'})")
    if args.reload:
        # 재시작 시 create_app() 이 환경변수로 Config 를 다시 만든다
        os.environ["GUKGAM_HOST"], os.environ["GUKGAM_PORT"] = cfg.host, str(cfg.port)
        uvicorn.run("gukgam.web.app:create_app", factory=True, host=cfg.host, port=cfg.port, reload=True,
                    reload_dirs=[str(ROOT / "gukgam")], log_level=args.log_level)
    else:
        from .web.app import create_app

        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level=args.log_level)
    return 0


def cmd_cloud_sync(args: argparse.Namespace) -> int:
    """포트나 수집 서비스를 시작하지 않고 연결 확인·백필을 실행한다."""
    import asyncio
    from types import SimpleNamespace

    from .cloudsync import CloudSync

    try:
        _load_env()
        cfg = _config(args)
        if not cfg.cloud_url or not cfg.agent_token:
            print("클라우드 URL과 에이전트 토큰 환경변수를 모두 설정하세요.", file=sys.stderr)
            return 1

        async def run() -> bool:
            rt = None
            sync = None
            try:
                if args.check:
                    sync = CloudSync(SimpleNamespace(config=cfg))
                    return bool(await sync.check())
                from .runtime import Runtime

                rt = Runtime(cfg)
                sync = CloudSync(rt)
                if not await sync.backfill(args.session):
                    return False
                return bool(await sync.flush(timeout=30))
            finally:
                if sync is not None:
                    await sync.stop(flush_timeout=0)
                if rt is not None:
                    rt.db.close()

        ok = asyncio.run(run())
    except Exception:
        # 네트워크 예외에는 URL·헤더가 들어갈 수 있어 내용을 출력하지 않는다.
        print("클라우드 동기화에 실패했습니다. 설정과 연결 상태를 확인하세요.", file=sys.stderr)
        return 1
    print(("클라우드 연결 확인" if args.check else "클라우드 백필") + (" 성공" if ok else " 실패"))
    return 0 if ok else 1


def _demo_body(args: argparse.Namespace) -> dict:
    fixture = Path(args.fixture).resolve() if args.fixture else DEMO_FIXTURE
    return {
        "title": "[시연] 국토교통위원회 국정감사(국토교통부 등) 리플레이",
        "committee": "국토교통위원회",
        "agencies": "국토교통부 등",
        "meeting_date": "2026-10-07",
        "source_type": "replay",
        "source_config": {"fixture": str(fixture), "speed": args.speed, "with_hints": args.with_hints},
        "roster_text": DEMO_ROSTER,
        "notes": "tests/fixtures 의 실제 국회 AI자막 텍스트를 재생하는 시연 세션입니다.",
        "start": True,
    }


def _demo_via_api(base: str, body: dict) -> str | None:
    """이미 떠 있는 서버가 있으면 API 로 만든다. 서버가 없으면 None."""
    import httpx

    headers = {"X-Requested-With": "gukgam"}
    try:
        with httpx.Client(base_url=base, timeout=10, headers=headers) as c:
            try:
                h = c.get("/api/health").json()
            except ValueError:
                raise SystemExit(f"{base} 에 다른 프로그램이 떠 있습니다. --port 를 확인하세요.")
            if h.get("auth_enabled"):
                code = os.environ.get("GUKGAM_OPERATOR_PASSCODE", "")
                r = c.post("/api/auth/login", json={"passcode": code, "name": "replay-demo"})
                if r.status_code != 200:
                    raise SystemExit("운영자 로그인 실패: GUKGAM_OPERATOR_PASSCODE 를 확인하세요")
            r = c.post("/api/sessions", json=body)
            if r.status_code != 200:
                raise SystemExit(f"세션 생성 실패({r.status_code}): {r.text}")
            s = r.json()
            if s.get("start_error"):
                print(f"[경고] 수집 시작 실패: {s['start_error']}", file=sys.stderr)
            return s["id"]
    except httpx.TransportError:
        return None


def cmd_replay_demo(args: argparse.Namespace) -> int:
    cfg = _config(args)
    base = f"http://{cfg.host}:{cfg.port}"
    body = _demo_body(args)
    if not Path(body["source_config"]["fixture"]).is_file():
        print(f"리플레이 파일이 없습니다: {body['source_config']['fixture']}", file=sys.stderr)
        return 1
    sid = _demo_via_api(base, body)
    if sid:
        print(f"리플레이 세션을 만들고 시작했습니다(실행 중인 서버): {base}/s/{sid}")
        return 0
    # 서버가 없으면 세션을 DB 에 만들고 이 프로세스에서 서버를 띄워 바로 시작한다
    import uvicorn

    from .db import Database
    from .repo import Repo
    from .web.app import create_app
    from .web.routes_api import parse_roster_text

    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        repo = Repo(db)
        s = repo.create_session(title=body["title"], source_type="replay", committee=body["committee"],
                                agencies=body["agencies"], meeting_date=body["meeting_date"],
                                source_config=body["source_config"], created_by="replay-demo", notes=body["notes"])
        repo.set_roster(s["id"], parse_roster_text(DEMO_ROSTER))
        repo.audit("replay-demo", "session.create", s["id"], {"source_type": "replay"})
    finally:
        db.close()
    _warn_exposure(cfg)
    print(f"리플레이 세션: {base}/s/{s['id']}  (배속 {args.speed}x, Ctrl+C 로 종료)")
    uvicorn.run(create_app(cfg, autostart=[s["id"]]), host=cfg.host, port=cfg.port, log_level=args.log_level)
    return 0


def cmd_init_admin(args: argparse.Namespace) -> int:
    cfg = Config()
    op, vw = secrets.token_urlsafe(9), secrets.token_urlsafe(9)
    key_file = cfg.data_dir / "secret.key"
    print(f"""국감 라이브 — 관리자 초기 설정 안내

[현재 상태]
  인증: {'켜짐' if cfg.auth_enabled else '꺼짐(로컬 개발 모드 — 모든 사용자가 운영자)'}
  데이터 폴더: {cfg.data_dir}
  서명 키: {'환경변수 GUKGAM_SECRET_KEY' if cfg.secret_key else (str(key_file) + (' (있음)' if key_file.exists() else ' (첫 실행 때 자동 생성, 권한 600)'))}
  국회 의사중계 연동: {'허용' if cfg.allow_assembly_network else '차단(폐쇄망)'}

[1] 패스코드 설정(서버 실행 전 환경변수) — 아래는 무작위 추천값입니다
  export GUKGAM_OPERATOR_PASSCODE='{op}'   # 운영자: 수집 제어·편집·설정
  export GUKGAM_VIEWER_PASSCODE='{vw}'     # 열람자: 중계·기록 보기, 북마크
  패스코드를 바꾸면 기존 로그인은 모두 풀립니다.

[2] (선택) 서명 키 고정: export GUKGAM_SECRET_KEY='{secrets.token_hex(32)}'
[3] (선택) 폐쇄망 배치: export GUKGAM_ALLOW_ASSEMBLY_NETWORK=0
[4] 실행: python -m gukgam serve --host 127.0.0.1 --port 8765
       내부망에 공개할 때는 반드시 패스코드를 설정하고 HTTPS 역방향 프록시 뒤에 두세요.
[5] 운영자로 로그인 → /admin 에서 기관명·별칭·기관장, 감시 키워드, 담당부서, AI 엔진을 설정합니다.
[6] 시연: python -m gukgam replay-demo --speed 20
[7] 백업: python -m gukgam backup --out /Volumes/외장/백업   (복구: python -m gukgam restore <파일> --yes, 서버를 끈 뒤)
""")
    return 0


def _server_running(cfg: Config) -> bool:
    """설정된 주소에서 국감 라이브 서버가 응답하는지(복구 전 확인)."""
    import httpx

    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::", "") else cfg.host
    try:
        r = httpx.get(f"http://{host}:{cfg.port}/api/health", timeout=2)
        return r.status_code < 500
    except httpx.HTTPError:
        return False


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def cmd_backup(args: argparse.Namespace) -> int:
    from . import maintenance

    cfg = Config()
    if not cfg.db_path.is_file():
        print(f"DB 파일이 없습니다: {cfg.db_path}", file=sys.stderr)
        return 1
    try:
        res = maintenance.backup(cfg, Path(args.out) if args.out else None, with_raw=not args.no_raw,
                                 with_uploads=args.with_uploads, with_audio=args.with_audio)
    except Exception as e:
        print(f"백업 실패: {e}", file=sys.stderr)
        return 1
    print(f"백업 완료: {res['path']}  ({_human(res['bytes'])}, 세션 {res['sessions']}개, 포함: {', '.join(res['includes'])})")
    print("같은 디스크가 고장 나면 백업도 함께 잃습니다. 외장 매체·다른 PC 로 복사해 두세요.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from . import maintenance

    cfg = Config()
    archive = Path(args.archive).expanduser()
    if not archive.is_file():
        print(f"백업 파일이 없습니다: {archive}", file=sys.stderr)
        return 1
    if _server_running(cfg) and not args.force:
        print(f"서버가 실행 중입니다(http://{cfg.host}:{cfg.port}). 서버를 먼저 끈 뒤 복구하세요.", file=sys.stderr)
        return 1
    if not args.yes:
        print(f"복구할 백업: {archive}\n데이터 폴더: {cfg.data_dir}\n"
              "현재 DB 와(백업에 들어 있는) raw·uploads·audio 폴더는 data/restore-before-<시각>/ 으로 옮겨 둡니다.\n"
              "진행하려면 --yes 를 붙여 다시 실행하세요.")
        return 2
    try:
        res = maintenance.restore(cfg, archive)
    except Exception as e:
        print(f"복구 실패: {e}", file=sys.stderr)
        return 1
    print(f"복구 완료: 세션 {res['sessions']}개, 복구 항목 {', '.join(res['restored'])}")
    print(f"이전 데이터 보관 위치: {res['moved_to']}  (확인 후 직접 지우세요)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m gukgam", description="국감 라이브 — 국정감사 실시간 기록·중계·분석")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="웹 서버 실행")
    sp.add_argument("--host", default=None, help=f"바인드 주소(기본 {DEFAULT_HOST})")
    sp.add_argument("--port", type=int, default=None, help=f"포트(기본 {DEFAULT_PORT})")
    sp.add_argument("--reload", action="store_true", help="코드 변경 시 자동 재시작(개발용)")
    sp.add_argument("--log-level", default="info")
    sp.set_defaults(func=cmd_serve)

    cs = sub.add_parser("cloud-sync", help="클라우드 연결 확인·백필(비밀값 출력 없음)")
    mode = cs.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="토큰과 연결 확인")
    mode.add_argument("--backfill", action="store_true", help="로컬 기록을 클라우드에 다시 전송")
    cs.add_argument("--session", default=None, help="백필할 세션 ID(생략 시 최근 기록)")
    cs.set_defaults(func=cmd_cloud_sync)

    rp = sub.add_parser("replay-demo", help="리플레이 시연 세션 생성·시작")
    rp.add_argument("--speed", type=float, default=20.0, help="재생 배속(기본 20)")
    rp.add_argument("--fixture", default=None, help="자막 텍스트 파일(기본 tests/fixtures/gukto_20261007_tail.txt)")
    rp.add_argument("--with-hints", action="store_true", help="파일의 화자 힌트를 수집원 정보로 사용")
    rp.add_argument("--host", default=None)
    rp.add_argument("--port", type=int, default=None)
    rp.add_argument("--log-level", default="info")
    rp.set_defaults(func=cmd_replay_demo)

    ip = sub.add_parser("init-admin", help="인증·초기 설정 안내 출력")
    ip.set_defaults(func=cmd_init_admin)

    bp = sub.add_parser("backup", help="DB(온라인 백업)와 국회 자막 원본을 tar.gz 로 백업")
    bp.add_argument("--out", default=None, help="백업 파일을 둘 폴더(기본 data/backups)")
    bp.add_argument("--no-raw", action="store_true", help="국회 자막 원본(data/raw)은 빼기")
    bp.add_argument("--with-uploads", action="store_true", help="업로드한 녹음 파일(data/uploads)도 포함")
    bp.add_argument("--with-audio", action="store_true", help="원음 WAV(data/audio)도 포함")
    bp.set_defaults(func=cmd_backup)

    rs = sub.add_parser("restore", help="백업 tar.gz 로 복구(서버를 끈 뒤)")
    rs.add_argument("archive", help="gukgam-backup-*.tar.gz 경로")
    rs.add_argument("--yes", action="store_true", help="확인 없이 진행")
    rs.add_argument("--force", action="store_true", help="서버 응답 확인을 건너뜀(서버가 꺼져 있는 것이 확실할 때만)")
    rs.set_defaults(func=cmd_restore)

    args = p.parse_args(argv)
    if args.cmd == "cloud-sync" and args.session and not args.backfill:
        p.error("--session은 --backfill과 함께 사용하세요")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
