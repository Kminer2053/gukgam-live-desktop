# 재사용한 코드

## 선택 다운로드 압축 처리

고정된 다운로드 묶음의 압축 처리는 node-tar 7.5.22와 그 의존성의 실제 고정 판본을 사용합니다.
tar·chownr·minipass의 BlueOak-1.0.0, fs-minipass·yallist의 ISC,
minizlib·jose의 MIT 원문과 저작권은 `LICENSES/npm-*.txt`에 보존합니다.
원 라이브러리의 라이선스를 프로젝트 MIT로 바꿔 표시하지 않습니다.

## 릴레이 배포 도구

0.2.0 기본 앱과 별도로, 중계 기능을 선택할 때 Vercel CLI 50.0.0(Apache-2.0) 기반 도구 묶음을 준비합니다. 원본 프로젝트는
https://github.com/vercel/vercel 이며 판본은
`ef726cc19fb9f1b14b3c5c4f9ddc77c49947f4df`입니다. 국감 라이브 기여자는
2026-11-03에 확인된 프로젝트 ID를 공식 배포 요청으로 전달하는 한 줄을
보완했습니다. 함께 사용하는 @ts-morph/common 0.11.1(MIT)에는 minimatch의
현재 API를 선택하는 한 줄을 보완했습니다. 각 패키지의 원 LICENSE 파일과
저작권을 동봉하며, 변경 위치·원본 및 결과 SHA256은 공개 소스의
`desktop/relay-cli/scripts/apply-relay-compat-patches.cjs`에서 확인할 수 있습니다.

## Nemotron CPU 음성 화자 분리

NVIDIA의 `Nemotron-3-Diarization.q8_0.gguf`는 사용자가 음성 기능을 선택할 때 별도로 준비하는 자산이다. 0.2.0 기본 앱에 모델 가중치를 미리 포함하지 않는다. 출처는 https://huggingface.co/nvidia/Nemotron-3-Diarization 이며 고정 판본은 `f667ed73aee57d40cc39428eb768b4fd87a0a29e`, SHA256은 `08456d9e22cd9a323c0364d98375f3746d6e68507ebb705cd46438c534c7a3a1`이다. 모델은 OpenMDW-1.1이며 공식 약관 사본을 `LICENSES/Nemotron-OpenMDW-1.1.txt`에 보존한다. 모델 파일은 변경하지 않았다.

공식 NeMo-Speech.cpp `v0.2.0`의 Windows x86_64 CPU 실행 도구도 음성 기능 준비 때 함께 확인한다. 원본 ZIP의 Apache-2.0 및 제3자 고지는 별도로 준비한 실행 도구와 함께 그대로 보존한다. 음성 화자 번호는 실명·역할의 확인을 뜻하지 않는다. 전사용 Nemotron 3.5 ASR과 Julia 가중치는 별도 선택 준비이며, 운영 녹음·기록·계정은 설치파일에 넣지 않는다.

## Julia-1 선택 CPU 판정

선택적 사전 화자 역할 판정의 입력 직렬화는 Apache-2.0으로 공개된 SupersonicLabs Julia-1의 `julia/data.py`, `julia/typed.py` 계약에서 NumPy·tokenizers 방식으로 옮겼다. 원본 리비전은 `a85b127321d580d65176c89ced8273f305745d85`, 공식 ONNX 리비전은 `82a2fadf8fccfccdc5fd4e1009ba8f1a265eb7a8`이다. 원 저작자는 Supersonic Labs이며 출처는 https://huggingface.co/SupersonicLabs/Julia-1 및 https://huggingface.co/SupersonicLabs/Julia-1-ONNX 이다.

수정 사항은 Windows CPU 작업자·익명 파이프, 엄격한 길이·ID 검증, 네 화자 역할 입력, 앱 전용 모델 준비·검증·취소다. 설치물에 모델 가중치를 포함하지 않는다. 사용자가 선택해 공식 모델을 준비할 때만 내려받는다. Apache-2.0 전문은 `LICENSES/Julia-1-Apache-2.0.txt`에 보존한다. 역할 점수는 보정된 정확도나 Jev 확신과 동등하지 않다.

## ChatGPT 연결 코드

사용자 요청으로 기존 Shorts_gen의 ChatGPTService, OAuth PKCE·loopback, 계정 보안 저장소, Responses 스트림 파서를 새 앱에서 재사용했다. 원 저작권과 허가문은 LICENSES/Shorts_gen-MIT.txt에 포함하며 설치 패키지에도 동봉한다. 기존 앱의 자격증명·로그·운영 설정은 가져오지 않았다.

쇼츠 브랜딩과 구성안 문구를 국감 기록 문구로 바꾸고 주석을 한국어로 옮겼다. 로그인 후 자동 AI 시험을 없애고 사용자가 별도 시험 버튼을 눌렀을 때만 호출한다. 엔진 요청의 max_tokens를 max_output_tokens로 전달한다. 계정 정보는 렌더러에 전달하지 않고 운영체제 safeStorage로 보호한다.

이 코드의 포함은 ChatGPT 계정·플랜·고용주 업무 사용 권한이나 앱 등록 승인을 보장하지 않는다. 실제 연결 시 공식 로그인과 구독 사용 동의 및 짧은 연결 시험으로 계정 가용성을 확인해야 한다. 개발용 합성 검증은 사용자의 실제 계정 가용성 확인을 대신하지 않는다.

Windows 최초 검증에서 설치·서명·SmartScreen·사용자 프로필 ACL·safeStorage를 직접 확인해야 한다. 엔진 임시 설정은 앱 전용 userData/private 폴더의 임의 파일에만 만들고 ready/실패/종료에 회수한다. POSIX의 700/600 모드는 Windows ACL 검증을 대신하지 않는다. 장기 토큰은 encrypted vault에만 저장하며 이전 파일을 자동 복구하거나 다른 앱 자료를 읽지 않는다.


## 0.2.0 인터넷 설치 템플릿 보완

electron-builder/app-builder-lib 26.18.0의 NSIS 템플릿(MIT)을 사용합니다. 원 프로젝트는 https://github.com/electron-userland/electron-builder 입니다. 국감 라이브 기여자는 2026-11-03에 templates/nsis/include/installer.nsh와 templates/nsis/installSection.nsh 두 곳을 보완했습니다.

변경 내용은 기본 앱 자료를 기존판 종료·제거 전에 소유한 임시 사본으로 준비하고 SHA512를 확인하며 압축 해제 직전 같은 사본을 재확인하는 것입니다. 인접 파일·명시 파일·다운로드 파일에 같은 검증을 적용하고 원본 지정 파일은 이동하지 않습니다. 현재 x64용으로 고정합니다. 전체 설치 과정의 완전한 롤백을 보장하는 변경은 아닙니다.

적용 위치와 원본·결과 SHA256은 desktop/scripts/apply-web-installer-security.cjs에 고정합니다. 원 패키지 이름·버전·lock·라이선스는 변경하지 않습니다. 기본 앱 압축 자료는 NSIS의 소유 임시 폴더에서 처리하며 사용하지 않는 영구 설치 캐시로 복사하지 않습니다. 외부에서 지정한 원본 파일은 보존합니다. 0.2.0은 공개 준비 중이며 이 고지가 설치·공개 완료를 의미하지는 않습니다.

electron-builder의 원 MIT 고지 전문을 아래에 보존합니다.

```text
The MIT License (MIT)

Copyright (c) 2015 Loopline Systems

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
