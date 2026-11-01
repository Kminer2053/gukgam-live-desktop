# 국감 라이브 소개영상

Sign in with ChatGPT와 제작 배경을 먼저 소개한 뒤 실제 앱의 검색·시간 막대·질의자별 요약·자료 요구·보고서를 보여 줍니다. 두 비율 모두 54.167초·30fps입니다. 수정본은 기능의 위치를 보여 주고, 조작할 버튼과 그 결과로 이동한 뒤 읽는 동안 멈춥니다.

- [Threads용 세로형 9:16](https://github.com/Kminer2053/gukgam-live-desktop/releases/download/v0.1.0/Gukgam-Live-Introduction-9x16-Tracked.mp4)
- [발표·README용 가로형 16:9](https://github.com/Kminer2053/gukgam-live-desktop/releases/download/v0.1.0/Gukgam-Live-Introduction-16x9-Tracked.mp4)

일반 사용자는 영상 제작 도구를 설치할 필요가 없습니다.

## 제작 소스 재구성

기존 Node·Playwright·Edge 개발 환경에서 완성 영상의 AAC를 그대로 재사용합니다. 다음 명령은 기존 오디오를 필수 입력으로 받으며, 추가 음성 생성이나 모델 호출을 하지 않습니다.

```powershell
node docs/public-release/media/render-feature-tracking.mjs --source-audio docs/public-release/media/gukgam-live-product-tour.mp4 --output-dir outputs/feature-tracking-video
node docs/public-release/media/review-feature-tracking.mjs --input-dir outputs/feature-tracking-video --reuse-audio-review docs/public-release/media/introduction-review.json
```

`feature-tracking-shots.json`에는 장면과 버튼·결과별 이동·정지 계획이 있습니다. `feature-tracking-inputs.manifest.json`, `feature-tracking-review.json`, `feature-tracking-deliverables.json`에는 입력·출력 해시와 검수 근거가 있습니다. 실제 조작 전후 화면을 사용했으며, 로그인 성공이나 문서 다운로드를 연출하지 않았습니다. 계정 사진은 ChatGPT 사용 선택 화면입니다.

제작 화면의 내용은 별도 준비한 합성 예시입니다. 실제 회의의 AI 품질 측정 결과가 아니며 사진의 원래 픽셀은 변조하지 않았습니다.

이전 영상·명세·제작 소스는 이력과 기존 링크를 위해 보존합니다. `render-stable-launch.mjs`와 `introduction-*`는 직전 버전, `product-tour-*`는 preview.25 제작 이력입니다. 이 폴더의 기존 MP4 두 파일은 오디오 재사용과 호환 링크를 위한 자료이며, 현재 소개영상은 위 수정본 링크에서 받으세요.
