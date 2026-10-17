import test from 'node:test';
import assert from 'node:assert/strict';
import {buildWeekendRestPlan,verifyRows} from '../scripts/opensms_manual_kini_rest_restore.mjs';
import {recordKey} from '../scripts/opensms_task_g_engine.mjs';
function fixture(){
  const originalCarrier={name:'B12-精-Te-120',identif:'12'},restCarrier={name:'周末休息区T',identif:'99'};
  const record={phone:'4161234567',backendId:'123',slot:'101_1',iccid:'8912230200123456789',originalCarrier,restCarrier,
    entryDate:'2026-10-08',restoreDate:'2026-10-09',metricDate:'2026-10-07',status:'resting'};record.key=recordKey(record);
  const row={cardNo:record.phone,id:'123',slotIdentif:record.slot,cardStore:record.iccid,opr:'Koodo',carrireIdentif:'99'};
  return {record,row,carriers:[{name:originalCarrier.name,identif:'12'},{name:restCarrier.name,identif:'99'}],ledger:{version:1,records:{[record.key]:record}}};
}
test('all OPRs restore from exact G provenance without a due-date filter',()=>{
  const f=fixture();for(const opr of ['Koodo','Kini','Chatr','']){
    const p=buildWeekendRestPlan([{...f.row,opr}],f.carriers,f.ledger);assert.equal(p.eligibleCount,1);
    assert.equal(p.groups[0].target.identif,'12');assert.equal(p.selection,'task-g-weekend');
  }
});
test('missing provenance, changed card, changed slot, missing slot and retired origin never write',()=>{
  const f=fixture();for(const row of [{...f.row,cardStore:'other'},{...f.row,slotIdentif:'101_2'},
    {...f.row,slotIdentif:null}])assert.equal(buildWeekendRestPlan([row],f.carriers,f.ledger).eligibleCount,0);
  assert.equal(buildWeekendRestPlan([f.row],f.carriers,{version:1,records:{}}).excludedCount,1);
  assert.equal(buildWeekendRestPlan([f.row],f.carriers.slice(1),f.ledger).eligibleCount,0);
});
test('unresolved intents block restoration instead of replaying',()=>{
  const f=fixture();f.record.status='restoring';assert.throws(()=>buildWeekendRestPlan([f.row],f.carriers,f.ledger),/UNRESOLVED/);
});
test('duplicate identities, unsafe IDs, invalid field types and other zones fail closed',()=>{
  const f=fixture();assert.throws(()=>buildWeekendRestPlan([f.row,f.row],f.carriers,f.ledger));
  for(const row of [{...f.row,id:9007199254740992},{...f.row,slotIdentif:1},{...f.row,cardStore:1},
    {...f.row,carrireIdentif:'12'}])assert.throws(()=>buildWeekendRestPlan([row],f.carriers,f.ledger));
});
test('exact independent target and identity verification is preserved',()=>{
  const f=fixture(),p=buildWeekendRestPlan([f.row],f.carriers,f.ledger),g=p.groups[0];
  verifyRows(g.rows,{total:1,rows:[{...f.row,carrireIdentif:'12'}]},g.target);
  assert.throws(()=>verifyRows(g.rows,{total:1,rows:[{...f.row,id:'changed',carrireIdentif:'12'}]},g.target));
});
test('explicit ID authority follows renamed carrier without rewriting provenance',()=>{
  const f=fixture();f.carriers[0].name='B12V-精-Te-120';
  assert.equal(buildWeekendRestPlan([f.row],f.carriers,f.ledger).eligibleCount,0);
  const p=buildWeekendRestPlan([f.row],f.carriers,f.ledger,{originalCarrierIdAuthoritative:true});
  assert.equal(p.eligibleCount,1);assert.equal(p.groups[0].target.identif,'12');
  assert.equal(p.groups[0].target.name,'B12V-精-Te-120');
  assert.equal(p.groups[0].rows[0].originalCarrier.name,'B12-精-Te-120');
  assert.equal(f.record.originalCarrier.name,'B12-精-Te-120');
  f.carriers[0].name='周末休息区TV';
  assert.equal(buildWeekendRestPlan([f.row],f.carriers,f.ledger,{originalCarrierIdAuthoritative:true}).eligibleCount,0);
});
