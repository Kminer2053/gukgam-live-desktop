import type {LlmRequest, LlmResult} from '../shared/contracts';
type Worker = {request(system:string,user:string,model?:string,options?:{requestId?:string;maxTokens?:number}):Promise<string>;cancelRequest(id:string):unknown};
type Job = {request:LlmRequest;cancel:()=>void;cancelled:boolean};
/** JSONL 요청은 하나씩 처리하며 취소는 요청 번호에만 적용한다. */
export class LlmBridge {
  private pending:Job[]=[];
  private active?:Job;
  private closed=false;
  constructor(private worker:Worker,private model:()=>string|undefined,private send:(result:LlmResult)=>void) {}
  accept(request:LlmRequest) {
    if(this.closed)return;
    if(this.active?.request.id===request.id||this.pending.some(job=>job.request.id===request.id)) {
      this.send({type:'llm.error',id:request.id,message:'이미 처리 중인 AI 요청 번호입니다.'});return;
    }
    if(this.pending.length>=8) {this.send({type:'llm.error',id:request.id,message:'AI 요청 대기열이 가득 찼습니다. 잠시 후 다시 시도하세요.'});return;}
    this.pending.push({request,cancel:()=>{},cancelled:false});void this.pump();
  }
  cancel(id:string) {
    const waiting=this.pending.findIndex(job=>job.request.id===id);
    if(waiting>=0) {this.pending.splice(waiting,1);this.send({type:'llm.error',id,message:'이 AI 요청을 취소했습니다.'});return;}
    if(this.active?.request.id===id&&!this.active.cancelled) {
      this.active.cancelled=true;this.worker.cancelRequest(id);this.active.cancel();
      this.send({type:'llm.error',id,message:'이 AI 요청을 취소했습니다.'});
    }
  }
  close() {
    this.closed=true;
    for(const job of [...this.pending])this.cancel(job.request.id);
    if(this.active)this.cancel(this.active.request.id);
  }
  private async pump() {
    if(this.active||this.closed)return;
    const job=this.pending.shift();if(!job)return;
    this.active=job;
    const cancel=new Promise<never>((_,reject)=>{job.cancel=()=>reject(Error('요청 취소'));});
    try {
      const request=job.request;
      const response=this.worker.request(request.system,request.user,this.model(),{requestId:request.id,maxTokens:request.max_tokens});
      const text=await Promise.race([response,cancel]);
      if(!job.cancelled&&!this.closed)this.send({type:'llm.result',id:request.id,text});
    } catch {
      if(!job.cancelled&&!this.closed)this.send({type:'llm.error',id:job.request.id,message:'ChatGPT 요청을 완료하지 못했습니다. 앱의 계정 연결과 사용 한도를 확인하세요.'});
    } finally {
      if(this.active===job)this.active=undefined;
      // 취소된 서비스가 자신의 요청 정리를 마칠 수 있도록 다음 이벤트까지 양보한다.
      if(!this.closed)setTimeout(()=>void this.pump(),0);
    }
  }
}
