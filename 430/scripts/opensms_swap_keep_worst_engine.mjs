// Pure, offline ranking. Missing metrics are never converted to zero.
import {liveIdentity} from './opensms_swap_t_restore_engine.mjs';
import {normalizeIccid,normalizeSlot} from './opensms_task_g_engine.mjs';

export const SWAP_SCOPES=['换号区T','换号区C'];
export function rankSwapPopulation(population,metrics,zones,keepCount=200){
  if(!Number.isSafeInteger(keepCount)||keepCount<1||metrics.metric!=='4日回复率')throw Error('SWAP_RANKING_PARAMETERS_INVALID');
  const byPhone=new Map(),ranked=[],unknown=[],seenIds=new Set(),seenPhones=new Set();
  for(const row of metrics.rows){if(!byPhone.has(row.phone))byPhone.set(row.phone,[]);byPhone.get(row.phone).push(row);}
  for(const raw of population){
    const live=liveIdentity(raw),zone=zones.find(z=>z.identif===live.carrierIdentif);
    if(!zone||!SWAP_SCOPES.includes(zone.name))throw Error('SWAP_RANKING_OUTSIDE_SCOPE');
    if(seenIds.has(live.id)||seenPhones.has(live.phone))throw Error('SWAP_RANKING_DUPLICATE_LIVE_IDENTITY');
    seenIds.add(live.id);seenPhones.add(live.phone);
    const matches=byPhone.get(live.phone)||[];
    if(matches.length>1)throw Error('SWAP_RANKING_DUPLICATE_SOURCE_PHONE');
    const row=matches[0];let reason;
    if(!row)reason='not_in_latest_report';
    else if(typeof row.iccid!=='string'||!live.iccid||normalizeIccid(row.iccid)!==live.iccid
      ||(live.slot&&normalizeSlot(row.slot)!==live.slot))reason='metric_card_identity_changed_or_missing';
    else if(String(row.carrier||'').trim()!==zone.name)reason='metric_source_zone_changed';
    else if(typeof row.replyRate!=='number'||!Number.isFinite(row.replyRate)||row.replyRate<0||row.replyRate>1)reason='reply_rate_missing_or_invalid';
    else if(!Number.isSafeInteger(row.sendWindows)||row.sendWindows<=0)reason='send_windows_missing_invalid_or_zero';
    if(reason){unknown.push({...live,zone:zone.name,reason,sourceRow:row?.sourceRow??null});continue;}
    ranked.push({...live,zone:zone.name,replyRate:row.replyRate,sendWindows:row.sendWindows,sourceRow:row.sourceRow});
  }
  ranked.sort((a,b)=>a.replyRate-b.replyRate||b.sendWindows-a.sendWindows||a.phone.localeCompare(b.phone));
  const kept=ranked.slice(0,keepCount),restore=ranked.slice(keepCount);
  unknown.sort((a,b)=>a.phone.localeCompare(b.phone));
  return {kept,restore,unknown,summary:{keepRequestedCount:keepCount,rankableCount:ranked.length,
    keptCount:kept.length,restoreSelectedCount:restore.length,unknownMetricCount:unknown.length,
    cutoffReplyRate:kept.at(-1)?.replyRate??null,cutoffSendWindows:kept.at(-1)?.sendWindows??null,
    metric:'4日回复率',tieBreak:metrics.tieBreak,sourceRunId:metrics.runId,sourceAsOf:metrics.asOf}};
}
