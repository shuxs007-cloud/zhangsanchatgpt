import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { createRawOpenSmsApiSession, listPhonesByNumbers, updatePhoneCarrier } from '../opensms_protocol_client.mjs';
import { getCarrierConfigsReadOnly } from './opensms_carriers_readonly.mjs';
import { readPlatformDate } from './opensms_expired_card_probe.mjs';
import { digest, addDay, normalizeSlot, normalizeIccid, runTaskG, validateLedger } from './opensms_task_g_engine.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
export function decodePhoneRows(result, phones) {
  const {rows,total}=result, requested=new Set(phones),canonical=new Map(),rawIds=new Map();
  if(!Array.isArray(rows)||!Number.isSafeInteger(total)||total!==rows.length||rows.length>requested.size||phones.length>500)throw Error('TASK_G_PHONE_QUERY_INCOMPLETE');
  for(const row of rows){
    const full=String(row.cardNo||'').trim(),p=full.slice(-10),id=String(row.id||'').trim();
    if(!/^(?:1)?\d{10}$/.test(full)||!requested.has(p)||canonical.has(p)||!id||rawIds.has(id)
      || !['string','number'].includes(typeof row.id)
      || (typeof row.id==='number' && (!Number.isSafeInteger(row.id)||row.id<=0))
      || (row.slotIdentif!=null && typeof row.slotIdentif!=='string')
      || (row.cardStore!=null && typeof row.cardStore!=='string'))throw Error('TASK_G_PHONE_IDENTITY_AMBIGUOUS');
    // A uniquely identified row may lose its slot after removal/reassignment.
    // Keep that absence as a live observation, never fill it from the ledger.
    // The engine's full identity checks then detach/skip it without a PUT;
    // unresolved intents and post-write readback still fail closed.
    rawIds.set(id,row.id);canonical.set(p,{phone:p,id,slot:normalizeSlot(row.slotIdentif),iccid:normalizeIccid(row.cardStore),
      opr:typeof row.opr==='string' && row.opr.trim() ? row.opr.trim() : null,
      carrierIdentif:String(row.carrireIdentif||'').trim()});
  }
  return {canonical,rawIds};
}
export async function atomicJson(file,value,{exclusive=false}={}) {
  await fs.mkdir(path.dirname(file),{recursive:true});
  const bytes=JSON.stringify(value,null,2)+'\n';
  if(exclusive){const h=await fs.open(file,'wx',0o600);try{await h.writeFile(bytes);await h.sync();}finally{await h.close();}}
  else{const temp=file+'.'+process.pid+'.'+Date.now()+'.tmp',h=await fs.open(temp,'wx',0o600);
    try{await h.writeFile(bytes);await h.sync();}finally{await h.close();}await fs.rename(temp,file);}
  const dir=await fs.open(path.dirname(file),'r');try{await dir.sync();}finally{await dir.close();}
}
export async function main(argv=process.argv.slice(2)) {
  const [inputPath,receiptPath]=argv,runId=process.env.OPENSMS_RUN_ID;
  if(argv.length!==2 || !path.isAbsolute(inputPath||'') || !path.isAbsolute(receiptPath||'')
    || !/^\d{8}T\d{6}\+0800$/.test(runId||'') || process.env.OPENSMS_TASK_G_RUN_ID!==runId
    || !process.env.OPENSMS_RAW_API_TARGET_ID)throw Error('TASK_G_RUNNER_GATE_MISSING');
  const bytes=await fs.readFile(inputPath),spec=JSON.parse(bytes);
  if(digest(bytes)!==process.env.OPENSMS_TASK_G_INPUT_SHA256)throw Error('TASK_G_INPUT_HASH_GATE_MISSING');
  if(spec.version!==1 || spec.task!=='G' || spec.scope!=='previous_day_total_send_weekend_rest_v1' || spec.runId!==runId
    || spec.settings.enabled!==true || spec.settings.restNaturalDays!==1
    || !Number.isSafeInteger(spec.settings.previousDayTotalSendThresholdExclusive) || spec.settings.previousDayTotalSendThresholdExclusive<0
    || JSON.stringify(spec.settings.sourceGroups)!==JSON.stringify(['B1','B11','B12','B1V','B11V','B12V'])
    || JSON.stringify(spec.settings.excludedEntryOprTokens)!==JSON.stringify(['Kini'])
    || spec.settings.standardRestCarrier!=='周末休息区T' || spec.settings.vRestCarrier!=='周末休息区TV'
    || addDay(spec.previousDate)!==spec.serverDate || spec.metricColumn!==spec.previousDate.slice(5)+'总发送量')throw Error('TASK_G_INPUT_UNCONFIRMED');
  for(const field of ['source','scanCache','postBusinessWorkbook','postBusinessReadback'])
    if(digest(await fs.readFile(spec[field]))!==spec[field+'Sha256'])throw Error('TASK_G_INPUT_CHANGED');
  const settingsBytes=await fs.readFile(path.join(root,'scripts/opensms_task_g_settings.json'));
  if(digest(settingsBytes)!==spec.settingsSha256 || !isDeepStrictEqual(JSON.parse(settingsBytes),spec.settings))throw Error('TASK_G_SETTINGS_CHANGED');
  const ledgerPath=path.join(root,'opensms_artifacts/state/task_g_weekend_rest/ledger.json');
  const lockPath=ledgerPath+'.lock';await fs.mkdir(path.dirname(ledgerPath),{recursive:true});
  // A leftover lock is an explicit unresolved run, never automatically erased.
  const lock=await fs.open(lockPath,'wx',0o600);
  let session,created=false,receipt={runId,inputPath,inputSha256:digest(bytes),ledgerPath,status:'failed_no_replay',retryCount:0};
  try {
    await lock.writeFile(JSON.stringify({runId,pid:process.pid}));await lock.sync();
    await atomicJson(receiptPath,receipt,{exclusive:true});created=true;
    const journal={read:async()=>{try{const l=JSON.parse(await fs.readFile(ledgerPath,'utf8'));validateLedger(l);return l;}catch(e){if(e.code==='ENOENT')return {version:1,records:{}};throw e;}},
      save:l=>atomicJson(ledgerPath,l),evidence:r=>atomicJson(receiptPath,r)};
    session=await createRawOpenSmsApiSession({cdp:process.env.BITBROWSER_CDP,pathName:'/phoneConfig'});
    const configs=await getCarrierConfigsReadOnly(session.api);
    const carriers=configs.map(c=>({name:String(c.name||c.carrierName||c.carrireName||c.carrier||'').trim(),identif:String(c.carrireIdentif||c.carrierIdentif||c.identif||c.id||'').trim()}));
    const rawIds=new Map();
    const query=async phones=>{
      if(!phones.length)return new Map();
      const result=decodePhoneRows(await listPhonesByNumbers(session.api,phones,500),phones);
      for(const [id,value] of result.rawIds)rawIds.set(id,value);
      return result.canonical;
    };
    await runTaskG({spec,query,write:(ids,target)=>{
      if(ids.some(id=>!rawIds.has(id)))throw Error('TASK_G_BACKEND_ID_UNCONFIRMED');
      return updatePhoneCarrier(session.api,ids.map(id=>rawIds.get(id)),target);
    },
      readDate:()=>readPlatformDate(session.client),carriers,journal,receipt});
    receipt.ledgerAfterSha256=digest(await fs.readFile(ledgerPath));
  }catch(e){receipt.status='failed_no_replay';receipt.independentReadbackPassed=false;
    receipt.error=/^TASK_G_[A-Z_]+$/.test(e.message)?e.message:'TASK_G_OPERATION_UNCONFIRMED';}
  finally{session?.client.close();if(created)await atomicJson(receiptPath,receipt);await lock.close();await fs.unlink(lockPath);}
  if(!created)throw Error('TASK_G_REPLAY_FORBIDDEN');
  const {ledgerBefore,ledgerAfter,batches,reconciliations,skips,...summary}=receipt;
  console.log(JSON.stringify({...summary,receipt:receiptPath,receiptSha256:digest(await fs.readFile(receiptPath))}));
  if(receipt.status!=='passed')process.exitCode=1;return receipt;
}
if(process.argv[1] && path.resolve(process.argv[1])===fileURLToPath(import.meta.url))main().catch(e=>{console.error(e.message);process.exitCode=1;});
