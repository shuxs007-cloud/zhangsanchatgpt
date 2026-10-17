// Task G only: serial, receipt-gated transfers with durable no-replay intents.
import crypto from 'node:crypto';

export const digest = value => crypto.createHash('sha256').update(value).digest('hex');
export const group = name => String(name || '').trim().match(/^(B1V|B11V|B12V|B1|B11|B12)(?:-|\s|$)/i)?.[1].toUpperCase();
export const normalizeSlot = value => String(value || '').trim().replace(/_(\d+)$/, (_, n) => '_' + Number(n));
export const normalizeIccid = value => String(value || '').trim().toUpperCase().replace(/F$/, '');
export const entryOprReason = value => typeof value!=='string' || !value.trim() ? 'unknown_opr'
  : /\bKini\b/i.test(value) ? 'kini' : null;
export function addDay(value) {
  const d = new Date(value + 'T00:00:00Z');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(+d) || d.toISOString().slice(0, 10) !== value) throw Error('TASK_G_INVALID_DATE');
  d.setUTCDate(d.getUTCDate() + 1); return d.toISOString().slice(0, 10);
}
export const recordKey = r => digest(JSON.stringify([r.phone, r.backendId, r.slot, r.iccid, r.entryDate]));
const clone = value => JSON.parse(JSON.stringify(value));
const sameIdentity = (row, r) => row && row.id === r.backendId && row.phone === r.phone && row.slot === r.slot && row.iccid === r.iccid;
const active = r => ['entering', 'resting', 'restoring'].includes(r.status);
const immutable = r => ({key:r.key,phone:r.phone,backendId:r.backendId,slot:r.slot,iccid:r.iccid,
  originalCarrier:r.originalCarrier,restCarrier:r.restCarrier,entryDate:r.entryDate,restoreDate:r.restoreDate,metricDate:r.metricDate});

export function validateLedger(ledger) {
  if (ledger?.version !== 1 || !ledger.records || Array.isArray(ledger.records)) throw Error('TASK_G_LEDGER_INVALID');
  const phones = new Set();
  for (const [key, r] of Object.entries(ledger.records)) {
    const g = group(r.originalCarrier?.name);
    if (key !== r.key || key !== recordKey(r) || !/^\d{10}$/.test(r.phone) || !r.backendId || !r.slot
      || typeof r.iccid !== 'string' || !g || !r.originalCarrier.identif || !r.restCarrier?.identif
      || r.restCarrier.name !== (g.endsWith('V') ? '周末休息区TV' : '周末休息区T')
      || r.originalCarrier.identif === r.restCarrier.identif || addDay(r.entryDate) !== r.restoreDate
      || addDay(r.metricDate) !== r.entryDate || !['entering','resting','restoring','restored','detached'].includes(r.status)) throw Error('TASK_G_LEDGER_INVALID');
    if (active(r)) { if (phones.has(r.phone)) throw Error('TASK_G_LEDGER_DUPLICATE_ACTIVE_PHONE'); phones.add(r.phone); }
  }
}

export async function runTaskG({spec, query, write, readDate, carriers, journal, receipt}) {
  if(JSON.stringify(spec.settings.excludedEntryOprTokens)!==JSON.stringify(['Kini']))throw Error('TASK_G_KINI_EXCLUSION_GATE_MISSING');
  const today = spec.serverDate, threshold = spec.settings.previousDayTotalSendThresholdExclusive;
  let ledger = await journal.read(); validateLedger(ledger);
  Object.assign(receipt, {version:1,runId:spec.runId,scope:spec.scope,status:'running',retryCount:0,
    serverDate:today,threshold,ledgerBefore:clone(ledger),batches:[],reconciliations:[],skips:[],carriers:clone(carriers),dateChecks:[],
    enteredCount:0,restoredCount:0,alreadyHandledCount:0,sourceChangedCount:0,detachedCount:0,
    unknownMetricRowCount:spec.unknownMetricRowCount,
    sourceKiniExcludedCount:spec.rows.filter(r=>entryOprReason(r.opr)==='kini').length,
    sourceUnknownOprCount:spec.rows.filter(r=>entryOprReason(r.opr)==='unknown_opr').length,
    liveKiniExcludedCount:0,liveUnknownOprCount:0,independentReadbackPassed:false});
  const save = async () => {validateLedger(ledger); await journal.save(ledger); receipt.ledgerAfter=clone(ledger);};
  const dateGate = async () => { const actual=await readDate();receipt.dateChecks.push({actual});if(actual!==today)throw Error('TASK_G_PLATFORM_DATE_CHANGED'); };
  const carrier = (name, identif) => {
    const matches=carriers.filter(c=>c.name===name && (!identif || c.identif===identif));
    if(matches.length!==1) throw Error('TASK_G_CARRIER_MISSING_OR_AMBIGUOUS'); return matches[0];
  };
  const zones = {T:carrier('周末休息区T'),TV:carrier('周末休息区TV')};
  const eligible=spec.rows.filter(r=>group(r.sourceCarrier) && entryOprReason(r.opr)===null
    && Number.isSafeInteger(r.previousDayTotalSend) && r.previousDayTotalSend>threshold);
  if(eligible.length!==spec.entryCandidateCount || receipt.sourceKiniExcludedCount!==spec.sourceKiniExcludedCount
    || receipt.sourceUnknownOprCount!==spec.sourceUnknownOprCount)throw Error('TASK_G_OPR_CANDIDATE_COUNTS_UNCONFIRMED');
  if(new Set(spec.rows.map(r=>r.phone)).size!==spec.rows.length) throw Error('TASK_G_INPUT_DUPLICATE_PHONE');
  const allPhones=[...new Set([...eligible.map(r=>r.phone),...Object.values(ledger.records).filter(active).map(r=>r.phone)])];
  const live=new Map();
  for(let n=0;n<allPhones.length;n+=500) for(const [phone,row] of await query(allPhones.slice(n,n+500))) live.set(phone,row);
  try {
    await dateGate();
    // Only positive independent readback resolves an old intent. An unchanged
    // source cannot prove that a timed-out submission was never accepted.
    for(const r of Object.values(ledger.records).filter(r=>['entering','restoring'].includes(r.status))) {
      const row=live.get(r.phone), target=r.status==='entering'?r.restCarrier:r.originalCarrier;
      if(!sameIdentity(row,r) || row.carrierIdentif!==target.identif) throw Error('TASK_G_UNRESOLVED_INTENT_NO_REPLAY');
      const action=r.status==='entering'?'enter':'restore';
      receipt.reconciliations.push({action,record:immutable(r),after:row});
      r.status=action==='enter'?'resting':'restored'; r.confirmedRunId=spec.runId;
      if(action==='restore')r.restoredDate=today;
      await save();
    }
    const restoredToday=new Set(Object.values(ledger.records).filter(r=>r.status==='restored' && r.restoredDate===today).map(r=>r.phone));
    for(const rec of receipt.reconciliations.filter(r=>r.action==='restore')) {
      restoredToday.add(rec.record.phone);
    }
    const restores=[];
    for(const r of Object.values(ledger.records).filter(r=>r.status==='resting' && r.restoreDate<=today)) {
      const row=live.get(r.phone);
      if(!sameIdentity(row,r) || row.carrierIdentif!==r.restCarrier.identif) {
        r.status='detached';r.detachedRunId=spec.runId;receipt.detachedCount++;
        receipt.skips.push({reason:'ledger_identity_or_zone_changed',record:immutable(r),observed:row||null});continue;
      }
      carrier(r.originalCarrier.name,r.originalCarrier.identif);carrier(r.restCarrier.name,r.restCarrier.identif);
      restores.push(r);
    }
    receipt.candidateCount=eligible.length+restores.length;
    await save();
    const execute = async (action, records) => {
      const grouped=new Map();
      for(const r of records) {
        const target=action==='enter'?r.restCarrier:r.originalCarrier;
        if(!grouped.has(target.identif))grouped.set(target.identif,[]);grouped.get(target.identif).push(r);
      }
      for(const list of grouped.values())for(let n=0;n<list.length;n+=500) {
        await dateGate();
        const batch=list.slice(n,n+500), before=await query(batch.map(r=>r.phone));
        const accepted=[];
        for(const r of batch) {
          const from=action==='enter'?r.originalCarrier:r.restCarrier, row=before.get(r.phone);
          if(!sameIdentity(row,r) || row.carrierIdentif!==from.identif) {
            if(action==='restore'){r.status='detached';receipt.detachedCount++;}
            else receipt.sourceChangedCount++;
            receipt.skips.push({reason:'changed_before_'+action,record:immutable(r),observed:row||null});continue;
          }
          if(action==='enter' && entryOprReason(row.opr)!==null) {
            const reason=entryOprReason(row.opr);
            receipt[reason==='kini'?'liveKiniExcludedCount':'liveUnknownOprCount']++;
            receipt.skips.push({reason:reason+'_before_enter',record:immutable(r),observed:row});continue;
          }
          accepted.push(r);
        }
        if(!accepted.length){await save();continue;}
        await dateGate();
        const target=action==='enter'?accepted[0].restCarrier:accepted[0].originalCarrier;
        for(const r of accepted){r.status=action==='enter'?'entering':'restoring';r.intentRunId=spec.runId;ledger.records[r.key]=r;}
        await save(); // durable intent precedes the only PUT; never auto retry
        const evidence={action,serverDate:today,records:accepted.map(immutable),before:accepted.map(r=>before.get(r.phone)),after:[],accepted:false};
        receipt.batches.push(evidence); await journal.evidence(receipt);
        await write(accepted.map(r=>r.backendId),target.identif);evidence.accepted=true;
        await journal.evidence(receipt);
        const after=await query(accepted.map(r=>r.phone));evidence.after=accepted.map(r=>after.get(r.phone)||null);
        if(accepted.some(r=>!sameIdentity(after.get(r.phone),r) || after.get(r.phone).carrierIdentif!==target.identif)) throw Error('TASK_G_WRITE_READBACK_UNCONFIRMED');
        for(const r of accepted){r.status=action==='enter'?'resting':'restored';r.confirmedRunId=spec.runId;
          if(action==='restore'){r.restoredDate=today;restoredToday.add(r.phone);}}
        receipt[action==='enter'?'enteredCount':'restoredCount']+=accepted.length;await save();await journal.evidence(receipt);
      }
    };
    await execute('restore',restores);
    const entries=[],handled=new Set(Object.values(ledger.records).filter(r=>active(r)||r.entryDate===today||restoredToday.has(r.phone)).map(r=>r.phone));
    for(const source of eligible) {
      if(handled.has(source.phone)){receipt.alreadyHandledCount++;receipt.skips.push({reason:'already_handled',phone:source.phone});continue;}
      const row=live.get(source.phone), orig=carriers.find(c=>c.identif===row?.carrierIdentif);
      if(!row || !orig || orig.name!==source.sourceCarrier || !group(orig.name)
        || row.slot!==normalizeSlot(source.slot) || row.iccid!==normalizeIccid(source.iccid)) {
        receipt.sourceChangedCount++;receipt.skips.push({reason:'source_or_identity_changed',phone:source.phone,observed:row||null});continue;
      }
      if(entryOprReason(row.opr)!==null) {
        const reason=entryOprReason(row.opr);
        receipt[reason==='kini'?'liveKiniExcludedCount':'liveUnknownOprCount']++;
        receipt.skips.push({reason:reason+'_at_live_check',phone:source.phone,observed:row});continue;
      }
      const r={phone:source.phone,backendId:row.id,slot:row.slot,iccid:row.iccid,originalCarrier:carrier(orig.name,orig.identif),
        restCarrier:zones[group(orig.name).endsWith('V')?'TV':'T'],entryDate:today,restoreDate:addDay(today),metricDate:spec.previousDate};
      r.key=recordKey(r);entries.push(r);
    }
    await execute('enter',entries);
    receipt.status='passed';receipt.independentReadbackPassed=true;
  } finally {
    receipt.ledgerAfter=clone(ledger);receipt.restingCount=Object.values(ledger.records).filter(r=>r.status==='resting').length;
    receipt.verifiedBatchCount=receipt.batches.filter(b=>b.after.length && b.after.every(Boolean)
      && b.records.every(r=>['resting','restored'].includes(ledger.records[r.key]?.status))).length;
    receipt.refreshGroups=receipt.reconciliations.map(rec=>({mode:'expected',phones:[rec.record.phone],target:(rec.action==='enter'?rec.record.restCarrier:rec.record.originalCarrier).name,route:'task_g_reconcile'}));
    receipt.refreshGroups.push(...receipt.batches.filter(b=>b.after.length && b.after.every(Boolean)).map(b=>({mode:'expected',phones:b.records.map(r=>r.phone),
      target:(b.action==='enter'?b.records[0].restCarrier:b.records[0].originalCarrier).name,route:'task_g_'+b.action})));
    const aggregate=new Map();
    for(const b of receipt.batches.filter(b=>b.action==='enter' && b.after.length && b.after.every(Boolean)))for(const r of b.records){
      if(ledger.records[r.key].status!=='resting')continue;
      const key=JSON.stringify([r.originalCarrier.name,r.restCarrier.name,r.entryDate,r.restoreDate]);
      if(!aggregate.has(key))aggregate.set(key,{originalCarrier:r.originalCarrier.name,restCarrier:r.restCarrier.name,entryDate:r.entryDate,restoreDate:r.restoreDate,count:0});aggregate.get(key).count++;
    }
    receipt.byOriginalCarrier=[...aggregate.values()];
  }
  return receipt;
}
