// File-only provenance and exact-scope restoration planning; no business requests.
import fs from 'node:fs/promises';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {digest,normalizeSlot,normalizeIccid} from './opensms_task_g_engine.mjs';

export const SCOPE='换号区T';
export const MANUAL_RESTORE_SCOPES=['换号区T','换号区C','E-T休息区','E-C休息区'];
const text=v=>v==null?'':String(v).trim();
export const carrierId=r=>text(r.carrireIdentif||r.carrierIdentif||r.identif||r.id);
export const carrierName=r=>text(r.name||r.carrierName||r.carrireName||r.carrier);
export function phone(value){const full=text(value);if(!/^(?:1)?[0-9]{10}$/.test(full))throw Error('SWAP_PHONE_INVALID');return full.slice(-10);}
export function backendId(value){if(!['number','string'].includes(typeof value)||!/^\d+$/.test(text(value))
  ||(typeof value==='number'&&(!Number.isSafeInteger(value)||value<=0)))throw Error('SWAP_BACKEND_ID_INVALID');return text(value);}
export function liveIdentity(r){
  const iccids=['cardStore','iccid'].filter(k=>r[k]!=null).map(k=>{
    if(typeof r[k]!=='string')throw Error('SWAP_ICCID_TYPE_INVALID');return normalizeIccid(r[k]);}).filter(Boolean);
  if(new Set(iccids).size>1)throw Error('SWAP_ICCID_CONFLICT');
  const carriers=['carrireIdentif','carrierIdentif'].map(k=>text(r[k])).filter(Boolean);
  if(new Set(carriers).size!==1)throw Error('SWAP_CARRIER_CONFLICT');
  return {phone:phone(r.cardNo),id:backendId(r.id),slot:normalizeSlot(r.slotIdentif??r.slot),
    iccid:iccids[0]||'',opr:text(r.opr),carrierIdentif:carriers[0]};
}
const validTime=v=>typeof v==='string'&&/(?:Z|[+-]\d\d:\d\d)$/.test(v)&&Number.isFinite(Date.parse(v));
export function decodeWrite(write,candidate,verify,evidence,verifiedCandidateCopy=false,scopes=[SCOPE]){
  const raw=write.before?.eligibleRows;
  if(!scopes.includes(write.target?.name)||!Array.isArray(raw)||raw.length===0)return [];
  const confirmed=write.status==='written'&&write.actualWriteCount===raw.length
    &&write.before.eligibleCount===raw.length&&write.independentReadbackPerformed===true
    &&verify?.passed===true&&verify.independentReadbackPerformed===true
    &&verify.total===raw.length&&verify.targetCount===raw.length
    &&['missingCount','stillNotTargetCount','duplicateBackendCount'].every(k=>verify[k]===0)
    &&verify.target?.name===write.target.name&&verify.target.identif===write.target.identif
    &&(path.resolve(verify.source||'')===path.resolve(write.validatedCandidatePath||'')||verifiedCandidateCopy)
    &&candidate?.count===raw.length&&Array.isArray(candidate.rows)&&candidate.rows.length===raw.length
    &&validTime(write.ts)&&validTime(verify.checkedAt)&&Date.parse(verify.checkedAt)>=Date.parse(write.ts);
  const sources=write.sourceCarriers||[],events=[];
  for(const r of raw){
    const p=phone(r.cardNo),id=backendId(r.id),source=text(r.carrireIdentif);
    if(source===write.target.identif)throw Error('SWAP_HISTORY_ALREADY_TARGET_NOT_ENTRY');
    const matches=(candidate?.rows||[]).filter(c=>phone(c['电话'])===p&&text(c['后台ID'])===id);
    const sourceMatches=sources.filter(c=>text(c.identif)===source);
    const c=matches[0];
    const proven=confirmed&&matches.length===1&&sourceMatches.length===1
      &&text(c['原运营商ID'])===source&&text(c['精确原运营商'])===text(sourceMatches[0].name)
      &&normalizeSlot(c['卡槽'])===normalizeSlot(r.slot)&&text(c.opr)===text(r.opr);
    events.push({phone:p,id,slot:normalizeSlot(r.slot),opr:text(r.opr),at:write.ts,
      confirmed:!!proven,targetIdentif:text(write.target.identif),
      original:{identif:source,name:proven?text(sourceMatches[0].name):''},
      workbook:proven?{path:candidate.sourcePath,sha256:candidate.sourceSha256,sourceRow:c['源行号']}:null,evidence});
  }
  return events;
}
export function decodeManual(state,evidence,scopes=[SCOPE]){
  const result=[];
  for(const g of state.groups||[]){if(!scopes.includes(g.target?.name))continue;
    const actual=(g.rows||[]).filter(r=>!r.alreadyTarget);
    for(const row of actual){const p=phone(row.cardNo),id=backendId(row.id);
      const before=state.before?.rows?.filter(r=>phone(r.cardNo)===p&&text(r.id)===id)||[];
      const after=g.readback?.rows?.filter(r=>phone(r.cardNo)===p&&text(r.id)===id)||[];
      const final=state.finalReadback?.rows?.filter(r=>phone(r.cardNo)===p&&text(r.id)===id)||[];
      const confirmed=state.status==='passed'&&g.status==='independently_verified'
        &&g.actualWriteCount===actual.length&&before.length===1&&after.length===1&&final.length===1
        &&text(before[0].carrireIdentif)===g.originalCarrierIdentif
        &&text(after[0].carrireIdentif)===g.target.identif&&text(final[0].carrireIdentif)===g.target.identif;
      result.push({phone:p,id,slot:normalizeSlot(before[0]?.slotIdentif),
        iccid:normalizeIccid(before[0]?.cardStore),opr:text(row.opr),at:g.intentAt||state.startedAt,
        confirmed:!!confirmed,targetIdentif:g.target.identif,
        original:{identif:g.originalCarrierIdentif,name:g.originalCarrierName},evidence});
    }
  }
  return result;
}
export async function readHistory(root,{scopes=[SCOPE]}={}){
  if(!Array.isArray(scopes)||!scopes.length||scopes.some(s=>!MANUAL_RESTORE_SCOPES.includes(s)))throw Error('SWAP_HISTORY_SCOPE_INVALID');
  const directory=path.join(root,'opensms_artifacts/snapshots'),files=(await fs.readdir(directory)).sort();
  const events=[],proofs=[];
  const load=async file=>{const b=await fs.readFile(file);const proof={path:path.resolve(file),sha256:digest(b)};
    proofs.push(proof);return {value:JSON.parse(b),proof};};
  // Archived delivery receipts contain older entries no longer in local snapshots.
  // Duplicate copies must be byte-identical, never silently choose one version.
  const archiveFiles=execFileSync('rg',['--files','/Volumes/zs01/chatgpt/OpenSMS','-g',scopes.length===1&&scopes[0]===SCOPE?'opensms_set_carrier_*task_c_kooda*.json':'opensms_set_carrier_*.json'],
    {encoding:'utf8',maxBuffer:4*1024*1024}).trim().split('\n').filter(Boolean);
  const writtenFiles=[...files.filter(f=>f.startsWith('opensms_set_carrier_')&&f.endsWith('.json')).map(f=>path.join(directory,f)),...archiveFiles].sort();
  const seen=new Map();
  for(const writePath of writtenFiles){
    const filename=path.basename(writePath),writeBytes=await fs.readFile(writePath),sha=digest(writeBytes),write=JSON.parse(writeBytes);
    if(!scopes.includes(write.target?.name)||!write.before?.eligibleRows?.length)continue;
    if(seen.has(filename)){if(seen.get(filename)!==sha)throw Error('SWAP_HISTORY_RECEIPT_COPY_CONFLICT');continue;}
    seen.set(filename,sha);
    const writeProof={path:writePath,sha256:sha};proofs.push(writeProof);
    let candidate,verify,verifiedCandidateCopy=false,otherProof=[];
    try{
      let c;try{c=await load(write.validatedCandidatePath);}catch(e){if(e.code!=='ENOENT')throw e;
        c=await load(path.join(path.dirname(writePath),path.basename(write.validatedCandidatePath)));}
      candidate=c.value;otherProof.push(c.proof);
      const label=filename.slice('opensms_set_carrier_'.length).replace(/_\d{8}T.*Z\.json$/,'');
      const prefix='opensms_verify_carrier_'+label+'_';
      const options=new Map(files.filter(f=>f.startsWith(prefix)).map(f=>[f,path.join(directory,f)]));
      for(const f of await fs.readdir(path.dirname(writePath))){if(f.startsWith(prefix)&&!options.has(f))options.set(f,path.join(path.dirname(writePath),f));}
      const matching=[];
      for(const file of options.values()){const v=await load(file);
        if(path.basename(v.value.source||'')===path.basename(write.validatedCandidatePath))matching.push(v);}
      if(matching.length===1){verify=matching[0].value;otherProof.push(matching[0].proof);
        const copied=await load(verify.source);otherProof.push(copied.proof);
        verifiedCandidateCopy=copied.proof.sha256===c.proof.sha256;}
    }catch(error){if(error.code!=='ENOENT')throw error;}
    events.push(...decodeWrite(write,candidate,verify,[writeProof,...otherProof],verifiedCandidateCopy,scopes));
  }
  const manualRoot=path.join(root,'opensms_artifacts/state/manual_retired_b14_swap');
  for(const dir of await fs.readdir(manualRoot).catch(e=>{if(e.code==='ENOENT')return [];throw e;})){
    const file=path.join(manualRoot,dir,'execution.json');
    try{const m=await load(file);events.push(...decodeManual(m.value,[m.proof],scopes));}catch(e){if(e.code!=='ENOENT')throw e;}
  }
  if(events.some(e=>!validTime(e.at)))throw Error('SWAP_HISTORY_DATE_UNCONFIRMED');
  return {events,proofs};
}
export function latestEvents(events){
  const byPhone=new Map();
  for(const event of events){const old=byPhone.get(event.phone);
    if(!old||Date.parse(event.at)>Date.parse(old.at))byPhone.set(event.phone,event);
    else if(Date.parse(event.at)===Date.parse(old.at)&&JSON.stringify(old)!==JSON.stringify(event))
      byPhone.set(event.phone,{...old,confirmed:false,conflictingProof:true});
  }return byPhone;
}
export function buildPlan(population,carriers,events,zone,{onlineOnly=false,originalCarrierIdAuthoritative=false}={}){
  const latest=latestEvents(events),eligible=[],excluded=[],seenPhone=new Set(),seenId=new Set();
  for(const raw of population){const live=liveIdentity(raw);
    if(seenPhone.has(live.phone)||seenId.has(live.id))throw Error('SWAP_POPULATION_DUPLICATED');
    seenPhone.add(live.phone);seenId.add(live.id);
    if(live.carrierIdentif!==zone.identif)throw Error('SWAP_OUTSIDE_AUTHORIZED_SCOPE');
    const event=latest.get(live.phone);let reason;
    if(onlineOnly&&(raw.displayStatus!=='已上架'||raw.userAble!==true))reason='not_confirmed_currently_online';
    else if(!event)reason='no_original_record';
    else if(!event.confirmed)reason='latest_entry_not_independently_verified';
    else if(event.targetIdentif!==zone.identif)reason='historical_swap_id_changed';
    // A carrier belongs to the card record, not the current physical socket.
    // An empty live slot is allowed ONLY with the exact record ID, phone and
    // historical ICCID; a populated different slot still fails closed. The
    // live empty slot is frozen and checked before/after every manual write.
    // OPR is a discovered network label, not a SIM identifier. Explicit manual
    // restoration accepts a changed label only when the historical ICCID,
    // unique backend ID and phone all match. Current OPR is still frozen in
    // the preview and every independent write/readback check.
    else if(event.id!==live.id||(live.slot&&event.slot!==live.slot)||!live.iccid)reason='identity_changed_or_incomplete';
    const original=event?.original,targets=carriers.filter(c=>carrierId(c)===original?.identif
      &&(originalCarrierIdAuthoritative||carrierName(c)===original?.name)&&c.delData!==true);
    const forbidden=name=>[...MANUAL_RESTORE_SCOPES,'周末休息区T','周末休息区TV','B13'].includes(name);
    if(!reason&&(targets.length!==1||original.identif===zone.identif||forbidden(original.name)
      ||!carrierName(targets[0])||forbidden(carrierName(targets[0]))))reason='original_carrier_deleted_or_changed';
    if(!reason&&event.iccid!==live.iccid)reason=event.workbook?'historical_iccid_not_confirmed':'identity_changed_or_incomplete';
    if(reason){excluded.push({...live,reason,original:original||null,entryAt:event?.at||null});continue;}
    const target={identif:carrierId(targets[0]),name:carrierName(targets[0])};
    eligible.push({...live,rawId:raw.id,target,originalCarrier:original,...(onlineOnly?{requireOnlineBeforeWrite:true}:{}),entryAt:event.at,evidence:event.evidence,
      workbookProof:event.workbookProof||null});
  }
  eligible.sort((a,b)=>a.target.identif.localeCompare(b.target.identif)||a.phone.localeCompare(b.phone));
  excluded.sort((a,b)=>a.phone.localeCompare(b.phone));
  const groups=[];
  for(const row of eligible){let group=groups.find(g=>g.target.identif===row.target.identif);
    if(!group){group={target:row.target,rows:[]};groups.push(group);}group.rows.push(row);}
  return {populationCount:population.length,eligibleCount:eligible.length,excludedCount:excluded.length,groups,excluded};
}
export function enrichWorkbooks(root,events,population){
  const selected=latestEvents(events),needed=new Map();
  for(const raw of population){const e=selected.get(phone(raw.cardNo));if(!e?.confirmed||!e.workbook)continue;
    const k=JSON.stringify([e.workbook.path,e.workbook.sha256]);if(!needed.has(k))needed.set(k,[]);needed.get(k).push(e);}
  for(const [key,list] of needed){const [file,sha]=JSON.parse(key);
    if(!path.isAbsolute(file||'')||!/^[a-f0-9]{64}$/.test(sha||''))continue;
    const out=execFileSync('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3',
      [path.join(root,'scripts/opensms_swap_history_workbook.py'),file,sha,JSON.stringify(list.map(e=>e.phone))],{encoding:'utf8',maxBuffer:8*1024*1024});
    const rows=JSON.parse(out);
    for(const event of list){const matches=rows.filter(r=>r.phone===event.phone&&r.sourceRow===event.workbook.sourceRow);
      if(matches.length!==1)continue;const r=matches[0];
      if(typeof r.iccid!=='string'||normalizeSlot(r.slot)!==event.slot||text(r.opr)!==event.opr
        ||text(r.carrier)!==event.original.name)continue;
      event.iccid=normalizeIccid(r.iccid);event.workbookProof={path:file,sha256:sha,sourceRow:r.sourceRow};
    }
  }return events;
}
export function verifyRows(expected,result,target){
  if(result.total!==expected.length||result.rows.length!==expected.length)throw Error('SWAP_READBACK_INCOMPLETE');
  const byPhone=new Map();for(const r of result.rows){const live=liveIdentity(r);
    if(byPhone.has(live.phone))throw Error('SWAP_READBACK_DUPLICATED');byPhone.set(live.phone,live);}
  for(const e of expected){const actual=byPhone.get(e.phone);
    if(!actual||['phone','id','slot','iccid','opr'].some(k=>actual[k]!==e[k])
      ||actual.carrierIdentif!==(target?.identif||e.carrierIdentif))throw Error('SWAP_READBACK_IDENTITY_OR_CARRIER_CHANGED');}
}
export function verifyOnlineBeforeWrite(expected,result){
  for(const e of expected.filter(r=>r.requireOnlineBeforeWrite)){
    const rows=result.rows.filter(r=>phone(r.cardNo)===e.phone);
    if(rows.length!==1||rows[0].displayStatus!=='已上架'||rows[0].userAble!==true)
      throw Error('SWAP_ONLINE_STATUS_CHANGED_BEFORE_SUBMISSION');
  }
}
