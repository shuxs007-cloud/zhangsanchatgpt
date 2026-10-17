import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {validateArguments,validateStoppedRetry,validateBatchAgreement,checkedBackend,submitAndVerify} from '../scripts/opensms_manual_f_date_retry.mjs';
const run='20261008T225421+0800';
const input=()=>({version:1,runId:run,scope:'current_task_a_system_missing_f_dates',
  independentReadbackPassed:true,file:'/tmp/current.xlsx',fileSha256:'a'.repeat(64),fileRows:1,
  records:[{iccid:'12345678901234567890',phone:'6470000000',slot:'101_1',activation:'2026-10-01',cutoff:'2026-10-30'}]});
const row=()=>({id:123,cardNo:'6470000000',cardStore:'12345678901234567890F',slotIdentif:'101_1'});
const complete=()=>({...row(),chatrActivateDate:'2026-10-01',chatrPlanExpire:'2026-10-30'});
test('both manual F hidden-page entries wait for application readiness before protocol loading',async()=>{
  for(const name of ['opensms_manual_f_date_retry','opensms_reconcile_f_date_import']){
    const source=await fs.readFile(new URL(`../scripts/${name}.mjs`,import.meta.url),'utf8');
    const create=source.indexOf('page=await createRawBackgroundPage(');
    const ready=source.indexOf('await waitForPayloadPageReady(page.client)',create);
    const protocol=source.indexOf('session=await createRawOpenSmsApiSession(',create);
    assert.ok(create>=0&&ready>create&&protocol>ready);
  }
});
function harness(sequence){let queries=0,time=0,dispatches=0,intents=0,reads=0;const events=[];
  return {args:{query:async()=>{events.push('query');return [sequence[Math.min(queries++,sequence.length-1)]];},
    dispatch:async()=>{dispatches++;events.push('dispatch');return {ok:true,status:200,text:JSON.stringify({code:200,msg:'已提交后台处理，有效 1 行。完成后请刷新列表查看。'})};},
    persistIntent:async()=>{intents++;events.push('intent');},persistReadback:async()=>{reads++;events.push('readback');},
    recheckSource:async()=>events.push('source'),wait:async ms=>{time+=ms;},now:()=>time,timeoutMs:10000},
    stats:()=>({queries,dispatches,intents,reads,events})};
}
test('requires explicit current user, exact runs and absolute state; scheduled flags forbidden',()=>{
  const args=['--retry-pending',run,'--latest-state','/tmp/state.json','--authorization-source','current-user','--json'];
  assert.deepEqual(validateArguments(args),{priorRun:run,state:'/tmp/state.json'});
  for(const changed of [[],[...args,'--force'],args.map(v=>v==='current-user'?'heartbeat':v),args.map(v=>v==='/tmp/state.json'?'state.json':v)])assert.throws(()=>validateArguments(changed));
});
test('overlap allowed only for identical dates and identity; cross-file reused phone blocked',()=>{
  assert.equal(validateBatchAgreement([input(),input()]),1);
  for(const change of [i=>i.records[0].slot='102_1',i=>{i.records[0].activation='2026-09-30';i.records[0].cutoff='2026-10-29';},i=>i.records[0].iccid='22345678901234567890']){
    const other=input();change(other);assert.throws(()=>validateBatchAgreement([input(),other]),/SOURCE_DATE_OR_IDENTITY_CONFLICT/);
  }
});
test('another manual retry requires the exact stopped, cleaned, business-500 attempt',()=>{
  const id='82a236c0-605f-4e0d-bcd6-55a9781d310e',ledger='/tmp/ledger';
  const receipt=`${ledger}/manual_retries/${id}/prior/opensms_f_date_import_${run}.json`;
  const pending={runId:run,file:'/tmp/test.xlsx',sha256:'a'.repeat(64),manualRetry:{attemptId:id,receipt}};
  const summary={attemptId:id,priorRunId:run,status:'stopped',ownedHiddenPageClosed:true,
    finishedAt:'2026-10-11T12:00:00Z',importRequestsSent:1,results:[{receipt}]};
  const old={runId:run,file:pending.file,fileSha256:pending.sha256,status:'submission_outcome_uncertain',
    submitted:true,independentReadbackPassed:false,importRequestsSent:1,acceptanceDiagnostic:{httpStatus:200,responseCode:500}};
  assert.equal(validateStoppedRetry(pending,summary,old,ledger,id),true);
  for(const changed of [{...summary,status:'running'},{...summary,ownedHiddenPageClosed:false},
    {...summary,importRequestsSent:2},{...summary,results:[]}])
    assert.throws(()=>validateStoppedRetry(pending,changed,old,ledger,id));
  assert.throws(()=>validateStoppedRetry(pending,summary,{...old,acceptanceDiagnostic:{httpStatus:504}},ledger,id));
  const args=['--retry-pending',run,'--latest-state','/tmp/state.json','--authorization-source','current-user','--json'];
  assert.equal(validateArguments([...args,'--retry-attempt',id]).retryAttempt,id);
  assert.throws(()=>validateArguments([...args,'--retry-attempt','../unsafe']));
});
test('business 500 is checked twice and stops without replay or long acceptance polling',async()=>{
  const h=harness([row()]);let sent=0;
  h.args.dispatch=async()=>{sent++;return {ok:true,status:200,text:JSON.stringify({code:500,msg:'private'})};};
  await assert.rejects(submitAndVerify(input(),[row()],h.args),error=>
    error.message==='F_RETRY_BACKEND_BUSINESS_ERROR_NOT_VERIFIED'&&error.partialResult.verifiedDatePairs===0);
  assert.equal(sent,1);assert.equal(h.stats().reads,2);assert.equal(h.stats().queries,3);
});
test('unsafe numeric backend ID is rejected; exact string ID accepted',()=>{
  assert.throws(()=>checkedBackend(input(),[{...row(),id:Number.MAX_SAFE_INTEGER+1}]),/PRECISION_INVALID/);
  assert.equal(checkedBackend(input(),[{...row(),id:'123'}]).matchedDates,0);
});
test('durable intent precedes one dispatch; completion needs two independent reads',async()=>{
  const h=harness([row(),row(),complete(),complete()]);
  const result=await submitAndVerify(input(),[row()],h.args);
  assert.equal(result.status,'passed');assert.equal(result.independentReadbackPassed,true);
  assert.equal(result.acceptedRows,1);assert.equal(h.stats().dispatches,1);assert.equal(h.stats().intents,1);
  assert.equal(h.stats().reads,3);assert.ok(h.stats().events.indexOf('intent')<h.stats().events.indexOf('dispatch'));
});
test('already identical dates are not uploaded again; two new reads still required',async()=>{
  const h=harness([complete(),complete(),complete()]);const result=await submitAndVerify(input(),[row()],h.args);
  assert.equal(result.status,'skipped_backend_already_matches');assert.equal(result.submitted,false);
  assert.equal(h.stats().dispatches,0);assert.equal(h.stats().queries,3);assert.equal(h.stats().reads,2);
});
test('unrecognized acceptance is a warning; actual two-read dates can prove completion',async()=>{
  const h=harness([row(),complete(),complete()]);
  h.args.dispatch=async()=>({ok:true,status:200,text:JSON.stringify({code:200,msg:'success'})});
  const result=await submitAndVerify(input(),[row()],h.args);
  assert.equal(result.status,'passed');assert.equal(result.acceptedRows,null);
  assert.equal(result.acceptanceWarning,'F_IMPORT_ACCEPTED_COUNT_UNCONFIRMED');
  assert.equal(result.independentReadbackPassed,true);
});
test('backend conflict and changed identity stop before any upload',async()=>{
  for(const changed of [{...row(),id:456},{...row(),chatrActivateDate:'2026-09-01'},{...row(),slotIdentif:'other'}]){
    const h=harness([changed]);await assert.rejects(submitAndVerify(input(),[row()],h.args),/F_IMPORT_/);
    assert.equal(h.stats().dispatches,0);assert.equal(h.stats().intents,0);
  }
});
test('source or durable intent failures send no import',async()=>{
  for(const stage of ['recheckSource','persistIntent']){const h=harness([row()]);
    h.args[stage]=async()=>{throw Error('F_RETRY_TEST_PRE_SUBMIT_FAILURE');};
    await assert.rejects(submitAndVerify(input(),[row()],h.args),/PRE_SUBMIT_FAILURE/);assert.equal(h.stats().dispatches,0);
  }
});
test('response acceptance without actual dates times out; never dispatches twice',async()=>{
  const h=harness([row()]);await assert.rejects(submitAndVerify(input(),[row()],h.args),e=>
    e.message==='F_RETRY_DATES_STILL_UNCONFIRMED'&&e.partialResult.acceptedRows===1&&e.partialResult.verifiedDatePairs===0);
  assert.equal(h.stats().dispatches,1);assert.equal(h.stats().intents,1);
});
test('unknown request completion stops immediately; no read or automatic replay',async()=>{
  const h=harness([row()]);let sent=0;
  h.args.dispatch=async()=>{sent++;throw Object.assign(Error('F_RETRY_REQUEST_UNCONFIRMED'),{requestCompletionUnconfirmed:true});};
  await assert.rejects(submitAndVerify(input(),[row()],h.args),/REQUEST_UNCONFIRMED/);
  assert.equal(sent,1);assert.equal(h.stats().queries,1);assert.equal(h.stats().reads,0);
});
test('confirmed completed request error permits only readback, not replay',async()=>{
  const h=harness([row(),complete(),complete()]);let sent=0;
  h.args.dispatch=async()=>{sent++;throw Object.assign(Error('F_RETRY_REQUEST_ERROR'),{requestCompletionConfirmed:true});};
  const result=await submitAndVerify(input(),[row()],h.args);assert.equal(result.status,'passed');assert.equal(sent,1);
});
test('identity changes after upload stop even when expected dates appear',async()=>{
  const h=harness([row(),{...complete(),id:456}]);await assert.rejects(submitAndVerify(input(),[row()],h.args),/IDENTITY_CHANGED/);
  assert.equal(h.stats().dispatches,1);
});
