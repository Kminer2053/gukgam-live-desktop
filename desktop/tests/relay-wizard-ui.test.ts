import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {initializeRelayWizard} from '../src/renderer/relay-wizard';
import type {ManagedRelayStatus,RelayCatalog,RelayDesktopApi,RelayPlan,RelayProvisionInput} from '../src/relay/contracts';
import type {EngineState} from '../src/shared/contracts';

class Element {
  protected storedValue='';textContent='';hidden=false;disabled=false;checked=false;selected=false;children:Element[]=[];
  get value(){return this.storedValue;}
  set value(value:string){this.storedValue=value;}
  parentElement:Element|null=null;moveCount=0;focusCount=0;scrollCount=0;
  private classes=new Set<string>();
  classList={toggle:(name:string,force?:boolean)=>{const enabled=force??!this.classes.has(name);if(enabled)this.classes.add(name);else this.classes.delete(name);return enabled;},contains:(name:string)=>this.classes.has(name)};
  private handlers=new Map<string,Array<(event:any)=>void>>();
  addEventListener(name:string,action:(event:any)=>void){this.handlers.set(name,[...(this.handlers.get(name)||[]),action]);}
  emit(name:string){for(const action of this.handlers.get(name)||[])action({preventDefault:()=>{}});}
  click(){if(!this.disabled)this.emit('click');}
  append(...children:Element[]){for(const child of children){if(child.parentElement)child.parentElement.children=child.parentElement.children.filter(item=>item!==child);child.parentElement=this;child.moveCount++;this.children.push(child);}}
  replaceChildren(...children:Element[]){for(const child of this.children)child.parentElement=null;this.children=[];this.append(...children);}
  contains(child:Element|null):boolean{return child===this||this.children.some(item=>item.contains(child));}
  focus(){this.focusCount++;(document as unknown as {activeElement:Element|null}).activeElement=this;}
  scrollIntoView(){this.scrollCount++;}
}
class InputElement extends Element {}
class SelectElement extends Element {
  multiple=false;
  override get value(){return this.children.find(child=>child.selected)?.value??this.children[0]?.value??'';}
  override set value(value:string){for(const child of this.children)child.selected=child.value===value;}
  get selectedOptions(){return this.children.filter(child=>child.selected);}
}
const flush=async()=>{for(let i=0;i<6;i++)await new Promise<void>(resolve=>setImmediate(resolve));};
const idle=():ManagedRelayStatus=>({state:'idle',message:'계정 연결 전',teams:[],canResume:false,verified:false,sourceOnline:false});
const approvalUrl='https://vercel.com/oauth/device?user_code=SYNTHETIC-ONLY';

async function fixture(initial=idle(),options:{catalog?:RelayCatalog;plans?:RelayPlan[];allowProvision?:boolean;engine?:EngineState;deferConnection?:boolean;deferUpdate?:boolean;fakePolling?:boolean}={}) {
  const html=await readFile(new URL('../src/renderer/index.html',import.meta.url),'utf8');
  // 실제 HTML의 계층을 읽어 패널 위치를 검증한다. 화면 ID의 부모를 시험에 따로 만들지 않는다.
  const nodes=new Map<string,Element>(),stack:Array<{tag:string;node:Element}>=[];
  const voidTags=new Set(['area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr']);
  for(const match of html.matchAll(/<\/?([a-z][a-z0-9-]*)\b[^>]*>/gi)) {
    const tag=match[1].toLowerCase(),raw=match[0];
    if(raw.startsWith('</')){const index=stack.map(item=>item.tag).lastIndexOf(tag);if(index>=0)stack.length=index;continue;}
    const node=tag==='select'?new SelectElement():tag==='input'?new InputElement():new Element(),id=/\bid="([^"]+)"/.exec(raw)?.[1];
    if(id){assert.equal(nodes.has(id),false,`중복 ID ${id}`);nodes.set(id,node);}
    stack.at(-1)?.node.append(node);
    if(!voidTags.has(tag)&&!raw.endsWith('/>'))stack.push({tag,node});
  }
  const get=(id:string)=>{const node=nodes.get(id);assert.ok(node,`화면에 ${id}가 있어야 함`);return node;};
  const previousDocument=Object.getOwnPropertyDescriptor(globalThis,'document'),previousWindow=Object.getOwnPropertyDescriptor(globalThis,'window'),previousInput=Object.getOwnPropertyDescriptor(globalThis,'HTMLInputElement'),previousSelect=Object.getOwnPropertyDescriptor(globalThis,'HTMLSelectElement');
  const unload:Array<()=>void>=[];
  Object.defineProperty(globalThis,'document',{configurable:true,value:{getElementById:get,createElement:(tag:string)=>tag==='select'?new SelectElement():tag==='input'?new InputElement():new Element(),activeElement:null}});
  Object.defineProperty(globalThis,'window',{configurable:true,value:{addEventListener:(name:string,action:()=>void)=>{if(name==='beforeunload')unload.push(action);}}});
  Object.defineProperty(globalThis,'HTMLInputElement',{configurable:true,value:InputElement});
  Object.defineProperty(globalThis,'HTMLSelectElement',{configurable:true,value:SelectElement});
  const previousSetTimeout=Object.getOwnPropertyDescriptor(globalThis,'setTimeout'),previousClearTimeout=Object.getOwnPropertyDescriptor(globalThis,'clearTimeout');
  const scheduledPolls=new Map<number,()=>void>();let nextPoll=0;
  if(options.fakePolling){
    Object.defineProperty(globalThis,'setTimeout',{configurable:true,value:(callback:()=>void,delay:number)=>{if(delay!==1500)throw Error('시험에 예상하지 않은 타이머');const id=++nextPoll;scheduledPolls.set(id,callback);return id;}});
    Object.defineProperty(globalThis,'clearTimeout',{configurable:true,value:(id:number)=>scheduledPolls.delete(id)});
  }
  let current=initial,currentEngine:EngineState=options.engine??{state:'stopped',message:''},finishLogin:(value:ManagedRelayStatus)=>void=()=>{},finishCancel:()=>void=()=>{},finishConnection:(value:ManagedRelayStatus)=>void=()=>{},finishUpdate:(value:ManagedRelayStatus)=>void=()=>{},rejectConnection:(error:Error)=>void=()=>{},delayCancel=false,loginCount=0,cancelCount=0,openCount=0,resumeCount=0,connectCount=0,updateCount=0,siteOpenCount=0,statusReadCount=0,settingsChanged=0,openFailure=false;
  const planInputs:Array<Parameters<RelayDesktopApi['relayPlans']>[0]>=[],provisionInputs:RelayProvisionInput[]=[],pricingProviders:Array<'neon'|'vercel'>=[];
  const api:RelayDesktopApi={
    relayStatus:async()=>{statusReadCount++;return current;},
    connectRelayAccount:async()=>{loginCount++;current={...idle(),state:'login',message:'공식 페이지에서 로그인하세요.'};return new Promise(resolve=>{finishLogin=resolve;});},
    cancelRelay:async()=>{cancelCount++;if(delayCancel)await new Promise<void>(resolve=>{finishCancel=resolve;});current=idle();finishLogin(current);return current;},
    openRelayApproval:async()=>{openCount++;if(openFailure)throw Error('승인 페이지를 열지 못했습니다. 다시 열거나 로그인을 취소하세요.');},
    relayCatalog:async()=>options.catalog??{products:[]},relayPlans:async input=>{planInputs.push(structuredClone(input));return options.plans??[];},
    provisionRelay:async input=>{if(!options.allowProvision)throw Error('사이트 생성 금지');provisionInputs.push(structuredClone(input));return current;},
    openRelayPricing:async provider=>{pricingProviders.push(provider);},
    openManagedRelaySite:async()=>{siteOpenCount++;},
    updateManagedRelaySite:async()=>{updateCount++;if(options.deferUpdate)return new Promise(resolve=>{finishUpdate=value=>{current=value;resolve(value);};});return current;},
    connectManagedRelay:async()=>{connectCount++;if(options.deferConnection)return new Promise((resolve,reject)=>{finishConnection=value=>{current=value;resolve(value);};rejectConnection=reject;});return current;},
    resumeRelay:async()=>{resumeCount++;current={...idle(),state:'connected',message:'구성 대기'};return current;},
    verifyManagedRelay:async()=>{throw Error('실제 연결 검증 금지');},
  };
  const pane=initializeRelayWizard(api,{engine:()=>currentEngine,settingsChanged:async()=>{settingsChanged++;}});
  await flush();
  return {get,pane,html,api,
    setStatus:(value:ManagedRelayStatus)=>{current=value;},
    finishLogin:(value:ManagedRelayStatus)=>{current=value;finishLogin(value);},
    setEngine:(value:EngineState)=>{currentEngine=value;pane.updateAvailability();},
    finishConnection:(value:ManagedRelayStatus)=>finishConnection(value),failConnection:(message:string)=>rejectConnection(Error(message)),
    finishUpdate:(value:ManagedRelayStatus)=>finishUpdate(value),
    scheduledPolls:()=>scheduledPolls.size,runPoll:async()=>{const next=scheduledPolls.entries().next().value;assert.ok(next,'상태 조회가 예약되어야 함');scheduledPolls.delete(next[0]);next[1]();await flush();},
    exposeApproval:()=>{current={...current,approvalUrl};},
    failOpening:()=>{openFailure=true;},
    delayCancellation:()=>{delayCancel=true;},completeCancellation:()=>finishCancel(),
    counts:()=>({loginCount,cancelCount,openCount,resumeCount,connectCount,updateCount,siteOpenCount,statusReadCount,settingsChanged}),planInputs,provisionInputs,pricingProviders,
    close:async()=>{finishCancel();finishLogin(current);finishConnection(current);finishUpdate(current);await flush();for(const action of unload)action();for(const [key,previous] of [['document',previousDocument],['window',previousWindow],['HTMLInputElement',previousInput],['HTMLSelectElement',previousSelect],['setTimeout',previousSetTimeout],['clearTimeout',previousClearTimeout]] as const)previous?Object.defineProperty(globalThis,key,previous):Reflect.deleteProperty(globalThis,key);},
  };
}

test('로그인 응답 대기 중 도착한 승인 페이지를 다시 열고 취소 뒤 재시도한다',async()=>{
  const f=await fixture();
  try{
    const panel=f.get('relay-approval-panel'),button=f.get('relay-open-approval');
    assert.equal(panel.parentElement,f.get('relay-configuration-approval-slot'));
    f.get('relay-login').emit('click');f.get('relay-login').emit('click');await flush();
    assert.equal(panel.parentElement,f.get('relay-login-approval-slot'));
    assert.equal(f.get('relay-login-help').parentElement,f.get('relay-login-approval-slot').parentElement);
    const loginSiblings=f.get('relay-login-approval-slot').parentElement!.children;
    assert.ok(loginSiblings.indexOf(f.get('relay-login-help'))<loginSiblings.indexOf(f.get('relay-login-approval-slot')));
    assert.ok(loginSiblings.indexOf(f.get('relay-login-approval-slot'))<loginSiblings.indexOf(f.get('relay-team')));
    assert.equal(f.counts().loginCount,1);assert.equal(f.get('relay-login').disabled,true);
    assert.equal(f.get('relay-cancel').disabled,false);assert.equal(f.get('relay-cancel').textContent,'로그인 취소');
    assert.equal(f.get('relay-login-help').hidden,false);assert.match(f.get('relay-login-help').textContent,/승인 페이지를 준비/);
    f.exposeApproval();await f.pane.refresh();
    assert.equal(f.get('relay-approval-panel').hidden,false);assert.equal(f.get('relay-open-approval').disabled,false);
    assert.equal(f.get('relay-approval-title').textContent,'Vercel 로그인 승인');
    assert.match(f.get('relay-approval-help').textContent,/Vercel 공식 페이지/);
    assert.equal(f.get('relay-approval-help').textContent.includes('요금제'),false);
    assert.equal(f.get('relay-approval-confirm-row').hidden,true);assert.equal(f.get('relay-resume').hidden,true);
    button.focus();const moves=panel.moveCount;await f.pane.refresh();
    assert.equal(panel.moveCount,moves);assert.equal(document.activeElement,button);assert.equal(f.get('relay-open-approval'),button);
    f.get('relay-open-approval').emit('click');await flush();assert.equal(f.counts().openCount,1);
    assert.equal([...['relay-login-help','relay-approval-help','managed-relay-message']].some(id=>f.get(id).textContent.includes(approvalUrl)),false);
    f.get('relay-cancel').emit('click');await flush();
    assert.equal(f.counts().cancelCount,1);assert.equal(f.get('relay-login').disabled,false);
    assert.equal(f.get('relay-approval-panel').hidden,true);assert.equal(f.get('relay-cancel').disabled,true);
    assert.equal(panel.parentElement,f.get('relay-configuration-approval-slot'));
    f.get('relay-login').emit('click');await flush();assert.equal(f.counts().loginCount,2);
    assert.equal(panel.parentElement,f.get('relay-login-approval-slot'));assert.equal(f.get('relay-open-approval'),button);
    assert.equal(f.get('relay-login').disabled,true);assert.equal(f.counts().resumeCount,0);
  }finally{await f.close();}
});

test('브라우저 열기 실패 뒤 승인 페이지 재열기와 로그인 취소가 남는다',async()=>{
  const f=await fixture();
  try{
    f.get('relay-login').emit('click');await flush();f.exposeApproval();await f.pane.refresh();f.failOpening();
    f.get('relay-open-approval').emit('click');await flush();
    assert.match(f.get('managed-relay-message').textContent,/승인 페이지를 열지 못했습니다/);
    assert.equal(f.get('relay-open-approval').disabled,false);assert.equal(f.get('relay-cancel').disabled,false);
    assert.equal(f.get('relay-login').disabled,true);assert.equal(f.counts().resumeCount,0);
    f.get('relay-cancel').emit('click');await flush();assert.equal(f.get('relay-login').disabled,false);
  }finally{await f.close();}
});

test('로그인 취소 응답을 기다리는 동안 승인 페이지를 열지 않고 완료 뒤 새 로그인만 허용한다',async()=>{
  const f=await fixture();
  try{
    f.get('relay-login').click();await flush();f.exposeApproval();await f.pane.refresh();
    assert.equal(f.get('relay-open-approval').disabled,false);f.delayCancellation();
    f.get('relay-cancel').click();await flush();
    assert.equal(f.get('relay-open-approval').disabled,true);assert.equal(f.get('relay-login').disabled,true);
    assert.equal(f.get('relay-cancel').disabled,true);assert.equal(f.get('relay-approval-panel').hidden,false);
    f.get('relay-open-approval').click();f.get('relay-login').click();await flush();
    assert.equal(f.counts().openCount,0);assert.equal(f.counts().loginCount,1);
    f.completeCancellation();await flush();
    assert.equal(f.get('relay-approval-panel').hidden,true);assert.equal(f.get('relay-open-approval').disabled,true);
    assert.equal(f.get('relay-cancel').disabled,true);assert.equal(f.get('relay-resume').disabled,true);
    assert.equal(f.get('relay-login').disabled,false);f.get('relay-login').click();await flush();
    assert.equal(f.counts().loginCount,2);assert.equal(f.counts().openCount,0);
  }finally{await f.close();}
});

test('이미 진행 중인 로그인도 중복 시작을 막고 취소를 제공한다',async()=>{
  const f=await fixture({...idle(),state:'login',approvalUrl,message:'로그인 승인 대기'});
  try{
    assert.equal(f.get('relay-login').disabled,true);assert.equal(f.get('relay-cancel').disabled,false);
    assert.equal(f.get('relay-open-approval').disabled,false);assert.equal(f.get('relay-resume').hidden,true);
    f.get('relay-cancel').emit('click');await flush();assert.equal(f.get('relay-login').disabled,false);
  }finally{await f.close();}
});

test('로그인 실패 뒤 재시도 버튼이 활성화되며 사이트 약관 동의는 따로 요구한다',async()=>{
  const f=await fixture({...idle(),state:'error',message:'로그인을 완료하지 못했습니다.'});
  try{
    assert.equal(f.get('relay-login').textContent,'Vercel 로그인 재시도');assert.equal(f.get('relay-login').disabled,false);
    f.setStatus({...idle(),state:'approval',approvalUrl:'https://vercel.com/integrations/accept-terms/neon',canResume:true,message:'약관 승인 대기'});await f.pane.refresh();
    assert.equal(f.get('managed-relay-state').textContent,'외부 서비스 승인 대기');
    assert.equal(f.get('relay-approval-panel').parentElement,f.get('relay-configuration-approval-slot'));
    assert.equal(f.get('relay-approval-title').textContent,'승인 또는 이어서 구성');
    assert.equal(f.get('relay-approval-confirm-row').hidden,false);assert.equal(f.get('relay-resume').hidden,false);
    assert.match(f.get('relay-approval-help').textContent,/약관·요금제/);
    f.get('relay-resume').emit('click');await flush();assert.equal(f.counts().resumeCount,0);
    assert.match(f.get('managed-relay-message').textContent,/확인란을 선택/);
    f.get('relay-approval-confirm').checked=true;f.get('relay-resume').emit('click');await flush();
    assert.equal(f.counts().resumeCount,1);assert.equal(f.counts().settingsChanged,1);
  }finally{await f.close();}
});

const connected=():ManagedRelayStatus=>({...idle(),state:'connected',teamId:'team_fixture',teams:[{id:'team_fixture',slug:'fixture-team',name:'시험 팀'}]});

test('기본값이 켜진 auth·realtime은 선택 항목에서 빠지고 요금제 조회와 생성에 false를 보낸다',async()=>{
  const f=await fixture(connected(),{catalog:{products:[{id:'neon_fixture',slug:'neon',name:'시험 DB',fields:[
    {key:'region',label:'리전',type:'string',required:true,options:['sin1','syd1'],default:'sin1'},
    {key:'auth',label:'제거할 계정 기능',type:'boolean',required:false,default:true},
    {key:'realtime',label:'제거할 갱신 기능',type:'boolean',required:false,default:true},
    {key:'audit',label:'별도 감사 기능',type:'boolean',required:false,default:true},
    {key:'authentication',label:'이름이 비슷한 다른 항목',type:'boolean',required:false,default:true},
  ]}]},plans:[{id:'plan_fixture',name:'시험 요금제',description:'공식 제공 시험 조건',type:'subscription'}],allowProvision:true});
  try{
    const labels=f.get('relay-product-fields').children.map(wrap=>wrap.children[0].textContent);
    assert.deepEqual(labels,['리전 (필수)','별도 감사 기능','이름이 비슷한 다른 항목']);
    f.get('relay-load-plans').click();await flush();
    const expected={auth:false,realtime:false,region:'sin1',audit:true,authentication:true};
    assert.deepEqual(f.planInputs[0].metadata,expected);
    assert.equal(f.get('relay-plan').value,'');assert.equal(f.get('relay-provision').disabled,true);
    f.get('relay-consent').checked=true;f.get('relay-plan').value='plan_fixture';f.get('relay-plan').emit('change');
    assert.equal(f.get('relay-consent').checked,false);
    f.get('relay-operator-passcode').value='SYNTHETIC-OPERATOR-ONLY';f.get('relay-viewer-passcode').value='SYNTHETIC-VIEWER-ONLY';f.get('relay-consent').checked=true;
    f.get('managed-relay-form').emit('submit');await flush();
    assert.equal(f.provisionInputs.length,1);assert.deepEqual(f.provisionInputs[0].metadata,expected);
    assert.equal(f.get('relay-operator-passcode').value,'');assert.equal(f.get('relay-viewer-passcode').value,'');
  }finally{await f.close();}
});

test('같은 이름의 비 boolean 항목은 제거 대상에 포함하지 않는다',async()=>{
  const f=await fixture(connected(),{catalog:{products:[{id:'product_fixture',slug:'fixture',name:'시험 저장공간',fields:[{key:'auth',label:'문자열 설정',type:'string',required:true,default:'fixture-value'}]}]}});
  try{
    assert.equal(f.get('relay-product-fields').children[0].children[0].textContent,'문자열 설정 (필수)');
    f.get('relay-load-plans').click();await flush();assert.deepEqual(f.planInputs[0].metadata,{auth:'fixture-value'});
  }finally{await f.close();}
});

test('선택한 요금제 설명은 줄바꿈을 유지한 일반 텍스트이며 제공되지 않은 비용을 만들지 않는다',async()=>{
  const description='기본 비용: 미확인\n제공량: 공식 화면에서 확인\n<img src=x onerror=example()>\n무료 범위: 미확인';
  const f=await fixture(connected(),{catalog:{products:[{id:'product_fixture',slug:'neon',name:'시험 DB',fields:[]}]},plans:[{id:'plan_fixture',name:'선택할 요금제',description,type:'subscription'}]});
  try{
    f.get('relay-load-plans').click();await flush();f.get('relay-plan').value='plan_fixture';f.get('relay-plan').emit('change');
    const node=f.get('relay-plan-description');assert.equal(node.textContent,`선택할 요금제\n${description}`);assert.equal(node.children.length,0);
    assert.equal(node.textContent.includes('subscription'),false);assert.equal(f.provisionInputs.length,0);
    assert.match(f.html,/화면에 없는 금액·제공량·초과 조건은 미확인/);assert.match(f.html,/Vercel의 사이트 운영 비용은 별도로/);
  }finally{await f.close();}
});

test('공식 가격 안내는 사용자가 누른 공급자만 열며 가입이나 사이트 생성을 요청하지 않는다',async()=>{
  const f=await fixture();
  try{
    assert.deepEqual(f.pricingProviders,[]);
    f.get('relay-neon-pricing').click();await flush();f.get('relay-vercel-pricing').click();await flush();
    assert.deepEqual(f.pricingProviders,['neon','vercel']);assert.equal(f.counts().loginCount,0);assert.equal(f.counts().resumeCount,0);assert.equal(f.provisionInputs.length,0);
    assert.match(f.html,/<button id="relay-neon-pricing" type="button">/);assert.match(f.html,/<button id="relay-vercel-pricing" type="button">/);
  }finally{await f.close();}
});

const preparedSite=():ManagedRelayStatus=>({...connected(),state:'ready',url:'https://relay-fixture.vercel.app',verified:true,sourceOnline:false,message:'사이트 검증 완료'});

test('엔진이 꺼져 있으면 연결 버튼 한 번으로 시작·확인을 요청하고 진행 중 상태를 계속 조회한다',async()=>{
  const f=await fixture(preparedSite(),{deferConnection:true,fakePolling:true});
  try{
    const button=f.get('relay-verify'),progress=f.get('relay-connection-progress');
    assert.equal(f.get('relay-ready-actions').hidden,false);assert.equal(button.textContent,'기록 엔진 시작 후 연결 확인');assert.equal(button.disabled,false);
    button.focus();button.click();
    assert.equal(button.disabled,true);assert.match(progress.textContent,/기록 엔진을 시작/);assert.equal(f.counts().connectCount,1);
    button.click();assert.equal(f.counts().connectCount,1);
    await flush();assert.ok(f.scheduledPolls()>0);
    f.setEngine({state:'starting',message:'시작 중'});assert.equal(button.textContent,'기록 엔진 시작 중…');assert.match(progress.textContent,/시작이 끝나면/);
    f.setEngine({state:'ready',message:'준비됨'});assert.equal(button.textContent,'현재 PC 연결 확인 중…');assert.match(progress.textContent,/실제 회의 목록 조회/);
    const reads=f.counts().statusReadCount;f.setStatus({...preparedSite(),message:'사이트 로그인 확인 후 현재 PC 응답 대기'});await f.runPoll();
    assert.ok(f.counts().statusReadCount>reads);assert.match(f.get('managed-relay-message').textContent,/현재 PC 응답 대기/);
    f.finishConnection({...preparedSite(),sourceOnline:true,message:'이 PC 연결과 실제 회의 목록 조회를 확인했습니다.'});await flush();
    assert.equal(button.disabled,false);assert.equal(button.textContent,'현재 PC 연결 확인');assert.match(progress.textContent,/현재 PC 연결 확인 완료/);assert.match(progress.textContent,/실제 회의 목록/);
    assert.equal(progress.scrollCount,1);assert.equal(document.activeElement,button);assert.equal(f.counts().settingsChanged,1);
  }finally{await f.close();}
});

test('이전 연결 성공 기록이 있어도 엔진이 꺼지면 현재 연결 완료로 표시하지 않는다',async()=>{
  const saved={...preparedSite(),sourceOnline:true,message:'이전 연결 확인 성공'};
  const f=await fixture(saved,{engine:{state:'ready',message:'준비됨'}});
  try{
    assert.equal(f.get('managed-relay-state').textContent,'현재 PC의 원본 연결 검증 완료');
    f.setEngine({state:'stopped',message:'종료됨'});
    assert.equal(f.get('managed-relay-state').textContent,'사이트 검증 완료 · 현재 PC 연결 대기');assert.match(f.get('relay-connection-progress').textContent,/기록 엔진이 꺼져/);
    assert.equal(f.get('relay-verify').textContent,'기록 엔진 시작 후 연결 확인');assert.equal(saved.sourceOnline,true);
  }finally{await f.close();}
});

test('연결 실패는 버튼 가까이에 표시하고 URL이 있는 오류 상태에서 다시 확인할 수 있다',async()=>{
  const f=await fixture(preparedSite(),{deferConnection:true,engine:{state:'ready',message:'준비됨'}});
  try{
    const progress=f.get('relay-connection-progress'),button=f.get('relay-verify');button.focus();button.click();await flush();
    f.finishConnection({...preparedSite(),state:'error',canResume:true,message:'현재 PC의 회의 목록 응답을 확인하지 못했습니다.'});await flush();
    assert.match(progress.textContent,/회의 목록 응답을 확인하지 못했습니다/);assert.equal(progress.classList.contains('error'),true);assert.equal(button.disabled,false);
    const previousScroll=progress.scrollCount;button.click();await flush();const typing=f.get('relay-viewer-passcode');typing.focus();
    f.failConnection('네트워크 연결 확인에 실패했습니다.');await flush();
    assert.match(progress.textContent,/네트워크 연결 확인에 실패/);assert.equal(document.activeElement,typing);assert.equal(progress.scrollCount,previousScroll);assert.equal(button.disabled,false);
  }finally{await f.close();}
});

test('엔진 전환·사이트 구성·외부 승인 중에는 연결을 시작하지 않는다',async()=>{
  const f=await fixture(preparedSite());
  try{
    const button=f.get('relay-verify');
    for(const state of ['starting','stopping'] as const){f.setEngine({state,message:'전환 중'});assert.equal(button.disabled,true);assert.match(button.textContent,state==='starting'?/시작 중/:/종료 중/);button.click();}
    f.setEngine({state:'stopped',message:'종료됨'});
    for(const state of ['configuring','approval','login'] as const){f.setStatus({...preparedSite(),state});await f.pane.refresh();assert.equal(button.disabled,true);button.click();}
    assert.equal(f.counts().connectCount,0);
    f.setStatus({...preparedSite(),state:'error',canResume:true,message:'다시 확인 필요'});await f.pane.refresh();assert.equal(button.disabled,false);
  }finally{await f.close();}
});

test('준비된 주소가 있을 때만 두 사이트 열기 버튼을 제공하며 renderer 주소를 전송하지 않는다',async()=>{
  const f=await fixture();
  try{
    for(const id of ['relay-open-site','relay-open-site-address']){assert.equal(f.get(id).disabled,true);f.get(id).click();}
    f.get('relay-verify').click();assert.equal(f.counts().connectCount,0);assert.equal(f.counts().siteOpenCount,0);
    f.setStatus(preparedSite());await f.pane.refresh();
    assert.equal(f.get('relay-ready-actions').hidden,false);assert.equal(f.get('relay-created-url-row').hidden,false);
    f.get('relay-created-url').value='https://renderer-input.invalid';
    for(const id of ['relay-open-site','relay-open-site-address']){assert.equal(f.get(id).disabled,false);f.get(id).click();await flush();}
    assert.equal(f.counts().siteOpenCount,2);assert.equal(f.counts().loginCount,0);assert.equal(f.counts().connectCount,0);assert.equal(f.provisionInputs.length,0);
    assert.match(f.html,/사이트에서는 직접 정한 운영자 또는 열람자 패스코드로 로그인/);assert.match(f.get('relay-connection-help').textContent,/기록 화면으로 따로 이동할 필요/);
  }finally{await f.close();}
});

test('기존 사이트 업데이트는 명시 버튼으로만 요청하고 엔진 실행·중복 요청과 다른 구성을 막는다',async()=>{
  const f=await fixture(preparedSite(),{deferUpdate:true,fakePolling:true});
  try{
    const button=f.get('relay-update-site');assert.equal(f.counts().updateCount,0);assert.equal(button.disabled,false);
    assert.match(f.html,/기록 엔진을 종료/);assert.match(f.html,/새 배포 주소로 바뀔 수/);assert.match(f.html,/접속 암호는 그대로/);
    f.setEngine({state:'ready',message:'실행 중'});assert.equal(button.disabled,true);button.click();assert.equal(f.counts().updateCount,0);
    f.setEngine({state:'stopped',message:'종료됨'});button.click();button.click();await flush();
    assert.equal(f.counts().updateCount,1);assert.equal(button.disabled,true);assert.equal(button.textContent,'기존 사이트 업데이트 중…');
    assert.match(f.get('relay-connection-progress').textContent,/사이트 코드를 업데이트/);assert.equal(f.get('relay-login').disabled,true);assert.equal(f.get('relay-verify').disabled,true);assert.equal(f.get('relay-cancel').disabled,false);
    f.finishUpdate({...preparedSite(),url:'https://new-fixture.vercel.app',siteUpdate:{state:'succeeded',stage:'completed',version:'0.1.0-preview.14',targetVersion:'0.1.0-preview.14',deployAttempted:true},message:'사이트 주소가 새 배포 주소로 바뀌었습니다. 암호는 그대로입니다.'});await flush();
    assert.equal(button.disabled,false);assert.equal(f.get('relay-created-url').value,'https://new-fixture.vercel.app');assert.match(f.get('relay-site-update-state').textContent,/사이트 코드 업데이트 확인 완료/);
    assert.match(f.get('relay-site-update-state').textContent,/0\.1\.0-preview\.14/);assert.match(f.get('relay-site-update-help').textContent,/원본 연결은.*별도로/);
    assert.equal(f.counts().connectCount,0);assert.equal(f.counts().resumeCount,0);assert.equal(f.provisionInputs.length,0);assert.equal(f.counts().settingsChanged,1);
  }finally{await f.close();}
});

test('업데이트 불명확 결과는 이전 주소와 안내를 유지하고 상태 조회가 자동 재배포하지 않는다',async()=>{
  const f=await fixture(preparedSite(),{deferUpdate:true});
  try{
    f.get('relay-update-site').click();await flush();
    f.finishUpdate({...preparedSite(),state:'error',verified:false,canResume:false,siteUpdate:{state:'unknown',stage:'deploying',deployAttempted:true},message:'업데이트 결과를 확인하지 못했습니다. 이전 주소를 유지했으며 자동 재배포하지 않습니다.'});await flush();
    assert.equal(f.get('relay-created-url').value,preparedSite().url);assert.match(f.get('managed-relay-message').textContent,/업데이트 결과 미확인/);
    await f.pane.refresh();await f.pane.refresh();assert.equal(f.counts().updateCount,1);assert.equal(f.get('relay-update-site').disabled,false);
    assert.equal(f.get('relay-update-site').textContent,'배포 내역 확인 후 다시 업데이트');assert.equal(f.get('relay-approval-panel').hidden,true);
    for(const state of ['starting','stopping'] as const){f.setEngine({state,message:'전환 중'});assert.equal(f.get('relay-update-site').disabled,true);}
    f.setEngine({state:'stopped',message:'종료됨'});f.setStatus({...preparedSite(),state:'approval'});await f.pane.refresh();assert.equal(f.get('relay-update-site').disabled,true);
  }finally{await f.close();}
});

test('URL 없는 미완료 사이트 구성은 약관 승인 안내 없이 기존 설정으로 이어간다',async()=>{
  const f=await fixture({...connected(),state:'error',canResume:true,message:'이전 구성 확인 필요'});
  try{
    assert.equal(f.get('relay-approval-panel').hidden,false);assert.equal(f.get('relay-approval-title').textContent,'사이트 구성 이어가기');
    assert.equal(f.get('relay-open-approval').hidden,true);assert.equal(f.get('relay-open-approval').disabled,true);
    assert.equal(f.get('relay-approval-confirm-row').hidden,true);assert.equal(f.get('relay-resume').hidden,false);assert.equal(f.get('relay-resume').disabled,false);
    assert.equal(f.get('relay-resume').textContent,'이어서 구성');assert.equal(f.get('relay-approval-help').textContent.includes('약관·요금제 승인을 확인'),false);
    f.get('relay-resume').click();await flush();assert.equal(f.counts().resumeCount,1);assert.equal(f.counts().openCount,0);assert.equal(f.provisionInputs.length,0);
  }finally{await f.close();}
});

test('승인 상태 문자열만 있고 실제 승인 주소가 없으면 승인 완료 확인란을 요구하지 않는다',async()=>{
  const f=await fixture({...connected(),state:'approval',canResume:true,message:'미완료 구성'});
  try{
    assert.equal(f.get('managed-relay-state').textContent,'사이트 구성 이어가기');
    assert.equal(f.get('relay-approval-title').textContent,'사이트 구성 이어가기');assert.equal(f.get('relay-approval-confirm-row').hidden,true);
    assert.equal(f.get('relay-open-approval').hidden,true);f.get('relay-resume').click();await flush();assert.equal(f.counts().resumeCount,1);
  }finally{await f.close();}
});

test('이전 업데이트 미확인이 있어도 새 Vercel 로그인 승인과 계정 연결 안내를 가리지 않는다',async()=>{
  const siteUpdate={state:'unknown',stage:'deploying',deployAttempted:true,knownCode:'INTERRUPTED'} as const;
  const initial={...preparedSite(),state:'error',canResume:false,siteUpdate,message:'SYNTHETIC_RAW_DIAGNOSTIC'} as ManagedRelayStatus;
  const f=await fixture(initial,{fakePolling:true});
  try{
    f.get('relay-login').click();await flush();assert.equal(f.counts().loginCount,1);
    f.setStatus({...initial,state:'login',approvalUrl:undefined});await f.pane.refresh();
    assert.equal(f.get('managed-relay-state').textContent,'Vercel 로그인 진행 중');assert.match(f.get('managed-relay-message').textContent,/로그인 승인 페이지를 준비/);
    assert.equal(f.get('relay-login-help').hidden,false);assert.equal(f.get('relay-site-update-state').textContent,'사이트 업데이트 결과 미확인');
    f.setStatus({...initial,state:'login',approvalUrl});await f.pane.refresh();
    assert.equal(f.get('relay-approval-panel').parentElement,f.get('relay-login-approval-slot'));assert.equal(f.get('relay-approval-panel').hidden,false);
    assert.equal(f.get('relay-approval-title').textContent,'Vercel 로그인 승인');assert.match(f.get('relay-approval-help').textContent,/Vercel 공식 페이지/);
    assert.match(f.get('managed-relay-message').textContent,/계정 연결을 승인/);assert.match(f.get('relay-login-help').textContent,/승인 페이지 열기/);
    assert.equal(f.get('managed-relay-message').classList.contains('error'),false);assert.equal(f.get('relay-open-approval').hidden,false);assert.equal(f.get('relay-open-approval').disabled,false);
    assert.equal(f.get('relay-resume').hidden,true);assert.equal(f.get('relay-approval-confirm-row').hidden,true);
    f.get('relay-open-approval').click();await flush();assert.equal(f.counts().openCount,1);assert.equal(f.get('relay-site-update-state').textContent,'사이트 업데이트 결과 미확인');
    f.finishLogin({...preparedSite(),state:'connected',siteUpdate,message:'SYNTHETIC_RAW_DIAGNOSTIC'});await flush();
    assert.equal(f.get('managed-relay-state').textContent,'사이트 계정 연결 · 구성 대기');assert.match(f.get('managed-relay-message').textContent,/Vercel 계정을 연결했습니다/);
    assert.equal(f.get('relay-approval-panel').hidden,true);assert.equal(f.get('relay-site-update-state').textContent,'사이트 업데이트 결과 미확인');
    for(const id of ['managed-relay-message','relay-login-help','relay-approval-help','relay-site-update-state','relay-site-update-help']){
      assert.equal(f.get(id).textContent.includes(approvalUrl),false);assert.equal(f.get(id).textContent.includes('SYNTHETIC_RAW_DIAGNOSTIC'),false);assert.equal(f.get(id).textContent.includes('INTERRUPTED'),false);
    }
    assert.equal(f.counts().resumeCount,0);assert.equal(f.counts().updateCount,0);assert.equal(f.provisionInputs.length,0);
  }finally{await f.close();}
});

test('원본 연결 성공 뒤에도 업데이트 미확인은 남고 잘못된 승인·재개 패널을 열지 않는다',async()=>{
  const update={state:'unknown',stage:'deploying',deployAttempted:true,knownCode:'INTERRUPTED',targetVersion:'0.1.0-preview.14'} as const;
  const unsafe='SYNTHETIC_DIAGNOSTIC_SECRET https://diagnostic.invalid/?token=synthetic';
  const initial={...preparedSite(),state:'error',verified:false,canResume:true,approvalUrl,siteUpdate:update,message:unsafe} as ManagedRelayStatus;
  const f=await fixture(initial,{deferConnection:true,engine:{state:'ready',message:'준비됨'}});
  try{
    assert.equal(f.get('relay-approval-panel').hidden,true);assert.equal(f.get('relay-resume').disabled,true);assert.equal(f.get('relay-resume').hidden,true);
    assert.equal(f.get('relay-open-approval').disabled,true);f.get('relay-resume').emit('click');f.get('relay-open-approval').click();await flush();
    assert.equal(f.counts().resumeCount,0);assert.equal(f.counts().openCount,0);
    const button=f.get('relay-verify');button.click();await flush();assert.equal(f.counts().connectCount,1);
    f.finishConnection({...initial,state:'ready',verified:true,sourceOnline:true,canResume:false});await flush();
    assert.equal(f.get('managed-relay-state').textContent,'현재 PC의 원본 연결 검증 완료');
    assert.match(f.get('managed-relay-message').textContent,/현재 PC의 원본 연결과 실제 회의 목록 조회를 확인/);
    assert.match(f.get('relay-connection-progress').textContent,/현재 PC 연결 확인 완료/);
    assert.equal(f.get('relay-site-update-state').textContent,'사이트 업데이트 결과 미확인');assert.equal(f.get('relay-site-update-state').classList.contains('warning'),true);assert.match(f.get('relay-site-update-help').textContent,/공식 배포 내역/);
    assert.match(f.get('relay-site-update-help').textContent,/자동으로 재배포하지 않습니다/);assert.equal(f.get('relay-update-site').disabled,true);
    for(const id of ['managed-relay-message','relay-connection-progress','relay-site-update-state','relay-site-update-help']){
      assert.equal(f.get(id).textContent.includes(unsafe),false);assert.equal(f.get(id).textContent.includes('INTERRUPTED'),false);assert.equal(f.get(id).textContent.includes('deploying'),false);
    }
    await f.pane.refresh();await f.pane.refresh();assert.equal(f.get('relay-site-update-state').textContent,'사이트 업데이트 결과 미확인');assert.equal(f.get('relay-approval-panel').hidden,true);
    f.setEngine({state:'stopped',message:'종료됨'});assert.equal(f.get('relay-update-site').disabled,false);assert.equal(f.get('relay-update-site').textContent,'배포 내역 확인 후 다시 업데이트');
    assert.equal(f.counts().updateCount,0);assert.equal(f.provisionInputs.length,0);
    f.get('relay-update-site').click();await flush();assert.equal(f.counts().updateCount,1);assert.equal(f.counts().resumeCount,0);assert.equal(f.counts().openCount,0);
    assert.equal(f.get('relay-site-update-state').textContent,'사이트 업데이트 결과 미확인');assert.equal(f.get('relay-approval-panel').hidden,true);
  }finally{await f.close();}
});

test('완료 실패·취소 상태도 구성 재개나 약관 승인으로 바꾸지 않는다',async()=>{
  const f=await fixture(preparedSite());
  try{
    for(const [state,label] of [['failed','사이트 업데이트 완료되지 않음'],['cancelled','사이트 업데이트 취소됨']] as const){
      f.setStatus({...preparedSite(),state:'error',approvalUrl,canResume:true,siteUpdate:{state,stage:'preparing',deployAttempted:false},message:'SYNTHETIC_RAW_DIAGNOSTIC'});await f.pane.refresh();
      assert.equal(f.get('relay-site-update-state').textContent,label);assert.equal(f.get('relay-approval-panel').hidden,true);assert.equal(f.get('relay-resume').disabled,true);
      assert.equal(f.get('relay-open-approval').disabled,true);assert.equal(f.get('managed-relay-message').textContent.includes('SYNTHETIC_RAW_DIAGNOSTIC'),false);
      assert.equal(f.get('relay-update-site').disabled,false);assert.match(f.get('relay-site-update-help').textContent,/원본 연결은 별도로/);
    }
    assert.equal(f.counts().resumeCount,0);assert.equal(f.counts().openCount,0);assert.equal(f.counts().updateCount,0);
  }finally{await f.close();}
});

test('기존 연결 검증만 있거나 잘못된 버전 값이면 사이트 코드 버전은 미확인으로 표시한다',async()=>{
  const f=await fixture({...preparedSite(),sourceOnline:true},{engine:{state:'ready',message:'준비됨'}});
  try{
    assert.equal(f.get('managed-relay-state').textContent,'현재 PC의 원본 연결 검증 완료');assert.equal(f.get('relay-site-update-state').textContent,'사이트 코드 버전 미확인');
    const cases:Array<ManagedRelayStatus['siteUpdate']>=[
      {state:'unverified',stage:'unverified'},
      {state:'succeeded',stage:'completed'},
      {state:'succeeded',stage:'completed',version:'0.1.0-preview.14'},
      {state:'succeeded',stage:'verifying',version:'0.1.0-preview.14'},
      {state:'succeeded',stage:'completed',version:'<img src=x onerror=synthetic()>',targetVersion:'<img src=x onerror=synthetic()>'},
      {state:'succeeded',stage:'completed',version:'0.1.0-preview.13',targetVersion:'0.1.0-preview.14'},
    ];
    for(const siteUpdate of cases){
      f.setStatus({...preparedSite(),sourceOnline:true,siteUpdate});await f.pane.refresh();
      assert.equal(f.get('relay-site-update-state').textContent,'사이트 코드 버전 미확인');assert.equal(f.get('relay-site-update-state').children.length,0);
      assert.equal(f.get('relay-site-update-state').textContent.includes('<img'),false);assert.match(f.get('relay-site-update-help').textContent,/최신이라는 뜻은 아닙니다/);
    }
    assert.equal(f.counts().updateCount,0);assert.equal(f.counts().connectCount,0);assert.equal(f.counts().resumeCount,0);
  }finally{await f.close();}
});

test('사이트 업데이트 단계는 직원용 고정 문구이며 진단 코드나 원격 오류를 노출하지 않는다',async()=>{
  const f=await fixture(preparedSite(),{fakePolling:true});
  try{
    const stages={preparing:'사이트 파일 준비 중',deploying:'사이트 배포 중',binding:'사이트 주소 연결 중',verifying:'사이트 로그인·저장공간 확인 중',saving:'확인된 사이트 주소 저장 중'} as const;
    for(const [stage,label] of Object.entries(stages)){
      f.setStatus({...preparedSite(),state:'configuring',siteUpdate:{state:'updating',stage:stage as keyof typeof stages,knownCode:'SYNTHETIC_RAW_DIAGNOSTIC' as never,httpStatus:503,exitCode:17},message:'SYNTHETIC_RAW_DIAGNOSTIC'});await f.pane.refresh();
      assert.equal(f.get('relay-site-update-state').textContent,label);assert.equal(f.get('relay-update-site').disabled,true);assert.equal(f.get('relay-verify').disabled,true);
      assert.equal(f.get('relay-approval-panel').hidden,true);assert.equal(f.get('managed-relay-message').textContent.includes('SYNTHETIC_RAW_DIAGNOSTIC'),false);
      assert.equal(f.get('relay-site-update-help').textContent.includes(stage),false);assert.equal(f.get('relay-site-update-help').textContent.includes('503'),false);
    }
    f.setStatus({...preparedSite(),siteUpdate:{state:'updating',stage:'constructor' as never},message:'SYNTHETIC_RAW_DIAGNOSTIC'});await f.pane.refresh();
    assert.equal(f.get('relay-site-update-state').textContent,'사이트 업데이트 진행 중');assert.equal(f.get('relay-update-site').disabled,true);
    assert.equal(f.counts().updateCount,0);assert.equal(f.counts().resumeCount,0);assert.equal(f.counts().loginCount,0);
  }finally{await f.close();}
});

test('업데이트 예외는 원격 출력 대신 고정 안내를 보여주고 자동으로 재요청하지 않는다',async()=>{
  const f=await fixture(preparedSite());let attempted=0;
  try{
    f.api.updateManagedRelaySite=async()=>{attempted++;throw Error('SYNTHETIC_RAW_DIAGNOSTIC https://failure.invalid/?token=synthetic');};
    f.get('relay-update-site').click();await flush();
    assert.equal(attempted,1);assert.match(f.get('managed-relay-message').textContent,/업데이트를 완료하지 못했습니다/);
    assert.equal(f.get('managed-relay-message').textContent.includes('SYNTHETIC_RAW_DIAGNOSTIC'),false);assert.equal(f.get('managed-relay-message').textContent.includes('https://failure'),false);
    await f.pane.refresh();await f.pane.refresh();assert.equal(attempted,1);assert.equal(f.get('relay-created-url').value,preparedSite().url);assert.equal(f.counts().resumeCount,0);
  }finally{await f.close();}
});
