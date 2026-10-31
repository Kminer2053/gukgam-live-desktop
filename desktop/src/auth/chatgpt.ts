import {randomUUID} from 'node:crypto';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {join} from 'node:path';
import {createRemoteJWKSet, jwtVerify} from 'jose';
import {loopbackCallback, oauthProof} from './loopback';
import {readResponseStream, aiError} from '../ai/stream';
import type {AiRequestStatus,ChatGPTConnectionHealth} from '../shared/contracts';
import type {RequestDiagnostic} from '../main/diagnostics';

export interface ChatGPTVault {
  get<T>(key: string): Promise<T | undefined>;
  set(key: string, value: unknown): Promise<void>;
  delete(key: string): Promise<void>;
}
type Credential = {clientId:string; subject:string; email?:string; accessToken:string; refreshToken?:string;
  idToken:string; scopes:string[]; expiresAt:number; verified:boolean; models:string[]};
type TokenResponse = {id_token?:string; access_token?:string; refresh_token?:string; scope?:string; expires_in?:number};
type Operation = {epoch:number; signal:AbortSignal};
type Identity = {sub?:string; nonce?:unknown; email?:unknown};
export interface AiRequestClock {
  now():number;
  setTimeout(callback:()=>void,delayMs:number):unknown;
  clearTimeout(handle:unknown):void;
}
type Options = {fetch?:typeof fetch; callback?:typeof loopbackCallback;
  verifyIdentity?:(token:string, clientId:string) => Promise<Identity>;
  requestTimeouts?:{totalMs?:number;idleMs?:number};requestClock?:AiRequestClock;
  onRequestStatus?:(status:RequestDiagnostic)=>void|Promise<void>};
type ActiveRequest={status:AiRequestStatus;controller:AbortController;signal:AbortSignal};
const tokenEndpoint = 'https://auth.openai.com/api/accounts/oauth/token';
const resource = 'https://api.openai.com/v1';
const jwks = createRemoteJWKSet(new URL('https://auth.openai.com/.well-known/jwks.json'), {timeoutDuration:15_000});
class CancelledAuthentication extends Error {constructor(){super('ChatGPT 요청이 취소되었습니다.');this.name='AbortError';}}
class TokenFailure extends Error {constructor(message:string, readonly terminal:boolean){super(message);}}
class AiRequestTimeout extends Error {
  constructor(readonly code:'AI_RESPONSE_IDLE_TIMEOUT'|'AI_REQUEST_TOTAL_TIMEOUT'){
    super(code==='AI_RESPONSE_IDLE_TIMEOUT'?'ChatGPT에서 3분 동안 응답이 오지 않아 요청을 중단했습니다. 연결 상태를 확인한 뒤 다시 시도하세요.':'AI 요청이 최대 대기 시간 10분을 넘었습니다. 원문을 짧게 나누어 직접 다시 시도하세요.');
    this.name='TimeoutError';
  }
}
function waitForAbort<T>(promise:Promise<T>,signal:AbortSignal):Promise<T>{
  if(signal.aborted){void promise.catch(()=>{});return Promise.reject(signal.reason);}
  return new Promise((resolve,reject)=>{
    const aborted=()=>{signal.removeEventListener('abort',aborted);reject(signal.reason);};
    signal.addEventListener('abort',aborted,{once:true});
    promise.then(value=>{signal.removeEventListener('abort',aborted);resolve(value);},error=>{signal.removeEventListener('abort',aborted);reject(error);});
  });
}

/** 계정 정보는 메인 프로세스에 두고 화면에는 연결 요약만 전달한다. */
export class ChatGPTService {
  private pending = false;
  private diagnosing = false;
  private disconnecting = false;
  private epoch = 0;
  private lifetime = new AbortController();
  private refreshing?:{epoch:number; signal:AbortSignal; promise:Promise<Credential>};
  private closePending?:() => void;
  private writes:Promise<void> = Promise.resolve();
  private message = 'ChatGPT 로그인 대기';
  private latestRequest?:AiRequestStatus;
  private latestRequestEpoch = -1;
  private connectionFailureEpoch = -1;
  private activeRequest?:ActiveRequest;
  private readonly requestClock:AiRequestClock;
  private readonly requestTimeouts:{totalMs:number;idleMs:number};
  private readonly fetcher:typeof fetch;
  private readonly callbackFactory:typeof loopbackCallback;
  private readonly verifyIdentity:(token:string,clientId:string) => Promise<Identity>;
  private readonly onRequestStatus?:Options['onRequestStatus'];
  constructor(private vault:ChatGPTVault, private dataDir:string, private openExternal:(url:string) => Promise<void>, options:Options = {}) {
    this.fetcher=options.fetch ?? fetch;
    this.callbackFactory=options.callback ?? loopbackCallback;
    this.onRequestStatus=options.onRequestStatus;
    this.requestClock=options.requestClock??{now:()=>Date.now(),setTimeout:(callback,delay)=>setTimeout(callback,delay),clearTimeout:handle=>clearTimeout(handle as ReturnType<typeof setTimeout>)};
    this.requestTimeouts={totalMs:options.requestTimeouts?.totalMs??600_000,idleMs:options.requestTimeouts?.idleMs??180_000};
    if(Object.values(this.requestTimeouts).some(value=>!Number.isFinite(value)||value<=0))throw Error('AI 요청 대기 시간 설정을 확인하세요.');
    this.verifyIdentity=options.verifyIdentity ?? (async (token,clientId) => {
      const {payload}=await jwtVerify(token,jwks,{issuer:'https://auth.openai.com',audience:clientId,
        requiredClaims:['sub','exp','iat'],clockTolerance:5});
      return payload;
    });
  }
  private operation():Operation {
    if(this.disconnecting) throw new CancelledAuthentication();
    return {epoch:this.epoch,signal:this.lifetime.signal};
  }
  private check(op:Operation) {if(op.signal.aborted)throw op.signal.reason??new CancelledAuthentication();if(op.epoch !== this.epoch || this.disconnecting) throw new CancelledAuthentication();}
  private signal(op:Operation,timeout:number) {this.check(op);return AbortSignal.any([op.signal,AbortSignal.timeout(timeout)]);}
  private async saveCredential(op:Operation, update:(current:Credential|undefined) => Credential|undefined) {
    const write=this.writes.catch(()=>{}).then(async()=>{
      this.check(op);
      const current=await this.vault.get<Credential>('chatgpt');this.check(op);
      const next=update(current);
      if(next) await this.vault.set('chatgpt',next);
      else await this.vault.delete('chatgpt');
      this.check(op);
    });
    this.writes=write.catch(()=>{});
    await write;
  }
  private matching(current:Credential|undefined, selected:Credential) {
    if(!current || current.clientId!==selected.clientId || current.subject!==selected.subject) throw new CancelledAuthentication();
    return current;
  }
  private friendly(error:unknown,op:Operation):Error {
    if(op.signal.aborted&&op.signal.reason instanceof Error&&op.signal.reason.name==='TimeoutError')return op.signal.reason instanceof AiRequestTimeout?op.signal.reason:Error('ChatGPT 응답 대기 시간이 지났습니다. 다시 시도하세요.');
    if(op.epoch!==this.epoch || op.signal.aborted || error instanceof CancelledAuthentication) return new CancelledAuthentication();
    if(error instanceof Error && error.name==='TimeoutError') return error instanceof AiRequestTimeout?error:Error('ChatGPT 응답 대기 시간이 지났습니다. 다시 시도하세요.');
    if(error instanceof Error && error.name==='AbortError')return new CancelledAuthentication();
    if(error instanceof TypeError) return Error('네트워크에 연결할 수 없습니다. 기존 회의 기록은 그대로 남아 있습니다.');
    if(error instanceof Error && /[가-힣]/.test(error.message))return error;
    return Error('ChatGPT 연결 또는 응답을 확인하지 못했습니다. 다시 연결하거나 요청을 재시도하세요.');
  }
  async status() {
    const c=await this.vault.get<Credential>('chatgpt').catch(()=>undefined);
    return {connected:Boolean(c),verified:Boolean(c?.verified),
      message:c && this.message==='ChatGPT 로그인 대기' ? (c.verified?'ChatGPT 연결됨 / AI 요청 시험 성공':'ChatGPT 연결됨 / 구독 사용 승인·AI 요청 시험 필요') : this.message,
      models:c?.models||[],account:c?.email};
  }
  async connectionHealth():Promise<ChatGPTConnectionHealth> {
    // 로컬 보관 상태만 읽는다. 이 조회는 갱신·모델 목록·AI 시험을 실행하지 않는다.
    const epoch=this.epoch;
    let c:Credential|undefined;
    try {c=await this.vault.get<Credential>('chatgpt');}
    catch {return {state:'unavailable'};}
    if(epoch!==this.epoch||this.pending||this.disconnecting||this.diagnosing)return {state:'checking'};
    if(!c)return {state:'disconnected'};
    const currentRequest=this.latestRequestEpoch===epoch?this.latestRequest:undefined;
    if(this.connectionFailureEpoch===epoch||currentRequest?.state==='failed')return {state:'error'};
    if(!Array.isArray(c.scopes)||!c.scopes.includes('chatgpt.tokens.use.direct'))return {state:'unverified'};
    const verified=c.verified===true||currentRequest?.state==='complete';
    if(!Number.isFinite(c.expiresAt))return {state:'expired'};
    if(c.expiresAt<=Date.now()) {
      const refreshable=typeof c.refreshToken==='string'&&c.refreshToken.length>0&&!/[\x00-\x20\x7f]/.test(c.refreshToken);
      // 갱신 수단 보관은 성공이 아니다. 실제 갱신은 다음 사용자가 요청한 작업에 맡긴다.
      return {state:verified&&refreshable?'refreshable':'expired'};
    }
    return {state:verified?'verified':'unverified'};
  }
  private async hostId() {
    const file=join(this.dataDir,'host-id');
    try{return await readFile(file,'utf8');}
    catch(error){
      if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;
      await mkdir(this.dataDir,{recursive:true,mode:0o700});
      const id=`urn:uuid:${randomUUID()}`;await writeFile(file,id,{mode:0o600});return id;
    }
  }
  async connect() {
    if(this.pending || this.disconnecting)throw Error('로그인이 이미 진행 중입니다.');
    this.pending=true;
    this.cancel(); // 이전 갱신·AI 요청과 새 로그인이 겹치지 않게 한다.
    const op=this.operation();
    let callback:Awaited<ReturnType<typeof loopbackCallback>>|undefined;
    try{
      const previous=await this.vault.get<Credential>('chatgpt');this.check(op);
      const proof=oauthProof();
      callback=await this.callbackFactory(proof.state);this.check(op);
      this.closePending=callback.close;
      const hostId=await this.hostId();this.check(op);
      const url=new URL('https://auth.openai.com/api/accounts/authorize');
      url.search=new URLSearchParams({client_id:previous?.clientId||'dynamic_agent_client',ext_agent_host_id:hostId,
        response_type:'code',redirect_uri:callback.redirectUri,scope:'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct',
        resource,state:proof.state,nonce:proof.nonce,code_challenge_method:'S256',code_challenge:proof.challenge}).toString();
      if(previous?.idToken)url.searchParams.set('id_token_hint',previous.idToken);
      if(!previous)url.searchParams.set('agent_name_hint','국감 라이브 데스크톱');
      this.message='브라우저에서 ChatGPT 로그인과 구독 사용 동의를 완료하세요';
      try{await this.openExternal(url.toString());}
      catch{throw Error('공식 로그인 브라우저를 열지 못했습니다. 기본 브라우저 설정을 확인하세요.');}
      this.check(op);
      const parameters=await callback.result;this.check(op);
      if(parameters.get('error'))throw Error(parameters.get('error')==='access_denied'?'ChatGPT 연결 동의가 취소되었습니다':'ChatGPT 로그인에 실패했습니다');
      const clientId=parameters.get('client_id')||previous?.clientId;
      if(!clientId||clientId==='dynamic_agent_client'||(previous&&clientId!==previous.clientId))throw Error('올바른 앱 등록 정보를 받지 못했습니다');
      const code=parameters.get('code');if(!code)throw Error('로그인 코드가 없습니다');
      const tokens=await this.tokenRequest({grant_type:'authorization_code',client_id:clientId,code,code_verifier:proof.verifier,
        redirect_uri:callback.redirectUri,resource},op);
      if(typeof tokens.id_token!=='string')throw Error('계정 확인 토큰이 없습니다');
      let identity:Identity;
      try{identity=await this.verifyIdentity(tokens.id_token,clientId);}
      catch{throw Error('계정 확인 토큰을 검증하지 못했습니다. 다시 연결하세요.');}
      this.check(op);
      if(identity.nonce!==proof.nonce || !identity.sub || (previous&&previous.subject!==identity.sub))throw Error('로그인한 계정의 신원을 확인할 수 없습니다');
      if(typeof tokens.access_token!=='string'||!tokens.access_token)throw Error('사용 가능한 계정 연결 정보를 받지 못했습니다');
      const c:Credential={clientId,subject:identity.sub,email:typeof identity.email==='string'?identity.email:undefined,
        accessToken:tokens.access_token,refreshToken:tokens.refresh_token,idToken:tokens.id_token,
        scopes:typeof tokens.scope==='string'?tokens.scope.split(' '):[],expiresAt:Date.now()+(Number(tokens.expires_in)||3600)*1000,
        verified:false,models:[]};
      await this.saveCredential(op,()=>c);this.check(op);
      this.message=c.scopes.includes('chatgpt.tokens.use.direct')?'ChatGPT 연결됨 / AI 요청 시험 필요':'ChatGPT 연결됨 / 구독 사용 승인 필요';
      // 연결 시험은 사용자가 별도 버튼으로 요청할 때만 실행한다.
      this.check(op);return this.status();
    }catch(error){
      const failure=this.friendly(error,op);
      if(op.epoch===this.epoch && !op.signal.aborted){this.message=failure.message;this.connectionFailureEpoch=op.epoch;}
      throw failure;
    }finally{
      callback?.close();
      if(this.closePending===callback?.close)this.closePending=undefined;
      this.pending=false;
    }
  }
  private async tokenRequest(fields:Record<string,string>,op:Operation):Promise<TokenResponse> {
    const response=await this.fetcher(tokenEndpoint,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:new URLSearchParams(fields),signal:this.signal(op,30_000),redirect:'error'});this.check(op);
    if(!response.ok){
      let terminal=response.status===401;
      try{const body=await response.json();terminal ||= body.error==='invalid_grant';}catch{}
      this.check(op);throw new TokenFailure(aiError('token_exchange',response.status),terminal);
    }
    let tokens:TokenResponse;
    try{tokens=await response.json();}catch{throw Error('계정 연결 응답을 확인하지 못했습니다. 다시 연결하세요.');}
    this.check(op);return tokens;
  }
  private async credential(op:Operation):Promise<Credential> {
    const c=await this.vault.get<Credential>('chatgpt');this.check(op);
    if(!c)throw Error('먼저 ChatGPT 계정을 연결하세요');
    if(!c.scopes.includes('chatgpt.tokens.use.direct'))throw Error('ChatGPT 구독 사용 동의가 필요합니다');
    if(c.expiresAt>Date.now()+60_000)return c;
    if(!c.refreshToken)throw Error('ChatGPT 연결이 만료되었습니다. 다시 연결하세요');
    if(this.refreshing?.epoch===op.epoch&&!this.refreshing.signal.aborted)return this.refreshing.promise;
    const promise=(async()=>{
      let tokens:TokenResponse;
      try{tokens=await this.tokenRequest({grant_type:'refresh_token',client_id:c.clientId,refresh_token:c.refreshToken!,resource},op);}
      catch(error){
        if(error instanceof TokenFailure && error.terminal){await this.saveCredential(op,current=>{this.matching(current,c);return undefined;});throw Error('ChatGPT 연결이 철회되었거나 만료되었습니다. 다시 연결하세요.');}
        throw error;
      }
      this.check(op);
      if(typeof tokens.access_token!=='string'||!tokens.access_token)throw Error('계정 연결을 갱신하지 못했습니다');
      let updated!:Credential;
      await this.saveCredential(op,current=>{
        const latest=this.matching(current,c);
        updated={...latest,accessToken:tokens.access_token!,refreshToken:tokens.refresh_token||latest.refreshToken,
          idToken:tokens.id_token||latest.idToken,scopes:typeof tokens.scope==='string'?tokens.scope.split(' '):latest.scopes,
          expiresAt:Date.now()+(Number(tokens.expires_in)||3600)*1000};
        return updated;
      });
      this.check(op);
      if(!updated.scopes.includes('chatgpt.tokens.use.direct'))throw Error('ChatGPT 구독 사용 동의가 필요합니다');
      return updated;
    })();
    const refreshing={epoch:op.epoch,signal:op.signal,promise};this.refreshing=refreshing;
    void promise.finally(()=>{if(this.refreshing===refreshing)this.refreshing=undefined;}).catch(()=>{});
    return promise;
  }
  private async modelList(c:Credential,op:Operation):Promise<string[]> {
    const response=await this.fetcher(`${resource}/models`,{headers:{Authorization:`Bearer ${c.accessToken}`},
      signal:this.signal(op,30_000),redirect:'error'});this.check(op);
    if(!response.ok)throw Error(aiError('models',response.status));
    let body:any;
    try{body=await response.json();}catch{throw Error('계정의 모델 목록 응답을 확인하지 못했습니다.');}
    this.check(op);
    const entries=body.models||body.data||[];
    if(!Array.isArray(entries))throw Error('계정의 모델 목록을 확인하지 못했습니다');
    const models=entries.filter((m:any)=>m && (!m.visibility||m.visibility==='list'))
      .map((m:any)=>m.slug||m.id).filter((m:unknown):m is string=>typeof m==='string').slice(0,500);
    if(!models.length)throw Error('현재 계정에서 사용할 수 있는 모델이 없습니다');
    await this.saveCredential(op,current=>({...this.matching(current,c),models}));
    this.check(op);return models;
  }
  async models():Promise<string[]> {
    const op=this.operation();
    try{return await this.modelList(await this.credential(op),op);}
    catch(error){throw this.friendly(error,op);}
  }
  requestStatus(id?:string):AiRequestStatus|undefined {
    return this.latestRequest&&(id===undefined||this.latestRequest.id===id)?{...this.latestRequest}:undefined;
  }
  cancelRequest(id:string):AiRequestStatus|undefined {
    const active=this.activeRequest;
    if(active?.status.id===id&&!active.signal.aborted)active.controller.abort(new CancelledAuthentication());
    return this.requestStatus(id);
  }
  private updateRequest(active:ActiveRequest,state:AiRequestStatus['state'],message:string,receivedChars=active.status.receivedChars){
    if(this.latestRequest!==active.status||['complete','cancelled','failed'].includes(active.status.state))return;
    const previous=active.status.state;
    Object.assign(active.status,{state,message,receivedChars,updatedAt:this.requestClock.now()});
    if(previous!==state&&['connecting','complete','cancelled','failed'].includes(state))this.reportRequest(active.status);
  }
  private reportRequest(status:AiRequestStatus){
    if(!this.onRequestStatus)return;
    const {id,state,model,startedAt,updatedAt,receivedChars}=status;
    try{void Promise.resolve(this.onRequestStatus({id,state,model,startedAt,updatedAt,receivedChars})).catch(()=>{});}catch{}
  }
  async request(instructions:string,input:unknown,model?:string,options:{requestId?:string;maxTokens?:number}={}):Promise<string> {
    if(this.diagnosing)throw Error('ChatGPT 연결 진단이 진행 중입니다. 진단이 끝난 뒤 AI 요청을 시작하세요.');
    return this.performRequest(instructions,input,model,options);
  }
  private async performRequest(instructions:string,input:unknown,model?:string,options:{requestId?:string;maxTokens?:number}={}):Promise<string> {
    if(this.activeRequest)throw Error('AI 요청이 이미 진행 중입니다. 완료하거나 취소한 뒤 다시 시도하세요.');
    const base=this.operation(),controller=new AbortController();
    const op={epoch:base.epoch,signal:AbortSignal.any([base.signal,controller.signal])};
    const id=options.requestId??randomUUID();if(!/^[a-zA-Z0-9_-]{1,80}$/.test(id))throw Error('AI 요청 번호를 확인하세요.');
    const now=this.requestClock.now();
    const status:AiRequestStatus={id,state:'preparing',startedAt:now,updatedAt:now,receivedChars:0,message:'계정 권한과 사용할 모델을 확인하고 있습니다.'};
    const active:ActiveRequest={status,controller,signal:op.signal};this.latestRequest=status;this.latestRequestEpoch=op.epoch;this.activeRequest=active;
    this.reportRequest(status);
    const aborted=()=>{
      const reason=op.signal.reason;
      this.updateRequest(active,reason instanceof Error&&reason.name==='TimeoutError'?'failed':'cancelled',reason instanceof AiRequestTimeout?reason.message:'AI 요청을 취소했습니다. 기존 회의 기록은 그대로 남아 있습니다.');
    };
    op.signal.addEventListener('abort',aborted,{once:true});
    const totalTimer=this.requestClock.setTimeout(()=>controller.abort(new AiRequestTimeout('AI_REQUEST_TOTAL_TIMEOUT')),this.requestTimeouts.totalMs);
    let idleTimer:unknown;
    const touch=()=>{
      if(op.signal.aborted)return;
      if(idleTimer!==undefined)this.requestClock.clearTimeout(idleTimer);
      idleTimer=this.requestClock.setTimeout(()=>controller.abort(new AiRequestTimeout('AI_RESPONSE_IDLE_TIMEOUT')),this.requestTimeouts.idleMs);
    };
    try{
      const c=await waitForAbort(this.credential(op),op.signal);
      const models=c.models.length?c.models:await waitForAbort(this.modelList(c,op),op.signal);this.check(op);
      const selected=model||models[0];if(!models.includes(selected))throw Error('계정에서 사용할 수 있는 모델을 선택하세요');
      status.model=selected;
      this.updateRequest(active,'connecting','ChatGPT에 AI 요청을 보내고 있습니다.');touch();
      const response=await waitForAbort(this.fetcher(`${resource}/responses`,{method:'POST',headers:{Authorization:`Bearer ${c.accessToken}`,'Content-Type':'application/json'},
        body:JSON.stringify({model:selected,instructions,input:[{role:'user',content:typeof input==='string'?input:JSON.stringify(input)}],store:false,stream:true,...(options.maxTokens?{max_output_tokens:options.maxTokens}:{})}),signal:op.signal,redirect:'error'}),op.signal);
      this.check(op);
      touch();this.updateRequest(active,'waiting','ChatGPT가 요청을 처리하고 있습니다. 첫 응답을 기다려 주세요.');
      if(!response.ok){
        let code='request';try{const body=await waitForAbort(response.json(),op.signal);if(typeof body.error?.code==='string')code=body.error.code;}catch{}
        this.check(op);this.message=aiError(code,response.status);throw Error(this.message);
      }
      const result=await readResponseStream(response,op.signal,{onActivity:()=>{touch();this.updateRequest(active,'receiving','AI 응답을 수신하고 있습니다. 응답이 완성될 때까지 기다려 주세요.');},onText:receivedChars=>this.updateRequest(active,'receiving',`AI가 응답을 작성하고 있습니다. ${receivedChars}자를 받았습니다.`,receivedChars)});
      this.check(op);this.updateRequest(active,'complete','AI 응답을 모두 받았습니다. 결과를 확인하고 있습니다.');this.connectionFailureEpoch=-1;return result;
    }catch(error){
      const failure=this.friendly(error,op);
      this.updateRequest(active,failure instanceof CancelledAuthentication?'cancelled':'failed',failure.message);
      throw failure;
    }finally{
      this.requestClock.clearTimeout(totalTimer);if(idleTimer!==undefined)this.requestClock.clearTimeout(idleTimer);
      op.signal.removeEventListener('abort',aborted);
      if(this.activeRequest===active)this.activeRequest=undefined;
    }
  }
  async test() {
    // 이미 실행 중인 요청은 계정 진단 실패로 처리하지 않는다.
    // 모델 목록을 기다리기 전에 진단을 예약한다.
    // 진단 중에는 새 AI 요청을 받지 않는다.
    if(this.activeRequest||this.diagnosing)throw Error('AI 요청 또는 연결 진단이 이미 진행 중입니다. 완료하거나 취소한 뒤 다시 진단하세요.');
    const op=this.operation();
    this.diagnosing=true;
    try{
      await waitForAbort(this.models(),op.signal);this.check(op);
      await this.performRequest('사용자의 짧은 연결 시험에 응답하세요.','연결 성공이라고 한 번만 말하세요.');this.check(op);
      const c=await this.credential(op);
      await this.saveCredential(op,current=>({...this.matching(current,c),verified:true}));this.check(op);
      this.message='ChatGPT 연결됨 / AI 요청 시험 성공';return this.status();
    }catch(error){
      const failure=this.friendly(error,op);
      if(op.epoch===this.epoch && !op.signal.aborted){
        this.connectionFailureEpoch=op.epoch;
        this.message=failure.message;
        const c=await this.vault.get<Credential>('chatgpt');this.check(op);
        if(c)await this.saveCredential(op,current=>({...this.matching(current,c),verified:false}));
      }
      throw failure;
    }finally{this.diagnosing=false;}
  }
  cancel() {
    ++this.epoch;this.lifetime.abort(new CancelledAuthentication());this.lifetime=new AbortController();this.closePending?.();
  }
  async disconnect() {
    if(this.disconnecting)throw Error('ChatGPT 연결을 해제하는 중입니다.');
    this.disconnecting=true;this.cancel();
    try{
      // 예약된 저장이 끝난 뒤 로컬 계정 정보를 지운다.
      await this.writes.catch(()=>{});
      const c=await this.vault.get<Credential>('chatgpt');await this.vault.delete('chatgpt');
      this.message='로컬 연결을 해제했습니다. ChatGPT 설정에서도 앱 접근을 철회할 수 있습니다';
      if(c?.refreshToken){
        try{
          const response=await this.fetcher('https://auth.openai.com/api/accounts/oauth/revoke',{method:'POST',
            headers:{'Content-Type':'application/x-www-form-urlencoded'},
            body:new URLSearchParams({client_id:c.clientId,token:c.refreshToken,token_type_hint:'refresh_token'}),
            signal:AbortSignal.any([this.lifetime.signal,AbortSignal.timeout(15_000)]),redirect:'error'});
          if(response.ok)this.message='ChatGPT 연결을 해제했습니다';
        }catch{}
      }
      return this.status();
    }finally{this.disconnecting=false;}
  }
}
