import type {RelayProvisionInput} from '../relay/contracts';
export function validateRelayForm(value:RelayProvisionInput):RelayProvisionInput {
  if(!value.teamId||!value.productId||!value.planId)throw Error('팀, 저장공간 종류, 요금제를 차례로 선택하세요.');
  if(!/^gukgam-[a-z0-9][a-z0-9-]{2,45}$/.test(value.projectName))throw Error('사이트 이름은 gukgam-으로 시작하는 영문 소문자·숫자·하이픈으로 10~53자 입력하세요.');
  if(Array.from(value.operatorPasscode).length<16||Array.from(value.viewerPasscode).length<12)throw Error('운영자 패스코드는 16자 이상, 열람자 패스코드는 12자 이상으로 직접 정하세요.');
  if(value.operatorPasscode===value.viewerPasscode)throw Error('운영자와 열람자 패스코드는 서로 다르게 정하세요.');
  if(value.operatorPasscode.length>200||value.viewerPasscode.length>200||/[\x00-\x1f]/.test(value.operatorPasscode+value.viewerPasscode))throw Error('패스코드는 줄바꿈 없이 각각 200자 이내로 입력하세요.');
  if(!value.consent)throw Error('선택한 요금제와 외부 서비스의 이용 조건을 확인하고 구성 진행에 동의하세요.');
  return value;
}
