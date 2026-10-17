// Current-user Kini-only correction; no export, other tasks or timer changes.
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {isDeepStrictEqual} from 'node:util';
import {acquireSharedOpenSmsSession} from './opensms_shared_browser.mjs';
import {createRawBackgroundPage,createRawOpenSmsApiSession,listPhonesByNumbers,updatePhoneCarrier} from '../opensms_protocol_client.mjs';
import {getCarrierConfigsReadOnly} from './opensms_carriers_readonly.mjs';
import {collectPopulation,readPlatformDate} from './opensms_expired_card_probe.mjs';
import {waitForPayloadPageReady} from '../opensms_payload_readiness.mjs';
import {atomicJson} from './opensms_execute_task_g.mjs';
import {digest,normalizeSlot,normalizeIccid,validateLedger} from './opensms_task_g_engine.mjs';

const ROOT=path.resolve(path.dirname(new URL(import.meta.url).pathname),'..');
const LEDGER=path.join(ROOT,'opensms_artifacts/state/task_g_weekend_rest/ledger.json');
export const REST_ZONES=['周末休息区T','周末休息区TV','E-T休息区','E-C休息区'];
const text=v=>v==null?'':String(v).trim();
const cid=r=>text(r.carrireIdentif||r.carrierIdentif||r.identif||r.id);
const cname=r=>text(r.name||r.carrierName||r.carrireName||r.carrier);
export const isKini=opr=>typeof opr==='string' && /\bKini\b/i.test(opr);
export function identity(row) {
  const full=text(row.cardNo),id=text(row.id);
  if(!/^(?:1)?\d{10}$/.test(full)||!id||!normalizeSlot(row.slotIdentif)
    || (typeof row.id==='number' && !Number.isSafeInteger(row.id))
    || (row.cardStore!=null && typeof row.cardStore!=='string'))throw Error('KINI_BACKEND_IDENTITY_INVALID');
  return {phone:full.slice(-10),id,slot:normalizeSlot(row.slotIdentif),iccid:normalizeIccid(row.cardStore),
          opr:text(row.opr),carrierIdentif:text(row.carrireIdentif)};
}
export function buildPlan(rows,carriers,ledger) {
  validateLedger(ledger);
  const names=new Map(carriers.map(c=>[cid(c),cname(c)]));
  const eligible=[],excluded=[];
  const kini=rows.filter(r=>isKini(r.opr));
  if(new Set(kini.map(r=>identity(r).phone)).size!==kini.length)throw Error('KINI_DUPLICATE_PHONE');
  for(const raw of kini) {
    const live=identity(raw),zone=names.get(live.carrierIdentif);
    if(!REST_ZONES.includes(zone))throw Error('KINI_OUTSIDE_REST_ZONE');
    if(/\bChatr\b/i.test(live.opr)){excluded.push({...live,zone,reason:'ambiguous_opr'});continue;}
    const matches=Object.values(ledger.records).filter(r=>r.phone===live.phone && r.backendId===live.id
      && r.slot===live.slot && r.iccid===live.iccid && r.restCarrier.identif===live.carrierIdentif
      && r.restCarrier.name===zone && ['resting','entering','restoring'].includes(r.status));
    if(matches.length!==1 || matches[0].status!=='resting') {
      excluded.push({...live,zone,reason:matches.length?'unresolved_ledger_intent':'no_verified_original_carrier'});continue;
    }
    const record=matches[0],target=carriers.filter(c=>cid(c)===record.originalCarrier.identif && cname(c)===record.originalCarrier.name);
    if(target.length!==1 || target[0].delData===true || REST_ZONES.includes(record.originalCarrier.name)) {
      excluded.push({...live,zone,reason:'original_carrier_missing_or_changed'});continue;
    }
    eligible.push({...live,zone,rawId:raw.id,ledgerKey:record.key,target:record.originalCarrier});
  }
  const groups=[];
  for(const row of eligible) {
    let group=groups.find(g=>g.target.identif===row.target.identif && g.target.name===row.target.name);
    if(!group){group={target:row.target,rows:[]};groups.push(group);}group.rows.push(row);
  }
  return {kiniCount:kini.length,eligibleCount:eligible.length,excludedCount:excluded.length,excluded,groups};
}
export function buildWeekendRestPlan(rows,carriers,ledger,{originalCarrierIdAuthoritative=false}={}) {
  validateLedger(ledger);
  if(Object.values(ledger.records).some(r=>['entering','restoring'].includes(r.status)))
    throw Error('REST_UNRESOLVED_INTENT_NO_REPLAY');
  const zones=['周末休息区T','周末休息区TV'],names=new Map(),phones=new Set(),ids=new Set();
  for(const c of carriers){const id=cid(c),name=cname(c);
    if(!id||!name||names.has(id))throw Error('REST_CARRIER_CONFIG_AMBIGUOUS');names.set(id,name);}
  const eligible=[],excluded=[];
  for(const raw of rows){
    const full=text(raw.cardNo),id=text(raw.id),phone=full.slice(-10);
    if(!/^(?:1)?\d{10}$/.test(full)||!id||!['string','number'].includes(typeof raw.id)
      ||(typeof raw.id==='number'&&(!Number.isSafeInteger(raw.id)||raw.id<=0))
      ||(raw.slotIdentif!=null&&typeof raw.slotIdentif!=='string')
      ||(raw.cardStore!=null&&typeof raw.cardStore!=='string')||phones.has(phone)||ids.has(id))
      throw Error('REST_BACKEND_IDENTITY_AMBIGUOUS');
    phones.add(phone);ids.add(id);
    const live={phone,id,slot:normalizeSlot(raw.slotIdentif),iccid:normalizeIccid(raw.cardStore),
      opr:text(raw.opr),carrierIdentif:text(raw.carrireIdentif)},zone=names.get(live.carrierIdentif);
    if(!zones.includes(zone))throw Error('REST_POPULATION_OUTSIDE_AUTHORIZED_SCOPE');
    if(!live.slot){excluded.push({...live,zone,reason:'current_slot_missing'});continue;}
    const matches=Object.values(ledger.records).filter(r=>r.status==='resting'&&r.phone===phone
      &&r.backendId===id&&r.slot===live.slot&&r.iccid===live.iccid
      &&r.restCarrier.identif===live.carrierIdentif&&r.restCarrier.name===zone);
    if(matches.length!==1){excluded.push({...live,zone,reason:'no_verified_original_carrier'});continue;}
    const record=matches[0],targets=carriers.filter(c=>cid(c)===record.originalCarrier.identif
      &&(originalCarrierIdAuthoritative||cname(c)===record.originalCarrier.name)&&c.delData!==true);
    if(targets.length!==1||REST_ZONES.includes(record.originalCarrier.name)
      ||REST_ZONES.includes(cname(targets[0]))){
      excluded.push({...live,zone,reason:'original_carrier_missing_or_changed'});continue;}
    eligible.push({...live,zone,rawId:raw.id,ledgerKey:record.key,
      originalCarrier:record.originalCarrier,target:{identif:cid(targets[0]),name:cname(targets[0])}});
  }
  const groups=[];
  for(const row of eligible){let g=groups.find(g=>g.target.identif===row.target.identif&&g.target.name===row.target.name);
    if(!g){g={target:row.target,rows:[]};groups.push(g);}g.rows.push(row);}
  return {selection:'task-g-weekend',populationCount:rows.length,selectedCount:rows.length,
    kiniCount:rows.filter(r=>isKini(r.opr)).length,eligibleCount:eligible.length,excludedCount:excluded.length,excluded,groups};
}
export function verifyRows(expected,result,target=null) {
  if(result.total!==expected.length || result.rows.length!==expected.length
    || new Set(result.rows.map(r=>identity(r).phone)).size!==expected.length)throw Error('KINI_READBACK_INCOMPLETE');
  for(const before of expected) {
    const raw=result.rows.find(r=>identity(r).phone===before.phone),after=identity(raw);
    if(['phone','id','slot','iccid','opr'].some(k=>after[k]!==before[k])
      || after.carrierIdentif!==(target?.identif||before.carrierIdentif))throw Error('KINI_IDENTITY_OR_CARRIER_CHANGED');
  }
}
export async function runManualRestRestore(argv=process.argv.slice(2),{selection='kini'}={}) {
  const mode=argv[0],out=argv[1];
  if(!['kini','task-g-weekend'].includes(selection))throw Error('REST_SCOPE_INVALID');
  const weekend=selection==='task-g-weekend',authorizationFlag=weekend
    ?'--current-user-authorized-task-g-rest-restore':'--current-user-authorized-kini-only';
  const originalCarrierIdAuthoritative=weekend&&argv.includes('--original-carrier-id-authoritative');
  const scope=weekend?['周末休息区T','周末休息区TV']:REST_ZONES;
  if(!['--preview','--apply'].includes(mode)||!path.isAbsolute(out||'')
    || (mode==='--apply' && argv[2]!==authorizationFlag))throw Error('KINI_CURRENT_USER_AUTHORIZATION_REQUIRED');
  const directory=path.resolve(out),runId=path.basename(directory);
  if(!/^\d{8}T\d{6}\+0800$/.test(runId))throw Error('KINI_RUN_ID_INVALID');
  await fs.mkdir(directory,{recursive:true});
  if(mode==='--apply'){
    try{await fs.access(path.join(directory,'execution.json'));throw Error('REST_EXECUTION_REPLAY_FORBIDDEN');}
    catch(e){if(e.code!=='ENOENT')throw e;}
  }
  const lease=await acquireSharedOpenSmsSession({timeoutMs:301000});
  let page,session,ledgerLock,state,created=false;
  const execution=path.join(directory,'execution.json');
  try {
    ledgerLock=await fs.open(LEDGER+'.lock','wx',0o600);
    await ledgerLock.writeFile(JSON.stringify({runId,pid:process.pid,operation:weekend?'manual_task_g_restore':'manual_kini_restore'}));await ledgerLock.sync();
    const bytes=await fs.readFile(LEDGER),ledger=JSON.parse(bytes);validateLedger(ledger);
    page=await createRawBackgroundPage({cdp:lease.session.cdp,url:'https://adminsms.smsoug.com/day'});
    await waitForPayloadPageReady(page.client);
    process.env.OPENSMS_RAW_API_TARGET_ID=page.targetId;
    session=await createRawOpenSmsApiSession({cdp:lease.session.cdp,pathName:'/day'});
    let responseAt=0;
    const api=async(...args)=>{const gap=Math.max(0,responseAt+2000-Date.now());if(gap)await new Promise(r=>setTimeout(r,gap));
      try{return await session.api(...args);}finally{responseAt=Date.now();}};
    const carriers=await getCarrierConfigsReadOnly(api,{concurrency:1});
    const zones=scope.flatMap(name=>{
      const matches=carriers.filter(c=>cname(c)===name);
      if(matches.length>1)throw Error('KINI_REST_ZONE_AMBIGUOUS');
      return matches.length?[{name,identif:cid(matches[0])}]:[];
    });
    if(!zones.length)throw Error('KINI_REST_ZONES_NOT_FOUND');
    const population=await collectPopulation(api,zones);
    const plan=weekend?buildWeekendRestPlan(population,carriers,ledger,{originalCarrierIdAuthoritative}):buildPlan(population,carriers,ledger);
    const today=await readPlatformDate(page.client);
    const previewPath=path.join(directory,'preview.json');
    if(mode==='--preview') {
      const result={runId,mode:'readonly-preview',selection,originalCarrierIdAuthoritative,checkedAt:new Date().toISOString(),serverDate:today,
        ledgerSha256:digest(bytes),zones,carriers,population,plan,businessWrites:0};
      await atomicJson(previewPath,result,{exclusive:true});
      console.log(JSON.stringify({path:previewPath,runId,serverDate:today,populationCount:population.length,
        selection,selectedCount:plan.selectedCount??plan.kiniCount,kiniCount:plan.kiniCount,eligibleCount:plan.eligibleCount,excludedCount:plan.excludedCount,
        groups:plan.groups.map(g=>({target:g.target.name,count:g.rows.length})),excluded:plan.excluded}));return;
    }
    const preview=JSON.parse(await fs.readFile(previewPath,'utf8'));
    if(preview.runId!==runId || preview.mode!=='readonly-preview' || preview.serverDate!==today
      || Boolean(preview.originalCarrierIdAuthoritative)!==originalCarrierIdAuthoritative
      || (weekend&&preview.selection!==selection)
      || !Number.isFinite(Date.parse(preview.checkedAt)) || Date.parse(preview.checkedAt)>Date.now()
      || Date.now()-Date.parse(preview.checkedAt)>15*60*1000 || preview.ledgerSha256!==digest(bytes)
      || JSON.stringify(preview.plan)!==JSON.stringify(plan))throw Error('KINI_PREVIEW_CHANGED_OR_EXPIRED');
    state={runId,operation:weekend?'manual_task_g_rest_restore':'manual_kini_rest_restore',authorization:'current-user',serverDate:today,
      startedAt:new Date().toISOString(),status:'preflight_passed',ledgerBefore:ledger,
      ledgerBeforeSha256:digest(bytes),previewSha256:digest(await fs.readFile(previewPath)),
      scope,selection,originalCarrierIdAuthoritative,selectedCount:plan.selectedCount??plan.kiniCount,kiniCount:plan.kiniCount,
      excluded:plan.excluded,groups:[],actualWriteCount:0,independentVerifiedCount:0};
    await atomicJson(execution,state,{exclusive:true});created=true;
    const afterLedger=structuredClone(ledger);
    for(const group of plan.groups)for(let offset=0;offset<group.rows.length;offset+=500) {
      const rows=group.rows.slice(offset,offset+500),phones=rows.map(r=>r.phone);
      const before=await listPhonesByNumbers(api,phones,500);verifyRows(rows,before);
      if(await readPlatformDate(page.client)!==today)throw Error('KINI_PLATFORM_DATE_CHANGED');
      const batch={target:group.target,rows,before,status:'submission_intent'};state.groups.push(batch);
      for(const r of rows){const rec=afterLedger.records[r.ledgerKey];rec.status='restoring';rec.intentRunId=runId;
        rec.manualRestoreReason=weekend?'current_user_task_g_rest_restore':'current_user_kini_rest_restore';rec.manualRestoreReceipt=execution;}
      await atomicJson(execution,state);await atomicJson(LEDGER,afterLedger);
      batch.response=await updatePhoneCarrier(api,rows.map(r=>r.rawId),group.target.identif);
      batch.status='submitted_pending_readback';await atomicJson(execution,state);
      batch.readback=await listPhonesByNumbers(api,phones,500);verifyRows(rows,batch.readback,group.target);
      for(const r of rows){const rec=afterLedger.records[r.ledgerKey];rec.status='restored';rec.confirmedRunId=runId;rec.restoredDate=today;}
      validateLedger(afterLedger);await atomicJson(LEDGER,afterLedger);
      batch.status='independently_verified';state.actualWriteCount+=rows.length;state.independentVerifiedCount+=rows.length;
      await atomicJson(execution,state);
    }
    for(const group of plan.groups)for(let i=0;i<group.rows.length;i+=500) {
      const rows=group.rows.slice(i,i+500),final=await listPhonesByNumbers(api,rows.map(r=>r.phone),500);verifyRows(rows,final,group.target);
      state.finalReadbacks??=[];state.finalReadbacks.push(final);
    }
    state.ledgerAfter=afterLedger;state.ledgerAfterSha256=digest(await fs.readFile(LEDGER));
    const persisted=JSON.parse(await fs.readFile(LEDGER,'utf8'));validateLedger(persisted);
    if(!isDeepStrictEqual(persisted,afterLedger))throw Error('REST_LEDGER_INDEPENDENT_READBACK_FAILED');
    state.ledgerIndependentReadbackPassed=true;
    state.status='passed';state.completedAt=new Date().toISOString();await atomicJson(execution,state);
    console.log(JSON.stringify({status:state.status,path:execution,runId,selection,selectedCount:state.selectedCount,kiniCount:plan.kiniCount,
      actualWriteCount:state.actualWriteCount,independentVerifiedCount:state.independentVerifiedCount,
      excludedCount:plan.excludedCount,groups:plan.groups.map(g=>({target:g.target.name,count:g.rows.length})),
      ledgerAfterSha256:state.ledgerAfterSha256}));
  }catch(error){if(state&&created){state.status='stopped_no_retry';state.error=error.message;await atomicJson(execution,state).catch(()=>{});}throw error;}
  finally {
    session?.client.close();
    let cleanupError;
    try{if(page)await page.close();if(state&&created){state.ownedHiddenPageClosed=true;await atomicJson(execution,state);}}
    catch(e){cleanupError=e;if(state&&created){state.ownedHiddenPageClosed=false;state.status='stopped_no_retry';
      state.error='REST_OWNED_PAGE_CLEANUP_UNCONFIRMED';await atomicJson(execution,state).catch(()=>{});}}
    if(ledgerLock){await ledgerLock.close();await fs.unlink(LEDGER+'.lock');}
    // Retain whole-site authority if our own target cleanup is unconfirmed.
    if(!cleanupError)lease.release();else throw cleanupError;
  }
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href)
  runManualRestRestore().catch(e=>{console.error(e.message);process.exitCode=1;});
