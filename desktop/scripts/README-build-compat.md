# 빌드 의존성 API 호환 보완

Node.js 24 이상에서 실행하는 빌드 도우미입니다. 위치는
`desktop/scripts/apply-build-compat-patches.cjs`이며, 인수 없이 실행하거나
같은 `desktop` 경로를 `--root`로 명시할 수 있습니다.

- `dir-compare 4.2.0`의 기본 필터가 `minimatch 11.0.2`의 named `minimatch` API를 사용하도록 두 지점을 보완합니다.
- `temp 0.9.4`는 명시적인 `rimraf 6.1.3` callback/sync adapter를 사용합니다. 원래 추적·카운트·Promise 코드는 유지하고, 추적된 절대 경로를 glob 확장 없이 정리합니다.
- `glob 14.0.3`을 포함한 승인된 빌드 의존성 버전을 검사합니다. 이 도우미가 패키지를 설치하거나 버전·의존성 선언을 바꾸지는 않습니다.

상위 패키지의 라이선스와 버전 표기를 유지합니다. 원본·패치 SHA가 알려진
값과 다르거나 상위 패키지 버전이 바뀌면 적용 전에 중단합니다. 새 버전은
별도 검토와 실제 API 계약 검증이 필요합니다.

원본 복원 자료와 적용 영수증은 `node_modules/.cache/gukgam-build-compat/`에만
둡니다. 반복 실행은 같은 보완 상태를 확인합니다. 앱 버전 등의 정상
metadata 변경은 허용하되, 매 실행이 `package.json`과 `package-lock.json`을
바꾸지 않았는지 전후 해시로 검사합니다.

Windows의 rimraf JS 구현은 자체 backoff 정책을 사용하므로,
`maxRetries: 6`이 옛 구현과 동일한 대기 시간을 뜻하지는 않습니다.

`npm run build`와 `npm run dist`가 시작될 때 이 도우미를 자동 실행합니다. 깨끗한 소스 사본에서 `npm ci`로 고정 의존성을 설치한 뒤 실행하세요. 기존 `node_modules`의 버전이 다르면 먼저 새 잠금 파일에 맞춰 다시 설치해야 합니다. 이 변경은 공개 main의 후속 소스 보완이며 기존 정식 0.1.0 설치파일을 재생성하지 않았습니다.
