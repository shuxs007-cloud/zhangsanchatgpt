import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {validatePending,validateReconcileArguments,validateCurrentSessionProof} from '../scripts/opensms_reconcile_f_date_import.mjs';

const runId='20261008T225421+0800';
const input={version:1,runId,scope:'current_task_a_system_missing_f_dates',independentReadbackPassed:true,
  file:'/immutable.xlsx',fileSha256:'a'.repeat(64),fileRows:1,
  records:[{phone:'6470000001',slot:'1_1',iccid:'8912230200000000001',activation:'2026-10-01',cutoff:'2026-10-30'}]};
const pending={runId,status:'submission_intent',file:input.file,sha256:input.fileSha256};
const receipt={runId,status:'submission_outcome_uncertain',submitted:true,independentReadbackPassed:false,
  file:input.file,fileSha256:input.fileSha256,fileRows:1};
const intent={runId,submitted:true,fileSha256:input.fileSha256};
test('exact uncertain pending input is eligible only for readback',()=>{
  assert.doesNotThrow(()=>validatePending(pending,input,receipt,intent,runId));
});
test('changed file or count cannot resolve intent',()=>{
  for(const changed of [{fileRows:2},{fileSha256:'b'.repeat(64)},{submitted:false},{runId:'20261008T231127+0800'}])
    assert.throws(()=>validatePending(pending,input,{...receipt,...changed},intent,runId));
});
test('already terminal receipt is not replayed',()=>{
  assert.throws(()=>validatePending(pending,input,{...receipt,status:'passed'},intent,runId));
});
test('session restart reconciliation needs an explicit absolute current state',()=>{
  assert.deepEqual(validateReconcileArguments([runId,'current-user']),{runId,currentStatePath:null});
  assert.equal(validateReconcileArguments([runId,'current-user','--current-state','/current.json']).currentStatePath,'/current.json');
  for(const args of [[runId,'scheduler'],[runId,'current-user','--current-state','relative.json'],[runId,'current-user','--ignore-browser']])
    assert.throws(()=>validateReconcileArguments(args));
});
const currentId='20261011T213723+0800';
const currentPath=`/receipts/opensms_patrol_runner_state_${currentId}.json`;
const priorState={runId,browser:{sessionId:'old-session'}};
const currentState={runId:currentId,status:'success',browser:{sessionId:'live-session'},
  fDateImport:{status:'failed_before_submission',submitted:false,blockingRunId:runId}};
test('authorized current successful blocked run may prove the restarted session',()=>{
  const proof=validateCurrentSessionProof(priorState,currentState,currentPath,{sessionId:'live-session'});
  assert.equal(proof.currentRunId,currentId);
  assert.equal(proof.verification,'two_exact_backend_identity_and_date_readbacks_required');
});
test('wrong session, pending source, successful import or failed current run stay blocked',()=>{
  for(const changed of [{status:'running'},{runId},{browser:{sessionId:'other-session'}},
    {fDateImport:{...currentState.fDateImport,submitted:true}},
    {fDateImport:{...currentState.fDateImport,blockingRunId:'20261007T050709+0800'}},
    {fDateImport:{...currentState.fDateImport,status:'passed'}}])
    assert.throws(()=>validateCurrentSessionProof(priorState,{...currentState,...changed},currentPath,{sessionId:'live-session'}));
  assert.throws(()=>validateCurrentSessionProof(priorState,currentState,'/wrong.json',{sessionId:'live-session'}));
});
test('reconciler has no upload or backend mutation entry',async()=>{
  const code=await fs.readFile(new URL('../scripts/opensms_reconcile_f_date_import.mjs',import.meta.url),'utf8');
  for(const forbidden of ['uploadOnce(',"'/api/phone/config/importExpire'",'updatePhoneCarrier(','session.api(\'PUT\''])
    assert.equal(code.includes(forbidden),false);
});
