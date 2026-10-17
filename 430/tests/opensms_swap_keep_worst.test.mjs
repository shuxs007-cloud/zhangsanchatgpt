import test from 'node:test';
import assert from 'node:assert/strict';
import {rankSwapPopulation} from '../scripts/opensms_swap_keep_worst_engine.mjs';
import {assertFrozenPopulation} from '../scripts/opensms_manual_swap_t_restore.mjs';
const zones=[{name:'换号区T',identif:'T'},{name:'换号区C',identif:'C'}];
const row=(i,zone='T')=>({id:String(i),cardNo:String(4165550000+i),cardStore:String(8901000000000000000n+BigInt(i))+'F',
  slotIdentif:`101_${i}`,opr:zone==='T'?'Koodo':'Chatr',carrireIdentif:zone,displayStatus:'未上架',userAble:false});
const metric=(i,rate,send,zone='T')=>({phone:String(4165550000+i),iccid:String(8901000000000000000n+BigInt(i)),
  slot:`101_${i}`,carrier:zone==='T'?'换号区T':'换号区C',replyRate:rate,sendWindows:send,sourceRow:i+1});
const data=rows=>({metric:'4日回复率',tieBreak:'send-desc-phone-asc',runId:'source',asOf:'fixed',rows});
test('both zones and offline numbers rank together, lowest rate first and exposure tie-break',()=>{
  const r=rankSwapPopulation([row(1),row(2,'C'),row(3),row(4)],data([metric(1,.01,50),metric(2,.0,40,'C'),metric(3,0,100),metric(4,.02,200)]),zones,2);
  assert.deepEqual(r.kept.map(x=>x.phone),[String(4165550003),String(4165550002)]);
  assert.equal(r.restore.length,2);assert.equal(r.unknown.length,0);
});
test('real zero is valid; unknown, invalid and zero-denominator metrics are excluded',()=>{
  const r=rankSwapPopulation([row(1),row(2),row(3),row(4)],data([metric(1,0,100),metric(2,null,10),metric(3,.01,0)]),zones,1);
  assert.equal(r.kept.length,1);assert.equal(r.restore.length,0);assert.equal(r.unknown.length,3);
});
test('same-card empty live slot allowed, wrong card or populated slot excluded',()=>{
  const r=rankSwapPopulation([{...row(1),slotIdentif:null},{...row(2),slotIdentif:'wrong'},{...row(3),cardStore:'8901000000000000999'}],
    data([metric(1,0,20),metric(2,0,20),metric(3,0,20)]),zones,1);
  assert.equal(r.kept.length,1);assert.equal(r.unknown.length,2);
});
test('duplicate and unrelated identities stop instead of silent deletion',()=>{
  assert.throws(()=>rankSwapPopulation([row(1),row(1)],data([metric(1,0,100)]),zones),/DUPLICATE/);
  assert.throws(()=>rankSwapPopulation([row(1)],data([metric(1,0,100),metric(1,0,100)]),zones),/DUPLICATE/);
  assert.throws(()=>rankSwapPopulation([row(1,'OTHER')],data([]),zones),/OUTSIDE/);
});
test('keep is exactly 200, stable on complete ties and never included in restore',()=>{
  const pop=Array.from({length:205},(_,i)=>row(i+1));const metrics=data(pop.map((_,i)=>metric(i+1,0,50)));
  const r=rankSwapPopulation(pop.reverse(),metrics,zones);assert.equal(r.kept.length,200);assert.equal(r.restore.length,5);
  assert.equal(new Set([...r.kept,...r.restore].map(x=>x.id)).size,205);
  assert.equal(r.kept[0].phone,'4165550001');
});
test('entire population must remain unchanged from preview, not just selected phones',()=>{
  const pop=[row(1),row(2)];assertFrozenPopulation({population:pop},[row(2),row(1)]);
  assert.throws(()=>assertFrozenPopulation({population:pop},[row(1),row(3)]),/FROZEN_POPULATION_CHANGED/);
  assert.throws(()=>assertFrozenPopulation({population:pop},[row(1),{...row(2),opr:'other'}]),/FROZEN_POPULATION_CHANGED/);
});
