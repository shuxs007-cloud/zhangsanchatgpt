// Explicit manual renewal workflow. Canonical shared leases, own hidden page,
// original serial protocol and uploader. No Task A/F dispatch or Grab requests.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
import {setTimeout as sleep} from 'node:timers/promises';
import {acquireSharedOpenSmsSession} from './opensms_shared_browser.mjs';
import {createRawBackgroundPage,createRawOpenSmsApiSession,listPhonesByNumbers} from '../opensms_protocol_client.mjs';
import {waitForPayloadPageReady} from '../opensms_payload_readiness.mjs';
import {withOpenSmsRequestPacing} from '../opensms_request_pacing.mjs';
import {uploadOnce} from './opensms_f_date_import.mjs';
import {validateImportInput,checkBackend,validateImportAcceptance} from './opensms_f_date_import_lib.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const python='/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3';
const digest=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
async function save(file,value){const h=await fs.open(file,'wx',0o600);try{await h.writeFile(JSON.stringify(value,null,2));await h.sync();}finally{await h.close();}}
async function syncDir(directory){const h=await fs.open(directory,'r');try{await h.sync();}finally{await h.close();}}
export async function archiveResolvedIntent(pendingPath,directory,prefix,expectedSha256){
  if(!/^[a-f0-9]{64}$/.test(expectedSha256)||!/^\w+$/.test(prefix))throw Error('RENEWAL_ARCHIVE_ARGUMENT_INVALID');
  const bytes=await fs.readFile(pendingPath);
  if(digest(bytes)!==expectedSha256)throw Error('RENEWAL_PENDING_CHANGED');
  const pending=JSON.parse(bytes);
  if(!/^\d{8}T\d{6}\+0800$/.test(pending.runId||''))throw Error('RENEWAL_ARCHIVE_RUN_INVALID');
  const copy=path.join(directory,`${prefix}_pending_resolved.json`);
  const output=await fs.open(copy,'wx',0o600);
  try{await output.writeFile(bytes);await output.sync();}finally{await output.close();}
  if(digest(await fs.readFile(copy))!==expectedSha256||digest(await fs.readFile(pendingPath))!==expectedSha256)throw Error('RENEWAL_PENDING_CHANGED');
  // Pending is local while delivery can be on zs01. Preserve an exact copy and
  // atomically move the original within its own filesystem, never across disks.
  const ledger=path.dirname(pendingPath),archiveDir=path.join(ledger,'resolved');
  await fs.mkdir(archiveDir,{recursive:true});
  const localArchive=path.join(archiveDir,`${pending.runId}_${crypto.randomUUID()}.json`);
  await fs.rename(pendingPath,localArchive);await syncDir(ledger);await syncDir(archiveDir);await syncDir(directory);
  const result={status:'passed',sha256:expectedSha256,copy,localArchive,originalIntentPreserved:true};
  await save(path.join(directory,`${prefix}_intent_archive_receipt.json`),result);return result;
}
const rawRows=rows=>rows.map(r=>({id:r.id,cardNo:r.phone,cardStore:r.iccid,slotIdentif:r.slot,chatrActivateDate:r.activation,chatrPlanExpire:r.cutoff}));
const safeError=e=>/^[A-Z][A-Z0-9_]+$/.test(e?.message||'')?e.message:'RENEWAL_OPERATION_UNCONFIRMED';
const canonicalDate=s=>typeof s==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(s)&&Number.isFinite(Date.parse(s+'T00:00:00Z'))&&new Date(s+'T00:00:00Z').toISOString().slice(0,10)===s;
// A single explicit human deadline is not a balance-derived renewal. It never
// enters the automatic A/F importer or changes the original activation library.
export function validateExplicitDeadlineInput(input){
  const r=input?.records?.[0],p=r?.manualDeadline;
  if(input?.version!==1||input.scope!=='current_user_authorized_explicit_deadline'
    ||!/^\d{8}T\d{6}\+0800$/.test(input.runId||'')||input.independentReadbackPassed!==true
    ||input.fileRows!==1||input.records?.length!==1||!path.isAbsolute(input.file||'')
    ||! /^[a-f0-9]{64}$/.test(input.fileSha256||'')||! /^[a-f0-9]{64}$/.test(input.sourceSha256||'')
    ||typeof r?.phone!=='string'||!/^\d{10}$/.test(r.phone)
    ||typeof r.iccid!=='string'||!/^\d{18,22}$/.test(r.iccid)
    ||!((typeof r.backendId==='string'&&/^[1-9]\d*$/.test(r.backendId))||(typeof r.backendId==='number'&&Number.isSafeInteger(r.backendId)&&r.backendId>0))
    ||typeof r.slot!=='string'||!r.slot||!canonicalDate(r.activation)||!canonicalDate(r.cutoff)
    ||r.cutoff<r.activation||p?.authorization!=='current-user-explicit-deadline-source'
    ||p.rule!=='explicit_user_deadline_preserve_live_activation'||p.sourceSha256!==input.sourceSha256
    ||p.phone!==r.phone||p.queryIccid!==r.iccid||p.backendId!==r.backendId||p.slot!==r.slot
    ||p.activationDate!==r.activation||p.cutoffDate!==r.cutoff)throw Error('MANUAL_DEADLINE_INPUT_INVALID');
  return input;
}
export function validateExplicitDeadlineSource(input,source){
  validateExplicitDeadlineInput(input);
  if(source?.version!==1||source.authorizationSource!=='current-user'
    ||source.kind!=='explicit_manual_renewal_deadline'||source.phone!==input.records[0].phone
    ||source.cutoff!==input.records[0].cutoff)throw Error('MANUAL_DEADLINE_SOURCE_CHANGED');
}
export function validateRenewalInput(input){
  if(input?.scope==='current_user_authorized_explicit_deadline')return validateExplicitDeadlineInput(input);
  if(input?.version!==1||input.scope!=='current_user_authorized_renewal'||!/^\d{8}T\d{6}\+0800$/.test(input.runId||'')
    ||input.independentReadbackPassed!==true||!path.isAbsolute(input.file||'')||! /^[a-f0-9]{64}$/.test(input.fileSha256||'')
    ||!Number.isSafeInteger(input.fileRows)||input.fileRows<1||input.records?.length!==input.fileRows)throw Error('RENEWAL_INPUT_UNCONFIRMED');
  const seen=new Set(),phones=new Set();
  for(const r of input.records){const p=r.renewal;
    if(typeof r.iccid!=='string'||!/^\d{18,22}$/.test(r.iccid)||seen.has(r.iccid)||phones.has(r.phone)||typeof r.phone!=='string'||!/^\d{10}$/.test(r.phone)
      ||!((typeof r.backendId==='string'&&/^[1-9]\d*$/.test(r.backendId))||(typeof r.backendId==='number'&&Number.isSafeInteger(r.backendId)&&r.backendId>0))
      ||!['充值成功','不需要充值'].includes(r.status)||!canonicalDate(r.baseCutoffDate)
      ||!canonicalDate(r.activation)||!canonicalDate(r.cutoff)||!canonicalDate(r.sourceCutoff)
      ||typeof r.slot!=='string'||!r.slot||!p||p.authorization!=='current-user-renewal-source'
      ||p.rule!=='current_source_cutoff_plus_30_if_balance_gte_30_else_source_cutoff'
      ||p.sourceSha256!==input.sourceSha256||p.queryIccid!==r.iccid||p.phone!==r.phone||p.slot!==r.slot||p.backendId!==r.backendId
      ||p.cutoffDate!==r.cutoff||p.activationDate!==r.activation||p.sourceCutoff!==r.sourceCutoff||p.baseCutoffDate!==r.baseCutoffDate
      ||p.status!==r.status||p.balanceCad!==r.balanceCad||p.extensionDays!==r.extensionDays||p.sourceRow!==r.sourceRow
      ||typeof r.balanceCad!=='string'||!/^\d+(?:\.\d+)?$/.test(r.balanceCad)||!Number.isFinite(Number(r.balanceCad))||Number(r.balanceCad)<0
      ||r.extensionDays!==(Number(r.balanceCad)>=30?30:0)
      ||(Date.parse(r.cutoff)-Date.parse(r.sourceCutoff))/86400000!==r.extensionDays
      ||(Date.parse(r.baseCutoffDate)-Date.parse(r.activation))/86400000!==29)throw Error('RENEWAL_IDENTITY_OR_DATE_INVALID');
    seen.add(r.iccid);phones.add(r.phone);
  }
  return input;
}
export function checkRenewalBackend(input,rows,{baseline=null,unchangedDates=false}={}){
  validateRenewalInput(input);
  if(rows.length!==input.fileRows)throw Error('RENEWAL_BACKEND_COVERAGE_CHANGED');
  const live=new Map(rows.map(r=>[r.phone,r])),ids=new Set(rows.map(r=>String(r.id))),iccids=new Set(rows.map(r=>r.iccid));
  if(live.size!==rows.length||ids.size!==rows.length||iccids.size!==rows.length)throw Error('RENEWAL_BACKEND_DUPLICATE');
  const previous=new Map((baseline||[]).map(r=>[r.phone,r]));let matched=0;
  for(const r of input.records){const row=live.get(r.phone),before=previous.get(r.phone);
    if(!row||row.id!==r.backendId||row.iccid!==r.iccid||row.slot!==r.slot||row.activation!==r.activation
      ||(baseline&&(!before||before.id!==row.id||before.iccid!==row.iccid||before.slot!==row.slot))
      ||(unchangedDates&&row.cutoff!==before.cutoff))throw Error('RENEWAL_BACKEND_IDENTITY_OR_ACTIVATION_CHANGED');
    if(row.cutoff===r.cutoff)matched++;
  }
  return {matchedDates:matched,passed:matched===input.fileRows};
}
export function identity(row){
  if(typeof row.cardNo!=='string'||!/^\d{10}$/.test(row.cardNo)
    ||!((typeof row.id==='string'&&/^[1-9]\d*$/.test(row.id))||(Number.isSafeInteger(row.id)&&row.id>0)))throw Error('RENEWAL_BACKEND_IDENTITY_INVALID');
  const iccid=typeof row.cardStore==='string'?row.cardStore.replace(/[Ff]$/,''):'';
  if(!/^\d{18,22}$/.test(iccid)||typeof row.slotIdentif!=='string'||!row.slotIdentif)throw Error('RENEWAL_BACKEND_ICCID_OR_SLOT_INVALID');
  return {id:row.id,phone:row.cardNo,iccid,slot:row.slotIdentif,
    activation:row.chatrActivateDate??null,cutoff:row.chatrPlanExpire??null};
}
export async function queryPhones(session,phones){
  const rows=[];const requested=new Set(phones);
  for(let start=0;start<phones.length;start+=500){
    const result=await listPhonesByNumbers(session.api,phones.slice(start,start+500),500);
    if(!Array.isArray(result.rows)||!Number.isSafeInteger(result.total)||result.total!==result.rows.length)throw Error('RENEWAL_QUERY_INCOMPLETE');
    rows.push(...result.rows.map(identity));
  }
  const found=new Set(),ids=new Set(),iccids=new Set();
  for(const row of rows){if(!requested.has(row.phone)||found.has(row.phone)||ids.has(String(row.id))||iccids.has(row.iccid))throw Error('RENEWAL_QUERY_DUPLICATE_OR_OUT_OF_SCOPE');
    found.add(row.phone);ids.add(String(row.id));iccids.add(row.iccid);}
  return rows.sort((a,b)=>a.phone.localeCompare(b.phone));
}
export async function withSession(operation){
  let lease,page,session;
  try{
    lease=await acquireSharedOpenSmsSession({timeoutMs:300000});
    page=await createRawBackgroundPage({cdp:lease.session.cdp,url:'https://adminsms.smsoug.com/phoneConfig',authenticatedTargetId:lease.target.id});
    process.env.OPENSMS_RAW_API_TARGET_ID=page.targetId;
    await waitForPayloadPageReady(page.client);
    session=await createRawOpenSmsApiSession({cdp:lease.session.cdp,pathName:'/phoneConfig'});
    return await operation(session,{sessionId:lease.session.sessionId,targetId:page.targetId});
  }finally{
    session?.client.close();
    try{if(page){const targetId=page.targetId;await page.close();
      const reply=await fetch(lease.session.cdp+'/json/list',{signal:AbortSignal.timeout(5000)});
      if(!reply.ok||(await reply.json()).some(t=>t.id===targetId))throw Error('RENEWAL_HIDDEN_PAGE_CLOSE_UNCONFIRMED');
    }}finally{lease?.release();}
  }
}
async function reconcilePrior(directory,session){
  const ledger=path.join(root,'opensms_artifacts/state/f_date_import'),pendingPath=path.join(ledger,'pending.json');
  let bytes;try{bytes=await fs.readFile(pendingPath);}catch(e){if(e.code==='ENOENT')return null;throw e;}
  const prior=JSON.parse(await fs.readFile(path.join(directory,'prior_pending_preflight.json'),'utf8'));
  if(digest(bytes)!==prior.pendingSha256)throw Error('RENEWAL_PRIOR_INTENT_CHANGED');
  const old=prior.input;validateImportInput(old,old.runId);
  if(old.runId!==prior.pending.runId||old.file!==prior.pending.file||old.fileSha256!==prior.pending.sha256
    ||digest(await fs.readFile(old.file))!==old.fileSha256
    ||JSON.stringify(JSON.parse(await fs.readFile(prior.inputFile,'utf8')))!==JSON.stringify(old))throw Error('RENEWAL_PRIOR_SOURCE_CHANGED');
  const originalBefore=JSON.parse(await fs.readFile(prior.pending.receipt+'.before.json','utf8'));
  const a=await queryPhones(session,old.records.map(r=>r.phone));
  const first=checkBackend(old,rawRows(a),originalBefore,true);
  const b=await queryPhones(session,old.records.map(r=>r.phone));
  const second=checkBackend(old,rawRows(b),originalBefore,true);
  await save(path.join(directory,'prior_readback_1.json'),a);await save(path.join(directory,'prior_readback_2.json'),b);
  if(!first.passed||!second.passed)throw Error('RENEWAL_PRIOR_DATES_UNCONFIRMED');
  await save(path.join(directory,'prior_resolution.json'),{status:'passed',runId:old.runId,currentUserAuthorized:true,
    independentReadbackPassed:true,verifiedDatePairs:old.fileRows,importRequestsSent:0,originalReceiptPreserved:true,
    pendingSha256:prior.pendingSha256,verifiedAt:new Date().toISOString()});
  if(digest(await fs.readFile(pendingPath))!==prior.pendingSha256)throw Error('RENEWAL_PRIOR_INTENT_CHANGED');
  await archiveResolvedIntent(pendingPath,directory,'prior',prior.pendingSha256);
  return {runId:old.runId,status:'independently_confirmed',verifiedDatePairs:old.fileRows,importRequestsSent:0};
}
async function prepare(directory){
  const plan=JSON.parse(await fs.readFile(path.join(directory,'source_plan.json'),'utf8'));
  const previous=JSON.parse(await fs.readFile(path.join(directory,'backend_mapping.json'),'utf8'));
  if(digest(await fs.readFile(plan.source))!==plan.sourceSha256)throw Error('RENEWAL_SOURCE_CHANGED');
  return withSession(async(session,context)=>{
    const current=await queryPhones(session,plan.rows.map(r=>r.phone));
    if(JSON.stringify(current)!==JSON.stringify(previous.rows))throw Error('RENEWAL_MAPPING_CHANGED');
    const mapping=path.join(directory,'commit_mapping.json');await save(mapping,{context,rows:current});
    const runId=path.basename(directory);
    const output=execFileSync(python,['-B',path.join(root,'scripts/opensms_f_renewal.py'),'commit','--directory',directory,'--mapping',mapping,
      '--run-id',runId,'--authorization-source','current-user'],{cwd:root,stdio:['ignore','pipe','pipe'],timeout:120000});
    return JSON.parse(output.toString().trim());
  });
}
export async function submitRenewals(directory){
  const input=validateRenewalInput(JSON.parse(await fs.readFile(path.join(directory,'renewal_input.json'),'utf8')));
  const original=JSON.parse(await fs.readFile(path.join(directory,'commit_mapping.json'),'utf8')).rows;
  const plan=JSON.parse(await fs.readFile(path.join(directory,'source_plan.json'),'utf8'));
  const workbookBytes=await fs.readFile(input.file);
  if(digest(workbookBytes)!==input.fileSha256)throw Error('RENEWAL_WORKBOOK_HASH_CHANGED');
  const ledger=path.join(root,'opensms_artifacts/state/f_date_import');
  const receiptFile=path.join(directory,'backend_import_receipt.json'),pendingPath=path.join(ledger,'pending.json');
  await fs.access(receiptFile).then(()=>{throw Error('RENEWAL_SUBMISSION_REPLAY_FORBIDDEN');},e=>{if(e.code!=='ENOENT')throw e;});
  const lockPath=path.join(ledger,'manual_retry.lock'),lock=await fs.open(lockPath,'wx',0o600);
  const receipt={status:'preflight',runId:input.runId,scope:input.scope,file:input.file,fileSha256:input.fileSha256,
    fileRows:input.fileRows,submitted:false,importRequestsSent:0,independentReadbackPassed:false,verifiedDatePairs:0,sourceSha256:input.sourceSha256};
  let expectedPending=null;
  const recheck=async()=>{const sourceBytes=await fs.readFile(plan.source);
    if(digest(await fs.readFile(input.file))!==input.fileSha256||digest(sourceBytes)!==plan.sourceSha256||plan.sourceSha256!==input.sourceSha256)throw Error('RENEWAL_SOURCE_OR_FILE_CHANGED');
    if(input.scope==='current_user_authorized_explicit_deadline')validateExplicitDeadlineSource(input,JSON.parse(sourceBytes));};
  try{
    await lock.writeFile(JSON.stringify({pid:process.pid,runId:input.runId,scope:input.scope}));await lock.sync();
    await withSession(async(session,context)=>{
      receipt.context=context;await recheck();
      const before=await queryPhones(session,input.records.map(r=>r.phone));
      checkRenewalBackend(input,before,{baseline:original,unchangedDates:true});await save(path.join(directory,'renewal_before.json'),before);
      if(input.scope==='current_user_authorized_explicit_deadline'){
        // This new single-phone request does not authorize resolving old intents.
        await fs.access(pendingPath).then(()=>{throw Error('F_IMPORT_PRIOR_SUBMISSION_UNRESOLVED');},e=>{if(e.code!=='ENOENT')throw e;});
        receipt.priorResolution=null;
      }else receipt.priorResolution=await reconcilePrior(directory,session);
      const fresh=await queryPhones(session,input.records.map(r=>r.phone));checkRenewalBackend(input,fresh,{baseline:before,unchangedDates:true});
      await recheck();
      if(!checkRenewalBackend(input,fresh).passed){
        const intent={runId:input.runId,scope:input.scope,file:input.file,sha256:input.fileSha256,receipt:receiptFile,
          input:path.join(directory,'renewal_input.json'),status:'submission_intent',authorization:input.scope==='current_user_authorized_explicit_deadline'?'current-user-explicit-deadline-source':'current-user-renewal-source'};
        await save(pendingPath,intent);await syncDir(ledger);expectedPending=digest(await fs.readFile(pendingPath));
        await save(path.join(directory,'renewal_submission_intent.json'),intent);
        receipt.status='submission_outcome_uncertain';receipt.submitted=true;receipt.importRequestsSent=1;
        const auth=session.token.startsWith('Bearer ')?session.token:`Bearer ${session.token}`;
        const response=await withOpenSmsRequestPacing(()=>uploadOnce(session.client,{name:path.basename(input.file),base64:workbookBytes.toString('base64')},auth));
        try{Object.assign(receipt,validateImportAcceptance(response,input.fileRows));}
        catch(e){receipt.acceptanceWarning=safeError(e);receipt.acceptanceDiagnostic=e.acceptanceDiagnostic;}
        await save(path.join(directory,'renewal_acceptance.json'),receipt);
      }
      const deadline=Date.now()+300000;let reads=0;
      do{
        if(receipt.submitted||reads)await sleep(5000);
        const rows=await queryPhones(session,input.records.map(r=>r.phone));const checked=checkRenewalBackend(input,rows,{baseline:before});
        await save(path.join(directory,`renewal_readback_${++reads}.json`),rows);receipt.verifiedDatePairs=checked.matchedDates;
        if(checked.passed){const confirm=await queryPhones(session,input.records.map(r=>r.phone));
          const independent=checkRenewalBackend(input,confirm,{baseline:before});
          await save(path.join(directory,`renewal_readback_${++reads}.json`),confirm);
          if(independent.passed){receipt.status='passed';receipt.completed=true;receipt.independentReadbackPassed=true;receipt.verifiedDatePairs=input.fileRows;break;}
        }
        if(receipt.acceptanceWarning==='F_IMPORT_BACKEND_BUSINESS_ERROR'&&reads>=2)throw Error('RENEWAL_BUSINESS_ERROR_NOT_VERIFIED');
      }while(Date.now()<deadline);
      if(!receipt.independentReadbackPassed)throw Error('RENEWAL_IMPORT_NOT_VERIFIED');
      await recheck();
      if(expectedPending)receipt.intentArchive=await archiveResolvedIntent(pendingPath,directory,'renewal',expectedPending);
    });
    receipt.ownedHiddenPageClosed=true;
  }catch(e){receipt.status=receipt.submitted?'submission_outcome_uncertain':'failed_before_submission';receipt.error=safeError(e);receipt.errorClass=e.name;receipt.localErrorCode=/^[A-Z_]+$/.test(e.code||'')?e.code:null;}
  finally{receipt.finishedAt=new Date().toISOString();await save(receiptFile,receipt);await lock.close();await fs.unlink(lockPath);await syncDir(ledger);}
  return receipt;
}
async function preview(source,directory){
  await fs.mkdir(path.dirname(directory),{recursive:true,mode:0o700});
  await fs.mkdir(directory,{mode:0o700});
  const planFile=path.join(directory,'source_plan.json');
  execFileSync(python,['-B',path.join(root,'scripts/opensms_f_renewal.py'),'plan','--source',source,'--output',planFile],{cwd:root,stdio:['ignore','pipe','pipe']});
  const plan=JSON.parse(await fs.readFile(planFile,'utf8'));
  const phones=plan.rows.map(row=>row.phone);
  return withSession(async(session,context)=>{
    const first=await queryPhones(session,phones),second=await queryPhones(session,phones);
    if(JSON.stringify(first)!==JSON.stringify(second))throw Error('RENEWAL_MAPPING_CHANGED');
    await save(path.join(directory,'backend_mapping.json'),{context,queriedAt:new Date().toISOString(),rows:second});
    const pendingFile=path.join(root,'opensms_artifacts/state/f_date_import/pending.json');
    let prior;
    try{
      const pendingBytes=await fs.readFile(pendingFile);const pending=JSON.parse(pendingBytes);
      const inputFile=path.join(path.dirname(pending.receipt),`opensms_f_date_import_${pending.runId}_input.json`);
      const old=JSON.parse(await fs.readFile(inputFile,'utf8'));
      const oldFirst=await queryPhones(session,old.records.map(row=>row.phone));
      const oldSecond=await queryPhones(session,old.records.map(row=>row.phone));
      if(JSON.stringify(oldFirst)!==JSON.stringify(oldSecond))throw Error('RENEWAL_PRIOR_MAPPING_CHANGED');
      prior={pendingSha256:digest(pendingBytes),pending,inputFile,input:old,rows:oldSecond,
        overlappingPhones:old.records.filter(row=>phones.includes(row.phone)).length,
        matchedDatePairs:old.records.filter(r=>oldSecond.some(s=>s.phone===r.phone&&s.iccid===r.iccid&&s.slot===r.slot&&s.activation===r.activation&&s.cutoff===r.cutoff)).length};
      await save(path.join(directory,'prior_pending_preflight.json'),prior);
    }catch(error){if(error.code!=='ENOENT')throw error;}
    const summary={status:'previewed',directory,sourceSha256:plan.sourceSha256,eligibleRows:plan.eligibleRows,computableRows:plan.computableRows,
      excludedRows:plan.excluded.length,backendMatchedRows:second.length,backendMissingRows:phones.length-second.length,
      extensionRows:plan.extensionRows,directDateRows:plan.directDateRows,dateCounts:plan.dateCounts,
      priorPending:prior?{runId:prior.pending.runId,fileRows:prior.input.fileRows,matchedDatePairs:prior.matchedDatePairs,overlappingPhones:prior.overlappingPhones}:null};
    await save(path.join(directory,'preview.json'),summary);return summary;
  });
}
async function main(){const [command,...args]=process.argv.slice(2);let result;
  if(command==='preview'){const [source,directory,auth,...extra]=args;
    if(!path.isAbsolute(source||'')||!path.isAbsolute(directory||'')||auth!=='--current-user-authorized-renewal'||extra.length)throw Error('RENEWAL_CURRENT_USER_REQUIRED');
    result=await preview(source,directory);
  }else{const [directory,auth,...extra]=args;
    if(!['prepare','submit'].includes(command)||!path.isAbsolute(directory||'')||auth!=='--current-user-authorized-renewal'||extra.length)throw Error('RENEWAL_CURRENT_USER_REQUIRED');
    result=command==='prepare'?await prepare(directory):await submitRenewals(directory);
  }
  console.log(JSON.stringify(result));if(!['passed','previewed'].includes(result.status))process.exitCode=1;
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))main().catch(e=>{console.error(/^[A-Z_]+$/.test(e.message)?e.message:'RENEWAL_PREVIEW_FAILED');process.exitCode=1;});
