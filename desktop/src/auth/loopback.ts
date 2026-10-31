import {createServer} from 'node:http';
import {randomBytes,createHash,timingSafeEqual} from 'node:crypto';
export function oauthProof(){const verifier=randomBytes(48).toString('base64url');return {verifier,challenge:createHash('sha256').update(verifier).digest('base64url'),state:randomBytes(32).toString('base64url'),nonce:randomBytes(32).toString('base64url')};}
export function stateMatches(actual:string|null,expected:string){if(!actual)return false;const a=Buffer.from(actual),b=Buffer.from(expected);return a.length===b.length&&timingSafeEqual(a,b);}
export async function loopbackCallback(state:string){
 let done=false;let resolve!:(value:URLSearchParams)=>void,reject!:(e:Error)=>void;
 const result=new Promise<URLSearchParams>((res,rej)=>{resolve=res;reject=rej;});result.catch(()=>{});
 const server=createServer((req,res)=>{const address=server.address();if(typeof address!=='object'||!address||req.headers.host!==`127.0.0.1:${address.port}`||(req.url?.length||0)>8192){res.writeHead(400);res.end();return;}const url=new URL(req.url||'/',`http://127.0.0.1`);if(req.method!=='GET'||url.pathname!=='/auth/callback'){res.writeHead(404);res.end();return;}if(!stateMatches(url.searchParams.get('state'),state)){res.writeHead(400);res.end('로그인 확인값이 일치하지 않습니다');return;}if(done){res.writeHead(409);res.end();return;}done=true;res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'",'Cache-Control':'no-store'});res.end('<!doctype html><meta charset="utf-8"><h2>로그인 응답을 받았습니다.</h2><p>국감 라이브 데스크톱로 돌아가세요.</p>');resolve(url.searchParams);close();});
 await new Promise<void>((res,rej)=>{server.once('error',rej);server.listen(0,'127.0.0.1',res);});
 const port=(server.address() as {port:number}).port;
 const timer=setTimeout(()=>{if(!done){done=true;reject(Error('로그인 대기 시간이 지났습니다. 다시 연결하세요.'));}close();},180000);
 function close(){clearTimeout(timer);server.close();}
 return {redirectUri:`http://127.0.0.1:${port}/auth/callback`,result,close:()=>{if(!done){done=true;reject(Error('로그인이 취소되었습니다'));}close();}};
}
