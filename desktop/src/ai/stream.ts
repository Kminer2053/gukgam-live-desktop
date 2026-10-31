export function extractJson(text:string):unknown {const stripped=text.trim().replace(/^```(?:json)?\s*/i,'').replace(/\s*```$/,'');return JSON.parse(stripped);}
export type ResponseStreamOptions={onActivity?:()=>void;onText?:(receivedChars:number)=>void};
export async function readResponseStream(response:Response,signal?:AbortSignal,options:ResponseStreamOptions={}):Promise<string>{
 if(!response.body)throw Error('AI 응답 스트림이 없습니다');
 const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',output='',completed=false,cancelled=false;
 const handle=(frame:string)=>{
  const data=frame.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n').trim();
  if(!data||data==='[DONE]')return;
  let event:any;try{event=JSON.parse(data);}catch{throw Error('AI 응답 형식을 확인하지 못했습니다. 다시 시도하세요.');}
  if(event?.type==='response.output_text.delta'&&typeof event.delta==='string'){
   output+=event.delta;if(output.length>200000)throw Error('AI 응답이 너무 깁니다');options.onText?.(output.length);
  }
  if(event?.type==='response.completed')completed=true;
  if(['response.failed','response.incomplete','error'].includes(event?.type)){
   const code=event.response?.error?.code||event.error?.code||event.code;
   throw Error(aiError(typeof code==='string'?code:'stream_failed'));
  }
 };
 const check=()=>{if(signal?.aborted)throw signal.reason??new DOMException('AI 요청이 취소되었습니다.','AbortError');};
 // 완료 뒤 서버나 취소 훅이 열린 상태여도 결과를 즉시 반환한다.
 // 정리는 마지막 결과 전달을 기다리게 하지 않는다.
 const cancelReader=(reason?:unknown)=>{if(cancelled)return;cancelled=true;void reader.cancel(reason).catch(()=>{});};
 const aborted=()=>cancelReader(signal?.reason);
 signal?.addEventListener('abort',aborted,{once:true});
 try{
  check();
  while(true){
   const {value,done}=await reader.read();check();
   if(value?.byteLength)options.onActivity?.();
   buffer=(buffer+decoder.decode(value,{stream:!done})).replace(/\r\n/g,'\n');
   if(buffer.length>300000)throw Error('AI 응답이 너무 깁니다');
   let n:number;
   while((n=buffer.indexOf('\n\n'))>=0){handle(buffer.slice(0,n));buffer=buffer.slice(n+2);if(completed)return output;}
   if(done)break;
  }
  if(buffer.trim())handle(buffer);
  if(!completed)throw Error('AI 응답이 완료되기 전에 연결이 끊겼습니다. 다시 시도하세요');
  return output;
 }finally{signal?.removeEventListener('abort',aborted);cancelReader(signal?.aborted?signal.reason:undefined);reader.releaseLock();}
}
export function aiError(code:string,status?:number){if(code.includes('usage_limit')||status===429)return 'ChatGPT 구독 사용 한도에 도달했습니다. 사용량 설정을 확인하거나 나중에 다시 시도하세요';if(code.includes('usage_unavailable')||code.includes('unsupported')||status===403)return '현재 계정 또는 모델에서 구독 사용을 지원하지 않습니다';if(status===401)return 'ChatGPT 연결이 만료되었거나 철회되었습니다. 다시 연결하세요';if(code==='model_not_found'||status===404)return '선택한 모델을 현재 계정에서 사용할 수 없습니다. 연결 진단 후 모델을 다시 선택하세요';if(status===400)return 'AI 요청 설정을 확인하지 못했습니다. 연결 진단 후 다시 시도하세요';if(status&&status>=500)return 'ChatGPT 서버에서 요청을 처리하지 못했습니다. 잠시 후 직접 다시 시도하세요';return 'AI 요청을 완료하지 못했습니다. 연결 상태를 확인한 뒤 다시 시도하세요';}
