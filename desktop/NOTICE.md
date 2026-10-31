# 재사용한 코드

## Nemotron CPU 음성 화자 분리

NVIDIA의 `Nemotron-3-Diarization.q8_0.gguf`를 Windows 설치파일에 포함한다. 출처는 https://huggingface.co/nvidia/Nemotron-3-Diarization 이며 고정 판본은 `f667ed73aee57d40cc39428eb768b4fd87a0a29e`, SHA256은 `08456d9e22cd9a323c0364d98375f3746d6e68507ebb705cd46438c534c7a3a1`이다. 모델은 OpenMDW-1.1이며 공식 약관 사본을 `LICENSES/Nemotron-OpenMDW-1.1.txt`에 보존한다. 모델 파일은 변경하지 않았다.

공식 NeMo-Speech.cpp `v0.2.0`의 Windows x86_64 CPU 실행 도구를 함께 포함한다. 원본 ZIP의 Apache-2.0 및 제3자 고지를 `resources/nemotron-assets/runtimes/`에 그대로 보존한다. 음성 화자 번호는 실명·역할의 확인을 뜻하지 않는다. 전사용 Nemotron 3.5 ASR과 Julia 가중치는 별도 선택 준비이며, 운영 녹음·기록·계정은 설치파일에 넣지 않는다.

## Julia-1 선택 CPU 판정

선택적 사전 화자 역할 판정의 입력 직렬화는 Apache-2.0으로 공개된 SupersonicLabs Julia-1의 `julia/data.py`, `julia/typed.py` 계약에서 NumPy·tokenizers 방식으로 옮겼다. 원본 리비전은 `a85b127321d580d65176c89ced8273f305745d85`, 공식 ONNX 리비전은 `82a2fadf8fccfccdc5fd4e1009ba8f1a265eb7a8`이다. 원 저작자는 Supersonic Labs이며 출처는 https://huggingface.co/SupersonicLabs/Julia-1 및 https://huggingface.co/SupersonicLabs/Julia-1-ONNX 이다.

수정 사항은 Windows CPU 작업자·익명 파이프, 엄격한 길이·ID 검증, 네 화자 역할 입력, 앱 전용 모델 준비·검증·취소다. 설치물에 모델 가중치를 포함하지 않는다. 사용자가 선택해 공식 모델을 준비할 때만 내려받는다. Apache-2.0 전문은 `LICENSES/Julia-1-Apache-2.0.txt`에 보존한다. 역할 점수는 보정된 정확도나 Jev 확신과 동등하지 않다.

## ChatGPT 연결 코드

사용자 요청으로 기존 Shorts_gen의 ChatGPTService, OAuth PKCE·loopback, 계정 보안 저장소, Responses 스트림 파서를 새 앱에서 재사용했다. 원 저작권과 허가문은 LICENSES/Shorts_gen-MIT.txt에 포함하며 설치 패키지에도 동봉한다. 기존 앱의 자격증명·로그·운영 설정은 가져오지 않았다.

쇼츠 브랜딩과 구성안 문구를 국감 기록 문구로 바꾸고 주석을 한국어로 옮겼다. 로그인 후 자동 AI 시험을 없애고 사용자가 별도 시험 버튼을 눌렀을 때만 호출한다. 엔진 요청의 max_tokens를 max_output_tokens로 전달한다. 계정 정보는 렌더러에 전달하지 않고 운영체제 safeStorage로 보호한다.

이 코드의 포함은 ChatGPT 계정·플랜·고용주 업무 사용 권한이나 앱 등록 승인을 보장하지 않는다. 실제 연결 시 공식 로그인과 구독 사용 동의 및 짧은 연결 시험으로 계정 가용성을 확인해야 한다. 이번 구현 중 실제 계정 로그인·API 요청은 실행하지 않았다.

Windows 최초 검증에서 설치·서명·SmartScreen·사용자 프로필 ACL·safeStorage를 직접 확인해야 한다. 엔진 임시 설정은 앱 전용 userData/private 폴더의 임의 파일에만 만들고 ready/실패/종료에 회수한다. POSIX의 700/600 모드는 Windows ACL 검증을 대신하지 않는다. 장기 토큰은 encrypted vault에만 저장하며 이전 파일을 자동 복구하거나 다른 앱 자료를 읽지 않는다.
