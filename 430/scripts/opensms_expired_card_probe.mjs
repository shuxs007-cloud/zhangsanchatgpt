// Narrow Task A standing authorization, with one-shot offline recovery ledger.
// Uses only its worker-owned hidden target, website/report leases and serial API.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash,randomUUID} from 'node:crypto';
import {setTimeout as sleep} from 'node:timers/promises';
import {createRawOpenSmsApiSession} from '../opensms_protocol_client.mjs';
import {collectCarrierConfigPagesReadOnly} from './opensms_carriers_readonly.mjs';
import {ZONES,SCOPE,identityKey,listingState,selectCandidates,assertSame,decide,validateRecoveryAcceptance} from './opensms_expired_card_probe_lib.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const digest=bytes=>createHash('sha256').update(bytes).digest('hex');
async function save(file,data,exclusive=false){
  await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700});
  const temporary=exclusive?file:file+'.'+randomUUID()+'.tmp';
  const handle=await fs.open(temporary,'wx',0o600);
  try{await handle.writeFile(JSON.stringify(data,null,2));await handle.sync();}finally{await handle.close();}
  if(!exclusive)await fs.rename(temporary,file);
  const directory=await fs.open(path.dirname(file),'r');
  try{await directory.sync();}finally{await directory.close();}
}
export function createProbeJournal(directory){
  if(!path.isAbsolute(directory))throw Error('EXPIRED_PROBE_LEDGER_INVALID');
  const fileFor=key=>{
    if(typeof key!=='string'||!/^[a-f0-9]{64}$/.test(key))throw Error('EXPIRED_PROBE_LEDGER_INVALID');
    return path.join(directory,key+'.json');
  };
  return {
    read:async key=>{
      try{
        const record=JSON.parse(await fs.readFile(fileFor(key),'utf8'));
        if(record?.key!==key)throw Error('EXPIRED_PROBE_LEDGER_INVALID');
        return record;
      }catch(e){if(e.code==='ENOENT')return null;throw e;}
    },
    write:(key,record)=>{
      if(record?.key!==key)throw Error('EXPIRED_PROBE_LEDGER_INVALID');
      return save(fileFor(key),record);
    },
  };
}
export async function collectPopulation(api,zones){
  const all=[];
  for(const zone of zones){
    let total=null;
    for(let page=1;page<=100;page++){
      const result=await api('POST','/phone/config/list',{carrireIdentif:zone.identif,showAble:null,pageNum:page,pageSize:500},{pageNum:page,pageSize:500});
      if(result.code!==200||!Array.isArray(result.rows)||!Number.isSafeInteger(result.total)||result.total<0)throw Error('EXPIRED_PROBE_POPULATION_UNCONFIRMED');
      if(total===null)total=result.total;
      if(total!==result.total||result.rows.length!==Math.min(500,Math.max(0,total-(page-1)*500)))throw Error('EXPIRED_PROBE_POPULATION_CHANGED');
      if(result.rows.some(r=>String(r.carrireIdentif)!==zone.identif))throw Error('EXPIRED_PROBE_WRONG_ZONE');
      all.push(...result.rows);if(page*500>=total)break;
      if(page===100)throw Error('EXPIRED_PROBE_POPULATION_LIMIT');
    }
  }
  if(new Set(all.map(r=>String(r.id))).size!==all.length)throw Error('EXPIRED_PROBE_POPULATION_DUPLICATED');
  return all;
}
export async function runProbe({api,today,runId,checkDate,journal,evidence,wait=sleep,clock=Date.now,maxWaitMs=90000,dryRun=false}){
  const config=await collectCarrierConfigPagesReadOnly(api,{concurrency:1});
  const zones=ZONES.map(name=>{
    const matched=config.rows.filter(r=>r.name===name);
    if(matched.length!==1)throw Error('EXPIRED_PROBE_ZONE_NOT_UNIQUE');
    const identif=String(matched[0].identif??'');if(!/^\d+$/.test(identif))throw Error('EXPIRED_PROBE_ZONE_ID_INVALID');
    return {name,identif};
  });
  const before=await collectPopulation(api,zones),confirmation=await collectPopulation(api,zones);
  const canonical=rows=>rows.map(r=>[String(r.id),r.cardNo,r.cardStore,r.slotIdentif,String(r.carrireIdentif),r.displayStatus,r.userAble]).sort((a,b)=>a[0].localeCompare(b[0]));
  if(JSON.stringify(canonical(before))!==JSON.stringify(canonical(confirmation)))throw Error('EXPIRED_PROBE_POPULATION_CHANGED');
  await evidence('population',{zones,today,first:before,second:confirmation});
  const selection=selectCandidates(confirmation,zones,today);
  const receipt={version:2,scope:SCOPE,runId,serverDate:today,zones,
    ...selection,candidateCount:selection.candidates.length,offlineSubmittedCount:0,recoverySubmittedCount:0,acceptedCount:0,
    onlineVerifiedCount:0,pendingCount:0,alreadyTestedSkippedCount:0,blockedCount:0,retryCount:0,carrierWriteCount:0,
    dryRun,silentBrowserMode:true,browserForegroundActivationAttempted:false,results:[]};
  delete receipt.candidates;
  const query=async items=>{
    const r=await api('POST','/phone/config/list',{cardNo:items.map(i=>i.phone).join('\n'),showAble:null,pageNum:1,pageSize:500},{pageNum:1,pageSize:500});
    if(r.code!==200||!Array.isArray(r.rows)||r.total!==items.length||r.rows.length!==items.length||new Set(r.rows.map(row=>String(row.id))).size!==items.length)throw Error('EXPIRED_PROBE_READBACK_INCOMPLETE');
    const byPhone=new Map(r.rows.map(row=>[row.cardNo,row]));
    items.forEach(item=>assertSame(item,byPhone.get(item.phone),today));return byPhone;
  };
  for(let start=0;start<selection.candidates.length;start+=500){
    const batch=selection.candidates.slice(start,start+500),work=[],initialRows=await query(batch);
    await evidence('initial_readback_'+start,{rows:[...initialRows.values()],at:new Date(clock()).toISOString()});
    const priorLedger=[];
    for(const original of batch){
      const item={...original,initialState:listingState(initialRows.get(original.phone))};
      const previous=await journal.read(item.key),action=decide(item,previous,runId);
      priorLedger.push({key:item.key,previous});
      if(dryRun){receipt.results.push({...item,action,status:'dry_run_no_write'});continue;}
      if(action==='skip_offline_already_tested'||action==='skip_same_run_already_tested'){receipt.alreadyTestedSkippedCount++;receipt.results.push({...item,action,status:'skipped'});continue;}
      if(action==='blocked_uncertain_offline'){receipt.blockedCount++;receipt.results.push({...item,action,status:'uncertain_no_replay'});continue;}
      if(action==='read_only_pending'){
        if(item.initialState==='online'){
          await journal.write(item.key,{...previous,status:'online_verified',verifiedAt:new Date(clock()).toISOString()});
          receipt.onlineVerifiedCount++;receipt.results.push({...item,action,status:'online_verified_pending_resolved'});
        }else{receipt.pendingCount++;receipt.results.push({...item,action,status:'awaiting_async_readback'});}
        continue;
      }
      work.push({item,previous,action});
    }
    await evidence('prior_ledger_'+start,priorLedger);
    if(!work.length)continue;
    const online=work.filter(w=>w.action==='offline_then_restore');
    if(online.length){
      const latest=await query(online.map(w=>w.item));
      if(online.some(w=>listingState(latest.get(w.item.phone))!=='online'))throw Error('EXPIRED_PROBE_STATE_CHANGED_BEFORE_OFFLINE');
      for(const w of online)await journal.write(w.item.key,{key:w.item.key,runId,status:'pending_offline',attempts:w.previous?.attempts||0,item:w.item});
      await evidence('offline_intent_'+start,{method:'POST',endpoint:'/phone/config/batchOffline',body:{ids:online.map(w=>w.item.id)},items:online.map(w=>w.item),at:new Date(clock()).toISOString()});
      const accepted=await api('POST','/phone/config/batchOffline',{ids:online.map(w=>w.item.id)},null,{timeoutMs:300000});
      if(accepted.code!==200)throw Error('EXPIRED_PROBE_OFFLINE_RESPONSE_UNCONFIRMED');
      receipt.offlineSubmittedCount+=online.length;
      await evidence('offline_acceptance_'+start,accepted);
      const deadline=clock()+maxWaitMs;let rows;
      do{await wait(5000);rows=await query(online.map(w=>w.item));if(online.every(w=>listingState(rows.get(w.item.phone))==='offline'))break;}while(clock()<deadline);
      await evidence('offline_readback_'+start,{rows:[...rows.values()],at:new Date(clock()).toISOString()});
      if(online.some(w=>listingState(rows.get(w.item.phone))!=='offline'))throw Error('EXPIRED_PROBE_OFFLINE_NOT_VERIFIED');
    }
    const items=work.map(w=>w.item);
    const offlineRows=await query(items);
    if(items.some(item=>listingState(offlineRows.get(item.phone))!=='offline'))throw Error('EXPIRED_PROBE_STATE_CHANGED_BEFORE_RESTORE');
    // Consume the one offline attempt before sending. Crashes, errors and timeouts
    // cannot replay it; only independent online readback resolves a pending test.
    for(const w of work)await journal.write(w.item.key,{key:w.item.key,runId,status:'pending_restore',attempts:(w.previous?.attempts||0)+1,item:w.item,submittedAt:new Date(clock()).toISOString()});
    await evidence('restore_intent_'+start,{method:'POST',endpoint:'/phone/config/offlineRecover',body:{phoneIds:items.map(i=>i.id)},items,at:new Date(clock()).toISOString()});
    const accepted=await api('POST','/phone/config/offlineRecover',{phoneIds:items.map(i=>i.id)},null,{timeoutMs:300000});
    validateRecoveryAcceptance(accepted,items.length);receipt.recoverySubmittedCount+=items.length;receipt.acceptedCount+=items.length;
    await evidence('restore_acceptance_'+start,accepted);
    const deadline=clock()+maxWaitMs;let after;
    do{await wait(10000);after=await query(items);if(items.every(i=>listingState(after.get(i.phone))==='online'))break;}while(clock()<deadline);
    await evidence('restore_readback_'+start,{rows:[...after.values()],at:new Date(clock()).toISOString()});
    for(const w of work){
      const status=listingState(after.get(w.item.phone))==='online'?'online_verified':'awaiting_async_readback';
      const previous=await journal.read(w.item.key);await journal.write(w.item.key,{...previous,status,verifiedAt:new Date(clock()).toISOString()});
      if(status==='online_verified')receipt.onlineVerifiedCount++;else receipt.pendingCount++;
      receipt.results.push({...w.item,action:w.action,status});
    }
  }
  receipt.status=dryRun?'dry_run':receipt.pendingCount||receipt.blockedCount?'pending':'passed';
  receipt.independentReadbackPassed=receipt.pendingCount===0&&receipt.blockedCount===0&&!dryRun;
  return receipt;
}
export async function readPlatformDate(client,{wait=sleep,clock=Date.now}={}){
  const deadline=clock()+30000;
  do{
    const dates=await client.evaluate(`(() => {if(location.origin!=='https://adminsms.smsoug.com'||/\\/login(?:[/?#]|$)/.test(location.pathname))throw Error('EXPIRED_PROBE_AUTH_UNCONFIRMED');const dates=new Set();for(const e of document.querySelectorAll('body *')){const r=e.getBoundingClientRect(),s=getComputedStyle(e),t=String(e.innerText||e.textContent||'').trim();if(!r.width||!r.height||r.top< -1||r.top>180||s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0||t.length>80)continue;const m=t.match(/(20\\d{2})[-/.年](\\d{1,2})[-/.月](\\d{1,2})/);if(m)dates.add(m[1]+'-'+m[2].padStart(2,'0')+'-'+m[3].padStart(2,'0'));}return [...dates];})()`,10000);
    if(dates.length===1)return dates[0];
    if(dates.length>1)throw Error('EXPIRED_PROBE_SERVER_DATE_AMBIGUOUS');
    await wait(250);
  }while(clock()<deadline);
  throw Error('EXPIRED_PROBE_SERVER_DATE_UNCONFIRMED');
}
export async function main(argv=process.argv.slice(2)){
  const [inputPath,receiptPath]=argv,runId=process.env.OPENSMS_RUN_ID;
  if(argv.length!==2||!path.isAbsolute(inputPath||'')||!path.isAbsolute(receiptPath||'')||!/^\d{8}T\d{6}\+0800$/.test(runId||'')||process.env.OPENSMS_EXPIRED_PROBE_RUN_ID!==runId||!process.env.OPENSMS_RAW_API_TARGET_ID)throw Error('EXPIRED_PROBE_RUNNER_GATE_MISSING');
  const input=JSON.parse(await fs.readFile(inputPath,'utf8'));
  if(input.runId!==runId||input.serverDateVerified!==true||input.scope!==SCOPE||input.deadlineFilterEnabled!==false)throw Error('EXPIRED_PROBE_INPUT_UNCONFIRMED');
  for(const proof of input.dateReceipts||[]){const bytes=await fs.readFile(proof.path),r=JSON.parse(bytes);if(digest(bytes)!==proof.sha256||r.serverDate!==input.serverDate)throw Error('EXPIRED_PROBE_DATE_PROOF_CHANGED');}
  if(input.dateReceipts?.length!==2)throw Error('EXPIRED_PROBE_DATE_PROOF_MISSING');
  try{await fs.access(receiptPath);throw Error('EXPIRED_PROBE_REPLAY_FORBIDDEN');}catch(e){if(e.code!=='ENOENT')throw e;}
  const ledger=path.join(root,'opensms_artifacts/state/expired_card_probe');
  // Keep the existing durable files; never reset the one-shot history per run.
  const journal=createProbeJournal(ledger);
  const evidence=(label,r)=>save(receiptPath+'.'+label+'.json',r,true);
  let session,receipt={version:2,runId,scope:SCOPE,deadlineFilterEnabled:false,status:'failed',retryCount:0,carrierWriteCount:0,independentReadbackPassed:false};
  try{
    session=await createRawOpenSmsApiSession({cdp:process.env.BITBROWSER_CDP,pathName:'/phoneConfig'});
    receipt=await runProbe({api:session.api,today:input.serverDate,runId,journal,evidence});
    receipt.inputPath=inputPath;receipt.inputSha256=digest(await fs.readFile(inputPath));receipt.dateReceipts=input.dateReceipts;
    receipt.populationReceipt=receiptPath+'.population.json';receipt.populationSha256=digest(await fs.readFile(receipt.populationReceipt));
  }catch(e){receipt.error=/^EXPIRED_PROBE_[A-Z_]+$/.test(e.message)?e.message:'EXPIRED_PROBE_OPERATION_UNCONFIRMED';receipt.status='failed_no_replay';}
  finally{session?.client.close();await save(receiptPath,receipt,true);}
  const {results,...summary}=receipt;console.log(JSON.stringify({...summary,receipt:receiptPath,receiptSha256:digest(await fs.readFile(receiptPath))}));
  if(receipt.error)process.exitCode=1;
  return receipt;
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))main().catch(e=>{console.error(e.message);process.exitCode=1;});
