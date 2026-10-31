"""공개 합성 시험 전용: 실제 명단·회의 fixture·사용자 설정을 가져오지 않는다."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gukgam.config import Config
from gukgam.web.app import create_app


@pytest.fixture(autouse=True)
def isolate_external_environment(monkeypatch):
    for name in (
        "GUKGAM_CLOUD_URL", "GUKGAM_AGENT_TOKEN", "GUKGAM_CLOUD_READ_MODE",
        "GUKGAM_CLOUD_PERSONAL_TEST", "MONGODB_URI", "MONGODB_DATABASE",
        "GUKGAM_TYPESAFE_API_KEY", "GUKGAM_TRANSCRIPT_REVIEW", "GUKGAM_AUDIO_REVIEW",
        "GUKGAM_TEST_NETWORK", "GUKGAM_TEST_MLX", "GUKGAM_TEST_NEMOTRON",
    ):
        monkeypatch.delenv(name, raising=False)


def make_config(tmp_path: Path, **overrides) -> Config:
    values = dict(
        data_dir=tmp_path / "data", operator_passcode="", viewer_passcode="",
        secret_key="public-synthetic-test-only-0123456789abcdef",
        allow_assembly_network=False, disable_heavy_engines=True,
        cloud_url="", agent_token="", cloud_mirror_sensitive=False, cloud_read_mode="mirror",
    )
    values.update(overrides)
    return Config(**values)


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def client(config):
    with TestClient(create_app(config)) as value:
        value.headers.update({"X-Requested-With": "gukgam"})
        yield value


@pytest.fixture
def rt(client):
    """이 시험이 만든 임시 앱의 런타임만 사용한다."""
    return client.app.state.rt
