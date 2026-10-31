import type {AppStatus} from '../shared/contracts';
export type Task='account'|'engine-start'|'engine-stop'|'settings'|'relay-check'|'relay-help'|'refresh';
/** 계정·사이트 작업이 기록 시작과 상태 탐색을 잠그지 않게 한다. */
export function controls(state:AppStatus|undefined,pending:ReadonlySet<Task>) {
  const engine=state?.engine.state??'stopped',running=['starting','ready','stopping'].includes(engine);
  const engineBusy=pending.has('engine-start')||pending.has('engine-stop');
  const configured=Boolean(state?.setup.organization);
  return {
    settingsDisabled:pending.has('settings')||running||engineBusy,
    accountDisabled:pending.has('account'),
    startDisabled:!configured||running||engineBusy||pending.has('settings'),
    startWithoutAiDisabled:!configured||engineBusy||pending.has('settings')||engine==='starting'||engine==='stopping',
    stopDisabled:pending.has('engine-stop')||(!running&&!pending.has('engine-start')),
    reopenDisabled:engine!=='ready'||pending.has('engine-stop'),
    checkRelayDisabled:pending.has('relay-check'),deployDisabled:pending.has('relay-help'),running
  };
}
