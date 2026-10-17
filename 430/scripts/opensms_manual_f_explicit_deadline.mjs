// One human-confirmed renewal deadline, using the original F template, uploader,
// shared browser leases, persistent intent and double independent readback.
// No Grab query, activation-library update, report export or other task dispatch.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {withSession,queryPhones,validateExplicitDeadlineSource,submitRenewals,checkRenewalBackend,archiveResolvedIntent} from './opensms_f_renewal_backend.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const dependencies='/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies';
const python=path.join(dependencies,'python/bin/python3'),node=path.join(dependencies,'node/bin/node');
const template=path.join(root,'scripts/templates/task_f_deadline_import_1790891063096.xlsx');
const templateHash='3ca5d981113a6da94d2fabaea2085da2a1d00045064af70faf2c2f6727ce86ea';
const sha=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
async function save(file,value){const h=await fs.open(file,'wx',0o600);try{await h.writeFile(JSON.stringify(value,null,2));await h.sync();}finally{await h.close();}}
function offline(code,args=[]){return execFileSync(python,['-B','-c',code,...args],{cwd:root,encoding:'utf8',timeout:120000});}
function runId(){const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(new Date());
  const p=Object.fromEntries(parts.map(x=>[x.type,x.value]));return `${p.year}${p.month}${p.day}T${p.hour}${p.minute}${p.second}+0800`;}
export function writerReceipt(stdout){
  // Artifact Tool can emit an informational line before the writer's JSON.
  const result=JSON.parse(stdout.trim().split('\n').at(-1));
  if(result.status!=='success'||result.recordCount!==1)throw Error('MANUAL_DEADLINE_WRITER_UNCONFIRMED');
  return result;
}

async function main(){
  const args=process.argv.slice(2);
  if(args.length===5&&args[0]==='resume'&&args[1]==='--directory'&&path.isAbsolute(args[2])
    &&args[3]==='--authorization-source'&&args[4]==='current-user')return completeManual(args[2]);
  if(args.length===5&&args[0]==='reconcile'&&args[1]==='--directory'&&path.isAbsolute(args[2])
    &&args[3]==='--authorization-source'&&args[4]==='current-user')return reconcileManual(args[2]);
  if(args.length!==6||args[0]!=='--phone'||!/^\d{10}$/.test(args[1])||args[2]!=='--cutoff'
    ||!/^\d{4}-\d{2}-\d{2}$/.test(args[3])||new Date(args[3]+'T00:00:00Z').toISOString().slice(0,10)!==args[3]
    ||args[4]!=='--authorization-source'||args[5]!=='current-user')throw Error('MANUAL_DEADLINE_CURRENT_USER_REQUIRED');
  const phone=args[1],cutoff=args[3];
  const pending=path.join(root,'opensms_artifacts/state/f_date_import/pending.json');
  await fs.access(pending).then(()=>{throw Error('F_IMPORT_PRIOR_SUBMISSION_UNRESOLVED');},e=>{if(e.code!=='ENOENT')throw e;});
  const settings=JSON.parse(offline('import sys,json;sys.path.insert(0,"scripts");from opensms_storage_policy import config,require_external;s=config();require_external(s);print(json.dumps(s))'));
  const id=runId(),directory=path.join(settings.bulkRoot,'f_manual_explicit_deadlines',id);
  await fs.mkdir(path.dirname(directory),{recursive:true});await fs.mkdir(directory);
  const source={version:1,kind:'explicit_manual_renewal_deadline',authorizationSource:'current-user',phone,cutoff,recordedAt:new Date().toISOString()};
  const sourceFile=path.join(directory,'user_request.json');await save(sourceFile,source);
  const sourceSha256=sha(await fs.readFile(sourceFile));
  await save(path.join(directory,'source_plan.json'),{source:sourceFile,sourceSha256});
  console.log(JSON.stringify({event:'manual_f_deadline_started',runId:id,phone,cutoff,directory}));
  const mapping=await withSession(async(session,context)=>{
    const first=await queryPhones(session,[phone]),second=await queryPhones(session,[phone]);
    if(first.length!==1||JSON.stringify(first)!==JSON.stringify(second))throw Error('MANUAL_DEADLINE_IDENTITY_UNCONFIRMED');
    return {context,queriedAt:new Date().toISOString(),rows:second};
  });
  await save(path.join(directory,'commit_mapping.json'),mapping);
  const live=mapping.rows[0];
  if(!/^\d{4}-\d{2}-\d{2}$/.test(live.activation||'')||new Date(live.activation+'T00:00:00Z').toISOString().slice(0,10)!==live.activation||cutoff<live.activation)throw Error('MANUAL_DEADLINE_ACTIVATION_INVALID');
  if(sha(await fs.readFile(template))!==templateHash)throw Error('MANUAL_DEADLINE_TEMPLATE_CHANGED');
  const rows=[[live.iccid,cutoff,live.activation]],rowsFile=path.join(directory,'rows.json');await save(rowsFile,rows);
  const file=path.join(directory,`任务F_手工续费_${phone}_${id}.xlsx`),preview=path.join(directory,'preview.png');
  const work=path.join(directory,'writer');await fs.mkdir(work);
  await fs.symlink(path.join(dependencies,'node/node_modules'),path.join(work,'node_modules'),'dir');
  const writer=path.join(work,'writer.mjs');await fs.copyFile(path.join(root,'scripts/opensms_task_f_deadline_writer.mjs'),writer);
  execFileSync(node,['/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/plugins/openai-primary-runtime/plugins/spreadsheets/skills/spreadsheets/container_tools/mark_artifact_operation_started.mjs',
    '--operation-kind','create','--expected-output-count','1','--output-format','xlsx'],{cwd:root,stdio:'pipe'});
  writerReceipt(execFileSync(node,[writer,template,rowsFile,file],{cwd:root,encoding:'utf8',timeout:120000,
    env:{...process.env,OPENSMS_RENEWAL_RENDER_PATH:preview}}));
  return completeManual(directory);
}
async function completeManual(directory){
  const settings=JSON.parse(offline('import sys,json;sys.path.insert(0,"scripts");from opensms_storage_policy import config,require_external;s=config();require_external(s);print(json.dumps(s))'));
  const id=path.basename(directory);
  if(!/^\d{8}T\d{6}\+0800$/.test(id)||directory!==path.join(settings.bulkRoot,'f_manual_explicit_deadlines',id))throw Error('MANUAL_DEADLINE_DIRECTORY_INVALID');
  // Resume is only the never-submitted local checkpoint, not a business retry.
  for(const name of ['renewal_input.json','backend_import_receipt.json','renewal_submission_intent.json','completion.json'])
    await fs.access(path.join(directory,name)).then(()=>{throw Error('MANUAL_DEADLINE_RESUME_REPLAY_FORBIDDEN');},e=>{if(e.code!=='ENOENT')throw e;});
  const sourceFile=path.join(directory,'user_request.json'),source=JSON.parse(await fs.readFile(sourceFile,'utf8'));
  const sourceSha256=sha(await fs.readFile(sourceFile));
  const plan=JSON.parse(await fs.readFile(path.join(directory,'source_plan.json'),'utf8'));
  if(plan.source!==sourceFile||plan.sourceSha256!==sourceSha256||sha(await fs.readFile(template))!==templateHash)throw Error('MANUAL_DEADLINE_SOURCE_OR_TEMPLATE_CHANGED');
  const mapping=JSON.parse(await fs.readFile(path.join(directory,'commit_mapping.json'),'utf8'));
  if(mapping.rows?.length!==1||mapping.rows[0].phone!==source.phone)throw Error('MANUAL_DEADLINE_IDENTITY_UNCONFIRMED');
  const live=mapping.rows[0],phone=source.phone,cutoff=source.cutoff;
  const file=path.join(directory,`任务F_手工续费_${phone}_${id}.xlsx`),preview=path.join(directory,'preview.png'),rowsFile=path.join(directory,'rows.json');
  if(JSON.stringify(JSON.parse(await fs.readFile(rowsFile,'utf8')))!==JSON.stringify([[live.iccid,cutoff,live.activation]]))throw Error('MANUAL_DEADLINE_ROWS_CHANGED');
  offline('import sys,json;from pathlib import Path;sys.path.insert(0,"scripts");from opensms_export_task_f_deadlines import check_template,verify_output;check_template(Path(sys.argv[1]));verify_output(Path(sys.argv[2]),json.loads(Path(sys.argv[3]).read_text()))',[template,file,rowsFile]);
  const record={backendId:live.id,phone,iccid:live.iccid,slot:live.slot,activation:live.activation,cutoff,
    manualDeadline:{authorization:'current-user-explicit-deadline-source',rule:'explicit_user_deadline_preserve_live_activation',sourceSha256,
      backendId:live.id,phone,queryIccid:live.iccid,slot:live.slot,activationDate:live.activation,cutoffDate:cutoff}};
  const input={version:1,scope:'current_user_authorized_explicit_deadline',runId:id,sourceSha256,file,fileSha256:sha(await fs.readFile(file)),fileRows:1,
    independentReadbackPassed:true,records:[record]};
  validateExplicitDeadlineSource(input,JSON.parse(await fs.readFile(sourceFile,'utf8')));
  await save(path.join(directory,'renewal_input.json'),input);
  await save(path.join(directory,'workbook_verification.json'),{status:'passed',runId:id,file,fileSha256:input.fileSha256,fileRows:1,templateSha256:templateHash,
    independentReadbackPassed:true,dateFormat:'yyyy-mm-dd',activationPreserved:true,preview});
  const receipt=await submitRenewals(directory);
  offline('import sys,json;from pathlib import Path;sys.path.insert(0,"scripts");from opensms_export_task_f_deadlines import verify_output;verify_output(Path(sys.argv[1]),json.loads(Path(sys.argv[2]).read_text()))',[file,rowsFile]);
  const reads=(await fs.readdir(directory)).filter(n=>/^renewal_readback_\d+\.json$/.test(n)).sort((a,b)=>Number(a.match(/\d+/)[0])-Number(b.match(/\d+/)[0]));
  const expected={id:live.id,phone,iccid:live.iccid,slot:live.slot,activation:live.activation,cutoff};
  const last=await Promise.all(reads.slice(-2).map(n=>fs.readFile(path.join(directory,n),'utf8').then(JSON.parse)));
  const verified=receipt.status==='passed'&&receipt.ownedHiddenPageClosed===true&&receipt.independentReadbackPassed===true&&last.length===2
    &&last.every(r=>r.length===1&&Object.keys(expected).every(k=>r[0][k]===expected[k]))&&sha(await fs.readFile(file))===input.fileSha256
    &&sha(await fs.readFile(sourceFile))===sourceSha256;
  const result={status:verified?'passed':'unconfirmed',runId:id,phone,iccid:live.iccid,activation:live.activation,cutoff,file,
    fileSha256:input.fileSha256,importRequestsSent:receipt.importRequestsSent,submitted:receipt.submitted,
    independentReadbackPassed:verified,verifiedRecords:verified?1:null,activationPreserved:true,
    localActivationLibraryChanged:false,receipt:path.join(directory,'backend_import_receipt.json'),error:receipt.error??null};
  await save(path.join(directory,'completion.json'),result);console.log(JSON.stringify(result));if(!verified)process.exitCode=1;
}
async function reconcileManual(directory){
  const settings=JSON.parse(offline('import sys,json;sys.path.insert(0,"scripts");from opensms_storage_policy import config,require_external;s=config();require_external(s);print(json.dumps(s))'));
  const id=path.basename(directory);
  if(!/^\d{8}T\d{6}\+0800$/.test(id)||directory!==path.join(settings.bulkRoot,'f_manual_explicit_deadlines',id))throw Error('MANUAL_DEADLINE_DIRECTORY_INVALID');
  const input=JSON.parse(await fs.readFile(path.join(directory,'renewal_input.json'),'utf8'));
  const sourceFile=path.join(directory,'user_request.json'),source=JSON.parse(await fs.readFile(sourceFile,'utf8'));
  validateExplicitDeadlineSource(input,source);
  const receiptFile=path.join(directory,'backend_import_receipt.json'),priorBytes=await fs.readFile(receiptFile),prior=JSON.parse(priorBytes);
  if(input.runId!==id||input.scope!=='current_user_authorized_explicit_deadline'||prior.runId!==id||prior.scope!==input.scope
    ||prior.submitted!==true||prior.importRequestsSent!==1||prior.fileSha256!==input.fileSha256)throw Error('MANUAL_DEADLINE_PRIOR_RECEIPT_INVALID');
  const ledger=path.join(root,'opensms_artifacts/state/f_date_import'),pendingPath=path.join(ledger,'pending.json');
  const lockPath=path.join(ledger,'manual_retry.lock'),lock=await fs.open(lockPath,'wx',0o600);
  let result;
  try{
    await lock.writeFile(JSON.stringify({pid:process.pid,runId:id,scope:'read_only_manual_deadline_reconciliation'}));await lock.sync();
    const bytes=await fs.readFile(pendingPath),pending=JSON.parse(bytes);
    if(pending.runId!==id||pending.scope!==input.scope||pending.input!==path.join(directory,'renewal_input.json')
      ||pending.receipt!==receiptFile||pending.sha256!==input.fileSha256||pending.file!==input.file)throw Error('MANUAL_DEADLINE_PENDING_CHANGED');
    const baseline=JSON.parse(await fs.readFile(path.join(directory,'commit_mapping.json'),'utf8')).rows;
    const recheck=async()=>{if(sha(await fs.readFile(input.file))!==input.fileSha256||sha(await fs.readFile(sourceFile))!==input.sourceSha256
      ||sha(await fs.readFile(receiptFile))!==sha(priorBytes)||sha(await fs.readFile(pendingPath))!==sha(bytes))throw Error('MANUAL_DEADLINE_SOURCE_CHANGED');};
    await withSession(async(session,context)=>{
      if(context.sessionId!==prior.context?.sessionId)throw Error('MANUAL_DEADLINE_SESSION_CHANGED');
      const targets=await fetch(process.env.BITBROWSER_CDP+'/json/list',{signal:AbortSignal.timeout(5000)});
      if(!targets.ok||(await targets.json()).some(t=>t.id===prior.context.targetId))throw Error('MANUAL_DEADLINE_PRIOR_PAGE_NOT_CLOSED');
      await recheck();
      offline('import sys,json;from pathlib import Path;sys.path.insert(0,"scripts");from opensms_export_task_f_deadlines import verify_output;verify_output(Path(sys.argv[1]),json.loads(Path(sys.argv[2]).read_text()))',[input.file,path.join(directory,'rows.json')]);
      for(let n=1;n<=2;n++){
        const rows=await queryPhones(session,input.records.map(r=>r.phone));
        if(!checkRenewalBackend(input,rows,{baseline}).passed)throw Error('MANUAL_DEADLINE_DATES_UNCONFIRMED');
        await save(path.join(directory,`resolution_readback_${n}.json`),rows);
      }
      await recheck();
      const intentArchive=await archiveResolvedIntent(pendingPath,directory,'renewal_resolution',sha(bytes));
      result={status:'passed',runId:id,phone:input.records[0].phone,iccid:input.records[0].iccid,
        activation:input.records[0].activation,cutoff:input.records[0].cutoff,file:input.file,fileSha256:input.fileSha256,
        importRequestsSent:1,reconciliationRequestsSent:0,submitted:true,independentReadbackPassed:true,verifiedRecords:1,
        activationPreserved:true,localActivationLibraryChanged:false,originalFailureReceiptPreserved:true,
        originalReceiptSha256:sha(priorBytes),intentArchive,priorOwnedHiddenPageClosed:true};
    });
    result.ownedHiddenPageClosed=true;
    await save(path.join(directory,'resolved_completion.json'),result);console.log(JSON.stringify(result));
  }finally{await lock.close();await fs.unlink(lockPath);}
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))main().catch(e=>{console.error(JSON.stringify({error:/^[A-Z_]+$/.test(e.message)?e.message:'MANUAL_DEADLINE_OPERATION_STOPPED',errorClass:e.name,childExitStatus:Number.isSafeInteger(e.status)?e.status:null}));process.exitCode=1;});
