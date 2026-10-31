export type RoleChoice='none'|'jev'|'julia';
export type RoleSelection={provider:RoleChoice;threads:1|2};
export type JevKeyStatus={hasKey:boolean;storageWarning?:string};
export type RoleSettingsStatus=JevKeyStatus&{engineReady:boolean;message:string;selectedProvider:RoleChoice|null;
  threads:1|2;enabled:boolean;autoBlock:boolean;preparing:boolean;
  model:{state:string;message:string;available:boolean;progress?:number};
  worker:{state:string;message:string;available:boolean}};
export type RoleDesktopApi={
  roleSettingsStatus():Promise<RoleSettingsStatus>;
  saveJevKey(value:string):Promise<JevKeyStatus>;
  deleteJevKey():Promise<JevKeyStatus>;
  saveRoleSettings(value:RoleSelection):Promise<RoleSettingsStatus>;
  prepareJulia():Promise<RoleSettingsStatus>;
  cancelJulia():Promise<RoleSettingsStatus>;
  openRoleSettingsPage():Promise<void>;
};
