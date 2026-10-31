import {contextBridge,ipcRenderer} from 'electron';
import type {DesktopApi} from '../shared/contracts';
async function invoke<T>(name:string,...args:unknown[]):Promise<T> {
  const result=await ipcRenderer.invoke(`gukgam:${name}`,...args);
  if(!result?.ok)throw Error(typeof result?.message==='string'?result.message:'앱 요청을 완료하지 못했습니다.');
  return result.data as T;
}
const api:DesktopApi={
  status:()=>invoke('status'),saveSetup:value=>invoke('save-setup',value),
  connectChatGPT:()=>invoke('connect-chatgpt'),cancelLogin:()=>invoke('cancel-login'),
  testChatGPT:()=>invoke('test-chatgpt'),disconnectChatGPT:()=>invoke('disconnect-chatgpt'),
  startEngine:()=>invoke('start-engine'),stopEngine:()=>invoke('stop-engine'),
  openWorkspace:()=>invoke('open-workspace'),openHelp:()=>invoke('open-help'),checkRelay:value=>invoke('check-relay',value),
  openLlmSettingsPage:()=>invoke('open-llm-settings-page'),openUserManual:()=>invoke('open-user-manual'),
  roleSettingsStatus:()=>invoke('role-settings-status'),saveJevKey:value=>invoke('save-jev-key',value),
  deleteJevKey:()=>invoke('delete-jev-key'),saveRoleSettings:value=>invoke('save-role-settings',value),
  prepareJulia:()=>invoke('prepare-julia'),cancelJulia:()=>invoke('cancel-julia'),openRoleSettingsPage:()=>invoke('open-role-settings-page'),
  relayStatus:()=>invoke('relay-status'),connectRelayAccount:()=>invoke('connect-relay-account'),
  relayCatalog:teamId=>invoke('relay-catalog',teamId),relayPlans:value=>invoke('relay-plans',value),
  provisionRelay:value=>invoke('provision-relay',value),resumeRelay:()=>invoke('resume-relay'),
  cancelRelay:()=>invoke('cancel-relay'),openRelayApproval:()=>invoke('open-relay-approval'),
  openRelayPricing:provider=>invoke('open-relay-pricing',provider),verifyManagedRelay:()=>invoke('verify-managed-relay'),
  connectManagedRelay:()=>invoke('connect-managed-relay'),openManagedRelaySite:()=>invoke('open-managed-relay-site'),
  updateManagedRelaySite:()=>invoke('update-managed-relay-site')
};
contextBridge.exposeInMainWorld('gukgam',Object.freeze(api));
