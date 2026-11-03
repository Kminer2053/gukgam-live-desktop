# 제삼자 코드·모델 고지

정식 0.1.0의 코드·모델 고지입니다. 프로젝트 코드는 MIT 라이선스이며 루트 LICENSE에 전문을 보존합니다. 아래 재사용 코드·모델·실행 도구에는 각각의 원 라이선스와 고지가 별도로 적용됩니다. 설치파일의 동봉 자료·개별 고지·검증값은 배포 확인 정보에 기록합니다.

## 기존 국감 라이브 코드

기존 국감 라이브 소스를 기반으로 Windows 설치형 기록·보정 앱을 구성했습니다. 프로젝트 코드의 MIT 배포 조건과 함께 재사용 코드의 원 저작권·허가문을 보존합니다. 운영 자료·계정·기존 서비스 설정은 공개 소스에 포함하지 않습니다.

## ChatGPT 연결 코드

ChatGPT 로그인·스트림 해석·계정 암호화 보관의 일부 구현을 기존 Shorts_gen에서 재사용하고 국감 앱에 맞게 수정했습니다. 기존 MIT 저작권·허가문은 [Shorts_gen-MIT.txt](../../desktop/LICENSES/Shorts_gen-MIT.txt)에 보존합니다. 로그인 연결이 계정·모델·서비스 사용 권한을 대신 보장하지 않습니다.

## Julia-1

선택적 네 역할 판정은 SupersonicLabs Julia-1의 공식 입력 직렬화 계약을 Windows CPU 작업자에 맞게 사용합니다. 가중치는 사용자가 준비하는 선택 자산이며 설치 앱의 준비 상태와 별도로 확인합니다.

- 원 모델: SupersonicLabs/Julia-1
- 사용한 입력 계약 판본: `a85b127321d580d65176c89ced8273f305745d85`
- ONNX 판본: `82a2fadf8fccfccdc5fd4e1009ba8f1a265eb7a8`
- 고지 전문: [Julia-1-Apache-2.0.txt](../../desktop/LICENSES/Julia-1-Apache-2.0.txt)

## Nemotron 음성 모델·실행 도구

Nemotron-3-Diarization CPU 화자 분리 모델을 설치파일의 별도 자산으로 동봉합니다. Nemotron 3.5 ASR 전사 가중치는 선택 준비 자산입니다. 각 모델의 준비 화면과 최종 배포 자산 목록을 확인합니다.

- 화자 분리 모델: NVIDIA Nemotron-3-Diarization, 판본 `f667ed73aee57d40cc39428eb768b4fd87a0a29e`
- 모델 약관 사본: [Nemotron-OpenMDW-1.1.txt](../../desktop/LICENSES/Nemotron-OpenMDW-1.1.txt)
- CPU 실행 도구: NeMo-Speech.cpp `v0.2.0`, Apache-2.0 및 동봉한 제삼자 고지
- 실행 도구 ZIP·추출 자산에 있는 원 고지를 설치 패키지에도 보존합니다.

모델 파일과 실행 도구를 프로젝트 코드의 라이선스 하나로 바꿔 표시하지 않습니다. 모델을 포함한 설치파일은 각 자산의 원 약관·고지·판본을 함께 확인해야 합니다.

## 화면 자산과 의존성

기존 HLS 화면 코드의 라이선스 헤더·고지를 유지합니다. Electron·Python·npm 패키지와 번들 실행 도구의 고지도 실제 설치파일과 함께 대조합니다. 별도 변환 도구를 추가 배포하는 경우 해당 도구의 라이선스·바이너리 출처를 새로 검토합니다.

이 문서는 법적 사용 권한을 새로 부여하거나 외부 서비스의 약관을 대신하는 문서가 아닙니다. 전체 고지와 필수 라이선스 파일이 빠진 설치물·공개 스냅샷을 배포하지 않습니다.

## 공개 main의 Windows 빌드 도구 호환 보완

정식 0.1.0 이후 공개 소스는 Node.js 24에서 minimatch 11·glob 14·ASAR 4·rimraf 6·FileList 2를 사용하도록 빌드 의존성을 고정했습니다. `dir-compare 4.2.0`의 named minimatch 호출과 `temp 0.9.4`의 callback/sync 정리 호출만 명시적으로 보완하며, 원 저작권·라이선스와 패키지 버전 표기를 유지합니다. 적용 도우미는 알려진 원본 SHA가 다르면 중단하고 복원 자료를 남깁니다. [보완 내역](../../desktop/scripts/README-build-compat.md)을 참고하세요. 이 소스 변경으로 기존 공개 설치파일이 교체된 것은 아닙니다.

## 릴레이 CLI 50의 호환 보완


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
