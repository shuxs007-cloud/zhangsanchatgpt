// Pure guards. Backend displayStatus, never a report colour, determines state.
import {createHash} from 'node:crypto';
export const ZONES=['过期下架卡T','过期下架卡C'];
export const SCOPE='expired_zones_all_listing_states_v2';
export const validDate=value=>typeof value==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value+'T00:00:00Z'))&&new Date(value+'T00:00:00Z').toISOString().slice(0,10)===value;
export function identity(row){
  const id=String(row.id??''),phone=row.cardNo,iccid=String(row.cardStore??'').trim().replace(/[fF]$/,''),slot=row.slotIdentif;
  if((typeof row.id==='number'&&!Number.isSafeInteger(row.id))||!/^\d+$/.test(id)||typeof phone!=='string'||!/^\d{10}$/.test(phone)||typeof row.cardStore!=='string'||!/^\d{18,22}$/.test(iccid)||typeof slot!=='string'||!slot.trim())throw Error('EXPIRED_PROBE_IDENTITY_INVALID');
  return {id,phone,iccid,slot};
}
export function identityKey(row){return createHash('sha256').update(JSON.stringify(identity(row))).digest('hex');}
export function listingState(row){
  if(row.displayStatus==='已上架'&&row.userAble===true)return 'online';
  if(row.displayStatus==='未上架'&&row.userAble===false)return 'offline';
  return 'ineligible_status';
}
export function isExpired(cutoff,today){if(!validDate(today))throw Error('EXPIRED_PROBE_SERVER_DATE_INVALID');return validDate(cutoff)&&cutoff<today;}
export function selectCandidates(rows,zones,today){
  // Membership and listing state alone define the standing authorization.
  // Dates may be missing, invalid, today or in the future; none exclude a card.
  if(zones.length!==2||new Set(zones.map(z=>z.identif)).size!==2||ZONES.some(name=>zones.filter(z=>z.name===name).length!==1))throw Error('EXPIRED_PROBE_SCOPE_INVALID');
  const zoneIds=new Set(zones.map(z=>z.identif)),ids=new Set(),phones=new Set(),candidates=[];
  let ineligibleStatusCount=0,invalidIdentityCount=0;
  for(const row of rows){
    if(!zoneIds.has(String(row.carrireIdentif))||ids.has(String(row.id)))throw Error('EXPIRED_PROBE_POPULATION_INVALID');ids.add(String(row.id));
    if(listingState(row)==='ineligible_status'){ineligibleStatusCount++;continue;}
    let confirmed;
    try{confirmed=identity(row);}catch(e){if(e.message!=='EXPIRED_PROBE_IDENTITY_INVALID')throw e;invalidIdentityCount++;continue;}
    const item={...confirmed,key:identityKey(row),carrierIdentif:String(row.carrireIdentif),initialState:listingState(row)};
    if(phones.has(item.phone))throw Error('EXPIRED_PROBE_DUPLICATE_PHONE');phones.add(item.phone);candidates.push(item);
  }
  return {candidates,deadlineFilterEnabled:false,ineligibleStatusCount,invalidIdentityCount,populationCount:rows.length};
}
export function assertSame(item,row,today){
  if(!row||identityKey(row)!==item.key||String(row.carrireIdentif)!==item.carrierIdentif)throw Error('EXPIRED_PROBE_IDENTITY_OR_SCOPE_CHANGED');
  return listingState(row);
}
export function decide(item,previous,runId){
  if(previous && (previous.key!==item.key||!Number.isSafeInteger(previous.attempts)||previous.attempts<0))throw Error('EXPIRED_PROBE_LEDGER_INVALID');
  if(previous&&!['pending_offline','pending_restore','awaiting_async_readback','online_verified'].includes(previous.status))throw Error('EXPIRED_PROBE_LEDGER_INVALID');
  if(previous&&previous.status!=='pending_offline'&&previous.attempts<1)throw Error('EXPIRED_PROBE_LEDGER_INVALID');
  if(previous?.status==='pending_offline')return 'blocked_uncertain_offline';
  if(previous?.status==='pending_restore')return 'read_only_pending';
  if(previous?.status==='awaiting_async_readback'&&item.initialState!=='online')return 'read_only_pending';
  if(runId&&previous?.runId===runId)return 'skip_same_run_already_tested';
  if(item.initialState==='offline'&&previous?.attempts>0)return 'skip_offline_already_tested';
  return item.initialState==='online'?'offline_then_restore':'restore_once';
}
export function validateRecoveryAcceptance(response,count){
  if(response?.code!==200||response.data?.acceptedCount!==count||response.data?.skippedCount!==0)throw Error('EXPIRED_PROBE_RECOVERY_ACCEPTANCE_UNCONFIRMED');
  return {acceptedCount:count,completed:false};
}
