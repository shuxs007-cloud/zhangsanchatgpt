// Pure Task H rules. No browser, database, Task F, or timer access.
import {isDeepStrictEqual} from 'node:util';

export const TASK_H_SCOPE = /^([AB]\d+(?:V)?)(?:$|[-\s])/i;
export const TASK_H_FIELDS = Object.freeze({dailyDispatchLimit:'sendLimitByDay',dailySendAmount:'sendNewLimitByDay'});
const DERIVED = new Set(['dailyRemaining','dailyTotal','phoneCount','userAblePhoneCount',
  'updateBy','updateTime','lastUpdateTime']);

export function validateSettings(settings){
  if(!settings||settings.version!==1||settings.executionMode!=='explicit-current-user-only'
    ||Object.keys(settings).sort().join(',')!=='dailyDispatchLimit,dailySendAmount,executionMode,version'
    ||![settings.dailyDispatchLimit,settings.dailySendAmount].every(n=>Number.isInteger(n)&&n>=1&&n<=99999)
    ||settings.dailySendAmount>settings.dailyDispatchLimit)throw Error('TASK_H_SETTINGS_INVALID');
  return settings;
}

function recordId(value){
  if(typeof value!=='string'||!/^\d+$/.test(value))throw Error('TASK_H_ID_INVALID');
  return value;
}

export function validateCarrier(row){
  if(!row||typeof row!=='object'||Array.isArray(row)||typeof row.name!=='string'
    ||row.name!==row.name.trim()||!TASK_H_SCOPE.test(row.name)||row.delData!==false)
    throw Error('TASK_H_CARRIER_IDENTITY_INVALID');
  recordId(row.id);recordId(row.identif);
  for(const field of Object.values(TASK_H_FIELDS)){
    if(!Number.isInteger(row[field])||row[field]<0||row[field]>99999)throw Error('TASK_H_LIMIT_FIELD_INVALID');
  }
  return row;
}

export function selectCarriers(rows){
  if(!Array.isArray(rows))throw Error('TASK_H_INVENTORY_INVALID');
  const selected=rows.filter(r=>typeof r?.name==='string'&&TASK_H_SCOPE.test(r.name)&&r.delData!==true);
  selected.forEach(validateCarrier);
  for(const key of ['id','identif']){
    if(new Set(selected.map(r=>r[key])).size!==selected.length)throw Error('TASK_H_INVENTORY_AMBIGUOUS');
  }
  const groups=selected.map(r=>r.name.match(TASK_H_SCOPE)[1].toUpperCase());
  if(new Set(groups).size!==groups.length)throw Error('TASK_H_INVENTORY_AMBIGUOUS');
  if(!selected.length)throw Error('TASK_H_SCOPE_EMPTY');
  return selected.sort((a,b)=>a.name.localeCompare(b.name,'en',{numeric:true}));
}

export function configValues(row){
  return Object.fromEntries(Object.entries(row).filter(([key])=>!DERIVED.has(key)));
}

export function assertSameIdentity(before,after){
  validateCarrier(before);validateCarrier(after);
  if(['id','identif','name','delData'].some(key=>before[key]!==after[key]))throw Error('TASK_H_IDENTITY_CHANGED');
}

export function assertSameConfig(before,after){
  assertSameIdentity(before,after);
  if(!isDeepStrictEqual(configValues(before),configValues(after)))throw Error('TASK_H_CONFIG_CHANGED_BEFORE_WRITE');
}

export function makePayload(before,settings){
  validateCarrier(before);validateSettings(settings);
  return {...structuredClone(before),sendLimitByDay:settings.dailyDispatchLimit,sendNewLimitByDay:settings.dailySendAmount};
}

export function needsUpdate(row,settings){
  validateCarrier(row);validateSettings(settings);
  return row.sendLimitByDay!==settings.dailyDispatchLimit||row.sendNewLimitByDay!==settings.dailySendAmount;
}

export function assertReadback(before,after,settings){
  assertSameIdentity(before,after);
  if(!isDeepStrictEqual(configValues(makePayload(before,settings)),configValues(after)))
    throw Error('TASK_H_INDEPENDENT_READBACK_FAILED');
}

export function assertSameScope(before,after){
  const a=selectCarriers(before),b=selectCarriers(after);
  if(a.length!==b.length||a.some((r,i)=>['id','identif','name'].some(k=>r[k]!==b[i][k])))
    throw Error('TASK_H_SCOPE_CHANGED');
}

export async function executeTaskH({rows,settings,readDetail,assertScope,persistIntent,submit,saveEntry,resolveIntent,savePlan=async()=>{}}){
  validateSettings(settings);
  const carriers=selectCarriers(rows),entries=[];
  // Establish every detail before the first write. Never submit list/statistics rows.
  const originals=[];
  for(const carrier of carriers){
    const detail=await readDetail(carrier.id);assertSameIdentity(carrier,detail);
    for(const key of Object.values(TASK_H_FIELDS))if(detail[key]!==carrier[key])throw Error('TASK_H_CONFIG_CHANGED_BEFORE_WRITE');
    originals.push(detail);
  }
  const plan=originals.map(before=>({before,payload:makePayload(before,settings),needsUpdate:needsUpdate(before,settings)}));
  await savePlan(plan);
  await assertScope(carriers);
  for(const item of plan){
    const current=await readDetail(item.before.id);assertSameConfig(item.before,current);
    const entry={id:current.id,name:current.name,before:current,needsUpdate:item.needsUpdate,
      submissionAttempted:false,accepted:false,independentVerified:false};
    entries.push(entry);
    if(item.needsUpdate){
      // Durable intent precedes the single request; errors leave it unresolved.
      await persistIntent(entry,item.payload);
      entry.submissionAttempted=true;await saveEntry(entry);
      const response=await submit(item.payload);
      if(!response||response.code!==200){
        const error=Error('TASK_H_SUBMISSION_UNCONFIRMED');
        if(Number.isSafeInteger(response?.code))error.responseCode=response.code;
        throw error;
      }
      entry.accepted=true;await saveEntry(entry);
    }
    entry.after=await readDetail(current.id);
    assertReadback(current,entry.after,settings);
    entry.independentVerified=true;await saveEntry(entry);
    if(item.needsUpdate)await resolveIntent(entry);
  }
  await assertScope(carriers);
  // Final independent sweep also verifies already-correct carriers.
  const final=[];
  for(const entry of entries){
    const after=await readDetail(entry.id);assertReadback(entry.before,after,settings);final.push(after);
  }
  return {entries,final,scoped:entries.length,alreadyCorrect:entries.filter(e=>!e.needsUpdate).length,
    attempted:entries.filter(e=>e.submissionAttempted).length,
    accepted:entries.filter(e=>e.accepted).length,
    independentlyVerified:entries.filter(e=>e.independentVerified).length,
    modified:entries.filter(e=>e.needsUpdate&&e.independentVerified).length};
}
