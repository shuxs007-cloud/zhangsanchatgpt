// Explicit current-user restoration only. Never used by Task A or its timers.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {isDeepStrictEqual} from 'node:util';
import {acquireSharedOpenSmsSession} from './opensms_shared_browser.mjs';
import {createRawBackgroundPage,createRawOpenSmsApiSession,listPhonesByNumbers,updatePhoneCarrier} from '../opensms_protocol_client.mjs';
import {getCarrierConfigsReadOnly} from './opensms_carriers_readonly.mjs';
import {collectPopulation,readPlatformDate} from './opensms_expired_card_probe.mjs';
import {waitForPayloadPageReady} from '../opensms_payload_readiness.mjs';
import {atomicJson} from './opensms_execute_task_g.mjs';
import {digest} from './opensms_task_g_engine.mjs';
import {resolveRestOrigins} from './opensms_swap_rest_origin_chain.mjs';
import {SCOPE,MANUAL_RESTORE_SCOPES,phone,carrierId,carrierName,liveIdentity,readHistory,enrichWorkbooks,buildPlan,verifyRows,verifyOnlineBeforeWrite} from './opensms_swap_t_restore_engine.mjs';

const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const BASE=path.join(ROOT,'opensms_artifacts/manual_swap_t_restore');
const canonical=rows=>rows.map(liveIdentity).sort((a,b)=>a.id.localeCompare(b.id));
export function parsePhoneSelection(bytes){
  const lines=bytes.toString('utf8').replace(/^\uFEFF/,'').split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
  if(lines[0]==='电话')lines.shift();
  const phones=lines.map(phone);
  if(!phones.length)throw Error('SWAP_PHONE_SELECTION_EMPTY');
  return {phones:[...new Set(phones)],inputCount:phones.length,duplicateCount:phones.length-new Set(phones).size};
}
export function assertPreview(preview,plan,zone,serverDate,runId,now=Date.now(),scope=SCOPE){
  const age=now-Date.parse(preview.checkedAt);
  if(preview.runId!==runId||preview.mode!=='readonly-preview'||!isDeepStrictEqual(preview.scope,scope)
    ||preview.serverDate!==serverDate||!Number.isFinite(age)||age<0||age>15*60*1000
    ||!isDeepStrictEqual(preview.zone,zone)||!isDeepStrictEqual(preview.plan,plan))throw Error('SWAP_PREVIEW_CHANGED_OR_EXPIRED');
}
export function assertFrozenPopulation(preview,population,canonicalize=canonical){
  if(!Array.isArray(preview.population)||!isDeepStrictEqual(canonicalize(preview.population),canonicalize(population)))
    throw Error('SWAP_FROZEN_POPULATION_CHANGED');
}
async function noUnresolvedRestores(bases=[BASE]){
  for(const base of new Set(bases))for(const dir of await fs.readdir(base).catch(e=>{if(e.code==='ENOENT')return [];throw e;})){
    try{const state=JSON.parse(await fs.readFile(path.join(base,dir,'execution.json'),'utf8'));
      if((state.groups||[]).some(g=>['submission_intent','submitted_pending_readback'].includes(g.status)))
        throw Error('SWAP_PRIOR_SUBMISSION_UNRESOLVED_NO_REPLAY');
    }catch(e){if(e.code!=='ENOENT')throw e;}
  }
}
export function combineZonePlans(zonePlans){
  const groups=[];
  for(const plan of zonePlans)for(const g of plan.groups){let group=groups.find(item=>item.target.identif===g.target.identif);
    if(!group){group={target:g.target,rows:[]};groups.push(group);}group.rows.push(...g.rows);}
  return {populationCount:zonePlans.reduce((n,p)=>n+p.populationCount,0),eligibleCount:zonePlans.reduce((n,p)=>n+p.eligibleCount,0),
    excludedCount:zonePlans.reduce((n,p)=>n+p.excludedCount,0),zonePlans,groups,excluded:zonePlans.flatMap(p=>p.excluded)};
}
export async function main(argv=process.argv.slice(2),{base=BASE,scopes=[SCOPE],onlineOnlyScopes=[],
  phoneSelectionPath=null,resolveWeekendOrigins=false,selectionPlanner=null,freezeEntirePopulation=false,
  originalCarrierIdAuthoritative=false,
  authorizationFlag='--current-user-authorized-swap-t-restore'}={}){
  if(selectionPlanner&&(phoneSelectionPath||typeof selectionPlanner!=='function'))throw Error('SWAP_SELECTION_PLANNER_INVALID');
  if(!scopes.length||scopes.some(s=>!MANUAL_RESTORE_SCOPES.includes(s))||onlineOnlyScopes.some(s=>!scopes.includes(s)))throw Error('SWAP_AUTHORIZED_SCOPE_INVALID');
  const multi=scopes.length>1,scope=multi?scopes:SCOPE,blockedBases=[BASE,base,
    path.join(ROOT,'opensms_artifacts/manual_swap_rest_restore'),path.join(ROOT,'opensms_artifacts/manual_selected_swap_restore'),
    path.join(ROOT,'opensms_artifacts/manual_swap_keep_worst')];
  if(phoneSelectionPath&&!path.isAbsolute(phoneSelectionPath))throw Error('SWAP_PHONE_SELECTION_PATH_INVALID');
  const selectionBytes=phoneSelectionPath?await fs.readFile(phoneSelectionPath):null;
  let selection=selectionBytes?{path:phoneSelectionPath,sha256:digest(selectionBytes),...parsePhoneSelection(selectionBytes)}:null;
  const checkSelection=async()=>{if(selection&&digest(await fs.readFile(selection.path))!==selection.sha256)throw Error('SWAP_PHONE_SELECTION_CHANGED');};
  await checkSelection();
  const canonicalPopulation=rows=>multi?rows.map(r=>{
    const identity=liveIdentity(r);
    return onlineOnlyScopes.some(name=>scopes.includes(name))
      ?{...identity,displayStatus:r.displayStatus,userAble:r.userAble}:identity;
  }).sort((a,b)=>a.id.localeCompare(b.id)):canonical(rows);
  const [mode,directory,authorization]=argv;
  if(!['--preview','--apply'].includes(mode)||!path.isAbsolute(directory||'')
    ||path.dirname(path.resolve(directory))!==base||!/^\d{8}T\d{6}\+0800$/.test(path.basename(directory))
    ||(mode==='--apply'&&authorization!==authorizationFlag))
    throw Error('SWAP_CURRENT_USER_AUTHORIZATION_REQUIRED');
  const runId=path.basename(directory),previewPath=path.join(directory,'preview.json'),execution=path.join(directory,'execution.json');
  await fs.mkdir(directory,{recursive:true});
  await noUnresolvedRestores(blockedBases);
  if(mode==='--apply'){try{await fs.access(execution);throw Error('SWAP_EXECUTION_REPLAY_FORBIDDEN');}
    catch(e){if(e.code!=='ENOENT')throw e;}}
  const history=await readHistory(ROOT,{scopes});
  const lease=await acquireSharedOpenSmsSession({timeoutMs:301000});
  let page,session,state,claimed=false;
  try{
    // Pin both the login context and our own hidden page; never borrow visible business pages.
    page=await createRawBackgroundPage({cdp:lease.session.cdp,url:'https://adminsms.smsoug.com/day',
      authenticatedTargetId:lease.session.lastVerifiedTargetId});
    await waitForPayloadPageReady(page.client);process.env.OPENSMS_RAW_API_TARGET_ID=page.targetId;
    session=await createRawOpenSmsApiSession({cdp:lease.session.cdp,pathName:'/day'});
    let respondedAt=0;
    const api=async(...args)=>{const delay=Math.max(0,respondedAt+2000-Date.now());
      if(delay)await new Promise(r=>setTimeout(r,delay));try{return await session.api(...args);}finally{respondedAt=Date.now();}};
    const carriers=await getCarrierConfigsReadOnly(api,{concurrency:1});
    const zones=scopes.map(name=>{const exact=carriers.filter(c=>carrierName(c)===name&&c.delData!==true);
      if(exact.length!==1)throw Error('SWAP_TARGET_ZONE_NOT_UNIQUE');return {name,identif:carrierId(exact[0])};});
    const zone=multi?zones:zones[0];
    const first=await collectPopulation(api,zones),population=await collectPopulation(api,zones);
    if(!isDeepStrictEqual(canonicalPopulation(first),canonicalPopulation(population)))throw Error('SWAP_POPULATION_CHANGED');
    const today=await readPlatformDate(page.client);
    if(selectionPlanner){
      selection=await selectionPlanner({population,carriers,zones,serverDate:today});
      if(!selection||!path.isAbsolute(selection.path||'')||!/^[a-f0-9]{64}$/.test(selection.sha256||'')
        ||!Array.isArray(selection.phones)||new Set(selection.phones).size!==selection.phones.length
        ||selection.phones.some(p=>phone(p)!==p||!population.some(r=>liveIdentity(r).phone===p)))
        throw Error('SWAP_SELECTION_PLANNER_INVALID');
    }
    const selectedPopulation=selection?population.filter(r=>selection.phones.includes(liveIdentity(r).phone)):population;
    await checkSelection();
    enrichWorkbooks(ROOT,history.events,selectedPopulation);
    if(resolveWeekendOrigins){if(!selection)throw Error('SWAP_CHAIN_REQUIRES_PHONE_SELECTION');await resolveRestOrigins(ROOT,history,selectedPopulation);}
    const zonePlans=zones.map(z=>({...buildPlan(selectedPopulation.filter(r=>liveIdentity(r).carrierIdentif===z.identif),carriers,history.events,z,
      {onlineOnly:onlineOnlyScopes.includes(z.name),originalCarrierIdAuthoritative}),zone:z,onlineOnly:onlineOnlyScopes.includes(z.name)}));
    const plan=multi?combineZonePlans(zonePlans):buildPlan(selectedPopulation,carriers,history.events,zone,{onlineOnly:onlineOnlyScopes.includes(SCOPE),originalCarrierIdAuthoritative});
    const summary=()=>({runId,serverDate:today,populationCount:plan.populationCount,
      eligibleCount:plan.eligibleCount,excludedCount:plan.excludedCount,
      ...(selection?{requestedCount:selection.phones.length,duplicateCount:selection.duplicateCount,notInCurrentSwapCount:selection.phones.length-selectedPopulation.length,zonePopulationCount:population.length}:{}),
      ...(selection?.rankingSummary?{ranking:selection.rankingSummary}:{}),
      groups:plan.groups.map(g=>({target:g.target.name,count:g.rows.length})),
      ...(multi?{zones:zonePlans.map(p=>({name:p.zone.name,populationCount:p.populationCount,eligibleCount:p.eligibleCount,excludedCount:p.excludedCount,onlineOnly:p.onlineOnly}))}:{}),
      exclusions:Object.fromEntries([...new Set(plan.excluded.map(r=>r.reason))].map(reason=>[reason,plan.excluded.filter(r=>r.reason===reason).length]))});
    if(mode==='--preview'){
      await atomicJson(previewPath,{runId,mode:'readonly-preview',scope,checkedAt:new Date().toISOString(),
        serverDate:today,zone,selection,originalCarrierIdAuthoritative,carriers,firstPopulation:first,population,historyProofs:history.proofs,plan,businessWrites:0},{exclusive:true});
      console.log(JSON.stringify({path:previewPath,...summary(),businessWrites:0}));return;
    }
    const previewBytes=await fs.readFile(previewPath),preview=JSON.parse(previewBytes);
    assertPreview(preview,plan,zone,today,runId,Date.now(),scope);
    if(Boolean(preview.originalCarrierIdAuthoritative)!==originalCarrierIdAuthoritative)throw Error('SWAP_PREVIEW_ID_AUTHORITY_CHANGED');
    if(freezeEntirePopulation)assertFrozenPopulation(preview,population,canonicalPopulation);
    if(!isDeepStrictEqual(preview.selection??null,selection))throw Error('SWAP_PHONE_SELECTION_CHANGED');
    await noUnresolvedRestores(blockedBases);
    state={runId,operation:multi?'current_user_manual_swap_rest_restore':'current_user_manual_swap_t_restore',scope,authorization:'current-user',onlineOnlyScopes,
      status:'preflight_passed',startedAt:new Date().toISOString(),serverDate:today,originalCarrierIdAuthoritative,
      previewSha256:digest(previewBytes),selection,zone,populationCount:plan.populationCount,backendValidCount:plan.eligibleCount,
      excluded:plan.excluded,groups:[],submissionAttemptCount:0,actualWriteCount:0,independentVerifiedCount:0,retryCount:0};
    await atomicJson(execution,state,{exclusive:true});claimed=true;
    for(const group of plan.groups)for(let start=0;start<group.rows.length;start+=500){
      const rows=group.rows.slice(start,start+500),phones=rows.map(r=>r.phone);
      await checkSelection();
      const before=await listPhonesByNumbers(api,phones,500);verifyRows(rows,before);verifyOnlineBeforeWrite(rows,before);
      if(await readPlatformDate(page.client)!==today)throw Error('SWAP_PLATFORM_DATE_CHANGED');
      const currentConfigs=await getCarrierConfigsReadOnly(api,{concurrency:1});
      if(currentConfigs.filter(c=>carrierId(c)===group.target.identif&&carrierName(c)===group.target.name&&c.delData!==true).length!==1)
        throw Error('SWAP_ORIGINAL_CARRIER_CHANGED_BEFORE_SUBMISSION');
      const batch={target:group.target,rows,before,status:'submission_intent',intentAt:new Date().toISOString()};
      state.groups.push(batch);state.submissionAttemptCount+=rows.length;
      await atomicJson(execution,state); // Durable intent precedes the one and only PUT.
      batch.response=await updatePhoneCarrier(api,rows.map(r=>r.rawId),group.target.identif);
      batch.status='submitted_pending_readback';await atomicJson(execution,state);
      batch.readback=await listPhonesByNumbers(api,phones,500);verifyRows(rows,batch.readback,group.target);
      batch.status='independently_verified';batch.verifiedAt=new Date().toISOString();
      state.actualWriteCount+=rows.length;state.independentVerifiedCount+=rows.length;await atomicJson(execution,state);
    }
    state.finalReadbacks=[];
    for(const group of plan.groups)for(let i=0;i<group.rows.length;i+=500){const rows=group.rows.slice(i,i+500);
      const result=await listPhonesByNumbers(api,rows.map(r=>r.phone),500);verifyRows(rows,result,group.target);
      state.finalReadbacks.push({target:group.target,result});}
    const remaining=await collectPopulation(api,zones);
    // Nothing outside the frozen preview may silently enter the scope while we write.
    const restoredIds=new Set(plan.groups.flatMap(g=>g.rows.map(r=>r.id)));
    if(!isDeepStrictEqual(canonicalPopulation(remaining),canonicalPopulation(population.filter(r=>!restoredIds.has(String(r.id))))))
      throw Error('SWAP_FINAL_REMAINING_POPULATION_CHANGED');
    state.remainingPopulation=remaining;state.remainingCount=remaining.length;
    state.finalIndependentReadbackPassed=true;state.status='passed';state.completedAt=new Date().toISOString();
    await atomicJson(execution,state);
    console.log(JSON.stringify({status:'passed',path:execution,...summary(),actualWriteCount:state.actualWriteCount,
      independentVerifiedCount:state.independentVerifiedCount,remainingCount:state.remainingCount}));
  }catch(error){if(claimed){state.status='stopped_no_retry';state.error=error.message;await atomicJson(execution,state).catch(()=>{});}throw error;}
  finally{
    session?.client.close();let cleanupError;
    try{if(page)await page.close();if(claimed){state.ownedHiddenPageClosed=true;await atomicJson(execution,state);}}
    catch(error){cleanupError=error;if(claimed){state.ownedHiddenPageClosed=false;state.status='stopped_no_retry';
      state.cleanupError='SWAP_OWNED_PAGE_CLOSE_UNCONFIRMED';await atomicJson(execution,state).catch(()=>{});}}
    if(!cleanupError)lease.release();else throw cleanupError;
  }
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href)
  main().catch(error=>{console.error(error.message);process.exitCode=1;});
