import {mkdir,readFile,rename,writeFile,unlink} from 'node:fs/promises';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
export class SecureVault {
 private tail:Promise<unknown>=Promise.resolve();
 constructor(private dir:string,private safeStorage:{isEncryptionAvailable():boolean;getSelectedStorageBackend?():string;encryptString(value:string):Buffer;decryptString(value:Buffer):string}){}
 private file(key:string){if(!/^[a-z0-9._-]{1,100}$/i.test(key))throw Error('잘못된 저장 키입니다');return join(this.dir,`${key}.enc`);}
 private available(){if(!this.safeStorage.isEncryptionAvailable()|| (process.platform==='linux'&&this.safeStorage.getSelectedStorageBackend?.()==='basic_text'))throw Error('운영체제 보안 저장소를 사용할 수 없습니다. 계정 연결을 중단했습니다.');}
 async get<T>(key:string):Promise<T|undefined>{this.available();await this.tail;try{return JSON.parse(this.safeStorage.decryptString(await readFile(this.file(key)))) as T;}catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT')return undefined;throw Error('보호된 계정 정보를 읽지 못했습니다. 연결을 다시 설정하세요.');}}
 set(key:string,value:unknown):Promise<void>{const op=this.tail.then(async()=>{this.available();await mkdir(this.dir,{recursive:true,mode:0o700});const path=this.file(key),temp=`${path}.${randomUUID()}.tmp`;await writeFile(temp,this.safeStorage.encryptString(JSON.stringify(value)),{mode:0o600,flag:'wx'});await rename(temp,path);});this.tail=op.catch(()=>{});return op;}
 delete(key:string):Promise<void>{const op=this.tail.then(async()=>{await unlink(this.file(key)).catch(e=>{if(e.code!=='ENOENT')throw e;});});this.tail=op.catch(()=>{});return op;}
}
