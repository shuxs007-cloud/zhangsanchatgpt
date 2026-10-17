// File-only provenance: a verified rest entry followed by a verified swap entry.
import fs from 'node:fs/promises';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {digest,normalizeSlot,normalizeIccid,validateLedger} from './opensms_task_g_engine.mjs';
import {latestEvents,liveIdentity} from './opensms_swap_t_restore_engine.mjs';
export function verifiedRestOrigin(event,record,receipt){
  if(!event.confirmed||receipt.status!=='passed'||receipt.independentReadbackPassed!==true
    ||receipt.runId!==record.confirmedRunId||record.restCarrier.name!==event.original.name
    ||record.restCarrier.identif!==event.original.identif||record.phone!==event.phone
    ||record.backendId!==event.id||normalizeSlot(record.slot)!==event.slot
    ||normalizeIccid(record.iccid)!==event.iccid)return null;
  const entries=(receipt.batches||[]).filter(b=>b.action==='enter'&&b.accepted===true)
    .flatMap(b=>b.records.map((r,i)=>({r,before:b.before[i],after:b.after[i]})))
    .filter(x=>x.r.key===record.key);
  if(entries.length!==1)return null;
  const {r,before,after}=entries[0];
  if(JSON.stringify(r.originalCarrier)!==JSON.stringify(record.originalCarrier))return null;
  for(const [row,carrier] of [[before,record.originalCarrier.identif],[after,record.restCarrier.identif]])
    if(!row||row.phone!==event.phone||row.id!==event.id||normalizeSlot(row.slot)!==event.slot
      ||normalizeIccid(row.iccid)!==event.iccid||row.carrierIdentif!==carrier)return null;
  const run=record.confirmedRunId.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})\+0800$/);
  if(!run||Date.parse(`${run[1]}-${run[2]}-${run[3]}T${run[4]}:${run[5]}:${run[6]}+08:00`)>Date.parse(event.at))return null;
  return record.originalCarrier;
}
export async function resolveRestOrigins(root,history,population){
  const latest=latestEvents(history.events),events=population.map(liveIdentity).map(r=>latest.get(r.phone))
    .filter(e=>e?.confirmed&&['周末休息区T','周末休息区TV'].includes(e.original?.name));
  if(!events.length)return;
  const ledgerPath=path.join(root,'opensms_artifacts/state/task_g_weekend_rest/ledger.json');
  const bytes=await fs.readFile(ledgerPath),ledger=JSON.parse(bytes);validateLedger(ledger);
  const files=execFileSync('rg',['--files','/Volumes/zs01/chatgpt/OpenSMS','-g','opensms_task_g_receipt_*.json'],{encoding:'utf8',maxBuffer:4*1024*1024}).trim().split('\n').filter(Boolean);
  for(const event of events){
    const records=Object.values(ledger.records).filter(r=>r.phone===event.phone&&r.backendId===event.id
      &&normalizeIccid(r.iccid)===event.iccid&&r.restCarrier.identif===event.original.identif
      &&!['entering','restoring'].includes(r.status))
      .sort((a,b)=>b.entryDate.localeCompare(a.entryDate));
    const record=records[0];if(!record)continue;
    const candidates=files.filter(f=>path.basename(f)===`opensms_task_g_receipt_${record.confirmedRunId}.json`);
    const origins=[];
    for(const file of candidates){const raw=await fs.readFile(file),receipt=JSON.parse(raw),origin=verifiedRestOrigin(event,record,receipt);
      if(origin)origins.push({origin,proof:{path:file,sha256:digest(raw)}});}
    if(!origins.length||origins.some(x=>JSON.stringify(x.origin)!==JSON.stringify(origins[0].origin)))continue;
    event.restOriginChain={via:event.original,recordKey:record.key};event.original=origins[0].origin;
    event.evidence=[...event.evidence,{path:ledgerPath,sha256:digest(bytes)},...origins.map(x=>x.proof)];
  }
}
