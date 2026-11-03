# 릴레이 배포 도구의 호환 보완

공식 Vercel CLI `50.0.0`을 사용합니다. 이 판본에는 sandbox 의존성이 없으며,
전체 잠금 그래프의 실제 패키지 300종을 OSV에 조회한 결과 알려진 항목은
0건이었습니다(2026-11-03). 이는 향후 취약점 부재를 보장하지 않습니다.
보안 수정이 있는 esbuild·srvx·minimatch·Zod 등의 정확한 버전은 package-lock.json에 고정합니다.

공식 CLI의 로그인·토큰 갱신·배포 기능을 사용합니다. CLI50에 없는
`integration list --json`은 공식 저장소 목록 API로 연결합니다. 배포할 프로젝트는
소유한 작업 폴더의 `.vercel/project.json`과 조직 범위를 검증한 뒤, 해당 자식
프로세스의 두 환경 변수로만 전달합니다. 다른 프로젝트의 자동 생성으로 전환하지 않습니다.

Node.js 24 이상에서 `scripts/apply-relay-compat-patches.cjs`를 실행합니다.
배포 빌드의 준비 단계에서도 자동 실행합니다. 변경하는 배포 의존성 소스는 두 곳입니다.

- `@ts-morph/common 0.11.1`: minimatch의 현재 named API를 선택하는 한 줄.
- `vercel 50.0.0`: 확인한 `project.id`를 기존 공식 배포 SDK의 `project` 인수로 전달하는 한 줄.

두 번째 보완은 프로젝트가 확인된 뒤 이름만으로요청하는 경쟁 상태를 막습니다.
상위 TypeScript 변경은 `patches/vercel-50-project-binding.patch`에 표시합니다.
CLI 원본 판본은 `vercel/vercel`의 `ef726cc19fb9f1b14b3c5c4f9ddc77c49947f4df`입니다.
패키지 이름·버전·잠금 파일·원 라이선스는 보완 전후 그대로 유지합니다.

전체 원본/보완 SHA256과 고유한 변경 위치가 일치할 때만 적용합니다.
원본과 영수증은 `node_modules/.cache/gukgam-relay-cli-50-compat/`에 보존하며
설치물에는 이 개발용 백업을 넣지 않습니다. 반복 실행과 소스 변경 시 중단도 확인합니다.

깨끗한 소스에서 `npm ci --prefix relay-cli --ignore-scripts`로 설치하고,
relay-cli에서 `npm run prepare:compat`로 독립 확인할 수 있습니다.
합성 회귀는 실제 계정 로그인·클라우드 배포를 대신하지 않습니다.
