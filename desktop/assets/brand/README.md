# 국감 라이브 아이콘

회의장 돔과 자막 말풍선을 결합한 벡터 아이콘이다. 작은 작업 표시줄과 바탕화면에서도 형태를 알아볼 수 있도록 글자 없이 구성했다.

- 원본: `gukgam-live.svg`
- 기본 PNG: `gukgam-live-1024.png`
- Windows 아이콘: `gukgam-live.ico` — 16/24/32/48/64/128/256px
- 작은 크기 검수: `icon-size-review.png`

기본 팔레트: Navy `#081A34`, Blue `#185184`, Mint `#37CEB9`.
설치·제거·실행 파일은 `desktop/package.json`에서 이 ICO를 사용한다. 시작·기록·매뉴얼 창은 동봉 PNG를, 시작 화면은 SVG를 사용한다. 기록 화면의 웹 아이콘도 같은 SVG다.

재생성은 `node desktop/scripts/render-app-icon.mjs --node-modules 기존_설치_경로`로 수행한다. 신뢰할 수 있는 기존 sharp 의존성을 사용하며 새 패키지를 설치하지 않는다. 이 스크립트는 아이콘 자산과 해시 목록만 만들며 영상·포스터·매뉴얼을 바꾸지 않는다.
