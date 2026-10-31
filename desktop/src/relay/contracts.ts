/** 릴레이 구성 화면에는 비밀값·원격 명령 출력 대신 진행 상태만 전달한다. */
export type RelayTeam={id:string;slug:string;name:string};
export type RelayField={key:string;label:string;type:'string'|'number'|'boolean'|'array';required:boolean;options?:string[];default?:unknown;description?:string};
export type RelayProduct={id:string;slug:string;name:string;fields:RelayField[]};
export type RelayCatalog={products:RelayProduct[]};
export type RelayPlan={id:string;name:string;description:string;type:string};
export type RelayProvisionInput={teamId:string;projectName:string;productId:string;planId:string;metadata:Record<string,unknown>;operatorPasscode:string;viewerPasscode:string;consent:boolean};
export const SITE_UPDATE_STATES=['unverified','updating','succeeded','failed','unknown','cancelled'] as const;
export const SITE_UPDATE_STAGES=['unverified','preparing','deploying','binding','verifying','saving','completed'] as const;
export const SITE_UPDATE_CODES=['INTERRUPTED','PREPARATION_FAILED','DEPLOY_FAILED','DEPLOY_URL_INVALID','BINDING_FAILED',
 'SITE_VERIFICATION_FAILED','SAVE_FAILED','JOURNAL_FAILED','RESTORE_UNCONFIRMED','API_HTTP_ERROR',
 'VERCEL_EXIT_NONZERO','VERCEL_ARGS_INVALID','VERCEL_PATH_INVALID','VERCEL_STOP_UNCONFIRMED','VERCEL_CANCELLED',
 'VERCEL_INPUT_LIMIT','VERCEL_OUTPUT_LIMIT','VERCEL_START_FAILED','VERCEL_TIMEOUT','VERCEL_FAILED',
 'VERCEL_AUTH_INVALID','VERCEL_AUTH_SAVE_FAILED','VERCEL_API_PATH_INVALID','VERCEL_API_TEAM_INVALID',
 'VERCEL_API_INPUT_INVALID','VERCEL_API_METHOD_INVALID','VERCEL_API_INPUT_LIMIT','VERCEL_API_CANCELLED',
 'VERCEL_API_AUTH_FAILED','VERCEL_API_TIMEOUT','VERCEL_API_FAILED','VERCEL_API_BODY_LIMIT','VERCEL_API_BODY_INVALID'] as const;
export type SiteUpdateStatus={state:typeof SITE_UPDATE_STATES[number];stage:typeof SITE_UPDATE_STAGES[number];
 deployAttempted?:boolean;targetVersion?:string;version?:string;knownCode?:typeof SITE_UPDATE_CODES[number];
 httpStatus?:number;exitCode?:number;durationMs?:number};
export function siteUpdateVersion(value:unknown):string|undefined {
 return typeof value==='string'&&value.length<=64&&/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-preview\.(0|[1-9]\d*))?$/.test(value)?value:undefined;
}
/** 임의 저장 메타를 공개하지 않고 알려진 최신 업데이트 결과만 보존한다. */
export function siteUpdateStatus(value:unknown):SiteUpdateStatus {
 const empty:SiteUpdateStatus={state:'unverified',stage:'unverified'};
 if(!value||typeof value!=='object'||Array.isArray(value))return empty;
 const raw=value as Record<string,unknown>;
 if(!SITE_UPDATE_STATES.includes(raw.state as any)||!SITE_UPDATE_STAGES.includes(raw.stage as any))return empty;
 if(raw.state==='unverified')return empty;
 if(raw.state==='unknown'&&raw.stage==='unverified'&&raw.knownCode==='INTERRUPTED'&&raw.deployAttempted===undefined)
  return {state:'unknown',stage:'unverified',knownCode:'INTERRUPTED'};
 if(typeof raw.deployAttempted!=='boolean'||raw.stage==='unverified')return empty;
 if(raw.state==='succeeded'&&(raw.stage!=='completed'||!raw.deployAttempted||!siteUpdateVersion(raw.version)||raw.version!==raw.targetVersion))return empty;
 if(raw.state!=='succeeded'&&raw.stage==='completed'||raw.state==='cancelled'&&raw.deployAttempted)return empty;
 if(raw.stage==='preparing'&&raw.deployAttempted||raw.stage!=='preparing'&&!raw.deployAttempted||raw.state==='failed'&&raw.deployAttempted)return empty;
 if((raw.state==='succeeded'||raw.state==='updating')&&(raw.knownCode!==undefined||raw.httpStatus!==undefined||raw.exitCode!==undefined&&raw.exitCode!==0))return empty;
 if(raw.targetVersion!==undefined&&!siteUpdateVersion(raw.targetVersion)||raw.version!==undefined&&!siteUpdateVersion(raw.version))return empty;
 if(raw.knownCode!==undefined&&!SITE_UPDATE_CODES.includes(raw.knownCode as any))return empty;
 for(const [key,min,max] of [['httpStatus',100,599],['exitCode',0,255],['durationMs',0,86_400_000]] as const)
  if(raw[key]!==undefined&&(!Number.isSafeInteger(raw[key])||(raw[key] as number)<min||(raw[key] as number)>max))return empty;
 const out:SiteUpdateStatus={state:raw.state as SiteUpdateStatus['state'],stage:raw.stage as SiteUpdateStatus['stage'],deployAttempted:raw.deployAttempted};
 if(raw.targetVersion!==undefined)out.targetVersion=raw.targetVersion as string;
 if(raw.state==='succeeded')out.version=raw.version as string;
 else if(raw.knownCode!==undefined)out.knownCode=raw.knownCode as SiteUpdateStatus['knownCode'];
 for(const key of ['httpStatus','exitCode','durationMs'] as const)if(raw[key]!==undefined)out[key]=raw[key] as number;
 return out;
}
export type ManagedRelayStatus={state:'idle'|'login'|'connected'|'configuring'|'approval'|'ready'|'error';message:string;teams:RelayTeam[];projectName?:string;teamId?:string;url?:string;approvalUrl?:string;canResume:boolean;verified:boolean;sourceOnline:boolean;siteUpdate?:SiteUpdateStatus};
export type RelayDesktopApi={
 relayStatus():Promise<ManagedRelayStatus>;
 connectRelayAccount():Promise<ManagedRelayStatus>;
 relayCatalog(teamId:string):Promise<RelayCatalog>;
 relayPlans(input:{teamId:string;productId:string;metadata:Record<string,unknown>}):Promise<RelayPlan[]>;
 provisionRelay(input:RelayProvisionInput):Promise<ManagedRelayStatus>;
 resumeRelay():Promise<ManagedRelayStatus>;
 cancelRelay():Promise<ManagedRelayStatus>;
 openRelayApproval():Promise<void>;
 openRelayPricing(provider:'neon'|'vercel'):Promise<void>;
 verifyManagedRelay():Promise<ManagedRelayStatus>;
 connectManagedRelay():Promise<ManagedRelayStatus>;
 openManagedRelaySite():Promise<void>;
 updateManagedRelaySite():Promise<ManagedRelayStatus>;
};
