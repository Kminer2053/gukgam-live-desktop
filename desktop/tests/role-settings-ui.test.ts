import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {initializeRoleSettings} from '../src/renderer/role-settings';
import type {RoleDesktopApi,RoleSettingsStatus} from '../src/role/contracts';

class Element{
  value='';textContent='';hidden=false;disabled=false;
  classList={toggle:()=>{}};
  private handlers=new Map<string,Array<()=>void>>();
  addEventListener(event:string,action:()=>void){this.handlers.set(event,[...(this.handlers.get(event)||[]),action]);}
  emit(event:string){for(const action of this.handlers.get(event)||[])action();}
}
const flush=async()=>{for(let i=0;i<5;i++)await new Promise<void>(resolve=>setImmediate(resolve));};

test('엔진이 꺼져도 Jev 키 입력을 찾고 저장하며 상태 갱신이 미저장 선택을 덮지 않는다',async()=>{
  const html=await readFile(new URL('../src/renderer/index.html',import.meta.url),'utf8');
  const nodes=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match=>[match[1],new Element()]));
  const get=(id:string)=>nodes.get(id)!;get('role-provider').value='none';get('role-threads').value='2';
  const beforeDocument=Object.getOwnPropertyDescriptor(globalThis,'document'),beforeWindow=Object.getOwnPropertyDescriptor(globalThis,'window');
  const unload:Array<()=>void>=[];
  Object.defineProperty(globalThis,'document',{configurable:true,value:{getElementById:get,activeElement:null,visibilityState:'visible'}});
  Object.defineProperty(globalThis,'window',{configurable:true,value:{addEventListener:(event:string,action:()=>void)=>{if(event==='beforeunload')unload.push(action);}}});
  let state='stopped',key='',prepared=0;const calls:unknown[]=[];
  const status=():RoleSettingsStatus=>({hasKey:!!key,engineReady:state==='ready',message:'',selectedProvider:state==='ready'?'none':null,
    threads:2,enabled:false,autoBlock:false,preparing:false,model:{state:'unavailable',message:'준비 전',available:false},worker:{state:'idle',message:'미기동',available:false}});
  const api:RoleDesktopApi={roleSettingsStatus:async()=>status(),saveJevKey:async value=>{key=value;calls.push('save');return {hasKey:true};},deleteJevKey:async()=>({hasKey:false}),
    saveRoleSettings:async value=>{calls.push(value);return status();},prepareJulia:async()=>{prepared++;return status();},cancelJulia:async()=>status(),openRoleSettingsPage:async()=>{}};
  try{
    const pane=initializeRoleSettings(api,{engine:()=>({state:state as any,message:''}),visible:()=>true});
    await pane.refresh(true);assert.equal(get('role-provider').disabled,false);
    get('role-provider').value='jev';get('role-provider').emit('change');
    assert.equal(get('role-jev-panel').hidden,false);assert.equal(get('jev-key').disabled,false);
    get('jev-key').value='SYNTHETIC_ONLY';get('jev-key-save').emit('click');
    assert.equal(get('jev-key').value,'');await flush();
    assert.equal(key,'SYNTHETIC_ONLY');assert.equal(get('role-provider').value,'jev');
    assert.equal(get('role-key-state').textContent.includes(key),false);assert.equal(prepared,0);
    state='ready';await pane.refresh();pane.updateAvailability();
    assert.equal(get('role-provider').value,'jev');assert.equal(get('jev-key-save').disabled,true);
    get('role-provider').value='julia';get('role-provider').emit('change');await pane.refresh();
    assert.equal(get('role-provider').value,'julia');assert.equal(prepared,0);
    get('role-julia-prepare').emit('click');await flush();assert.equal(prepared,1);
    assert.equal(html.includes('data-step-target="4">추가 판정 모델 · Jev/Julia'),true);
  }finally{
    for(const action of unload)action();
    beforeDocument?Object.defineProperty(globalThis,'document',beforeDocument):Reflect.deleteProperty(globalThis,'document');
    beforeWindow?Object.defineProperty(globalThis,'window',beforeWindow):Reflect.deleteProperty(globalThis,'window');
  }
});
