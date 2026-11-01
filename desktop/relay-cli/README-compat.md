# 릴레이 배포 도구의 빌드 호환 보완

Node.js 24 이상에서 쓰는 빌드 도우미입니다. 위치는
`desktop/relay-cli/scripts/apply-relay-compat-patches.cjs`이며, 인수 없이 실행하거나
같은 `desktop/relay-cli` 경로를 `--root`로 명시할 수 있습니다.

Vercel CLI는 `59.4.0`을 유지합니다. 이 도우미는 설치된 패키지의 이름·버전·라이선스·의존성
metadata를 바꾸지 않고 다음 **3개 파일, 총 35곳**만 보완합니다.

- `@ts-morph/common 0.11.1`: minimatch named API를 선택하는 fallback 한 줄.
- `@vercel/python-analysis 0.14.0`: ESM·CJS 각각 17개의 단일 인수 record를 `z.record(z.string(), valueSchema)`로 명확히 표현.

Zod는 네이티브 `4.6.6`을 사용하며 `/v3` 호환 alias를 만들지 않습니다.
길이 제한·검증 조기 종료 등 Zod 자체의 보안 동작을 별도 구현으로
대체하지 않고, 스키마의 기존 값 제약과 오류 처리 코드를 유지합니다.
앱의 Python 배포 경로가 실제 사용하는 CommonJS 공개 스키마 4개의 정상·오류 입력 32건과 이전 API 계약을 검증했습니다. ESM에는 같은 스키마 보완을 적용하고 정적으로 검토했습니다. 상위 패키지의 ESM 단독 Node 24 로더에는 기존 extensionless import 문제가 있어 그 경로의 실행을 보증하지 않습니다.

고정 버전·원본 및 패치 SHA·record 인수 수·alias별 개수가 다르면 쓰기 전에
중단합니다. 상위 패키지의 새 버전은 별도 검토해야 합니다. 반복 실행은
동일한 보완 상태와 원본 복원 자료를 확인합니다.

백업과 적용 영수증은 `relay-cli/node_modules/.cache/gukgam-native-zod4-compat/`에
둡니다. 매 실행이 package/lock을 바꾸지 않았는지 전후 해시로 확인하되,
앱의 정상 버전 metadata 변경을 과거 영수증의 해시로 차단하지는 않습니다.

깨끗한 소스에서 `npm ci --prefix relay-cli --ignore-scripts`로 CLI 의존성을 설치합니다. `npm run dist` 직전 배포 준비 도우미가 이 보완을 자동 적용합니다. 독립 점검은 relay-cli에서 `npm run prepare:compat`로 실행할 수 있습니다. 실제 계정 로그인·클라우드 배포를 시험한 것은 아니며 기존 정식 0.1.0 설치파일은 교체하지 않았습니다.
