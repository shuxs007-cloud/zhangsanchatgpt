import assert from 'node:assert/strict';
import test from 'node:test';
import {validateRenewalInput,checkRenewalBackend,identity,queryPhones,validateExplicitDeadlineSource,archiveResolvedIntent} from '../scripts/opensms_f_renewal_backend.mjs';
import {validateImportInput,checkBackend} from '../scripts/opensms_f_date_import_lib.mjs';
import {writerReceipt} from '../scripts/opensms_manual_f_explicit_deadline.mjs';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';

function fixture(){
  const r={iccid:'8912230200278094459',phone:'4389392694',backendId:'123',slot:'r110_01',
    activation:'2026-09-14',baseCutoffDate:'2026-10-13',sourceCutoff:'2026-10-13',cutoff:'2026-11-12',
    balanceCad:'30',extensionDays:30,status:'充值成功',sourceRow:2};
  r.renewal={version:1,authorization:'current-user-renewal-source',rule:'current_source_cutoff_plus_30_if_balance_gte_30_else_source_cutoff',
    sourceSha256:'a'.repeat(64),queryIccid:r.iccid,phone:r.phone,slot:r.slot,backendId:r.backendId,sourceCutoff:r.sourceCutoff,
    cutoffDate:r.cutoff,activationDate:r.activation,baseCutoffDate:r.baseCutoffDate,status:r.status,balanceCad:r.balanceCad,
    extensionDays:r.extensionDays,sourceRow:r.sourceRow};
  const input={version:1,scope:'current_user_authorized_renewal',runId:'20261013T120500+0800',independentReadbackPassed:true,
    file:'/private/renewal.xlsx',fileSha256:'b'.repeat(64),fileRows:1,sourceSha256:'a'.repeat(64),records:[r]};
  const before=[{id:'123',phone:r.phone,iccid:r.iccid,slot:r.slot,activation:r.activation,cutoff:r.baseCutoffDate}];
  return {r,input,before};
}
test('renewal accepts explicit source +30 without shifting activation',()=>{
  const {input,before}=fixture();assert.equal(validateRenewalInput(input),input);
  assert.equal(checkRenewalBackend(input,before).matchedDates,0);
  assert.equal(checkRenewalBackend(input,[{...before[0],cutoff:'2026-11-12'}]).passed,true);
});
test('CAD60 still extends exactly thirty days, no double extension',()=>{
  const {input,r}=fixture();r.balanceCad='60';r.renewal.balanceCad='60';validateRenewalInput(input);
  r.cutoff='2026-12-12';r.renewal.cutoffDate=r.cutoff;
  assert.throws(()=>validateRenewalInput(input),/RENEWAL_IDENTITY_OR_DATE_INVALID/);
});
test('activation and exact IDs, ICCID, slot remain protected',()=>{
  const {input,before}=fixture();
  for(const changes of [{id:'124'},{iccid:'8912230200278094491'},{slot:'r110_02'},{activation:'2026-09-15'}])
    assert.throws(()=>checkRenewalBackend(input,[{...before[0],...changes}]),/CHANGED/);
  assert.throws(()=>checkRenewalBackend(input,[]),/COVERAGE/);
  assert.throws(()=>checkRenewalBackend(input,[{...before[0],cutoff:'2026-10-14'}],{baseline:before,unchangedDates:true}),/CHANGED/);
});
test('wrong status, source hash, precision or proof cannot enter input',()=>{
  for(const mutate of [r=>r.status='充值失败',r=>r.backendId=9007199254740992,r=>r.renewal.sourceSha256='c'.repeat(64),r=>r.renewal.baseCutoffDate='2026-10-14',r=>r.balanceCad='NaN']){
    const {input,r}=fixture();mutate(r);assert.throws(()=>validateRenewalInput(input));
  }
});
test('original automatic importer is NOT relaxed for renewals',()=>{
  const {input,before}=fixture();assert.throws(()=>validateImportInput(input,input.runId));
  const normal={...input,scope:'current_task_a_system_missing_f_dates',records:[{...input.records[0],cutoff:'2026-10-13'}]};
  validateImportInput(normal,normal.runId);
  assert.throws(()=>checkBackend(normal,[{id:'123',cardNo:before[0].phone,cardStore:before[0].iccid,slotIdentif:before[0].slot,
    chatrActivateDate:'2026-09-14',chatrPlanExpire:'2026-11-12'}]),/CONFLICT/);
});
test('live identity refuses rounded ICCID and rounded backend ID',()=>{
  assert.throws(()=>identity({id:9007199254740992,cardNo:'4389392694',cardStore:'8912230200278094459',slotIdentif:'r110_01'}));
  assert.throws(()=>identity({id:'123',cardNo:'4389392694',cardStore:8912230200278094459,slotIdentif:'r110_01'}));
});
test('query coverage fails on duplicates and out-of-scope rows',async()=>{
  const row={id:'123',cardNo:'4389392694',cardStore:'8912230200278094459',slotIdentif:'r110_01'};
  await assert.rejects(queryPhones({api:async()=>({code:200,total:2,rows:[row,row]})},['4389392694']),/DUPLICATE/);
  await assert.rejects(queryPhones({api:async()=>({code:200,total:1,rows:[row]})},['4389392271']),/OUT_OF_SCOPE/);
});
test('single explicit human deadline needs no invented balance and preserves activation',()=>{
  const {input,before,r}=fixture();input.scope='current_user_authorized_explicit_deadline';delete r.renewal;delete r.balanceCad;
  r.manualDeadline={authorization:'current-user-explicit-deadline-source',rule:'explicit_user_deadline_preserve_live_activation',
    sourceSha256:input.sourceSha256,phone:r.phone,queryIccid:r.iccid,backendId:r.backendId,slot:r.slot,activationDate:r.activation,cutoffDate:r.cutoff};
  const source={version:1,kind:'explicit_manual_renewal_deadline',authorizationSource:'current-user',phone:r.phone,cutoff:r.cutoff};
  validateExplicitDeadlineSource(input,source);assert.equal(checkRenewalBackend(input,[{...before[0],cutoff:r.cutoff}]).passed,true);
  assert.throws(()=>validateImportInput(input,input.runId));
  assert.throws(()=>validateExplicitDeadlineSource(input,{...source,cutoff:'2026-11-13'}),/SOURCE_CHANGED/);
  for(const mutation of [x=>{x.records.push({...x.records[0]});x.fileRows=2;},x=>{x.records[0].activation='2026-02-30';},x=>{x.records[0].manualDeadline.activationDate='2026-09-13';},x=>{x.records[0].manualDeadline.authorization='historical-user';}]){
    const trial=structuredClone(input);mutation(trial);assert.throws(()=>validateRenewalInput(trial));
  }
});
test('artifact informational stdout does not prevent verifying an existing one-row workbook',()=>{
  assert.equal(writerReceipt('Saved workbook inspection\n{"status":"success","recordCount":1}\n').recordCount,1);
  assert.throws(()=>writerReceipt('{"status":"success","recordCount":2}'),/WRITER_UNCONFIRMED/);
  assert.throws(()=>writerReceipt('no JSON receipt'));
});
test('resolved pending intent stays archived on its own disk and is exactly copied to delivery',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'manual-deadline-archive-'));
  try{
    const ledger=path.join(dir,'local'),delivery=path.join(dir,'external');await fs.mkdir(ledger);await fs.mkdir(delivery);
    const pending=path.join(ledger,'pending.json'),bytes=Buffer.from(JSON.stringify({runId:'20261014T041230+0800',scope:'test'}));
    await fs.writeFile(pending,bytes,{flag:'wx'});const hash=crypto.createHash('sha256').update(bytes).digest('hex');
    await assert.rejects(archiveResolvedIntent(pending,delivery,'renewal','a'.repeat(64)),/PENDING_CHANGED/);
    const result=await archiveResolvedIntent(pending,delivery,'renewal',hash);
    assert.equal(path.dirname(path.dirname(result.localArchive)),ledger);
    assert.deepEqual(await fs.readFile(result.copy),bytes);assert.deepEqual(await fs.readFile(result.localArchive),bytes);
    await assert.rejects(fs.access(pending),{code:'ENOENT'});
  }finally{await fs.rm(dir,{recursive:true,force:true});}
});
