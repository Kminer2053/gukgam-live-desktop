import test from 'node:test';
import assert from 'node:assert/strict';
import {readResponseStream} from '../src/ai/stream';
const frame=(event:unknown)=>new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`);
test('완료 이벤트 뒤 연결 종료나 취소 훅을 기다리지 않는다',async()=>{
  let cancelled=0;
  const body=new ReadableStream<Uint8Array>({start(controller){controller.enqueue(frame({type:'response.output_text.delta',delta:'모의 응답'}));controller.enqueue(frame({type:'response.completed'}));},cancel(){cancelled++;return new Promise(()=>{});}});
  assert.equal(await readResponseStream(new Response(body)),'모의 응답');assert.equal(cancelled,1);assert.equal(body.locked,false);
});
test('한국어 스트림이 바이트마다 나뉘어도 전사를 보존한다',async()=>{
  const encoded=frame({type:'response.output_text.delta',delta:'한국어 모의 응답'});
  const body=new ReadableStream<Uint8Array>({start(controller){for(let i=0;i<encoded.length;i++)controller.enqueue(encoded.subarray(i,i+1));controller.enqueue(frame({type:'response.completed'}));controller.close();}});
  assert.equal(await readResponseStream(new Response(body)),'한국어 모의 응답');
});
test('부분 응답 뒤 실패와 조기 연결 종료를 성공으로 반환하지 않는다',async()=>{
  for(const suffix of ['', 'data: [DONE]\n\n', 'data: {"type":"response.failed","error":{"code":"MOCK_PRIVATE"}}\n\n'])await assert.rejects(readResponseStream(new Response('data: {"type":"response.output_text.delta","delta":"부분원문"}\n\n'+suffix)),error=>error instanceof Error&&!error.message.includes('MOCK_PRIVATE')&&!error.message.includes('부분원문'));
});
test('요청 취소는 같은 이유를 유지하고 스트림을 해제한다',async()=>{
  const controller=new AbortController(),reason=new Error('모의 취소');let cancelled=0;
  const body=new ReadableStream<Uint8Array>({cancel(){cancelled++;}});
  const request=readResponseStream(new Response(body),controller.signal);const rejected=assert.rejects(request,error=>error===reason);controller.abort(reason);await rejected;assert.equal(cancelled,1);assert.equal(body.locked,false);
});
