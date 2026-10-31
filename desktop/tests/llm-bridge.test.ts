import test from 'node:test';
import assert from 'node:assert/strict';
import {LlmBridge} from '../src/main/llm-bridge';
import type {LlmRequest,LlmResult} from '../src/shared/contracts';
const request=(id:string):LlmRequest=>({type:'llm.request',id,system:'근거만 요약',user:'모의 회의',max_tokens:200});
test('AI 요청은 한 번에 하나씩 전달하고 응답의 소유 번호를 지킨다',{timeout:5000},async()=>{
  const results:LlmResult[]=[],started:string[]=[];const finish:Array<(value:string)=>void>=[];
  let secondStarted!:()=>void,allCompleted!:()=>void;
  const second=new Promise<void>(resolve=>{secondStarted=resolve;}),completed=new Promise<void>(resolve=>{allCompleted=resolve;});
  const bridge=new LlmBridge({request:async(_s,_u,_m,options)=>{started.push(options!.requestId!);const result=new Promise<string>(resolve=>finish.push(resolve));if(options!.requestId==='two')secondStarted();return result;},cancelRequest:()=>{}},()=>undefined,result=>{results.push(result);if(result.id==='two')allCompleted();});
  bridge.accept(request('one'));bridge.accept(request('two'));assert.deepEqual(started,['one']);finish.shift()!('첫 응답');await second;assert.deepEqual(started,['one','two']);finish.shift()!('둘째 응답');await completed;
  assert.deepEqual(results,[{type:'llm.result',id:'one',text:'첫 응답'},{type:'llm.result',id:'two',text:'둘째 응답'}]);bridge.close();
});
test('취소는 해당 대기·실행 요청만 해제하며 다른 요청을 취소하지 않는다',{timeout:5000},async()=>{
  const results:LlmResult[]=[],cancelled:string[]=[],started:string[]=[];
  let keptCompleted!:()=>void;const completed=new Promise<void>(resolve=>{keptCompleted=resolve;});
  const bridge=new LlmBridge({request:async(_s,_u,_m,options)=>{started.push(options!.requestId!);if(options!.requestId==='first')return new Promise(()=>{});return '다음 응답';},cancelRequest:id=>cancelled.push(id)},()=>undefined,result=>{results.push(result);if(result.id==='keep')keptCompleted();});
  bridge.accept(request('first'));bridge.accept(request('remove'));bridge.accept(request('keep'));bridge.cancel('unknown');bridge.cancel('remove');bridge.cancel('first');await completed;
  assert.deepEqual(cancelled,['first']);assert.deepEqual(started,['first','keep']);assert.equal(results.filter(result=>result.id==='remove').length,1);assert.equal(results.filter(result=>result.id==='first').length,1);assert.deepEqual(results.at(-1),{type:'llm.result',id:'keep',text:'다음 응답'});bridge.close();
});
test('로그인 오류는 AI 요청 오류로 반환하며 기록 엔진 제어를 요구하지 않는다',{timeout:5000},async()=>{
  const results:LlmResult[]=[];
  let delivered!:()=>void;const completed=new Promise<void>(resolve=>{delivered=resolve;});
  const bridge=new LlmBridge({request:async()=>{throw Error('로그인 필요');},cancelRequest:()=>{}},()=>undefined,result=>{results.push(result);delivered();});
  bridge.accept(request('no-login'));await completed;assert.equal(results[0].type,'llm.error');bridge.close();
});
