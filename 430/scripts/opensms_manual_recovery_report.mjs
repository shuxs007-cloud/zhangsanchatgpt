// File-only completion reconciliation. Does not query or mutate business data.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {atomicJson} from './opensms_execute_task_g.mjs';
import {digest} from './opensms_task_g_engine.mjs';
import {liveIdentity,verifyRows} from './opensms_swap_t_restore_engine.mjs';

const [swapDirectory,weekendDirectory,...extra]=process.argv.slice(2);
if(extra.length||!path.isAbsolute(swapDirectory||'')||!path.isAbsolute(weekendDirectory||''))
  throw Error('RECOVERY_REPORT_ARGUMENTS_INVALID');
const proofs=[];
const load=async file=>{const bytes=await fs.readFile(file);proofs.push({path:file,sha256:digest(bytes)});return JSON.parse(bytes);};
const s=await load(path.join(swapDirectory,'execution.json'));
const sp=await load(path.join(swapDirectory,'preview.json'));
const w=await load(path.join(weekendDirectory,'execution.json'));
const wp=await load(path.join(weekendDirectory,'preview.json'));
const inventory=await load(path.join(swapDirectory,'all_zones_final.json'));
assert.equal(s.runId,w.runId);assert.equal(sp.runId,s.runId);assert.equal(wp.runId,s.runId);
assert.equal(inventory.status,'passed');assert.equal(inventory.independentReadbackPassed,true);
assert.equal(inventory.businessWrites,0);assert.equal(inventory.ownedHiddenPageClosed,true);
assert.equal(inventory.serverDate,s.serverDate);assert.equal(w.serverDate,s.serverDate);
assert.equal(s.previewSha256,proofs[1].sha256);assert.equal(w.previewSha256,proofs[3].sha256);
for(const [state,preview] of [[s,sp],[w,wp]]){
  assert.equal(state.status,'passed');assert.equal(state.ownedHiddenPageClosed,true);
  assert.equal(state.actualWriteCount,preview.plan.eligibleCount);
  assert.equal(state.independentVerifiedCount,preview.plan.eligibleCount);
  const expected=preview.plan.groups.flatMap(g=>g.rows);
  const written=state.groups.flatMap(g=>g.rows);
  assert.deepEqual(written.map(r=>r.id).sort(),expected.map(r=>r.id).sort());
  for(const batch of state.groups){assert.equal(batch.status,'independently_verified');
    verifyRows(batch.rows,batch.before);verifyRows(batch.rows,batch.readback,batch.target);}
  const finalRows=(state.finalReadbacks||[]).flatMap(result=>(result.result||result).rows);
  assert.deepEqual(finalRows.map(r=>String(r.id)).sort(),expected.map(r=>r.id).sort());
  for(const group of preview.plan.groups){const ids=new Set(group.rows.map(r=>r.id));
    const rows=finalRows.filter(r=>ids.has(String(r.id)));
    verifyRows(group.rows,{total:rows.length,rows},group.target);}
}
assert.equal(s.finalIndependentReadbackPassed,true);
assert.equal(w.ledgerIndependentReadbackPassed,true);
assert.equal(w.ledgerBeforeSha256,wp.ledgerSha256);
const ledgerFile='/Users/zhangsan/Downloads/430/opensms_artifacts/state/task_g_weekend_rest/ledger.json';
const ledger=await load(ledgerFile);
assert.equal(proofs.at(-1).sha256,w.ledgerAfterSha256);assert.deepEqual(ledger,w.ledgerAfter);
for(const group of wp.plan.groups)for(const row of group.rows){const record=ledger.records[row.ledgerKey];
  assert.equal(record.status,'restored');assert.equal(record.restoredDate,w.serverDate);
  assert.deepEqual(record.originalCarrier,row.target);}
const exclusions=[...sp.plan.excluded,...wp.plan.excluded];
const identity=r=>Object.fromEntries(['phone','id','slot','iccid','opr','carrierIdentif'].map(k=>[k,r[k]]));
const sorted=rows=>rows.map(identity).sort((a,b)=>a.id.localeCompare(b.id));
assert.deepEqual(sorted(inventory.population.map(liveIdentity)),sorted(exclusions));
assert.equal(new Set(exclusions.map(r=>r.id)).size,exclusions.length);
const names=new Map(inventory.zones.map(z=>[z.identif,z.name]));
const labels={no_original_record:'缺少原运营商记录',latest_entry_not_independently_verified:'最近转入未完成独立核验',
  historical_swap_id_changed:'历史分区标识不一致',historical_iccid_not_confirmed:'历史ICCID未能确认',
  original_carrier_deleted_or_changed:'原运营商已删除或变化',current_slot_missing:'当前卡槽为空'};
const summary={status:'verified',runId:s.runId,serverDate:s.serverDate,verifiedAt:new Date().toISOString(),
  allListingStates:true,keepWorst200:false,actualRestoredCount:s.actualWriteCount+w.actualWriteCount,
  independentlyVerifiedCount:s.independentVerifiedCount+w.independentVerifiedCount,
  swapRestoredCount:s.actualWriteCount,weekendRestoredCount:w.actualWriteCount,
  remainingCount:inventory.total,remainingByZone:inventory.zoneCounts,
  exceptionCounts:Object.fromEntries([...new Set(exclusions.map(r=>r.reason))].map(reason=>
    [labels[reason]||reason,exclusions.filter(r=>r.reason===reason).length])),
  exceptions:exclusions.map(r=>({...r,zone:names.get(r.carrierIdentif),reasonChinese:labels[r.reason]||r.reason})),
  restoredTargets:[...sp.plan.groups,...wp.plan.groups].map(g=>({target:g.target,count:g.rows.length})),
  evidence:proofs};
const file=path.join(swapDirectory,'恢复结果与暂留号码.json');
await atomicJson(file,summary,{exclusive:true});assert.deepEqual(JSON.parse(await fs.readFile(file)),summary);
console.log(JSON.stringify({status:summary.status,path:file,actualRestoredCount:summary.actualRestoredCount,
  independentlyVerifiedCount:summary.independentlyVerifiedCount,swapRestoredCount:summary.swapRestoredCount,
  weekendRestoredCount:summary.weekendRestoredCount,remainingCount:summary.remainingCount,
  exceptionCounts:summary.exceptionCounts,remainingByZone:summary.remainingByZone}));
