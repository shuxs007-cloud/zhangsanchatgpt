import test from 'node:test';
import assert from 'node:assert/strict';
import {buildPlan,isKini,verifyRows} from '../scripts/opensms_manual_kini_rest_restore.mjs';
import {recordKey} from '../scripts/opensms_task_g_engine.mjs';
function fixture(){
  const originalCarrier={name:'B12-精-Te-120',identif:'12'},restCarrier={name:'周末休息区T',identif:'99'};
  const record={phone:'4161234567',backendId:'123',slot:'101_1',iccid:'8912230200123456789',originalCarrier,restCarrier,
    entryDate:'2026-10-07',restoreDate:'2026-10-08',metricDate:'2026-10-06',status:'resting'};record.key=recordKey(record);
  const row={cardNo:record.phone,id:'123',slotIdentif:record.slot,cardStore:record.iccid,opr:'Kini',carrireIdentif:'99'};
  return {record,row,carriers:[{name:originalCarrier.name,identif:'12'},{name:restCarrier.name,identif:'99'}],ledger:{version:1,records:{[record.key]:record}}};
}
test('only independent Kini token is selected',()=>{assert.ok(isKini('Kini / Koodo'));assert.ok(!isKini('Koodo'));assert.ok(!isKini('notKini'));});
test('restores only exact ledger identity and original runtime carrier',()=>{const f=fixture(),p=buildPlan([f.row],f.carriers,f.ledger);assert.equal(p.eligibleCount,1);assert.equal(p.groups[0].target.identif,'12');});
test('changed card or unresolved intent never restores',()=>{const f=fixture();assert.equal(buildPlan([{...f.row,cardStore:'different'}],f.carriers,f.ledger).excludedCount,1);f.record.status='restoring';assert.equal(buildPlan([f.row],f.carriers,f.ledger).eligibleCount,0);});
test('missing origin and mixed Chatr are excluded',()=>{const f=fixture();assert.equal(buildPlan([f.row],f.carriers.slice(1),f.ledger).eligibleCount,0);assert.equal(buildPlan([{...f.row,opr:'Kini Chatr'}],f.carriers,f.ledger).excluded[0].reason,'ambiguous_opr');});
test('independent readback requires identity, OPR and exact target',()=>{const f=fixture(),p=buildPlan([f.row],f.carriers,f.ledger),rows=p.groups[0].rows,target=p.groups[0].target;verifyRows(rows,{total:1,rows:[{...f.row,carrireIdentif:'12'}]},target);assert.throws(()=>verifyRows(rows,{total:1,rows:[f.row]},target));assert.throws(()=>verifyRows(rows,{total:0,rows:[]},target));});
