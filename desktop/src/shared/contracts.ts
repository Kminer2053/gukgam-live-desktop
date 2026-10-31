export type AiRequestStatus = {id:string;state:'preparing'|'connecting'|'waiting'|'receiving'|'complete'|'cancelled'|'failed';model?:string;startedAt:number;updatedAt:number;receivedChars:number;message:string};
export type SetupInput = {organization:string;model?:string;relayUrl?:string;relayToken?:string;relayEnabled?:boolean;relayStartDate?:string;relayEndDate?:string;applyRelayPolicy?:boolean};
export type SavedSetup = Omit<SetupInput,'relayToken'|'applyRelayPolicy'> & {hasRelayToken:boolean;secureStorageWarning?:string};
export type ChatGPTStatus = {connected:boolean;verified:boolean;message:string;models:string[];account?:string};
export type ChatGPTConnectionHealth = {state:'verified'|'refreshable'|'unverified'|'expired'|'disconnected'|'checking'|'error'|'unavailable'};
export type EngineState = {state:'stopped'|'starting'|'ready'|'stopping'|'error';message:string};
export type RelayHealth = {reachable:boolean;readMode:'source'|'mirror';agentOnline:boolean;sourceReady:boolean;message:string};
export type AppStatus = {version:string;setup:SavedSetup;chatgpt:ChatGPTStatus;engine:EngineState};
export type DesktopApi = import('../relay/contracts').RelayDesktopApi & import('../role/contracts').RoleDesktopApi & {
  status():Promise<AppStatus>;
  saveSetup(input:SetupInput):Promise<SavedSetup>;
  connectChatGPT():Promise<ChatGPTStatus>;
  cancelLogin():Promise<void>;
  testChatGPT():Promise<ChatGPTStatus>;
  disconnectChatGPT():Promise<ChatGPTStatus>;
  startEngine():Promise<EngineState>;
  stopEngine():Promise<EngineState>;
  openWorkspace():Promise<void>;
  openLlmSettingsPage():Promise<void>;
  openRelaySettingsPage():Promise<void>;
  openUserManual():Promise<void>;
  openHelp():Promise<void>;
  checkRelay(value:string):Promise<RelayHealth>;
};
export type LlmRequest = {type:'llm.request';id:string;system:string;user:string;max_tokens:number};
export type LlmResult = {type:'llm.result';id:string;text:string}|{type:'llm.error';id:string;message:string};
export type BackendEvent = {type:'ready';url:string}|LlmRequest|{type:'llm.cancel';id:string}|{type:'account.status.request';id:string}|{type:'error';message:string};
declare global {interface Window {gukgam:DesktopApi}}
