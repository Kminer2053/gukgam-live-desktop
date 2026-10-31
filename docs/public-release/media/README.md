# 국감 라이브 홍보 자산

실제 Windows 설치 앱의 화면으로 사용 흐름을 보여 줍니다. 화면은 preview.24에서 촬영했고, 기관·인물·문답은 촬영을 위해 만든 가상 자료입니다. 운영 회의 기록·원음·계정 정보는 사용하지 않았습니다.

- `gukgam-live-threads-30s.mp4`: 실제 앱 화면, 한국어 내레이션·음악·자막을 담은 1080×1920 세로 영상입니다.
- `poster.png`: 영상과 같은 실제 화면을 넣은 9:16 포스터입니다.
- `screenshots/*.jpg`: 원본 바이트를 보존한 실제 화면 10장입니다. 영상에는 이 중 9장을 사용합니다.
- `promo-real-ui.html`, `render-real-ui.mjs`, `real-ui-shots.json`: 실제 캡처의 확대·장면 전환과 로컬 MP4 제작 소스입니다.
- `real-ui-inputs.manifest.json`: 사용한 캡처의 SHA-256과 확대 범위를 기록합니다.
- `real-ui-final-review.json`: 완성 MP4의 화면 크기·길이·대표 프레임·음성 정렬과 PCM 음량 관측입니다.
- `promo-script.ko.txt`, `promo.ko.srt`: 내레이션 원고와 문장 단위 편집 자막입니다.
- `deliverables.sha256.json`: 현재 배포 자산의 파일 크기와 해시입니다.

영상의 `실제 앱 · 가상 자료` 표시는 촬영 출처를 뜻합니다. 화자 확인 필요·불확실 표시는 실제 앱에 나타난 그대로 유지합니다. AI 초안은 원문 대조가 필요하며, 이 짧은 시연은 긴 회의의 처리 속도나 정확도를 측정한 자료가 아닙니다.

기존에 제작한 한국어 음성·음악 믹스를 재사용했습니다. 이번 화면 교체에는 유료 생성 도구를 추가 사용하지 않았습니다. 실제 Threads 게시나 소셜 계정 연결은 수행하지 않았습니다.

## 제작 소스 실행

개발 의존성이 준비된 저장소 루트에서 실행합니다. 직원용 앱 사용 절차에는 이 명령이 필요하지 않습니다.

```powershell
node docs/public-release/media/render-real-ui.mjs --node-modules desktop/node_modules
```

기본 입력은 이 폴더의 `screenshots/`와 현재 최종 MP4의 음성 트랙입니다. 최종 MP4가 없는 소스 사본에서는 공개 릴리스의 영상을 받은 뒤 `--source-audio 파일경로`를 지정하세요. 렌더러는 별도 `*-real-ui.mp4`를 만들며, 기존 음성 입력은 해시별 보존 사본으로 남깁니다. 검수 후 최종 배포 파일을 교체합니다. 기존 Edge의 격리된 Canvas·MediaRecorder를 사용하며 외부 요청과 모델 실행을 허용하지 않습니다.

고유 아이콘 원본과 Windows ICO는 `desktop/assets/brand/`에 있습니다. 국회 공식 앱·공식 인증·자동 판단의 정확도를 주장하지 않습니다.
