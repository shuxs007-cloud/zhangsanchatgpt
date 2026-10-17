// Explicit human-authorized, bounded import retry. Never called by Task A/F.
// Preserve old receipts; reuse the original uploader, pacing and identity guards.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
import {isDeepStrictEqual} from 'node:util';
import {setTimeout as sleep} from 'node:timers/promises';
import {acquireSharedOpenSmsSession} from './opensms_shared_browser.mjs';
import {createRawBackgroundPage,createRawOpenSmsApiSession,listPhonesByNumbers} from '../opensms_protocol_client.mjs';
import {withOpenSmsRequestPacing} from '../opensms_request_pacing.mjs';
import {waitForPayloadPageReady} from '../opensms_payload_readiness.mjs';
import {uploadOnce} from './opensms_f_date_import.mjs';
import {validateImportInput,checkBackend,validateImportAcceptance} from './opensms_f_date_import_lib.mjs';
import {validatePending} from './opensms_reconcile_f_date_import.mjs';
import {atomicJson} from './opensms_execute_task_g.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const python='/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3';
const sha=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const runPattern=/^\d{8}T\d{6}\+0800$/;
const read=async file=>JSON.parse(await fs.readFile(file,'utf8'));
const safeError=error=>/^[A-Z][A-Z0-9_]+$/.test(error?.message||'')?error.message:
  /^[A-Z][A-Z0-9_]+$/.test(error?.code||'')?error.code:'F_RETRY_OPERATION_UNCONFIRMED';
async function syncDir(directory){const h=await fs.open(directory,'r');try{await h.sync();}finally{await h.close();}}
async function preserveBytes(file,bytes){
  const h=await fs.open(file,'wx',0o600);try{await h.writeFile(bytes);await h.sync();}finally{await h.close();}
  await syncDir(path.dirname(file));
  if(sha(await fs.readFile(file))!==sha(bytes))throw Error('F_RETRY_EVIDENCE_COPY_UNCONFIRMED');
}

export function checkedBackend(input,rows,before=null,requireDates=false){
  for(const row of rows||[]){
    if(!((typeof row.id==='string'&&/^[1-9]\d*$/.test(row.id))
      ||(typeof row.id==='number'&&Number.isSafeInteger(row.id)&&row.id>0)))throw Error('F_RETRY_BACKEND_ID_PRECISION_INVALID');
  }
  return checkBackend(input,rows,before,requireDates);
}

export function validateArguments(argv){
  const [flag,priorRun,latestFlag,state,authFlag,auth,jsonFlag,...extra]=argv;
  if(flag!=='--retry-pending'||!runPattern.test(priorRun||'')
    ||latestFlag!=='--latest-state'||!path.isAbsolute(state||'')
    ||authFlag!=='--authorization-source'||auth!=='current-user'||jsonFlag!=='--json')throw Error('F_RETRY_CURRENT_USER_REQUIRED');
  if(extra.length){
    if(extra.length!==2||extra[0]!=='--retry-attempt'||! /^[a-f0-9-]{36}$/.test(extra[1]))throw Error('F_RETRY_CURRENT_USER_REQUIRED');
    return {priorRun,state,retryAttempt:extra[1]};
  }
  return {priorRun,state};
}

export function validateStoppedRetry(pending,summary,receipt,ledger,attempt){
  const expected=path.join(ledger,'manual_retries',attempt,'prior',`opensms_f_date_import_${pending.runId}.json`);
  if(pending.manualRetry?.attemptId!==attempt||pending.manualRetry.receipt!==expected
    ||summary.attemptId!==attempt||summary.priorRunId!==pending.runId
    ||summary.status!=='stopped'||summary.ownedHiddenPageClosed!==true||!summary.finishedAt
    ||summary.importRequestsSent!==1||summary.results?.length!==1
    ||summary.results[0].receipt!==expected||receipt.runId!==pending.runId
    ||receipt.file!==pending.file||receipt.fileSha256!==pending.sha256
    ||receipt.status!=='submission_outcome_uncertain'||receipt.submitted!==true
    ||receipt.independentReadbackPassed!==false||receipt.importRequestsSent!==1
    ||receipt.acceptanceDiagnostic?.httpStatus!==200||receipt.acceptanceDiagnostic.responseCode!==500)
    throw Error('F_RETRY_PRIOR_ATTEMPT_NOT_CONFIRMED_STOPPED');
  return true;
}

export function validateBatchAgreement(inputs){
  const records=new Map(),phones=new Map();
  for(const input of inputs){validateImportInput(input,input.runId);
    for(const record of input.records){const previous=records.get(record.iccid);
      if(previous&&!isDeepStrictEqual(previous,record))throw Error('F_RETRY_SOURCE_DATE_OR_IDENTITY_CONFLICT');
      if(phones.has(record.phone)&&phones.get(record.phone)!==record.iccid)throw Error('F_RETRY_SOURCE_DATE_OR_IDENTITY_CONFLICT');
      phones.set(record.phone,record.iccid);
      records.set(record.iccid,record);
    }
  }
  return records.size;
}

export async function submitAndVerify(input,before,{
  query,dispatch,persistIntent,persistReadback,recheckSource,wait=sleep,now=Date.now,timeoutMs=300000,
}){
  validateImportInput(input,input.runId);
  const fresh=await query();const preflight=checkedBackend(input,fresh,before);
  const result={runId:input.runId,file:input.file,fileSha256:input.fileSha256,fileRows:input.fileRows,status:'preflight',submitted:false,
    importRequestsSent:0,acceptedRows:null,independentReadbackPassed:false,verifiedDatePairs:0};
  if(preflight.matchedDates!==input.fileRows){
    await recheckSource();
    // Durable intent is mandatory before the single HTTP dispatch.
    await persistIntent(fresh);result.submitted=true;result.importRequestsSent=1;
    let response;
    try{response=await dispatch();}
    catch(error){if(error.requestCompletionConfirmed!==true){error.partialResult=result;throw error;}
      result.acceptanceWarning=safeError(error);
    }
    if(response){try{const accepted=validateImportAcceptance(response,input.fileRows);
      result.acceptedRows=accepted.acceptedRows;
    }catch(error){result.acceptanceWarning=safeError(error);result.acceptanceDiagnostic=error.acceptanceDiagnostic;}}
  }
  const deadline=now()+timeoutMs;let first=true,readCount=0;
  do{
    if(result.submitted||!first)await wait(5000);first=false;
    const after=await query();const checked=checkedBackend(input,after,before,true);
    result.verifiedDatePairs=checked.matchedDates;
    await persistReadback(after,checked);
    readCount++;
    if(checked.passed){
      // Two distinct backend reads, not a reused first response.
      const confirm=await query();const independent=checkedBackend(input,confirm,after,true);
      await persistReadback(confirm,independent);
      if(independent.passed){result.status=result.submitted?'passed':'skipped_backend_already_matches';
        result.independentReadbackPassed=true;result.verifiedDatePairs=input.fileRows;return result;}
    }
    // A completed business-error response is not an asynchronous acceptance.
    // Two fresh reads may prove a commit; otherwise stop, never replay here.
    if(result.acceptanceWarning==='F_IMPORT_BACKEND_BUSINESS_ERROR'&&readCount>=2)
      throw Object.assign(Error('F_RETRY_BACKEND_BUSINESS_ERROR_NOT_VERIFIED'),{partialResult:result});
  }while(now()<deadline);
  throw Object.assign(Error('F_RETRY_DATES_STILL_UNCONFIRMED'),{partialResult:result});
}

async function loadInput(receipts,runId){
  const inputPath=path.join(receipts,`opensms_f_date_import_${runId}_input.json`);
  const sourceProof=path.join(receipts,`opensms_system_missing_f_dates_${runId}.json`);
  const input=await read(inputPath);validateImportInput(input,runId);
  // Independent local reconstruction also verifies the template and XLSX cells.
  execFileSync(python,['-B',path.join(root,'scripts/opensms_verify_manual_f_import.py'),sourceProof,inputPath],
    {cwd:root,stdio:['ignore','pipe','pipe'],timeout:120000});
  const bytes=await fs.readFile(input.file);if(sha(bytes)!==input.fileSha256)throw Error('F_RETRY_FILE_CHANGED');
  return {input,inputPath,sourceProof,bytes,inputBytes:await fs.readFile(inputPath),sourceProofBytes:await fs.readFile(sourceProof)};
}

export async function main(argv=process.argv.slice(2)){
  const {priorRun,state:latestStatePath,retryAttempt}=validateArguments(argv);
  execFileSync(python,['-B',path.join(root,'scripts/opensms_storage_policy.py'),'guard'],
    {cwd:root,stdio:['ignore','pipe','pipe'],timeout:15000});
  const ledger=path.join(root,'opensms_artifacts/state/f_date_import');
  const pendingPath=path.join(ledger,'pending.json'),pendingOriginal=await fs.readFile(pendingPath);
  const pending=JSON.parse(pendingOriginal);
  if(pending.runId!==priorRun||Boolean(pending.manualRetry)!==Boolean(retryAttempt))throw Error('F_RETRY_PENDING_IDENTITY_OR_REPLAY_INVALID');
  let previousAttempt;
  if(retryAttempt){
    const directory=path.join(ledger,'manual_retries',retryAttempt);
    const summaryPath=path.join(directory,'receipt.json');
    const receiptPath=path.join(directory,'prior',`opensms_f_date_import_${priorRun}.json`);
    const summaryBytes=await fs.readFile(summaryPath),receiptBytes=await fs.readFile(receiptPath);
    validateStoppedRetry(pending,JSON.parse(summaryBytes),JSON.parse(receiptBytes),ledger,retryAttempt);
    previousAttempt={attemptId:retryAttempt,summaryPath,receiptPath,summaryBytes,receiptBytes};
  }
  const oldReceiptPath=pending.receipt;
  if(!path.isAbsolute(oldReceiptPath||'')||path.basename(oldReceiptPath)!==`opensms_f_date_import_${priorRun}.json`)throw Error('F_RETRY_PENDING_RECEIPT_INVALID');
  const old=await loadInput(path.dirname(oldReceiptPath),priorRun);
  const oldReceiptBytes=await fs.readFile(oldReceiptPath),oldIntentBytes=await fs.readFile(oldReceiptPath+'.intent.json');
  const oldReceipt=JSON.parse(oldReceiptBytes),oldIntent=JSON.parse(oldIntentBytes);
  validatePending(pending,old.input,oldReceipt,oldIntent,priorRun);
  const oldBefore=await read(oldReceiptPath+'.before.json');checkedBackend(old.input,oldBefore);
  const latestStateBytes=await fs.readFile(latestStatePath),latest=JSON.parse(latestStateBytes);
  if(latest.status!=='success'||!runPattern.test(latest.runId||'')||latest.runId===priorRun
    ||path.basename(latestStatePath)!==`opensms_patrol_runner_state_${latest.runId}.json`
    ||latest.fDateImport?.status!=='failed_before_submission'||latest.fDateImport.submitted!==false
    ||latest.fDateImport.blockingRunId!==priorRun)throw Error('F_RETRY_LATEST_BLOCKED_RUN_UNCONFIRMED');
  const current=await loadInput(path.dirname(latestStatePath),latest.runId);
  if(current.input.fileSha256!==latest.fDateImport.fileSha256)throw Error('F_RETRY_LATEST_FILE_CHANGED');
  const unique=validateBatchAgreement([old.input,current.input]);
  const id=crypto.randomUUID(),directory=path.join(ledger,'manual_retries',id);
  await fs.mkdir(directory,{recursive:true,mode:0o700});
  const summaryPath=path.join(directory,'receipt.json');
  const summary={version:1,attemptId:id,authorizationSource:'current-user',
    authorization:'explicit retry despite uncertain prior submission',scope:'prior_pending_and_latest_blocked_file',
    startedAt:new Date().toISOString(),status:'preflight',priorRunId:priorRun,latestRunId:latest.runId,
    priorPendingSha256:sha(pendingOriginal),uniqueInputRecords:unique,results:[],
    importRequestsSent:0,independentReadbackPassed:false,silentBrowserMode:true,receipt:summaryPath};
  await atomicJson(path.join(directory,'authorization.json'),summary,{exclusive:true});
  const lockPath=path.join(ledger,'manual_retry.lock'),lock=await fs.open(lockPath,'wx',0o600);
  let lease,page,session,failure,expectedPending=sha(pendingOriginal),safeCleanup=false;
  const assertPending=async()=>{if(sha(await fs.readFile(pendingPath))!==expectedPending)throw Error('F_RETRY_PENDING_CHANGED');};
  const queryFor=input=>async()=>{
    const rows=[];
    for(let offset=0;offset<input.records.length;offset+=500){
      const phones=input.records.slice(offset,offset+500).map(row=>row.phone);
      const reply=await listPhonesByNumbers(session.api,phones,500);
      if(!Number.isSafeInteger(reply.total)||reply.total!==reply.rows?.length||reply.rows.length!==phones.length)throw Error('F_RETRY_READBACK_INCOMPLETE');
      rows.push(...reply.rows);
    }return rows;
  };
  const retirePending=async label=>{
    await assertPending();const archive=path.join(directory,label+'.pending.json');
    await fs.rename(pendingPath,archive);await syncDir(ledger);await syncDir(directory);
    if(sha(await fs.readFile(archive))!==expectedPending)throw Error('F_RETRY_ARCHIVE_UNCONFIRMED');
    expectedPending=null;return archive;
  };
  try{
    await lock.writeFile(JSON.stringify({attemptId:id,pid:process.pid}));await lock.sync();await syncDir(ledger);
    await assertPending();
    await preserveBytes(path.join(directory,'prior_pending_original.json'),pendingOriginal);
    await preserveBytes(path.join(directory,'prior_receipt_original.json'),oldReceiptBytes);
    await preserveBytes(path.join(directory,'prior_intent_original.json'),oldIntentBytes);
    await preserveBytes(path.join(directory,'latest_state_original.json'),latestStateBytes);
    if(previousAttempt){
      if(sha(await fs.readFile(previousAttempt.summaryPath))!==sha(previousAttempt.summaryBytes)
        ||sha(await fs.readFile(previousAttempt.receiptPath))!==sha(previousAttempt.receiptBytes))throw Error('F_RETRY_PRIOR_EVIDENCE_CHANGED');
      await preserveBytes(path.join(directory,'prior_attempt_summary.json'),previousAttempt.summaryBytes);
      await preserveBytes(path.join(directory,'prior_attempt_receipt.json'),previousAttempt.receiptBytes);
      summary.previousAttemptId=previousAttempt.attemptId;
    }
    summary.stage='acquire_shared_session';
    lease=await acquireSharedOpenSmsSession({timeoutMs:300000});
    if(latest.browser?.sessionId!==lease.session.sessionId)throw Error('F_RETRY_BROWSER_IDENTITY_CHANGED');
    await assertPending();
    if(sha(await fs.readFile(latestStatePath))!==sha(latestStateBytes))throw Error('F_RETRY_SOURCE_STATE_CHANGED');
    summary.stage='create_hidden_page';
    page=await createRawBackgroundPage({cdp:lease.session.cdp,url:'https://adminsms.smsoug.com/phoneConfig',authenticatedTargetId:lease.target.id});
    process.env.OPENSMS_RAW_API_TARGET_ID=page.targetId;
    summary.stage='wait_application_ready';
    summary.applicationReadiness=await waitForPayloadPageReady(page.client);
    summary.stage='load_current_protocol';
    session=await createRawOpenSmsApiSession({cdp:lease.session.cdp,pathName:'/phoneConfig'});
    // Preflight both files before any import, including the old backend IDs.
    summary.stage='preflight_prior';
    const oldLive=await queryFor(old.input)();checkedBackend(old.input,oldLive,oldBefore);
    summary.stage='preflight_latest';
    const currentLive=await queryFor(current.input)();checkedBackend(current.input,currentLive);
    await atomicJson(path.join(directory,'all_preflight.json'),{old:oldLive,current:currentLive},{exclusive:true});
    for(const [label,spec,baseline] of [['prior',old,oldBefore],['latest',current,currentLive]]){
      summary.stage='submit_and_verify_'+label;
      const step=path.join(directory,label);await fs.mkdir(step,{mode:0o700});
      const receiptPath=path.join(step,`opensms_f_date_import_${spec.input.runId}.json`);
      let sent=false,readIndex=0;
      await preserveBytes(path.join(step,`opensms_f_date_import_${spec.input.runId}_input.json`),spec.inputBytes);
      await preserveBytes(path.join(step,`opensms_system_missing_f_dates_${spec.input.runId}.json`),spec.sourceProofBytes);
      const statePath=path.join(path.dirname(spec.inputPath),`opensms_patrol_runner_state_${spec.input.runId}.json`);
      await preserveBytes(path.join(step,path.basename(statePath)),await fs.readFile(statePath));
      const result=await submitAndVerify(spec.input,baseline,{
        query:queryFor(spec.input),recheckSource:async()=>{
          if(sha(await fs.readFile(spec.input.file))!==spec.input.fileSha256)throw Error('F_RETRY_FILE_CHANGED');
        },persistIntent:async fresh=>{
          await atomicJson(receiptPath+'.before.json',fresh,{exclusive:true});
          const intent={version:1,runId:spec.input.runId,file:spec.input.file,fileSha256:spec.input.fileSha256,
            fileRows:spec.input.fileRows,submitted:true,status:'submission_outcome_uncertain',
            independentReadbackPassed:false,authorizationSource:'current-user',attemptId:id,receipt:receiptPath};
          await atomicJson(receiptPath+'.intent.json',intent,{exclusive:true});
          if(label==='prior')await assertPending();
          else{try{await fs.access(pendingPath);throw Error('F_RETRY_UNEXPECTED_PENDING');}catch(e){if(e.code!=='ENOENT')throw e;}}
          const next=label==='prior'?{...pending,manualRetry:{attemptId:id,receipt:receiptPath}}:
            {runId:spec.input.runId,file:spec.input.file,sha256:spec.input.fileSha256,receipt:receiptPath,
              status:'submission_intent',manualRetry:{attemptId:id,authorizationSource:'current-user'}};
          await atomicJson(pendingPath,next,{exclusive:label==='latest'});expectedPending=sha(await fs.readFile(pendingPath));
        },dispatch:async()=>withOpenSmsRequestPacing(async()=>{
          sent=true;summary.importRequestsSent++;await atomicJson(summaryPath,summary);
          const auth=session.token.startsWith('Bearer ')?session.token:`Bearer ${session.token}`;
          return uploadOnce(session.client,
            {name:path.basename(spec.input.file),base64:spec.bytes.toString('base64')},auth);
        }),
        persistReadback:async(rows,checked)=>{
          await atomicJson(path.join(step,`readback_${++readIndex}.json`),{checkedAt:new Date().toISOString(),...checked,rows},{exclusive:true});
        },
      }).catch(async error=>{
        const failed={...error.partialResult,runId:spec.input.runId,status:sent?'submission_outcome_uncertain':'failed_before_submission',
          submitted:sent,independentReadbackPassed:false,error:safeError(error),fileRows:spec.input.fileRows,
          file:spec.input.file,fileSha256:spec.input.fileSha256,attemptId:id,importRequestsSent:sent?1:0,readbackPasses:readIndex};
        await atomicJson(receiptPath,failed,{exclusive:true});summary.results.push({...failed,receipt:receiptPath});throw error;
      });
      await atomicJson(receiptPath,result,{exclusive:true});
      if(!(await read(receiptPath)).independentReadbackPassed)throw Error('F_RETRY_RECEIPT_READBACK_UNCONFIRMED');
      summary.results.push({...result,receipt:receiptPath});await atomicJson(summaryPath,summary);
      if(label==='prior'||result.submitted){const archive=await retirePending(label);summary.results.at(-1).pendingIntentArchived=archive;}
    }
    if(sha(await fs.readFile(oldReceiptPath))!==sha(oldReceiptBytes)
      ||sha(await fs.readFile(oldReceiptPath+'.intent.json'))!==sha(oldIntentBytes)
      ||sha(await fs.readFile(latestStatePath))!==sha(latestStateBytes))throw Error('F_RETRY_ORIGINAL_EVIDENCE_CHANGED');
    summary.originalReceiptsUnmodified=true;summary.status='passed';summary.independentReadbackPassed=true;
  }catch(error){failure=error;summary.status='stopped';summary.error=safeError(error);
  }finally{
    session?.client.close();
    try{if(page){const targetId=page.targetId;await page.close();page=null;
      const r=await fetch(lease.session.cdp+'/json/list',{signal:AbortSignal.timeout(5000)});
      if(!r.ok||(await r.json()).some(t=>t.id===targetId))throw Error('F_RETRY_HIDDEN_PAGE_CLOSE_UNCONFIRMED');}
      safeCleanup=true;summary.ownedHiddenPageClosed=true;
    }catch(error){failure??=error;summary.cleanupError=safeError(error);summary.status='stopped';}
    summary.finishedAt=new Date().toISOString();await atomicJson(summaryPath,summary);
    try{lease?.release();}finally{await lock.close();if(safeCleanup){await fs.unlink(lockPath);await syncDir(ledger);}}
  }
  console.log(JSON.stringify(summary));if(failure)throw Error(summary.error||summary.cleanupError);return summary;
}

if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))main().catch(error=>{
  console.error(safeError(error));process.exitCode=1;
});
