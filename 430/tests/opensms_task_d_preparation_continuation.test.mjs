import test from 'node:test';
import assert from 'node:assert/strict';
import { verifyPreparationFailure } from '../scripts/opensms_task_d_preparation_continuation.mjs';
import { RETENTION_FILTERS } from '../scripts/opensms_task_d_filtered_retention.mjs';

function fixture() {
  const runId = '20261013T000000+0800', cardNo = '4160000001';
  const failure = {version:1,runId,task:'D',stage:'task_d_prepare',code:'OPENSMS_TASK_D_ROW_INVALID',
    category:'carrier_identity_missing',cardNo,backendId:'12',slot:'',businessWrites:0,notificationSubmitted:false};
  const state = {runId,status:'failed',taskDPreparationFailure:failure};
  const raw = {runId,task:'D',readOnly:true,businessWrites:0,cardNos:[cardNo],proof:{version:1,
    queries:RETENTION_FILTERS.map(spec => ({...spec,method:'POST',endpoint:'/phone/config/list',cardNos:[cardNo],
      pages:[{pageNum:1,pageSize:500,code:200,total:spec.filter==='chatr'?1:0,
        rows:spec.filter==='chatr'?[{cardNo,id:12,opr:'Chatr',carrireIdentif:null}]:[]}]}))}};
  return {state,raw};
}

test('only the exact stopped zero-submission failure is eligible; original proof bytes are never edited', () => {
  const {state,raw}=fixture(), before=JSON.stringify({state,raw});
  const result=verifyPreparationFailure(state,raw);
  assert.equal(result.preparationOnly,true);
  assert.equal(result.originalContractStillRejected,true);
  assert.equal(result.approvedAnomalyRows.length,1);
  assert.equal(JSON.stringify({state,raw}),before);
});

test('different identity, submissions, forged categories, existing new contracts and missing pages stop offline', () => {
  for(const mutate of [
    (s,r)=>{s.status='running';},
    (s,r)=>{s.taskDPreparationFailure.businessWrites=1;},
    (s,r)=>{s.taskDPreparationFailure.notificationSubmitted=true;},
    (s,r)=>{s.taskDPreparationFailure.category='backend_id_missing';},
    (s,r)=>{s.taskDPreparationFailure.backendId='13';},
    (s,r)=>{r.readOnly=false;},
    (s,r)=>{r.proof.dataAnomalyPolicy='skip_row_data_anomalies_v1';},
    (s,r)=>{r.proof.queries.pop();},
    (s,r)=>{r.proof.queries[0].pages[0].rows[0].carrierIdentif='conflicting';},
  ]) {const {state,raw}=fixture();mutate(state,raw);assert.throws(()=>verifyPreparationFailure(state,raw));}
});
