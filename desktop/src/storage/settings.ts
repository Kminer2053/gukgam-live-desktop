import {mkdir,readFile,rename,rm,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {validateSetup} from '../main/security';
import type {SavedSetup,SetupInput} from '../shared/contracts';
import type {ChatGPTVault} from '../auth/chatgpt';
export class SetupStore {
  private writes=Promise.resolve();
  constructor(private dir:string,private vault:ChatGPTVault) {}
  async read():Promise<SavedSetup> {
    let stored:SetupInput={organization:''};
    try {stored=validateSetup(JSON.parse(await readFile(join(this.dir,'setup.json'),'utf8')));} catch(error) {
      if((error as NodeJS.ErrnoException).code!=='ENOENT')throw Error('기관 설정을 읽지 못했습니다. 앱의 설정 파일을 확인하세요.');
    }
    try {return {...stored,hasRelayToken:Boolean(await this.vault.get('relay'))};}
    catch {return {...stored,hasRelayToken:false,secureStorageWarning:'계정 보안 저장소를 읽지 못했습니다. AI 계정과 릴레이 연결은 보류하지만 이 PC의 회의 기록과 실황 수집은 사용할 수 있습니다.'};}
  }
  async save(input:unknown):Promise<SavedSetup> {
    const valid=validateSetup(input);
    const write=this.writes.then(async()=>{
      await mkdir(this.dir,{recursive:true,mode:0o700});
      const {relayToken,...publicSettings}=valid;
      let previousRelay:string|undefined;
      try {previousRelay=JSON.parse(await readFile(join(this.dir,'setup.json'),'utf8')).relayUrl;} catch {}
      if(relayToken)await this.vault.set('relay',relayToken);
      else if(!valid.relayUrl||valid.relayUrl!==previousRelay)await this.vault.delete('relay');
      const target=join(this.dir,'setup.json'),temp=`${target}.${randomUUID()}.tmp`;
      try {await writeFile(temp,JSON.stringify(publicSettings),{mode:0o600,flag:'wx'});await rename(temp,target);} finally {await rm(temp,{force:true});}
    });
    this.writes=write.catch(()=>{});await write;return this.read();
  }
  async engineConfig():Promise<SetupInput> {
    await this.writes;
    const {hasRelayToken,secureStorageWarning,...settings}=await this.read();
    if(!settings.organization)throw Error('먼저 기관명을 입력하고 저장하세요.');
    if(secureStorageWarning){const {relayUrl,...local}=settings;return local;}
    let token:string|undefined;
    try {token=hasRelayToken?await this.vault.get<string>('relay'):undefined;} catch {const {relayUrl,...local}=settings;return local;}
    if(!token){const {relayUrl,...local}=settings;return local;}
    return {...settings,relayToken:token};
  }
}
