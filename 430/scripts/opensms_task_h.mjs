// Standalone H only. Intentionally not imported by A/F or any automation.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {acquireSharedOpenSmsSession} from './opensms_shared_browser.mjs';
import {createRawBackgroundPage,createRawOpenSmsApiSession} from '../opensms_protocol_client.mjs';
import {waitForPayloadPageReady} from '../opensms_payload_readiness.mjs';
import {collectCarrierConfigPagesReadOnly} from './opensms_carriers_readonly.mjs';
import {validateSettings,selectCarriers,assertSameIdentity,assertSameScope,needsUpdate,executeTaskH} from './opensms_task_h_engine.mjs';

const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const SETTINGS=path.join(ROOT,'scripts/opensms_task_h_settings.json');
const STATE=path.join(ROOT,'opensms_artifacts/state/task_h');

async function syncDirectory(directory){const h=await fs.open(directory,'r');try{await h.sync();}finally{await h.close();}}
async function atomicJson(filename,data,{exclusive=false}={}){
  const temp=`${filename}.${crypto.randomUUID()}.tmp`,h=await fs.open(temp,'wx',0o600);
  try{await h.writeFile(JSON.stringify(data,null,2)+'\n');await h.sync();}finally{await h.close();}
  if(exclusive){await fs.link(temp,filename);await fs.unlink(temp);}else await fs.rename(temp,filename);
  await syncDirectory(path.dirname(filename));
}
async function exists(filename){try{await fs.lstat(filename);return true;}catch(e){if(e.code==='ENOENT')return false;throw e;}}
function safeError(error){return String(error?.message||'').match(/TASK_H_[A-Z_]+/)?.[0]||'TASK_H_OPERATION_FAILED';}
function safeDiagnostic(error){
  const status=error?.status??Number(String(error?.message||'').match(/\bHTTP\s+(\d{3})\b/)?.[1]);
  return {
    ...(typeof error?.code==='string'&&/^[A-Z][A-Z0-9_]{2,80}$/.test(error.code)?{causeCode:error.code}:{}),
    ...(Number.isSafeInteger(status)&&status>=100&&status<=599?{httpStatus:status}:{}),
    ...(Number.isSafeInteger(error?.responseCode)?{backendCode:error.responseCode}:{}),
  };
}

export function parseTaskHArgs(args){
  if(args.length===2&&args[0]==='preview'&&args[1]==='--json')return 'preview';
  if(args.join(' ')==='run --force-task-h --authorization-source current-user --json')return 'run';
  throw Error('TASK_H_CURRENT_USER_AUTHORIZATION_REQUIRED');
}

export async function runTaskH(mode,{stateRoot=STATE,settingsPath=SETTINGS,connect=connectTaskH}={}){
  if(!['preview','run'].includes(mode))throw Error('TASK_H_MODE_INVALID');
  await fs.mkdir(stateRoot,{recursive:true});
  const runId=new Date().toISOString().replace(/[:.]/g,'-')+'_'+crypto.randomUUID();
  const runDir=path.join(stateRoot,runId),receiptPath=path.join(runDir,'receipt.json');
  await fs.mkdir(runDir,{recursive:false});
  const pendingPath=path.join(stateRoot,'pending.json'),lockPath=path.join(stateRoot,'lock');
  const receipt={version:1,task:'H',runId,mode,status:'started',startedAt:new Date().toISOString(),
    submissionAttempts:0,accepted:0,modified:0,independentlyVerified:0,entries:[],
    receiptPath,summaryPath:path.join(runDir,'summary.md'),taskFInvoked:false};
  await atomicJson(receiptPath,receipt,{exclusive:true});
  let locked=false,session;
  const persist=()=>atomicJson(receiptPath,receipt);
  try{
    try{await fs.mkdir(lockPath);locked=true;await atomicJson(path.join(lockPath,'owner.json'),{runId,pid:process.pid},{exclusive:true});}
    catch(e){if(e.code==='EEXIST')throw Error('TASK_H_LOCK_BUSY');throw e;}
    if(await exists(pendingPath))throw Error('TASK_H_PRIOR_SUBMISSION_UNRESOLVED');
    const settingsText=await fs.readFile(settingsPath,'utf8');
    receipt.settings=validateSettings(JSON.parse(settingsText));
    receipt.settingsSha256=crypto.createHash('sha256').update(settingsText).digest('hex');
    await persist();session=await connect();
    const snapshot=await collectCarrierConfigPagesReadOnly(session.api,{concurrency:1});
    await atomicJson(path.join(runDir,'inventory_before.json'),snapshot,{exclusive:true});
    const carriers=selectCarriers(snapshot.rows);receipt.scoped=carriers.length;
    const readDetail=async id=>{
      const response=await session.api('GET',`/carrier/config/${id}`);
      if(response?.code!==200||!response.data||typeof response.data!=='object'||Array.isArray(response.data))
        throw Error('TASK_H_DETAIL_READ_FAILED');
      return response.data;
    };
    const assertScope=async before=>{
      if(crypto.createHash('sha256').update(await fs.readFile(settingsPath)).digest('hex')!==receipt.settingsSha256)
        throw Error('TASK_H_SETTINGS_CHANGED');
      const fresh=await collectCarrierConfigPagesReadOnly(session.api,{concurrency:1});
      assertSameScope(before,fresh.rows);
    };
    if(mode==='preview'){
      for(const carrier of carriers){
        const detail=await readDetail(carrier.id);assertSameIdentity(carrier,detail);
        receipt.entries.push({id:detail.id,name:detail.name,before:detail,needsUpdate:needsUpdate(detail,receipt.settings),
          submissionAttempted:false,independentVerified:false});
      }
      await assertScope(carriers);
      receipt.alreadyCorrect=receipt.entries.filter(e=>!e.needsUpdate).length;
      receipt.toModify=receipt.scoped-receipt.alreadyCorrect;
      receipt.status='previewed_no_write';
    }else{
      const saveEntry=async entry=>{
        const i=receipt.entries.findIndex(e=>e.id===entry.id);
        if(i===-1)receipt.entries.push(structuredClone(entry));else receipt.entries[i]=structuredClone(entry);
        receipt.submissionAttempts=receipt.entries.filter(e=>e.submissionAttempted).length;
        receipt.accepted=receipt.entries.filter(e=>e.accepted).length;
        receipt.modified=receipt.entries.filter(e=>e.needsUpdate&&e.independentVerified).length;
        receipt.independentlyVerified=receipt.entries.filter(e=>e.independentVerified).length;
        await persist();
      };
      const result=await executeTaskH({rows:snapshot.rows,settings:receipt.settings,readDetail,assertScope,saveEntry,
        savePlan:plan=>atomicJson(path.join(runDir,'plan.json'),plan,{exclusive:true}),
        persistIntent:async(entry,payload)=>{
          if(crypto.createHash('sha256').update(await fs.readFile(settingsPath)).digest('hex')!==receipt.settingsSha256)
            throw Error('TASK_H_SETTINGS_CHANGED');
          const intent={version:1,task:'H',runId,receiptPath,createdAt:new Date().toISOString(),id:entry.id,
            before:entry.before,payload,settingsSha256:receipt.settingsSha256};
          await atomicJson(path.join(runDir,`intent_${entry.id}.json`),intent,{exclusive:true});
          await atomicJson(pendingPath,intent,{exclusive:true});
        },
        submit:payload=>session.api('PUT','/carrier/config',payload),
        resolveIntent:async entry=>{
          const pending=JSON.parse(await fs.readFile(pendingPath,'utf8'));
          if(pending.runId!==runId||pending.id!==entry.id||!entry.independentVerified)throw Error('TASK_H_PENDING_IDENTITY_CHANGED');
          await fs.rename(pendingPath,path.join(runDir,`intent_${entry.id}_resolved.json`));await syncDirectory(stateRoot);await syncDirectory(runDir);
        }});
      await atomicJson(path.join(runDir,'independent_final.json'),result.final,{exclusive:true});
      receipt.alreadyCorrect=result.alreadyCorrect;receipt.status='verified';
    }
  }catch(error){receipt.status='stopped_no_retry';receipt.error=safeError(error);receipt.diagnostic=safeDiagnostic(error);receipt.pendingIntent=await exists(pendingPath);}
  finally{
    try{if(session){await session.close();receipt.ownedHiddenPageClosed=true;}}
    catch{receipt.status='stopped_no_retry';receipt.error='TASK_H_OWNED_PAGE_CLEANUP_UNCONFIRMED';}
    if(locked){
      try{await fs.unlink(path.join(lockPath,'owner.json'));await fs.rmdir(lockPath);await syncDirectory(stateRoot);}
      catch{receipt.status='stopped_no_retry';receipt.error='TASK_H_LOCK_CLEANUP_UNCONFIRMED';}
    }
    receipt.completedAt=new Date().toISOString();await persist();
  }
  const rows=receipt.entries.map(e=>`| ${e.name.replaceAll('|','\\|')} | ${e.before.sendLimitByDay} | ${e.before.sendNewLimitByDay} | ${e.independentVerified?'独立核验通过':e.needsUpdate?'待修改':'已一致，仅预览'} |`);
  const summary=['# 任务H运营商配置','',`运行编号：${runId}`,'',`状态：${receipt.status}${receipt.error?'；'+receipt.error:''}${receipt.diagnostic?.httpStatus?'；HTTP '+receipt.diagnostic.httpStatus:''}`,'',
    `目标：日下发限制 ${receipt.settings?.dailyDispatchLimit??'未知'}；日发送量 ${receipt.settings?.dailySendAmount??'未知'}`,'',
    `范围 ${receipt.scoped??'未知'}；已一致 ${receipt.alreadyCorrect??'未知'}；提交尝试 ${receipt.submissionAttempts}；接受 ${receipt.accepted}；已修改并独立确认 ${receipt.modified}；独立核验 ${receipt.independentlyVerified}。`,'',
    '任务F未调用；未执行其它任务；未修改定时器。','','| 运营商 | 修改前日下发限制 | 修改前日发送量 | 结果 |','| --- | ---: | ---: | --- |',...rows,''];
  const summaryHandle=await fs.open(receipt.summaryPath,'wx',0o600);
  try{await summaryHandle.writeFile(summary.join('\n'));await summaryHandle.sync();}finally{await summaryHandle.close();}
  return receipt;
}

async function connectTaskH(){
  const lease=await acquireSharedOpenSmsSession({timeoutMs:0});let page,session;
  try{
    page=await createRawBackgroundPage({cdp:lease.session.cdp,url:'https://adminsms.smsoug.com/index'});
    await waitForPayloadPageReady(page.client);process.env.OPENSMS_RAW_API_TARGET_ID=page.targetId;
    session=await createRawOpenSmsApiSession({cdp:lease.session.cdp,pathName:'/index'});
    return {api:session.api,close:async()=>{try{session.client.close();await page.close();}finally{lease.release();}}};
  }catch(error){try{session?.client.close();if(page)await page.close();}finally{lease.release();}throw error;}
}

if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  try{
    const receipt=await runTaskH(parseTaskHArgs(process.argv.slice(2)));
    console.log(JSON.stringify({task:'H',runId:receipt.runId,status:receipt.status,error:receipt.error,diagnostic:receipt.diagnostic,
      scoped:receipt.scoped,alreadyCorrect:receipt.alreadyCorrect,toModify:receipt.toModify,
      submissionAttempts:receipt.submissionAttempts,accepted:receipt.accepted,modified:receipt.modified,
      independentlyVerified:receipt.independentlyVerified,receiptPath:receipt.receiptPath,summaryPath:receipt.summaryPath,taskFInvoked:false}));
    if(receipt.status==='stopped_no_retry')process.exitCode=1;
  }catch(error){console.error(safeError(error));process.exitCode=1;}
}
