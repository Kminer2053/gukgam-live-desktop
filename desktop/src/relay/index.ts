import {spawn} from 'node:child_process';
import {join} from 'node:path';
import {StringDecoder} from 'node:string_decoder';
import type {SetupStore} from '../storage/settings';
import type {ChatGPTVault} from '../auth/chatgpt';
import {buildLaunch} from '../main/engine';
import {VercelCliRunner} from './vercel-cli';
import {RelayProvisioner} from './provisioner';
import type {SiteUpdateStatus} from './contracts';

type Options={userData:string;templateDir:string;settings:SetupStore;vault:ChatGPTVault;openExternal:(url:string)=>Promise<unknown>;resourcesPath:string;projectRoot:string;packaged:boolean;engineIsReady?:()=>boolean;readRelayConnectionId?:()=>Promise<string|undefined>;version?:string;onSiteUpdate?:(value:SiteUpdateStatus)=>void};
let unconfirmedInitializer=false;
/** DB 준비는 포트를 열지 않으며 연결 정보는 표준입력으로만 전달한다. */
export function initializeRelayDatabase(options:Pick<Options,'userData'|'resourcesPath'|'projectRoot'|'packaged'>,dsn:string,signal?:AbortSignal):Promise<void> {
 return new Promise((resolve,reject)=>{
  if(unconfirmedInitializer){reject(Error('이전 DB 준비 도구의 종료를 확인하지 못했습니다. 앱을 다시 실행하세요.'));return;}
  if(signal?.aborted){reject(Error('릴레이 DB 준비를 취소했습니다.'));return;}
  const launch=buildLaunch(options,'unused');
  launch.args=options.packaged?['--relay-init']:['-m','gukgam.desktop_runtime','--relay-init'];
  const child=spawn(launch.executable,launch.args,{cwd:launch.cwd,env:launch.env,windowsHide:true,shell:false,stdio:['pipe','pipe','pipe']});
  let output='',finished=false,closed=false,pending:Error|undefined,force:ReturnType<typeof setTimeout>|undefined,abandon:ReturnType<typeof setTimeout>|undefined;
  const decoder=new StringDecoder('utf8');
  const finish=(error?:Error)=>{if(finished)return;finished=true;clearTimeout(timer);clearTimeout(force);clearTimeout(abandon);signal?.removeEventListener('abort',cancel);error?reject(error):resolve();};
  const terminate=(error:Error)=>{if(pending||finished)return;pending=error;child.kill('SIGTERM');if(closed)return;force=setTimeout(()=>{if(closed)return;child.kill('SIGKILL');if(closed)return;abandon=setTimeout(()=>{if(!closed){unconfirmedInitializer=true;finish(Error('DB 준비 도구의 종료를 확인하지 못했습니다. 앱을 다시 실행하세요.'));}},2000);},2000);};
  const cancel=()=>terminate(Error('릴레이 DB 준비를 취소했습니다.'));
  const timer=setTimeout(()=>terminate(Error('릴레이 DB 준비 시간이 지났습니다. DB 연결 상태를 확인하세요.')),30_000);
  child.stdin.on('error',()=>{});child.stdout.on('data',(chunk:Buffer)=>{output+=decoder.write(chunk);if(output.length>65536)terminate(Error('DB 준비 응답이 너무 큽니다.'));});
  // 드라이버 예외에는 DB URL이 들어갈 수 있어 진단 출력은 이 경로에서 수집하지 않는다.
  child.stderr.resume();child.once('error',()=>finish(Error('동봉한 DB 준비 도구를 실행하지 못했습니다.')));
  child.once('close',code=>{closed=true;output+=decoder.end();if(pending){finish(pending);return;}try{const data=JSON.parse(output.trim());if(code!==0||data?.type!=='relay.initialized'||data.ok!==true)throw Error();finish();}catch{finish(Error('릴레이 DB 준비에 실패했습니다. DB 연결과 권한을 확인하세요.'));}});
  signal?.addEventListener('abort',cancel,{once:true});if(signal?.aborted){cancel();return;}
  child.stdin.end(JSON.stringify({databaseUrl:dsn})+'\n');
 });
}
export function createManagedRelay(options:Options) {
 const cliPath=options.packaged?join(options.resourcesPath,'relay-cli','node_modules','vercel','dist','vc.js'):join(options.projectRoot,'desktop','relay-cli','node_modules','vercel','dist','vc.js');
 const cli=new VercelCliRunner({cliPath,tempRoot:join(options.userData,'relay','private-cli'),vault:options.vault});
 return new RelayProvisioner({...options,cli,expectedConnection:async()=>{
  if(!options.engineIsReady?.())return undefined;
  try{const value=await options.readRelayConnectionId?.();return typeof value==='string'&&/^[a-f0-9]{32}$/.test(value)?value:undefined;}catch{return undefined;}
 },initializeDatabase:(dsn,signal)=>initializeRelayDatabase(options,dsn,signal)});
}
