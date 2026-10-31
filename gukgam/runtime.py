"""앱 전역 런타임(의존성 묶음). FastAPI lifespan 에서 하나 만들어 app.state.rt 에 둔다."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .config import Config
from .db import Database
from .events import EventBus
from .pipeline import SessionPipeline
from .repo import Repo

if TYPE_CHECKING:
    from .cloudsync import CloudSync
    from .ingest.manager import IngestManager
    from .llm.service import AnalysisService
    from .stt.service import STTService

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, config: Config):
        self.config = config
        config.ensure_dirs()
        self.db = Database(config.db_path)
        self.repo = Repo(self.db)
        self.desktop_bridge = config.desktop_bridge
        if self.desktop_bridge is not None:
            from . import settings

            # 최초 appData DB만 초기화한다. 재실행이 사람이 바꾼 기관·엔진·허용 설정을 덮지 않는다.
            initial_settings = {
                "org": {"name": config.desktop_organization},
                "llm": {"provider": "desktop_chatgpt", "desktop_model": config.desktop_model, "rolling_interval_sec": 0},
                "transcript_review": {"enabled": True, "auto_block": True, "role_decision_provider": "none", "use_jev": False},
                "stt": {"provider": "none"}, "audio_review": {"enabled": False},
            }
            for key, value in initial_settings.items():
                if self.repo.get_setting(key) is None:
                    settings.update(self.repo, key, value)
            bootstrap = {"organization": config.desktop_organization, "model": config.desktop_model}
            previous = self.repo.get_setting("desktop_bootstrap")
            if isinstance(previous, dict):
                # 마법사에서 명시적으로 바꾼 필드만 갱신한다. 같은 입력의 재부팅은 UI 수정을 보존한다.
                if previous.get("organization") != bootstrap["organization"]:
                    settings.update(self.repo, "org", {"name": config.desktop_organization})
                if previous.get("model") != bootstrap["model"]:
                    settings.update(self.repo, "llm", {"desktop_model": config.desktop_model})
            if previous != bootstrap:
                self.repo.set_setting("desktop_bootstrap", bootstrap)
        # 국회 자막은 부분 갱신이 잦아 재접속 재전송 여유를 넉넉히 둔다(채널당 2000건)
        self.bus = EventBus(history=2000)
        self._pipelines: dict[str, SessionPipeline] = {}
        self._tick_task: asyncio.Task | None = None
        self.role_model_prepare_task: asyncio.Task | None = None
        self.role_model_prepare_error = ""
        self.stt_model_prepare_task: asyncio.Task | None = None
        self.cloud: CloudSync | None = None
        # 아래 서비스는 start() 에서 늦게 만든다(순환 import 방지)
        self.analysis: AnalysisService
        self.stt: STTService
        self.ingest: IngestManager

    def pipeline(self, session_id: str) -> SessionPipeline:
        p = self._pipelines.get(session_id)
        if p is None:
            p = SessionPipeline(self.repo, self.bus, session_id, hooks=self.analysis)
            self._pipelines[session_id] = p
        return p

    def drop_pipeline(self, session_id: str) -> None:
        self._pipelines.pop(session_id, None)

    def reload_settings(self) -> None:
        reviews = getattr(self, "reviews", None)
        engine = getattr(reviews, "engine", None)
        reconfigure = getattr(engine, "reconfigure", None)
        if callable(reconfigure):
            reconfigure()
        stages = getattr(self, "stages", None)
        if stages is not None:
            stages.settings_changed()
        for p in self._pipelines.values():
            p.reload_settings()

    async def start(self) -> None:
        from .ingest.manager import IngestManager
        from .llm.service import AnalysisService
        from .stt.service import STTService
        from .review_service import TranscriptReviewService
        from .ingest.audio_review import AudioReviewService
        from .question_audio import QuestionAudioService
        from .live_stages import LiveStagesService

        self.analysis = AnalysisService(self)
        self.stt = STTService(self)
        self.reviews = TranscriptReviewService(self)
        self.audio_review = AudioReviewService(self)
        self.question_audio = QuestionAudioService(self)
        self.stages = LiveStagesService(self)
        self.repo.interrupt_stage_documents()
        self.ingest = IngestManager(self)
        await self.analysis.start()
        await self.ingest.recover()
        from .display_service import DisplayService
        self.display = DisplayService(self)
        await self.display.start()
        self._tick_task = asyncio.create_task(self._ticker(), name="pipeline-ticker")

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(10)
            await self.tick_pipelines()

    async def attach_app(self, app) -> None:
        """로컬 앱이 런타임을 붙인 뒤 설정된 클라우드 동기화만 시작한다."""
        if self.cloud is not None or not (self.config.cloud_url and self.config.agent_token):
            return
        from .cloudsync import CloudSync

        self.cloud = CloudSync(self, app)
        await self.cloud.start()

    async def tick_pipelines(self) -> int:
        """미확정·분석 대기 발언이 남은 파이프라인만 tick 한다(다 처리된 종료 세션은 건너뜀). tick 한 개수."""
        n = 0
        for p in list(self._pipelines.values()):
            if not p.has_unfinished:
                continue
            try:
                await p.tick()
                n += 1
            except Exception:  # pragma: no cover - 방어
                log.exception("pipeline tick 실패: %s", p.sid)
        return n

    async def shutdown(self) -> None:
        stt_prepare = self.stt_model_prepare_task
        if stt_prepare is not None and not stt_prepare.done():
            stt_prepare.cancel()
            await asyncio.gather(stt_prepare, return_exceptions=True)
            if self.stt_model_prepare_task is stt_prepare:
                from .stt.nemotron_assets import mark_cancelled
                mark_cancelled(self.config.data_dir)
        prepare = self.role_model_prepare_task
        if prepare is not None and not prepare.done():
            prepare.cancel()
            await asyncio.gather(prepare, return_exceptions=True)
        if self.cloud is not None:
            try:
                # stop이 탭·작업을 멈춘 뒤 남은 push를 이 한도 안에 보내고 연결을 닫는다.
                await self.cloud.stop(flush_timeout=2)
            except Exception as exc:
                log.warning("클라우드 동기화 종료 실패(%s)", type(exc).__name__)
        if self._tick_task:
            self._tick_task.cancel()
        try:
            stages = getattr(self, "stages", None)
            if stages is not None:
                # 수집 종료 flush가 보정 모델을 새로 예약하기 전에 요청을 닫는다.
                await stages.close()
            await self.ingest.shutdown()
        finally:
            # 한 제공자의 정리 실패가 이 앱의 다른 작업·서브프로세스 정리를 막지 않는다.
            for name, method in (("display", "close"), ("stages", "close"), ("question_audio", "close"), ("audio_review", "close"), ("reviews", "close"),
                                 ("analysis", "shutdown"), ("stt", "shutdown")):
                service = getattr(self, name, None)
                if service is None:
                    continue
                try:
                    await getattr(service, method)()
                except Exception as exc:
                    log.warning("서비스 종료 실패(%s, %s)", name, type(exc).__name__)
            self.db.close()
