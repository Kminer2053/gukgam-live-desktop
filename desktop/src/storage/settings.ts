import {mkdir,readFile,rename,rm,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {validateSetup} from '../main/security';
import type {SavedSetup,SetupInput} from '../shared/contracts';
import type {ChatGPTVault} from '../auth/chatgpt';
type StoredSetup=Omit<SetupInput,'relayToken'|'applyRelayPolicy'> & {relayPolicyRevision?:string};
export class SetupStore {
  private writes=Promise.resolve();
  constructor(private dir:string,private vault:ChatGPTVault) {}
  private async stored():Promise<StoredSetup> {
    let stored:StoredSetup={organization:''};
    try {
      const raw=JSON.parse(await readFile(join(this.dir,'setup.json'),'utf8'));
      if(!raw||typeof raw!=='object'||Array.isArray(raw)||'applyRelayPolicy' in raw)throw Error();
      const {relayPolicyRevision,...fields}=raw;
      if(relayPolicyRevision!==undefined&&(typeof relayPolicyRevision!=='string'||!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(relayPolicyRevision)||!['relayEnabled','relayStartDate','relayEndDate'].every(key=>key in fields)))throw Error();
      stored={...validateSetup(fields),...(relayPolicyRevision?{relayPolicyRevision}:{})};
    } catch(error) {
      if((error as NodeJS.ErrnoException).code!=='ENOENT')throw Error('기관 설정을 읽지 못했습니다. 앱의 설정 파일을 확인하세요.');
    }
    return stored;
  }
  private async view(stored:StoredSetup):Promise<SavedSetup> {
    const {relayPolicyRevision,...visible}=stored;
    try {return {...visible,hasRelayToken:Boolean(await this.vault.get('relay'))};}
    catch {return {...visible,hasRelayToken:false,secureStorageWarning:'계정 보안 저장소를 읽지 못했습니다. AI 계정과 릴레이 연결은 보류하지만 이 PC의 회의 기록과 실황 수집은 사용할 수 있습니다.'};}
  }
  async read():Promise<SavedSetup> {return this.view(await this.stored());}
  async save(input:unknown):Promise<SavedSetup> {
    const valid=validateSetup(input);
    const write=this.writes.then(async()=>{
      await mkdir(this.dir,{recursive:true,mode:0o700});
      const {relayToken,applyRelayPolicy,...fields}=valid;
      const publicSettings:StoredSetup=fields;
      let previous:StoredSetup={organization:''};
      try {previous=await this.stored();} catch {}
      const previousRelay=previous.relayUrl;
      if(applyRelayPolicy) {
        // 같은 값도 명시적 저장을 구분한다. 판본은 main만 만들고 화면의 주입은 거부한다.
        publicSettings.relayPolicyRevision=randomUUID();
      } else {
        // 기관명·AI·사이트 설정 저장은 오래된 기간을 다시 적용하지 않는다.
        for(const key of ['relayEnabled','relayStartDate','relayEndDate','relayPolicyRevision'] as const) {
          if(previous[key]!==undefined)(publicSettings as Record<string,unknown>)[key]=previous[key];
          else delete publicSettings[key];
        }
      }
      if(relayToken)await this.vault.set('relay',relayToken);
      else if(!valid.relayUrl||valid.relayUrl!==previousRelay)await this.vault.delete('relay');
      const target=join(this.dir,'setup.json'),temp=`${target}.${randomUUID()}.tmp`;
      try {await writeFile(temp,JSON.stringify(publicSettings),{mode:0o600,flag:'wx'});await rename(temp,target);} finally {await rm(temp,{force:true});}
    });
    this.writes=write.catch(()=>{});await write;return this.read();
  }
  async engineConfig():Promise<SetupInput & {relayPolicyRevision?:string}> {
    await this.writes;
    const stored=await this.stored();
    const {hasRelayToken,secureStorageWarning,...visible}=await this.view(stored);
    const settings={...visible,...(stored.relayPolicyRevision?{relayPolicyRevision:stored.relayPolicyRevision}:{})};
    if(!settings.organization)throw Error('먼저 기관명을 입력하고 저장하세요.');
    if(secureStorageWarning){const {relayUrl,...local}=settings;return local;}
    let token:string|undefined;
    try {token=hasRelayToken?await this.vault.get<string>('relay'):undefined;} catch {const {relayUrl,...local}=settings;return local;}
    if(!token){const {relayUrl,...local}=settings;return local;}
    return {...settings,relayToken:token};
  }
}
