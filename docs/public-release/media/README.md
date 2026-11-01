# 국감 라이브 소개영상

내 ChatGPT로 쉽게 시작하는 방법, 앱의 구동 구조, 실제 주요 기능을 차례로 소개합니다. 매뉴얼의 PC·AI·중계·동료 그림을 활용해 기록이 어디에 보관되고 어떻게 정리·공유되는지 설명합니다. 두 비율 모두 약 65초·30fps입니다.

- [Threads용 세로형 9:16](https://github.com/Kminer2053/gukgam-live-desktop/releases/download/v0.1.0/Gukgam-Live-Introduction-9x16-Structure.mp4)
- [발표·README용 가로형 16:9](https://github.com/Kminer2053/gukgam-live-desktop/releases/download/v0.1.0/Gukgam-Live-Introduction-16x9-Structure.mp4)

일반 사용자는 영상 제작 도구를 설치할 필요가 없습니다.

## 제작 소스 재구성

기존 Node·Playwright·Edge 개발 환경에서 완성 영상의 오디오를 재사용할 수 있습니다. 위 가로형 영상을 `outputs/Gukgam-Live-Introduction-16x9-Structure.mp4`로 저장한 뒤 실행하세요. 추가 음성 생성이나 모델 호출은 하지 않습니다.

```powershell
node docs/public-release/media/render-structure-explainer.mjs --source-audio outputs/Gukgam-Live-Introduction-16x9-Structure.mp4 --output-dir outputs/structure-explainer-video
node docs/public-release/media/review-structure-explainer.mjs --input-dir outputs/structure-explainer-video --reuse-audio-review docs/public-release/media/structure-explainer-review.json
```

`structure-explainer-shots.json`은 14장면과 강조·읽기 정지 계획입니다. `structure-diagrams/`는 매뉴얼에서 가져온 영상용 SVG입니다. 사무담당자에게 필요한 흐름에 맞춰 일부 상세 표시를 덜어내고 AI 이름을 명확히 했습니다. 원래 매뉴얼은 보존합니다.

`structure-explainer-inputs.manifest.json`, `structure-explainer-review.json`, `structure-explainer-deliverables.json`에는 원본·출력 해시와 검수 근거가 있습니다. 기존 음성 11개와 Windows SAPI로 만든 구조 설명 3개를 사용했습니다. 유료 생성이나 실제 회의에 대한 새 AI 요청은 하지 않았습니다.

제작 화면의 내용은 별도 준비한 합성 예시이며, 실제 회의의 AI 품질 측정 결과가 아닙니다. 실제 조작 전후 사진을 사용하고 로그인 성공이나 다운로드를 연출하지 않았습니다. 사진의 원래 픽셀은 보존합니다.

직전 `feature-tracking-*` 영상·제작 소스, 그 이전 `stable-launch-*` 및 `product-tour-*` 자료는 기존 링크와 제작 이력을 위해 보존합니다.
