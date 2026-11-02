# Python 의존성 고정

Python 3.12용 네 설치 목록은 직접 의존성과 모든 전이 의존성을 `==`로 고정한다. 직원 PC의 기록·설정·모델 파일과는 관계없는 빌드 입력이다.

| 파일 | 대상과 포함 관계 |
|---|---|
| `requirements.txt` | 핵심 기능과 시험. Windows x64 및 Ubuntu 회귀를 함께 유지한다. |
| `requirements-julia-cpu.txt` | 선택한 Julia CPU 실행기와 전이 의존성. 가중치는 포함하지 않는다. |
| `requirements-desktop.txt` | 위 두 파일을 `-r`로 포함하고 Windows 엔진 빌드·시간대·릴레이 DB 실행기를 추가한다. |
| `requirements-ai.txt` | 선택적 Apple Silicon 로컬 AI. macOS 14 이상, ARM64, Python 3.12 대상이다. 모든 항목에 플랫폼 마커가 있어 Windows 기본 설치에는 적용되지 않는다. |

2026-11-02에 공식 PyPI와 uv 0.13.6으로 전체 의존성을 해석했다. 핵심·Julia·설치 목록에는 universal resolution의 플랫폼 마커를 유지했고, AI는 `--python-platform aarch64-apple-darwin --python-version 3.12`와 `MACOSX_DEPLOYMENT_TARGET=14.0`을 명시했다. `uvicorn[standard]`, `psycopg[binary]` 기능도 유지한다.

기존 세 목록의 직접 고정 선언 19줄(NumPy 중복 포함)은 유지했다. 이전에 열려 있던 전이 버전은 이번 해석 결과로 고정했으므로 기존 설치물과 동일한 패키지 바이트를 뜻하지 않는다. `pip freeze`나 기존 개발 환경에서 추출한 목록을 사용하지 않았다.

AI를 macOS 13으로 해석하면 MLX 0.29.3과 Torch 2.10.0이 선택되어 공개 취약점이 남았다. 현재 목록은 MLX 0.32.4와 Torch 2.15.0을 사용한다. 공식 수정 기준은 MLX 0.29.4, Torch 2.13.0이다. macOS 13에 취약한 구판으로 자동 대체하지 않는다. 실제 macOS 설치·모델 실행 검증은 별도로 필요하다.

- [uv의 플랫폼별·universal 해석](https://docs.astral.sh/uv/concepts/resolution/)
- [uv의 macOS 최소 버전 지정](https://docs.astral.sh/uv/reference/cli/#uv-pip-compile--python-platform)
- [MLX GGUF 로더 수정 기준](https://github.com/advisories/GHSA-j842-xgm4-wf88)
- [MLX 배열 로더 수정 기준](https://github.com/advisories/GHSA-w6vg-jg77-2qg6)
- [Torch 수정 기준](https://github.com/advisories/GHSA-rrmf-rvhw-rf47)

고정 목록을 바꿀 때는 깨끗한 Python 3.12 작업 환경에서 공식 PyPI로 전체 의존성을 다시 해석하고, 플랫폼 마커와 포함 관계를 검사한다. 전체 고정 이름·버전을 OSV에 조회한 뒤 Windows·Ubuntu 회귀 및 Windows 엔진·설치물 검증을 수행한다. Apple AI 변경은 macOS ARM에서도 검증한다. 한 번의 OSV 조회 0건은 향후 데이터베이스 변경이나 GitLab 서버 검사 통과를 보장하지 않는다.

현재 파일은 버전을 고정하지만 pip의 `--require-hashes` 모드는 사용하지 않는다. 패키지 해시까지 강제하려면 플랫폼별 배포 파일과 빌드 경로를 함께 검증한 별도 변경이 필요하다.
