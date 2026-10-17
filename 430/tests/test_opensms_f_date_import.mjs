import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {validateImportInput,checkBackend,validateImportAcceptance} from '../scripts/opensms_f_date_import_lib.mjs';
import {uploadOnce,assertNoPendingImport,blockingImportDiagnostic,main} from '../scripts/opensms_f_date_import.mjs';
const run='20261003T120000+0800';
const input=()=>({version:1,runId:run,scope:'current_task_a_system_missing_f_dates',
  independentReadbackPassed:true,file:'/tmp/current.xlsx',fileSha256:'a'.repeat(64),fileRows:1,
  records:[{iccid:'12345678901234567890',phone:'6470000000',slot:'101_1',activation:'2026-10-01',cutoff:'2026-10-30'}]});
const row=()=>({id:123,cardNo:'6470000000',cardStore:'12345678901234567890F',slotIdentif:'101_1',chatrActivateDate:null,chatrPlanExpire:null});
test('exact current nonempty validated input only',()=>{
  assert.equal(validateImportInput(input(),run).length,1);
  for(const mutate of [i=>i.runId='old',i=>i.fileRows=0,i=>i.independentReadbackPassed=false,
    i=>i.records[0].activation='2026-99-01',i=>i.records[0].cutoff='2026-10-31',
    i=>{i.records.push({...i.records[0]});i.fileRows=2;}]){
    const i=input();mutate(i);assert.throws(()=>validateImportInput(i,run),/F_IMPORT_/);
  }
});
test('identity, conflict and independent date readback gates',()=>{
  const before=[row()];assert.equal(checkBackend(input(),before).matchedDates,0);
  assert.equal(checkBackend(input(),before,before,true).passed,false);
  const after={...row(),chatrActivateDate:'2026-10-01',chatrPlanExpire:'2026-10-30'};
  assert.equal(checkBackend(input(),[after],before,true).passed,true);
  for(const changed of [{...after,id:456},{...after,slotIdentif:'other'},
    {...after,chatrPlanExpire:'2026-10-29'},{...after,cardStore:'99999999999999999999'}]){
    assert.throws(()=>checkBackend(input(),[changed],before,true),/F_IMPORT_/);
  }
});
test('accepted is not completed; count and HTTP must match',()=>{
  const r={ok:true,status:200,text:JSON.stringify({code:200,msg:'已提交后台处理，有效 1 行。完成后请刷新列表查看。'})};
  assert.equal(validateImportAcceptance(r,1).completed,false);
  assert.throws(()=>validateImportAcceptance(r,2));
  for(const bad of [{...r,status:403},{...r,text:'unknown'},{...r,text:'{"code":200,"msg":"成功"}'}])assert.throws(()=>validateImportAcceptance(bad,1));
});
test('failed acceptance retains only safe counts; malformed JSON cannot hide its code',()=>{
  const response=text=>({ok:true,status:200,text});
  assert.throws(()=>validateImportAcceptance(response(JSON.stringify({code:200,
    msg:'已提交后台处理，有效 2 行。完成后请刷新列表查看。'})),3),error=>{
    assert.equal(error.message,'F_IMPORT_ACCEPTED_COUNT_UNCONFIRMED');
    assert.deepEqual(error.acceptanceDiagnostic,{httpStatus:200,expectedRows:3,
      responseCode:200,acceptedRows:2,messageShapeMatched:true});return true;
  });
  for(const text of ['null','[]','"private credential text"','{"code":200,"msg":"private credential text"}']){
    assert.throws(()=>validateImportAcceptance(response(text),3),error=>{
      assert.equal(error.message,'F_IMPORT_ACCEPTED_COUNT_UNCONFIRMED');
      assert.equal(error.acceptanceDiagnostic.acceptedRows,null);
      assert.equal(JSON.stringify(error.acceptanceDiagnostic).includes('private'),false);return true;
    });
  }
  assert.throws(()=>validateImportAcceptance(response('invalid JSON'),3),error=>
    error.message==='F_IMPORT_RESPONSE_INVALID' && error.acceptanceDiagnostic.acceptedRows===null);
});
test('omitted null dates are not identity changes or completion evidence',()=>{
  const omitted=row();delete omitted.chatrActivateDate;delete omitted.chatrPlanExpire;
  assert.equal(checkBackend(input(),[omitted]).matchedDates,0);
  assert.equal(checkBackend(input(),[omitted],[omitted],true).passed,false);
  const partial={...omitted,chatrActivateDate:'2026-10-01'};
  assert.equal(checkBackend(input(),[partial],[omitted],true).passed,false);
  const after={...partial,chatrPlanExpire:'2026-10-30'};
  assert.equal(checkBackend(input(),[after],[omitted],true).passed,true);
  assert.throws(()=>checkBackend(input(),[{...omitted,cardStore:'99999999999999999999'}]),/IDENTITY_CHANGED/);
  assert.throws(()=>checkBackend(input(),[{...omitted,chatrPlanExpire:'2026-10-29'}]),/DATE_CONFLICT/);
});
test('HTTP success with business 500 is a business failure, not a count error',()=>{
  assert.throws(()=>validateImportAcceptance({ok:true,status:200,
    text:JSON.stringify({code:500,msg:'private backend details'})},3),error=>{
    assert.equal(error.message,'F_IMPORT_BACKEND_BUSINESS_ERROR');
    assert.equal(error.acceptanceDiagnostic.responseCode,500);
    assert.equal(JSON.stringify(error.acceptanceDiagnostic).includes('private'),false);
    return true;
  });
});
test('blocker diagnostics prefer exact latest manual receipt without exposing records',async t=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'opensms-f-blocker-'));
  t.after(()=>fs.rm(dir,{recursive:true,force:true}));
  const attemptId=crypto.randomUUID(),priorRun='20261008T225421+0800';
  const step=path.join(dir,'manual_retries',attemptId,'prior');await fs.mkdir(step,{recursive:true});
  const receipt=path.join(step,`opensms_f_date_import_${priorRun}.json`);
  const pending={runId:priorRun,file:'/tmp/test.xlsx',sha256:'a'.repeat(64),manualRetry:{attemptId,receipt}};
  const value={runId:priorRun,file:pending.file,fileSha256:pending.sha256,status:'submission_outcome_uncertain',
    submitted:true,independentReadbackPassed:false,fileRows:3,verifiedDatePairs:0,
    acceptanceDiagnostic:{httpStatus:200,responseCode:500},records:['private']};
  await fs.writeFile(receipt,JSON.stringify(value));
  const diagnostic=await blockingImportDiagnostic(pending,dir);
  assert.equal(diagnostic.fileRows,3);assert.equal(diagnostic.responseCode,500);
  assert.equal(diagnostic.currentBackendVerified,false);assert.equal(JSON.stringify(diagnostic).includes('private'),false);
  for(const changed of [{...pending,sha256:'b'.repeat(64)},
    {...pending,manualRetry:{attemptId,receipt:'/tmp/another.json'}}]) {
    const unknown=await blockingImportDiagnostic(changed,dir);
    assert.equal(unknown.responseCode,undefined);assert.equal(unknown.fileRows,undefined);
  }
});
test('uploader sends exactly once, polls without replay',async()=>{
  let dispatch=0;
  const client={evaluate:async expression=>{
    if(expression.includes('new FormData()')){dispatch++;return {started:true};}
    return {done:true,response:{ok:true,status:200,text:'done'}};
  }};
  const r=await uploadOnce(client,{name:'test.xlsx',base64:''},'test');
  assert.equal(r.text,'done');assert.equal(dispatch,1);
});
test('unconfirmed timeout never replays upload',async()=>{
  let dispatch=0,time=0;
  const client={evaluate:async expression=>{
    if(expression.includes('new FormData()')){dispatch++;return {started:true};}
    return {done:false};
  }};
  await assert.rejects(uploadOnce(client,{name:'test.xlsx',base64:''},'test',
    {clock:()=>time,wait:async ms=>{time+=ms;}}),e=>e.requestCompletionUnconfirmed===true);
  assert.equal(dispatch,1);
});
test('missing intent allows preflight, malformed intent still blocks',async t=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'opensms-f-import-'));
  t.after(()=>fs.rm(dir,{recursive:true,force:true}));
  const pending=path.join(dir,'pending.json');
  await assert.doesNotReject(assertNoPendingImport(pending));
  for(const value of ['invalid JSON',JSON.stringify({runId:'not-a-run'})]){
    await fs.writeFile(pending,value);
    await assert.rejects(assertNoPendingImport(pending),error=>
      error.message==='F_IMPORT_PRIOR_SUBMISSION_UNRESOLVED' && !error.blockingRunId);
    assert.equal(await fs.readFile(pending,'utf8'),value);
  }
});
test('old unresolved intent produces exact current receipt without browser or replay',async t=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'opensms-f-import-'));
  t.after(()=>fs.rm(dir,{recursive:true,force:true}));
  const ledger=path.join(dir,'ledger');await fs.mkdir(ledger);
  const pending=path.join(ledger,'pending.json');
  const blockingRunId='20261008T225421+0800';
  const pendingBytes=JSON.stringify({runId:blockingRunId,status:'submission_intent'});
  await fs.writeFile(pending,pendingBytes);
  const file=path.join(dir,'current.xlsx'),fileBytes=Buffer.from('offline fixture');
  await fs.writeFile(file,fileBytes);
  const current={...input(),file,fileSha256:crypto.createHash('sha256').update(fileBytes).digest('hex')};
  const inputPath=path.join(dir,'input.json'),receiptPath=path.join(dir,'receipt.json');
  await fs.writeFile(inputPath,JSON.stringify(current));
  const keys=['OPENSMS_RUN_ID','OPENSMS_RAW_API_TARGET_ID','OPENSMS_F_DATE_IMPORT_RUN_ID'];
  const previous=Object.fromEntries(keys.map(key=>[key,process.env[key]]));
  process.env.OPENSMS_RUN_ID=run;process.env.OPENSMS_RAW_API_TARGET_ID='offline-only';
  process.env.OPENSMS_F_DATE_IMPORT_RUN_ID=run;
  t.after(()=>{for(const key of keys){if(previous[key]===undefined)delete process.env[key];else process.env[key]=previous[key];}});
  t.mock.method(console,'log',()=>{});
  let sessionCalls=0;
  await assert.rejects(main([inputPath,receiptPath],{ledgerPath:ledger,createSession:async()=>{
    sessionCalls++;throw Error('test must not open a browser');
  }}),/F_IMPORT_PRIOR_SUBMISSION_UNRESOLVED/);
  const receipt=JSON.parse(await fs.readFile(receiptPath,'utf8'));
  assert.equal(receipt.runId,run);
  assert.equal(receipt.error,'F_IMPORT_PRIOR_SUBMISSION_UNRESOLVED');
  assert.equal(receipt.blockingRunId,blockingRunId);
  assert.equal(receipt.status,'failed_before_submission');
  assert.equal(receipt.submitted,false);assert.equal(receipt.cloudUploadAttempted,false);
  assert.equal(receipt.independentReadbackPassed,false);assert.equal(sessionCalls,0);
  assert.equal(await fs.readFile(pending,'utf8'),pendingBytes);
  assert.deepEqual(await fs.readdir(ledger),['pending.json']);
  await assert.rejects(fs.access(receiptPath+'.intent.json'),{code:'ENOENT'});
  const original=await fs.readFile(receiptPath);
  await assert.rejects(main([inputPath,receiptPath],{ledgerPath:ledger,createSession:async()=>{
    sessionCalls++;throw Error('replay must not open a browser');
  }}),/F_IMPORT_REPLAY_FORBIDDEN/);
  assert.deepEqual(await fs.readFile(receiptPath),original);assert.equal(sessionCalls,0);
});
