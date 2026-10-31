# 국감 라이브 소개영상

Sign in with ChatGPT와 제작 배경을 먼저 소개한 뒤 실제 앱의 검색·시간 막대·질의자별 요약·자료 요구·보고서를 보여 줍니다. 세로형 9:16과 가로형 16:9는 각각 54.167초·30fps입니다. 일반 사용자는 영상 제작 도구를 설치할 필요가 없습니다.

완성 영상 두 가지는 정식 0.1.0 GitHub 릴리스에서 제공합니다. 이 폴더의 `gukgam-live-product-tour.mp4`와 `gukgam-live-threads-30s.mp4`는 기존 링크를 위한 세로형 호환 파일명입니다. 파일명과 별개로 실제 길이는 검수 명세를 따릅니다.

## 제작 소스 재구성

기존 Node·Playwright·Edge 개발 환경에서 완성 영상의 AAC를 그대로 재사용합니다. 원 WAV·음악 파일이나 추가 유료 생성은 필요하지 않습니다.

```powershell
node docs/public-release/media/render-stable-launch.mjs --source-audio docs/public-release/media/gukgam-live-product-tour.mp4 --output-dir outputs/introduction-rebuild
```

`stable-launch-shots.json`은 장면·실제 사진·대본을, `introduction-inputs.manifest.json`과 `introduction-review.json`은 입력 해시·일정한 프레임·오디오·실제 표본 검수를 기록합니다. 제작 자료는 합성 예시이며 실제 회의의 모델 품질 측정으로 사용하지 않습니다. 실제 사진의 픽셀과 출처 표시는 변조하지 않았습니다.

이전 `product-tour-*` 명세와 `render-product-tour.mjs`는 preview.25의 7장면 제작 이력입니다. 현재 영상의 검수와 재구성에는 `introduction-*` 명세와 `render-stable-launch.mjs`를 사용하세요.
