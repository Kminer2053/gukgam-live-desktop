import {posix,win32} from 'node:path';
export type AppPathInput={platform:string;appData:string;tempDir:string;ci?:string;smokeSwitch:boolean;smokeDir?:string};
/** 설치판 CI는 임시 폴더에만 격리하며 제품의 정상 사용자 경로는 바꾸지 않는다. */
export function applicationPaths(input:AppPathInput):{userData:string;sessionData:string;ciSmoke:boolean} {
  const path=input.platform==='win32'?win32:posix;
  const requested=input.smokeSwitch||Boolean(input.smokeDir);
  const ciSmoke=requested&&input.platform==='win32'&&input.ci==='true'&&input.smokeSwitch;
  if(requested&&!ciSmoke)throw Error('설치판 시험 모드는 Windows CI에서만 사용할 수 있습니다.');
  let userData=path.join(input.appData,'GukgamLiveDesktop');
  if(ciSmoke) {
    if(!input.smokeDir||!path.isAbsolute(input.smokeDir))throw Error('설치판 시험용 임시 폴더가 올바르지 않습니다.');
    const relative=path.relative(input.tempDir,input.smokeDir);
    if(!relative||relative==='..'||relative.startsWith(`..${path.sep}`)||path.isAbsolute(relative))throw Error('설치판 시험 자료는 사용자 임시 폴더 아래에만 저장할 수 있습니다.');
    userData=path.normalize(input.smokeDir);
  }
  return {userData,sessionData:path.join(userData,'browser-cache'),ciSmoke};
}
export const isolatedWebPreferences={contextIsolation:true,sandbox:true,nodeIntegration:false,webviewTag:false,webSecurity:true,allowRunningInsecureContent:false} as const;
