import type {JevKeyStatus} from '../role/contracts';
import {randomUUID} from 'node:crypto';
type Vault={get<T>(key:string):Promise<T|undefined>;set(key:string,value:unknown):Promise<void>;delete(key:string):Promise<void>};
const SLOT='jev-api-key';
export function validateJevKey(value:unknown):string {
  if(typeof value!=='string'||!value.trim()||value.length>4096||/[^\x21-\x7e]/.test(value.trim()))throw Error('Jev API 키는 공백 없는 영문·숫자·기호로 입력하세요.');
  return value.trim();
}
/** 키 원문은 main의 vault와 소유 엔진 파이프에만 사용한다. */
export class JevKeyStore {
  constructor(private vault:Vault){}
  async status():Promise<JevKeyStatus>{
    try{return {hasKey:Boolean(await this.readForEngine())};}
    catch{return {hasKey:false,storageWarning:'Jev 키의 운영체제 보안 저장소를 읽지 못했습니다. 기록과 기존 ChatGPT 처리는 사용할 수 있습니다.'};}
  }
  async readForEngine():Promise<string>{
    return (await this.configurationForEngine()).key;
  }
  async configurationForEngine():Promise<{key:string;revision:string}>{
    const value=await this.vault.get<unknown>(SLOT);
    if(value===undefined)return {key:'',revision:'unconfigured'};
    if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).some(key=>!['key','revision'].includes(key)))throw Error('보호된 Jev 키를 다시 저장하세요.');
    const row=value as {key?:unknown;revision?:unknown};
    if(typeof row.revision!=='string'||!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(row.revision))throw Error('보호된 Jev 키를 다시 저장하세요.');
    return {key:row.key===undefined?'':validateJevKey(row.key),revision:row.revision};
  }
  async save(value:unknown):Promise<JevKeyStatus>{
    const key=validateJevKey(value);
    try{await this.vault.set(SLOT,{key,revision:randomUUID()});}catch{throw Error('Jev 키를 운영체제 보안 저장소에 저장하지 못했습니다. 평문으로 저장하지 않았습니다.');}
    return {hasKey:true};
  }
  async delete():Promise<JevKeyStatus>{
    // 키는 제거하고 무작위 변경 세대만 암호화 보관해 이전 실패 캐시와 구별한다.
    try{await this.vault.set(SLOT,{revision:randomUUID()});}catch{throw Error('이 앱의 보호된 Jev 키를 삭제하지 못했습니다.');}
    return {hasKey:false};
  }
}
