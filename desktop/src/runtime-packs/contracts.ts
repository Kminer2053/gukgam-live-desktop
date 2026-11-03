export type RuntimePackId='engine'|'relay-tools';
export type RuntimePackState='missing'|'checking'|'downloading'|'extracting'|'ready'|'cancelled'|'error';
export type RuntimePackStatus={id:RuntimePackId;state:RuntimePackState;message:string;receivedBytes:number;totalBytes:number;progress:number};
export type RuntimePackFile={path:string;bytes:number;sha256:string};
export type RuntimePackSpec={id:RuntimePackId;version:string;url:string;bytes:number;sha256:string;unpackedBytes:number;entryPoint:string;files:readonly RuntimePackFile[]};
