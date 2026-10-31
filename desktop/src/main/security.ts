import {isAbsolute} from 'node:path';
import type {BackendEvent, SetupInput} from '../shared/contracts';
export const ENGINE_ORIGIN = 'http://127.0.0.1:8765';
const requestId = /^[a-zA-Z0-9_-]{1,80}$/;
export function isEngineUrl(value:string):boolean {
  try {const url=new URL(value);return url.origin===ENGINE_ORIGIN&&!url.username&&!url.password;} catch {return false;}
}
export function isEngineDownloadUrl(value:string):boolean {
  if(isEngineUrl(value))return true;
  try {const url=new URL(value);return url.protocol==='blob:'&&url.origin===ENGINE_ORIGIN;} catch {return false;}
}
export function relayUrl(value:string):string {
  let url:URL;try {url=new URL(value);} catch {throw Error('릴레이 주소는 https://로 시작하는 전체 주소를 입력하세요.');}
  if(url.protocol!=='https:'||url.username||url.password||url.hash||url.search||url.pathname!=='/'||url.port||!url.hostname.includes('.')||!/[a-z]/i.test(url.hostname)||/[:\[\]]/.test(url.hostname)||/(?:^|\.)(localhost|local|internal)$/.test(url.hostname))throw Error('릴레이 주소는 계정 정보나 쿼리가 없는 공개 HTTPS 기본 주소여야 합니다.');
  return url.toString().replace(/\/$/,'');
}
export function validateSetup(raw:unknown):SetupInput {
  if(!raw||typeof raw!=='object'||Array.isArray(raw))throw Error('기관 설정을 확인하세요.');
  const data=raw as Record<string,unknown>;
  if(Object.keys(data).some(key=>!['organization','model','relayUrl','relayToken'].includes(key)))throw Error('허용되지 않은 설정 항목입니다.');
  if(typeof data.organization!=='string'||!data.organization.trim()||data.organization.length>100||/[\x00-\x1f]/.test(data.organization))throw Error('기관명은 1~100자로 입력하세요.');
  const result:SetupInput={organization:data.organization.trim()};
  for(const key of ['model','relayUrl','relayToken'] as const) {
    const value=data[key];if(value===undefined||value==='')continue;
    if(typeof value!=='string'||value.length>(key==='relayToken'?8192:2048)||/[\x00-\x1f]/.test(value))throw Error('설정의 길이와 형식을 확인하세요.');
    result[key]=key==='relayUrl'?relayUrl(value.trim()):value.trim();
  }
  if(result.model&&!/^[a-zA-Z0-9._:-]{1,120}$/.test(result.model))throw Error('모델 이름을 확인하세요.');
  return result;
}
export function parseBackendEvent(raw:unknown):BackendEvent {
  if(!raw||typeof raw!=='object'||Array.isArray(raw))throw Error('기록 엔진의 응답 형식이 올바르지 않습니다.');
  const item=raw as Record<string,unknown>;
  if(item.type==='ready'&&item.url===ENGINE_ORIGIN)return {type:'ready',url:ENGINE_ORIGIN};
  if(item.type==='llm.cancel'&&typeof item.id==='string'&&requestId.test(item.id))return {type:'llm.cancel',id:item.id};
  if(item.type==='account.status.request'&&typeof item.id==='string'&&requestId.test(item.id))return {type:'account.status.request',id:item.id};
  if(item.type==='error'&&typeof item.message==='string')return {type:'error',message:'기록 엔진을 시작하지 못했습니다. 다른 국감 앱이 열려 있는지 확인하세요.'};
  if(item.type==='llm.request'&&typeof item.id==='string'&&requestId.test(item.id)&&typeof item.system==='string'&&item.system.length<=100_000&&typeof item.user==='string'&&item.user.length<=500_000&&Number.isInteger(item.max_tokens)&&Number(item.max_tokens)>=1&&Number(item.max_tokens)<=32768)return item as unknown as BackendEvent;
  throw Error('기록 엔진의 응답 형식이 올바르지 않습니다.');
}
export function redactLog(value:string):string {
  return value.replace(/Bearer\s+[^\s"']+/gi,'Bearer [숨김]').replace(/((?:access_token|refresh_token|id_token|relayToken|relay_token|api_key|authorization|password|passcode)["'\s]*[:=]["'\s]*)[^\s,}"']+/gi,'$1[숨김]').replace(/https?:\/\/[^\s]+/gi,'[주소 숨김]').slice(0,2000);
}
export function explicitPython(value:string|undefined):string {
  if(!value||!isAbsolute(value)||/[\x00\r\n]/.test(value))throw Error('개발용 Python 경로가 지정되지 않았습니다. 설치형 앱을 사용하세요.');
  return value;
}
