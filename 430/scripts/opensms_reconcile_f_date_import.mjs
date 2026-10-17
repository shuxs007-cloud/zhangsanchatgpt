// Explicit-user reconciliation only. Never sends an import or changes dates.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {acquireSharedOpenSmsSession} from './opensms_shared_browser.mjs';
import {createRawBackgroundPage,createRawOpenSmsApiSession,listPhonesByNumbers} from '../opensms_protocol_client.mjs';
import {waitForPayloadPageReady} from '../opensms_payload_readiness.mjs';
import {validateImportInput,checkBackend} from './opensms_f_date_import_lib.mjs';
import {atomicJson} from './opensms_execute_task_g.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const sha=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
export function validateReconcileArguments(argv){
  const [runId,authorization,...extra]=argv;
  if(authorization!=='current-user'||!/^\d{8}T\d{6}\+0800$/.test(runId||''))throw Error('F_RECONCILE_CURRENT_USER_REQUIRED');
  let currentStatePath=null;
  if(extra.length){
    if(extra.length!==2||extra[0]!=='--current-state'||!path.isAbsolute(extra[1]||''))throw Error('F_RECONCILE_CURRENT_STATE_INVALID');
    currentStatePath=extra[1];
  }
  return {runId,currentStatePath};
}
export function validateCurrentSessionProof(prior,current,currentPath,session){
  if(!current||current.status!=='success'||!/^\d{8}T\d{6}\+0800$/.test(current.runId||'')
    ||current.runId===prior.runId
    ||path.basename(currentPath)!==`opensms_patrol_runner_state_${current.runId}.json`
    ||current.browser?.sessionId!==session.sessionId
    ||current.fDateImport?.status!=='failed_before_submission'
    ||current.fDateImport.submitted!==false||current.fDateImport.blockingRunId!==prior.runId)
    throw Error('F_RECONCILE_CURRENT_SESSION_UNCONFIRMED');
  return {authorizationSource:'current-user',currentRunId:current.runId,
    priorSessionId:prior.browser?.sessionId,currentSessionId:session.sessionId,
    verification:'two_exact_backend_identity_and_date_readbacks_required'};
}
export function validatePending(pending,input,receipt,intent,runId){
  validateImportInput(input,runId);
  if(pending.runId!==runId || receipt.runId!==runId || intent.runId!==runId
    || pending.status!=='submission_intent' || receipt.status!=='submission_outcome_uncertain'
    || receipt.submitted!==true || receipt.independentReadbackPassed!==false
    || intent.submitted!==true || pending.file!==input.file || receipt.file!==input.file
    || pending.sha256!==input.fileSha256 || receipt.fileSha256!==input.fileSha256
    || intent.fileSha256!==input.fileSha256 || receipt.fileRows!==input.fileRows)throw Error('F_RECONCILE_PENDING_UNCONFIRMED');
}

export async function main(argv=process.argv.slice(2)){
  const {runId,currentStatePath}=validateReconcileArguments(argv);
  const ledger=path.join(root,'opensms_artifacts/state/f_date_import'),pendingPath=path.join(ledger,'pending.json');
  const pendingBytes=await fs.readFile(pendingPath),pending=JSON.parse(pendingBytes);
  const receiptPath=pending.receipt;
  if(!path.isAbsolute(receiptPath||'')||path.basename(receiptPath)!==`opensms_f_date_import_${runId}.json`)throw Error('F_RECONCILE_RECEIPT_IDENTITY_INVALID');
  const inputPath=path.join(path.dirname(receiptPath),`opensms_f_date_import_${runId}_input.json`);
  const paths={input:inputPath,receipt:receiptPath,intent:receiptPath+'.intent.json',before:receiptPath+'.before.json'};
  const bytes=Object.fromEntries(await Promise.all(Object.entries(paths).map(async([key,file])=>[key,await fs.readFile(file)])));
  const values=Object.fromEntries(Object.entries(bytes).map(([key,value])=>[key,JSON.parse(value)]));
  validatePending(pending,values.input,values.receipt,values.intent,runId);
  if(sha(await fs.readFile(values.input.file))!==values.input.fileSha256)throw Error('F_RECONCILE_SOURCE_CHANGED');
  checkBackend(values.input,values.before);
  const state=JSON.parse(await fs.readFile(path.join(path.dirname(receiptPath),`opensms_patrol_runner_state_${runId}.json`),'utf8'));
  if(state.runId!==runId)throw Error('F_RECONCILE_PRIOR_STATE_INVALID');
  const currentStateBytes=currentStatePath?await fs.readFile(currentStatePath):null;
  const currentState=currentStateBytes?JSON.parse(currentStateBytes):null;
  const uid=crypto.randomUUID(),output=path.join(ledger,`reconciliation_${runId}_${uid}.json`);
  const proof={version:1,runId,scope:'exact_pending_import_readonly_reconciliation',
    status:'unresolved_not_retried',file:values.input.file,fileSha256:values.input.fileSha256,
    fileRows:values.input.fileRows,submitted:false,originalSubmissionAlreadyAttempted:true,
    importRequestsSent:0,independentReadbackPassed:false,pendingSha256:sha(pendingBytes),
    evidence:Object.fromEntries(Object.entries(paths).map(([key,file])=>[key,{path:file,sha256:sha(bytes[key])}])),readbacks:[]};
  let lease,page,session,archived;
  try{
    lease=await acquireSharedOpenSmsSession({timeoutMs:0});
    if(currentStatePath){
      proof.sessionTransition=validateCurrentSessionProof(state,currentState,currentStatePath,lease.session);
      proof.currentState={path:currentStatePath,sha256:sha(currentStateBytes)};
    }else if(state.browser?.sessionId!==lease.session.sessionId)throw Error('F_RECONCILE_BROWSER_IDENTITY_CHANGED');
    page=await createRawBackgroundPage({cdp:lease.session.cdp,url:'https://adminsms.smsoug.com/phoneConfig',authenticatedTargetId:lease.target.id});
    process.env.OPENSMS_RAW_API_TARGET_ID=page.targetId;
    proof.applicationReadiness=await waitForPayloadPageReady(page.client);
    session=await createRawOpenSmsApiSession({cdp:lease.session.cdp,pathName:'/phoneConfig'});
    for(let pass=0;pass<2;pass++){
      const rows=[];
      for(let start=0;start<values.input.records.length;start+=500){
        const phones=values.input.records.slice(start,start+500).map(r=>r.phone);
        const result=await listPhonesByNumbers(session.api,phones,500);
        if(result.total!==result.rows?.length||result.rows.length!==phones.length)throw Error('F_RECONCILE_READBACK_INCOMPLETE');
        rows.push(...result.rows);
      }
      const checked=checkBackend(values.input,rows,values.before,true);
      proof.readbacks.push({queriedAt:new Date().toISOString(),...checked,rows});
    }
    if(proof.readbacks.every(r=>r.passed&&r.matchedDates===values.input.fileRows)){
      proof.status='passed_readonly_reconciliation';proof.independentReadbackPassed=true;
      proof.verifiedDatePairs=values.input.fileRows;
    }
    session.client.close();session=null;
    await page.close();page=null;proof.ownedHiddenPageClosed=true;
    if(currentStatePath&&sha(await fs.readFile(currentStatePath))!==sha(currentStateBytes))throw Error('F_RECONCILE_CURRENT_STATE_CHANGED');
    await atomicJson(output,proof,{exclusive:true});
    const saved=JSON.parse(await fs.readFile(output,'utf8'));
    if(!saved.independentReadbackPassed)throw Object.assign(Error('F_RECONCILE_DATES_STILL_UNCONFIRMED'),{reconciliationReceipt:output});
    // Keep both canonical leases through proof publication and intent
    // disposition; no payload resubmission and no unlocked race window.
    if(sha(await fs.readFile(pendingPath))!==sha(pendingBytes))throw Error('F_RECONCILE_PENDING_CHANGED');
    for(const [key,file] of Object.entries(paths))if(sha(await fs.readFile(file))!==sha(bytes[key]))throw Error('F_RECONCILE_EVIDENCE_CHANGED');
    archived=path.join(ledger,`pending_${runId}_resolved_${uid}.json`);
    await fs.rename(pendingPath,archived);
    const dir=await fs.open(ledger,'r');try{await dir.sync();}finally{await dir.close();}
    if(sha(await fs.readFile(archived))!==sha(pendingBytes))throw Error('F_RECONCILE_ARCHIVE_UNCONFIRMED');
  }finally{
    session?.client.close();
    try{if(page)await page.close();}finally{lease?.release();}
  }
  const result={status:proof.status,runId,verifiedDatePairs:proof.verifiedDatePairs,
    independentReadbackPassed:true,importRequestsSent:0,receipt:output,receiptSha256:sha(await fs.readFile(output)),
    pendingIntentArchived:archived,originalReceiptsUnmodified:true};
  console.log(JSON.stringify(result));return result;
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))main().catch(e=>{
  console.log(JSON.stringify({status:'unresolved_not_retried',error:/^[A-Z_]+$/.test(e.message)?e.message:'F_RECONCILE_OPERATION_UNCONFIRMED',importRequestsSent:0,receipt:e.reconciliationReceipt||null}));process.exitCode=1;
});
