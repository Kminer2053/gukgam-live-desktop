# 국감 라이브 소개영상

현재 소개영상은 `gukgam-live-product-tour.mp4`, 포스터는 `poster-product-tour.png`입니다. 검색, 시간바로 기록 발언 이동, 질의자별로 모은 질의 요약, 자료 요구·약속, 보고서 내보내기와 내 ChatGPT 계정 사용을 보여 줍니다.

`gukgam-live-threads-30s.mp4`와 `poster.png`는 기존 링크를 위한 동일 내용의 호환 파일명입니다. 현재 영상의 실제 길이는 `product-tour-review.json`에 기록합니다.

## 제작 소스

- `promo-product-tour.html`, `render-product-tour.mjs`, `product-tour-shots.json`: 장면·캡처·편집 구성.
- `promo-script.ko.txt`, `promo.ko.srt`: 현재 내레이션 원고·자막.
- `screenshots/manifest.json`, `product-tour-inputs.manifest.json`: 캡처 및 오디오 출처·파일 해시.
- `product-tour-review.json`, `production-manifest.json`, `deliverables.sha256.json`: 최종 파일 관측·배포 자산 정보.

개발 의존성이 준비된 저장소 루트에서 다음과 같이 재구성할 수 있습니다. 같은 7발화 타이밍의 완성 영상 오디오를 재사용하며, 모든 기본 출력은 `outputs/product-tour-rerender/`에 분리합니다. 원래 7개 WAV는 필요하지 않습니다.

```powershell
node docs/public-release/media/render-product-tour.mjs --node-modules desktop/node_modules --source-audio docs/public-release/media/gukgam-live-product-tour.mp4
node docs/public-release/media/review-product-tour.mjs --input-dir outputs/product-tour-rerender
```

직원용 앱 사용에는 이 명령이나 개발 도구가 필요하지 않습니다. 시연 입력의 출처와 범위는 내부 제작 메타데이터에 기록하며, 별도 실회의 처리 속도·정확도 측정으로 사용하지 않습니다. 화면·음성 제작에 추가 유료 생성 비용은 없으며 실제 Threads 게시를 수행하지 않았습니다.
