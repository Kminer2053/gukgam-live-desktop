import {relayUrl} from './security';
import type {RelayHealth} from '../shared/contracts';
/** 공개 상태만 확인한다. 토큰·현재 PC·원본 조회 권한 시험을 대신하지 않는다. */
export async function checkRelayHealth(value:string,fetcher:typeof fetch=fetch):Promise<RelayHealth> {
  const address=relayUrl(value);
  try {
    const response=await fetcher(`${address}/api/health`,{method:'GET',redirect:'error',credentials:'omit',cache:'no-store',signal:AbortSignal.timeout(8000),headers:{Accept:'application/json'}});
    if(response.status===401||response.status===403)throw Error('RELAY_LOGIN_REQUIRED');
    if(!response.ok||!response.headers.get('content-type')?.includes('application/json')||!response.body)throw Error('상태 응답을 확인하지 못했습니다.');
    const reader=response.body.getReader();let total=0;const chunks:Uint8Array[]=[];
    try {
      while(true) {const {done,value:part}=await reader.read();if(done)break;total+=part.byteLength;if(total>65536)throw Error('상태 응답이 너무 큽니다.');chunks.push(part);}
    } finally {void reader.cancel().catch(()=>{});reader.releaseLock();}
    const body=JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if(body?.ok!==true||body.cloud!==true||!['source','mirror'].includes(body.read_mode)||typeof body.agent?.online!=='boolean'||!['source','mirror'].includes(body.agent.read_mode))throw Error('국감 릴레이 상태 응답이 아닙니다.');
    const sourceReady=body.read_mode==='source'&&body.agent.read_mode==='source'&&body.agent.online;
    return {reachable:true,readMode:body.read_mode,agentOnline:body.agent.online,sourceReady,message:sourceReady?'공개 상태에서 원본 열람 모드와 에이전트 온라인을 확인했습니다. 현재 PC·토큰·원본 조회 권한은 별도 확인이 필요합니다.':'릴레이 상태 응답을 받았습니다. 원본 열람 모드 또는 에이전트 연결이 아직 준비되지 않았습니다. 현재 PC 연결 성공을 뜻하지 않습니다.'};
  } catch(error) {
    if(error instanceof Error&&error.message==='RELAY_LOGIN_REQUIRED')throw Error('이 릴레이는 상태 조회에도 웹 로그인이 필요합니다. 이 버튼으로 인증 상태를 확인할 수 없습니다. 기관 관리자 절차로 연결을 확인하세요. 현재 PC·토큰·원본 조회는 미검증입니다.');
    throw Error('릴레이 상태를 확인하지 못했습니다. 배포 주소와 기관 관리자 설정을 확인하세요. 현재 PC 연결은 아직 검증되지 않았습니다.');
  }
}
