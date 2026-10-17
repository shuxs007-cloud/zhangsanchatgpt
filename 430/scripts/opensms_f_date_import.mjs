// Called only by the normal Task A runner with its exact hidden page and leases.
// One multipart submission; acceptance alone is not completion. No browser launch.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {setTimeout as sleep} from 'node:timers/promises';
import {createRawOpenSmsApiSession} from '../opensms_protocol_client.mjs';
import {withOpenSmsRequestPacing} from '../opensms_request_pacing.mjs';
import {validateImportInput,checkBackend,validateImportAcceptance} from './opensms_f_date_import_lib.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
async function save(file, value) {
  const handle = await fs.open(file, 'wx', 0o600);
  try {await handle.writeFile(JSON.stringify(value,null,2));await handle.sync();} finally {await handle.close();}
}
export async function assertNoPendingImport(pending) {
  let bytes;
  try {bytes=await fs.readFile(pending,'utf8');}
  catch(error){if(error.code==='ENOENT')return;throw error;}
  // Even an unreadable/invalid intent must block submission. Never remove it
  // merely because this run is new, or because the prior dates are still blank.
  const error=new Error('F_IMPORT_PRIOR_SUBMISSION_UNRESOLVED');
  try {
    const value=JSON.parse(bytes);
    if(/^\d{8}T\d{6}\+0800$/.test(value?.runId||'')) {
      error.blockingRunId=value.runId;
      error.blockingImport=await blockingImportDiagnostic(value,path.dirname(pending));
    }
  }catch{}
  throw error;
}
export async function blockingImportDiagnostic(pending, ledger) {
  const unknown={source:'local_historical_receipt',currentBackendVerified:false};
  const retry=pending.manualRetry;
  const receiptPath=retry?.receipt||pending.receipt;
  if(!path.isAbsolute(receiptPath||'')
      ||path.basename(receiptPath)!==`opensms_f_date_import_${pending.runId}.json`)return unknown;
  if(retry && (!/^[a-f0-9-]{36}$/.test(retry.attemptId||'')
      ||receiptPath!==path.join(ledger,'manual_retries',retry.attemptId,'prior',
        `opensms_f_date_import_${pending.runId}.json`)))return unknown;
  try {
    const receipt=JSON.parse(await fs.readFile(receiptPath,'utf8'));
    if(receipt.runId!==pending.runId||receipt.file!==pending.file
        ||receipt.fileSha256!==pending.sha256||receipt.submitted!==true
        ||receipt.independentReadbackPassed!==false
        ||receipt.status!=='submission_outcome_uncertain')return unknown;
    const count=v=>Number.isSafeInteger(v)&&v>=0?v:null;
    return {...unknown,receipt:receiptPath,fileRows:count(receipt.fileRows),
      verifiedDatePairs:count(receipt.verifiedDatePairs),
      httpStatus:count(receipt.acceptanceDiagnostic?.httpStatus),
      responseCode:count(receipt.acceptanceDiagnostic?.responseCode)};
  }catch{return unknown;}
}
export async function uploadOnce(client, input, authorization, {clock=Date.now,wait=sleep}={}) {
  const key = '__opensms_f_import_'+crypto.randomUUID().replaceAll('-','');
  const quoted = JSON.stringify(key), started=clock();
  const read=abort=>client.evaluate(`(() => {
    if(location.origin!=='https://adminsms.smsoug.com') throw Error('IMPORT_ORIGIN_CHANGED');
    const key=${quoted}, job=globalThis[key];
    if(!job){ if(${abort})globalThis[key]={cancelled:true};return {missing:true}; }
    if(${abort}&&!job.done)job.controller.abort();
    if(!job.done)return {done:false};
    delete globalThis[key];return {done:true,response:job.response};
  })()`,5000);
  try {
    const dispatch=await client.evaluate(`(() => {
      if(location.origin!=='https://adminsms.smsoug.com') throw Error('IMPORT_ORIGIN_CHANGED');
      const key=${quoted};if(Object.hasOwn(globalThis,key))throw Error('IMPORT_DISPATCH_FENCED');
      const job={controller:new AbortController(),done:false};globalThis[key]=job;
      const input=${JSON.stringify(input)};
      (async()=>{try {
        const bytes=Uint8Array.from(atob(input.base64),c=>c.charCodeAt(0));
        const body=new FormData();body.append('file',new Blob([bytes],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}),input.name);
        const r=await fetch('/api/phone/config/importExpire',{method:'POST',credentials:'include',
          signal:job.controller.signal,headers:{Authorization:${JSON.stringify(authorization)}},body});
        job.response={ok:r.ok,status:r.status,text:await r.text()};
      }catch {job.response={transportError:true};}finally{job.done=true;}})();
      return {started:true};
    })()`,5000);
    if(dispatch?.started!==true)throw Error('F_IMPORT_DISPATCH_UNCONFIRMED');
    while(clock()-started<60000){
      const result=await read(false);if(result?.done)return result.response;
      if(result?.missing)throw Error('F_IMPORT_CONTEXT_LOST');await wait(1000);
    }
    throw Error('F_IMPORT_SUBMISSION_TIMEOUT');
  }catch(error){
    let confirmed=false;const deadline=clock()+10000;
    try {while(clock()<deadline){const result=await read(true);
      if(result?.done){confirmed=true;if(result.response?.status)return result.response;break;}
      if(result?.missing)break;await wait(250);}}catch{}
    error.requestCompletionConfirmed=confirmed;error.requestCompletionUnconfirmed=!confirmed;
    throw error;
  }
}
export async function main(argv=process.argv.slice(2), {
  ledgerPath=path.join(root,'opensms_artifacts/state/f_date_import'),
  createSession=createRawOpenSmsApiSession,
}={}) {
  const [inputPath,receiptPath,...extra]=argv, runId=process.env.OPENSMS_RUN_ID;
  if(extra.length||!path.isAbsolute(inputPath||'')||!path.isAbsolute(receiptPath||'')
      ||!process.env.OPENSMS_RAW_API_TARGET_ID
      ||process.env.OPENSMS_F_DATE_IMPORT_RUN_ID!==runId)throw Error('F_IMPORT_RUNNER_GATE_MISSING');
  const policy=JSON.parse(await fs.readFile(path.join(root,'scripts/opensms_f_date_import_policy.json'),'utf8'));
  if(policy.enabled!==true||policy.authorization!=='current-user-standing-task-a-new-f-file'
      ||policy.scope!=='current_task_a_system_missing_f_dates'||policy.retrySubmission!==false
      ||policy.allowEmpty!==false||policy.allowHistoricalFiles!==false
      ||policy.overwriteExistingConflictingDates!==false)throw Error('F_IMPORT_STANDING_POLICY_INVALID');
  const input=JSON.parse(await fs.readFile(inputPath,'utf8'));validateImportInput(input,runId);
  const bytes=await fs.readFile(input.file);if(digest(bytes)!==input.fileSha256)throw Error('F_IMPORT_FILE_CHANGED');
  const ledger=ledgerPath;await fs.mkdir(ledger,{recursive:true,mode:0o700});
  for(const existing of [receiptPath,receiptPath+'.intent.json',path.join(ledger,runId+'.json')]) {
    try {await fs.access(existing);throw Error('F_IMPORT_REPLAY_FORBIDDEN');}
    catch(error){if(error.code!=='ENOENT')throw error;}
  }
  const pending=path.join(ledger,'pending.json');
  const receipt={version:1,runId,file:input.file,fileSha256:input.fileSha256,fileRows:input.fileRows,
    status:'preflight',submitted:false,cloudUploadAttempted:false,independentReadbackPassed:false,
    silentBrowserMode:true,receipt:receiptPath};
  let session, failure;
  const query=async()=>{
    const rows=[];
    for(let offset=0;offset<input.records.length;offset+=500){
      const phones=input.records.slice(offset,offset+500).map(r=>r.phone);
      const result=await session.api('POST','/phone/config/list',
        {cardNo:phones.join('\n'),showAble:null,pageNum:1,pageSize:500},{pageNum:1,pageSize:500});
      if(result.code!==200||!Array.isArray(result.rows)||result.total!==result.rows.length
          ||result.rows.length!==phones.length)throw Error('F_IMPORT_READBACK_INCOMPLETE');
      rows.push(...result.rows);
    }return rows;
  };
  try {
    // Publish a current-run failure receipt even when a previous intent blocks
    // us, before creating a browser client or issuing any business request.
    await assertNoPendingImport(pending);
    session=await createSession({cdp:process.env.BITBROWSER_CDP,pathName:'/index'});
    const before=await query();await save(receiptPath+'.before.json',before);
    const checked=checkBackend(input,before);
    if(checked.matchedDates===input.fileRows){
      receipt.status='skipped_backend_already_matches';receipt.independentReadbackPassed=true;
    }else{
      if(digest(await fs.readFile(input.file))!==input.fileSha256)throw Error('F_IMPORT_FILE_CHANGED');
      await save(pending,{runId,file:input.file,sha256:input.fileSha256,receipt:receiptPath,status:'submission_intent'});
      receipt.cloudUploadAttempted=true;receipt.submitted=true;receipt.status='submission_outcome_uncertain';
      // Persist before sending. Existing receipts and pending intents forbid replay.
      await save(receiptPath+'.intent.json',receipt);
      const auth=session.token.startsWith('Bearer ')?session.token:`Bearer ${session.token}`;
      const accepted=await withOpenSmsRequestPacing(async()=>validateImportAcceptance(
        await uploadOnce(session.client,{name:path.basename(input.file),base64:bytes.toString('base64')},auth),input.fileRows));
      Object.assign(receipt,accepted);
      receipt.status='accepted_pending_readback';
      await save(receiptPath+'.accepted.json',receipt);
      const deadline=Date.now()+300000;
      do {
        await sleep(5000);
        const after=await query();const verified=checkBackend(input,after,before,true);
        if(verified.passed){await save(receiptPath+'.after.json',after);
          receipt.status='passed';receipt.independentReadbackPassed=true;receipt.verifiedDatePairs=input.fileRows;break;}
      }while(Date.now()<deadline);
      if(!receipt.independentReadbackPassed)throw Error('F_IMPORT_ACCEPTED_NOT_VERIFIED');
      await save(path.join(ledger,runId+'.json'),receipt);
      const intent=JSON.parse(await fs.readFile(pending,'utf8'));
      if(intent.runId!==runId||intent.sha256!==input.fileSha256||intent.receipt!==receiptPath)throw Error('F_IMPORT_INTENT_CHANGED');
      await fs.unlink(pending); // Only our fully verified intent, under the same website lease.
    }
  }catch(error){failure=error;receipt.status=receipt.submitted?'submission_outcome_uncertain':'failed_before_submission';
    receipt.error=/^F_IMPORT_[A-Z_]+$/.test(error.message)?error.message:'F_IMPORT_OPERATION_FAILED';
    if(error.blockingRunId)receipt.blockingRunId=error.blockingRunId;
    if(error.blockingImport)receipt.blockingImport=error.blockingImport;
    // Numeric protocol diagnostics only: never archive an arbitrary response
    // body, login token, or an acceptance message containing personal data.
    if(error.acceptanceDiagnostic)receipt.acceptanceDiagnostic=error.acceptanceDiagnostic;
  }finally{session?.client.close();await save(receiptPath,receipt);}
  console.log(JSON.stringify(receipt));if(failure)throw Error(receipt.error);
  return receipt;
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  main().catch(error=>{console.error(error.message);process.exitCode=1;});
}
