<div align="center">

<img src="desktop/assets/brand/gukgam-live.svg" width="96" alt="국감 라이브 아이콘">

# 국감 라이브 데스크톱

**회의가 끝난 뒤에도, 누가 무엇을 물었고 무엇을 답했는지 놓치지 않도록.**

코딩 지식 없이 ChatGPT 계정·Ollama·API로 국감 자막과 녹음의 문답·요약·후속조치를 정리하는 Windows 앱입니다.

Windows x64 · 국회 제공 자막 · CPU 녹음 전사 · 원문 대조 · 단독 HTML

**[설치 안내](#windows-install) · [설치·활용 매뉴얼](docs/public-release/manual.html) · [30초 시연 영상](docs/public-release/media/gukgam-live-threads-30s.mp4) · [GitHub](https://github.com/Kminer2053/gukgam-live-desktop)**

</div>

> **Windows preview24 설치판을 준비하고 있습니다. 설치파일은 아직 게시되지 않았습니다.** 게시 후 [GitHub Releases](https://github.com/Kminer2053/gukgam-live-desktop/releases)에 버전·SHA-256과 함께 안내합니다. 프로젝트 소스코드는 [MIT 라이선스](LICENSE)로 제공합니다.

## 기록은 쌓이는데, 다시 확인하는 데 시간이 걸리나요?

긴 회의에서 우리 기관이 언급된 부분을 찾고, 질문과 답변을 구분하고, 자료 요구와 검토 약속까지 챙기는 일은 별도의 작업입니다. 이 앱은 회의 기록과 검토할 AI 초안을 한곳에 모으고, 원문을 다시 확인할 수 있게 돕습니다.

## 한눈에 보기

| 하고 싶은 일 | 앱에서 하는 일 |
|---|---|
| 회의 기록 | 국회 AI자막 수신, 녹음·녹화 파일 전사, 수기 입력 |
| 문답 확인 | 질의·답변·진행 역할 후보와 성명 확인 필요 구간 살펴보기 |
| 내용 검토 | 원문과 정리된 본문, 확인 필요 표시를 나란히 대조 |
| 업무 정리 | 위원별 질의, 우리 기관 언급, 요구자료·약속·지적사항 확인 |
| 자료 찾기 | 기록을 검색하고 관련 발언으로 이동 |
| 보관·전달 | 마지막 저장 상태를 단독 HTML로 내보내 서버 없이 열람 |
| 동료 열람 | 별도 기관 릴레이 사이트에서 연결된 PC의 기록 열람 |

## 쓰는 순서는 간단합니다

1. **설치하고 연결합니다.** 기관명을 저장하고 ChatGPT 계정 또는 사용할 Ollama·API 연결을 설정합니다. AI 연결 전에도 기록을 열 수 있습니다.
2. **필요한 회의만 기록합니다.** 국회 자막이나 녹음 파일을 고르고 **시작**을 누릅니다. 앱 실행만으로 수집을 시작하지 않습니다.
3. **검토하고 보관합니다.** 본문·화자·요약과 후속조치를 확인한 뒤 **단독 HTML**로 저장합니다.

## AI를 선택할 수 있습니다

- **ChatGPT 계정:** 앱의 로그인 안내로 연결합니다. API 키 입력과 별개입니다.
- **Ollama·LM Studio:** 준비된 로컬 AI 서버와 모델을 연결합니다. 이 앱이 해당 서버나 모델을 자동 설치하는 것은 아닙니다.
- **OpenAI API·호환 서버:** 서버·모델·API 키를 별도로 설정합니다. 로컬 무인증 서버는 키 없이 연결할 수 있습니다.
- **AI 없이 기록:** 자막 수집·열람부터 사용하고 AI는 나중에 연결할 수 있습니다.

ChatGPT 계정 이용 조건과 API 요금은 별개입니다. 외부 서비스를 고르면 필요한 글과 문맥이 전송될 수 있습니다. 로컬 연결은 사용자가 준비한 서버·모델의 실행 환경을 따릅니다.

## 음성 전사와 역할 보정은 다른 단계입니다

| 구성 | 담당하는 일 |
|---|---|
| 국회 제공 자막 | 음성 모델 없이 원수신 글 기록 |
| Nemotron 3.5 ASR · CPU | 녹음을 글로 전사 |
| Nemotron 3 Diarization · CPU | 목소리별 발화 구간과 겹침 후보 구분 |
| Julia-1 · 선택 | 글의 질의·답변·진행·기타 역할 후보 판단 |
| 선택한 LLM | 전체 문맥의 역할·본문 보완과 요약 |

AI 결과는 검토할 초안입니다. 이름·숫자·부정 표현·문답 귀속·요약에서 빠진 쟁점을 원문과 대조하세요. 목소리 번호가 같아도 실명이나 역할의 정답이 확인된 것은 아닙니다. 녹음은 CPU와 파일 조건에 따라 음성 길이보다 오래 처리될 수 있습니다.

<a id="windows-install"></a>
## Windows 설치

Windows 10/11 x64, Core i5급·메모리 16GB를 기준으로 준비합니다. 음성 CPU 기능은 AVX2와 모델·녹음·임시 변환 파일을 둘 저장공간이 필요합니다.

일반 사용자는 Python·Node.js·Git을 따로 설치하지 않습니다. 설치파일이 게시되면 [공식 릴리스](https://github.com/Kminer2053/gukgam-live-desktop/releases)에서 버전·SHA-256을 확인해 설치하세요. 현재 준비 중인 버전은 **0.1.0-preview.24**입니다.

설치 후 시작 화면의 **설치·활용 매뉴얼**을 누르면 인터넷 없이 설명서를 볼 수 있습니다. 설치 전에는 [HTML 매뉴얼 원본](docs/public-release/manual.html)을 파일로 저장해 브라우저에서 여세요. GitHub에서는 HTML 소스가 먼저 표시될 수 있습니다.

<a id="demo"></a>
## 시연

[30초 시연 영상 보기](docs/public-release/media/gukgam-live-threads-30s.mp4) · [포스터 보기](docs/public-release/media/poster.png)

가상 기관·가상 문답으로 사용 흐름을 소개합니다. 영상은 1080×1920 세로 화면이며 한국어 내레이션·음악·자막을 포함합니다.

## 개인 기록과 공유

회의 기록은 이 PC의 앱 전용 저장소에 둡니다. 원문·수동 화자·사람이 수정하거나 검수한 결과는 자동 보정으로 덮어쓰지 않는 구조입니다. 앱 종료·업데이트·제거와 기록 삭제는 구분합니다.

설정한 보존 기간이 지나거나 회의를 삭제하면 관련 기록·원음·업로드 파일이 삭제될 수 있습니다. 단독 HTML 보관과 기관의 전체 백업 절차를 구분해 사용하세요. 릴레이는 별도 외부 서비스와 설정을 사용하며 PC가 꺼지면 원본 연결이 끊길 수 있습니다.

## 더 알아보기

- [설치·활용 HTML 매뉴얼](docs/public-release/manual.html)
- [앱 소개와 사용 전 확인사항](docs/public-release/RELEASE_OVERVIEW.md)
- [공개 범위와 제외 기준](docs/public-release/EXPORT_REVIEW.md)
- [배포 확인 메타데이터](docs/public-release/RELEASE_MANIFEST.json)
- [제삼자 고지](docs/public-release/THIRD_PARTY_NOTICES.md)

소스와 배포 소식은 [공식 GitHub 저장소](https://github.com/Kminer2053/gukgam-live-desktop)에서 안내합니다. GitLab 미러는 주소가 확정되면 추가합니다.

프로젝트 소스코드는 [MIT 라이선스](LICENSE)입니다. 모델·재사용 코드·의존성에는 [제삼자 고지](docs/public-release/THIRD_PARTY_NOTICES.md)에 적힌 개별 라이선스가 적용됩니다.
