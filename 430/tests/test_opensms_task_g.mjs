import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import {runTaskG,group,addDay,recordKey,validateLedger,digest,normalizeIccid,normalizeSlot,entryOprReason} from '../scripts/opensms_task_g_engine.mjs';
import {atomicJson,decodePhoneRows,main as guardedMain} from '../scripts/opensms_execute_task_g.mjs';

const clone=x=>JSON.parse(JSON.stringify(x));
export const carriers=['B1','B11','B12','B1V','B11V','B12V'].map((g,n)=>({name:g+'-test',identif:String(n+1)}))
  .concat([{name:'周末休息区T',identif:'t'},{name:'周末休息区TV',identif:'tv'}]);
export function specification(rows=[],today='2026-10-05') {
  const date=new Date(today+'T00:00:00Z');date.setUTCDate(date.getUTCDate()-1);const previousDate=date.toISOString().slice(0,10);
  return {version:1,runId:'20261005T080000+0800',scope:'previous_day_total_send_weekend_rest_v1',serverDate:today,previousDate,
    metricColumn:previousDate.slice(5)+'总发送量',settings:{previousDayTotalSendThresholdExclusive:160,excludedEntryOprTokens:['Kini']},
    unknownMetricRowCount:rows.filter(r=>r.previousDayTotalSend===null).length,
    sourceKiniExcludedCount:rows.filter(r=>entryOprReason(r.opr)==='kini').length,
    sourceUnknownOprCount:rows.filter(r=>entryOprReason(r.opr)==='unknown_opr').length,
    entryCandidateCount:rows.filter(r=>group(r.sourceCarrier) && entryOprReason(r.opr)===null
      && Number.isSafeInteger(r.previousDayTotalSend) && r.previousDayTotalSend>160).length,rows};
}
export function source(n=1,name='B1-test',total=161) {
  return {phone:String(7000000000+n),sourceRow:n+1,sourceCarrier:name,slot:'101_'+n,iccid:'891234567890123456'+String(n).padStart(3,'0'),opr:'Koodo',previousDayTotalSend:total};
}
export async function fixture(spec=specification([source()]),options={}) {
  let ledger=clone(options.ledger||{version:1,records:{}}),writes=[],saved=[],queries=0;
  const live=new Map((options.live||spec.rows.map(r=>({phone:r.phone,id:'id'+r.phone,slot:normalizeSlot(r.slot),iccid:normalizeIccid(r.iccid),opr:r.opr,carrierIdentif:carriers.find(c=>c.name===r.sourceCarrier)?.identif||'other'}))).map(r=>[r.phone,clone(r)]));
  const receipt={},journal={read:async()=>clone(ledger),save:async l=>{ledger=clone(l);saved.push(clone(l));},evidence:async()=>{}};
  const query=async phones=>{queries++;options.beforeQuery?.({phones,queries,live});
    for(const change of options.queryOprChanges||[])if(change.queryNumber===queries && live.has(change.phone))live.get(change.phone).opr=change.opr;
    for(const change of options.querySlotChanges||[])if(change.queryNumber===queries && live.has(change.phone))live.get(change.phone).slot=change.slot;
    const rows=phones.filter(p=>live.has(p)).map(p=>clone(live.get(p)));
    if(options.useQueryAdapter)return decodePhoneRows({total:rows.length,rows:rows.map(r=>({id:r.id,cardNo:'1'+r.phone,
      ...(r.slot===undefined?{}:{slotIdentif:r.slot}),cardStore:r.iccid,opr:r.opr,carrireIdentif:r.carrierIdentif}))},phones).canonical;
    return new Map(rows.map(r=>[r.phone,r]));};
  const write=async (ids,target)=>{
    assert(ids.length<=500);assert(Object.values(ledger.records).some(r=>['entering','restoring'].includes(r.status)));
    writes.push({ids,target});options.beforeWrite?.({ids,target,live,ledger});
    if(options.writeError)throw Error('mock timeout');
    for(const row of live.values())if(ids.includes(row.id) && !options.noApply)row.carrierIdentif=target;
  };
  try{await runTaskG({spec,query,write,readDate:async()=>options.readDate?options.readDate():spec.serverDate,
    carriers:options.carriers||carriers,journal,receipt});}
  catch(error){if(!options.catchError)throw error;receipt.error=error.message;}
  receipt.ledgerAfterSha256=digest(JSON.stringify(ledger,null,2)+'\n');
  return {receipt,ledger,writes,saved,live:[...live.values()],queries};
}

if(process.argv.includes('--fixture')) {
  const index=process.argv.indexOf('--fixture'),input=process.argv[index+1],options=process.argv[index+2]?JSON.parse(await fs.readFile(process.argv[index+2],'utf8')):{};
  const spec=JSON.parse(await fs.readFile(input,'utf8'));const output=await fixture(spec,options);
  output.receipt.inputSha256=digest(await fs.readFile(input));
  console.log(JSON.stringify(output.receipt));
} else {
  test('strict >160, unknown and zero are excluded; all six sources map correctly',async()=>{
    const rows=carriers.slice(0,6).map((c,n)=>source(n+1,c.name)).concat([source(7,'B1-test',160),source(8,'B1-test',0),source(9,'B1-test',null),source(10,'B1-test',150),source(11,'B1-test',159)]);
    const r=await fixture(specification(rows));assert.equal(r.receipt.enteredCount,6);assert.equal(r.writes.length,2);
    assert.equal(r.writes[0].target,'t');assert.equal(r.writes[1].target,'tv');assert.equal(r.receipt.restingCount,6);
    assert.equal(r.receipt.byOriginalCarrier.length,6);assert(Object.values(r.ledger.records).every(r=>r.restoreDate==='2026-10-06'));
  });
  test('configurable common threshold uses strict boundary',async()=>{const s=specification([source(1,'B1-test',200),source(2,'B1-test',201)]);s.settings.previousDayTotalSendThresholdExclusive=200;s.entryCandidateCount=1;assert.equal((await fixture(s)).receipt.enteredCount,1);});
  test('source Kini is excluded across all six groups, even with live non-Kini OPR',async()=>{
    const rows=carriers.slice(0,6).map((c,n)=>({...source(n+1,c.name,1000),opr:n%2?'kiNI':'Canada Kini LTE'}));
    const r=await fixture(specification(rows),{beforeQuery:({live})=>{for(const row of live.values())row.opr='Koodo';}});
    assert.equal(r.writes.length,0);assert.equal(r.queries,0);assert.equal(r.receipt.sourceKiniExcludedCount,6);
    assert.equal(r.receipt.enteredCount,0);
  });
  test('OPR uses a case-insensitive independent Kini token, not a substring',()=>{
    for(const opr of ['Kini',' kini ','KINI-LTE','Koodo/Kini','(Kini)','加拿大Kini'])assert.equal(entryOprReason(opr),'kini');
    for(const opr of ['Koodo','Telus1','NotKini','Kinix','Kini_legacy'])assert.equal(entryOprReason(opr),null);
    for(const opr of [null,undefined,0,123,true,'','  '])assert.equal(entryOprReason(opr),'unknown_opr');
  });
  test('missing fixed Kini exclusion contract blocks before ledger/query access',async()=>{
    const s=specification([source()]);delete s.settings.excludedEntryOprTokens;
    await assert.rejects(fixture(s),/KINI_EXCLUSION_GATE_MISSING/);
  });
  test('blank or nontext source OPR is never treated as non-Kini',async()=>{
    const rows=[null,'',0].map((opr,n)=>({...source(n+1),opr}));
    const r=await fixture(specification(rows));assert.equal(r.writes.length,0);assert.equal(r.queries,0);
    assert.equal(r.receipt.sourceUnknownOprCount,3);
  });
  for(const opr of ['Kini',null])for(const phase of [1,2])test('live OPR '+opr+' excluded at query '+phase,async()=>{
    const row=source(),r=await fixture(specification([row]),{beforeQuery:({live,queries})=>{if(queries===phase)live.get(row.phone).opr=opr;}});
    assert.equal(r.writes.length,0);assert.equal(Object.keys(r.ledger.records).length,0);
    assert.equal(r.receipt[opr===null?'liveUnknownOprCount':'liveKiniExcludedCount'],1);
    assert.equal(r.receipt.skips[0].reason,(opr===null?'unknown_opr':'kini')+(phase===1?'_at_live_check':'_before_enter'));
  });
  test('new source exclusion does not block due old Kini ledger restoration',async()=>{
    const row=source(),oldSpec=specification([source(1,'B1-test',151)]);oldSpec.settings.previousDayTotalSendThresholdExclusive=150;oldSpec.entryCandidateCount=1;
    const a=await fixture(oldSpec);a.live[0].opr='Kini';row.opr='Kini';
    const b=await fixture(specification([row],'2026-10-06'),{ledger:a.ledger,live:a.live});
    assert.equal(b.receipt.restoredCount,1);assert.equal(b.receipt.enteredCount,0);assert.equal(b.writes[0].target,'1');
    assert.equal(b.receipt.sourceKiniExcludedCount,1);
  });
  test('group parsing does not confuse B1 with B11/B12/V or unrelated zones',()=>{for(const [n,g] of [['B1-test','B1'],['b11v-test','B11V'],['B12V-test','B12V'],['B13-test',undefined],['B1evil',undefined],['A1-test',undefined],['周末休息区TV',undefined]])assert.equal(group(n),g);});
  test('calendar year/month/leap rollover',()=>{assert.equal(addDay('2026-12-31'),'2027-01-01');assert.equal(addDay('2028-02-28'),'2028-02-29');assert.throws(()=>addDay('2026-02-29'));});
  test('outside source never moved',async()=>{assert.equal((await fixture(specification([source(1,'A1-test')]))).writes.length,0);});
  test('same-day rerun does not re-enter',async()=>{const s=specification([source()]),a=await fixture(s),b=await fixture(s,{ledger:a.ledger,live:a.live});assert.equal(b.writes.length,0);assert.equal(b.receipt.alreadyHandledCount,1);});
  test('next natural day restores exact original carrier, not fixed B1',async()=>{const row=source(1,'B12V-test'),a=await fixture(specification([row]));const b=await fixture(specification([row],'2026-10-06'),{ledger:a.ledger,live:a.live});assert.equal(b.receipt.restoredCount,1);assert.equal(b.receipt.enteredCount,0);assert.equal(b.writes[0].target,'6');assert.equal(Object.values(b.ledger.records)[0].status,'restored');});
  test('restored day remains protected on another run',async()=>{const row=source(),a=await fixture(specification([row])),s=specification([row],'2026-10-06');const b=await fixture(s,{ledger:a.ledger,live:a.live}),c=await fixture(s,{ledger:b.ledger,live:b.live});assert.equal(c.writes.length,0);});
  test('missed day restores overdue record even absent from current source',async()=>{const a=await fixture();const b=await fixture(specification([],'2026-10-08'),{ledger:a.ledger,live:a.live});assert.equal(b.receipt.restoredCount,1);});
  test('manually placed weekend number without ledger never restored',async()=>{const r=await fixture(specification([]),{live:[{phone:'7000000001',id:'id1',slot:'101_1',iccid:'',carrierIdentif:'t'}]});assert.equal(r.writes.length,0);});
  for(const field of ['carrierIdentif','id','iccid','slot'])test('changed '+field+' detaches without forced restoration',async()=>{const a=await fixture();a.live[0][field]='changed';const b=await fixture(specification([],'2026-10-06'),{ledger:a.ledger,live:a.live});assert.equal(b.writes.length,0);assert.equal(b.receipt.detachedCount,1);});
  test('other synchronous task changed carrier: skip fresh eligibility',async()=>{const row=source();const r=await fixture(specification([row]),{beforeQuery:({live})=>live.get(row.phone).carrierIdentif='other'});assert.equal(r.writes.length,0);assert.equal(r.receipt.sourceChangedCount,1);});
  test('post-business original B12 preserved',async()=>{const row=source(1,'B12-test'),r=await fixture(specification([row]));assert.equal(Object.values(r.ledger.records)[0].originalCarrier.name,'B12-test');});
  test('source change immediately before PUT is excluded',async()=>{const row=source(),r=await fixture(specification([row]),{beforeQuery:({live,queries})=>{if(queries===2)live.get(row.phone).carrierIdentif='other';}});assert.equal(r.writes.length,0);});
  test('platform midnight stops all writes',async()=>{const r=await fixture(specification([source()]),{readDate:()=> '2026-10-06',catchError:true});assert.equal(r.writes.length,0);assert.match(r.receipt.error,/DATE_CHANGED/);});
  test('timeout persists intent; unchanged source is never replayed',async()=>{const s=specification([source()]),a=await fixture(s,{writeError:true,catchError:true});assert.equal(a.writes.length,1);assert.equal(Object.values(a.ledger.records)[0].status,'entering');const b=await fixture(s,{ledger:a.ledger,live:a.live,catchError:true});assert.equal(b.writes.length,0);assert.match(b.receipt.error,/UNRESOLVED_INTENT/);});
  test('accepted PUT with no independent target readback remains pending',async()=>{const r=await fixture(specification([source()]),{noApply:true,catchError:true});assert.match(r.receipt.error,/READBACK_UNCONFIRMED/);assert.equal(Object.values(r.ledger.records)[0].status,'entering');assert.equal(r.receipt.enteredCount,0);});
  test('positive old-intent target readback resolves without another PUT',async()=>{const a=await fixture(),r=Object.values(a.ledger.records)[0];r.status='entering';const b=await fixture(specification([]),{ledger:a.ledger,live:a.live});assert.equal(b.writes.length,0);assert.equal(b.receipt.reconciliations.length,1);assert.equal(Object.values(b.ledger.records)[0].status,'resting');});
  test('restoration intent not replayed on unchanged weekend zone',async()=>{const a=await fixture();Object.values(a.ledger.records)[0].status='restoring';const b=await fixture(specification([],'2026-10-06'),{ledger:a.ledger,live:a.live,catchError:true});assert.match(b.receipt.error,/UNRESOLVED_INTENT/);assert.equal(b.writes.length,0);});
  test('reconciled restoration date is durable with the first restored status',async()=>{
    const row=source(),a=await fixture(specification([row]));Object.values(a.ledger.records)[0].status='restoring';a.live[0].carrierIdentif='1';
    const s=specification([row],'2026-10-06'),b=await fixture(s,{ledger:a.ledger,live:a.live});
    assert.equal(b.writes.length,0);assert.equal(b.receipt.reconciliations[0].action,'restore');
    assert(b.saved.every(l=>Object.values(l.records).every(r=>r.status!=='restored'||r.restoredDate==='2026-10-06')));
    assert.equal((await fixture(s,{ledger:b.ledger,live:b.live})).writes.length,0);
  });
  test('old enter intent confirmed and restored when due: final refresh uses original, not zone',async()=>{const a=await fixture();Object.values(a.ledger.records)[0].status='entering';const b=await fixture(specification([],'2026-10-06'),{ledger:a.ledger,live:a.live});assert.equal(b.receipt.restoredCount,1);assert.equal(b.receipt.refreshGroups.at(-1).target,'B1-test');});
  test('missing exact target or ambiguous config stops before writes',async()=>{await assert.rejects(fixture(specification([source()]),{carriers:carriers.filter(c=>c.identif!=='tv')}),/CARRIER_MISSING/);await assert.rejects(fixture(specification([source()]),{carriers:carriers.concat(carriers.at(-1))}),/AMBIGUOUS/);});
  test('damaged historical ledger is not reset',async()=>{await assert.rejects(fixture(specification([]),{ledger:{version:0,records:{}}}),/LEDGER_INVALID/);const a=await fixture();Object.values(a.ledger.records)[0].originalCarrier.name='A1-test';assert.throws(()=>validateLedger(a.ledger),/LEDGER_INVALID/);});
  test('more than 500 ids split into exact serial batches',async()=>{const rows=Array.from({length:1001},(_,n)=>source(n+1)),r=await fixture(specification(rows));assert.deepEqual(r.writes.map(w=>w.ids.length),[500,500,1]);assert.equal(r.receipt.enteredCount,1001);assert.equal(r.receipt.verifiedBatchCount,3);});
  test('identity normalization preserves ICCID precision and slot numeric equality',()=>{assert.equal(normalizeIccid('089123456789012345678F'),'089123456789012345678');assert.equal(normalizeSlot('101_01'),'101_1');});
  test('real query adapter preserves raw numeric backend ID and precise ICCID text',()=>{
    const d=decodePhoneRows({total:1,rows:[{id:17,cardNo:'17000000001',slotIdentif:'101_01',cardStore:'089123456789012345678F',opr:' Kini ',carrireIdentif:'1'}]},['7000000001']);
    assert.equal(d.rawIds.get('17'),17);assert.equal(d.canonical.get('7000000001').iccid,'089123456789012345678');
    assert.equal(d.canonical.get('7000000001').opr,'Kini');
  });
  test('real query adapter retains unknown OPR as null for a safe no-entry decision',()=>{
    for(const opr of [null,undefined,0,true,'  ']){
      const d=decodePhoneRows({total:1,rows:[{id:17,cardNo:'17000000001',slotIdentif:'101_01',cardStore:'',opr,carrireIdentif:'1'}]},['7000000001']);
      assert.equal(d.canonical.get('7000000001').opr,null);
    }
  });
  test('missing/blank slot is an observation, never a fabricated slot or a writable identity',()=>{
    for(const slot of [undefined,null,'','  ']){
      const row={id:17,cardNo:'17000000001',cardStore:'089123456789012345678F',carrireIdentif:'other'};
      if(slot!==undefined)row.slotIdentif=slot;
      const d=decodePhoneRows({total:1,rows:[row]},['7000000001']);
      assert.equal(d.canonical.get('7000000001').slot,'');
      assert.equal(d.canonical.get('7000000001').id,'17');
      assert.equal(d.canonical.get('7000000001').iccid,'089123456789012345678');
    }
  });
  test('three detached slot-less ledger rows do not block another due verified restoration',async()=>{
    const rows=Array.from({length:4},(_,n)=>source(n+1)),a=await fixture(specification(rows),{useQueryAdapter:true});
    for(const row of a.live.slice(0,3)){delete row.slot;row.carrierIdentif='other';}
    const b=await fixture(specification([],'2026-10-06'),{ledger:a.ledger,live:a.live,useQueryAdapter:true});
    assert.equal(b.receipt.detachedCount,3);assert.equal(b.receipt.restoredCount,1);assert.equal(b.receipt.restingCount,0);
    assert.equal(b.writes.length,1);assert.deepEqual(b.writes[0].ids,[a.live[3].id]);
    assert.equal(b.receipt.skips.filter(s=>s.reason==='ledger_identity_or_zone_changed').length,3);
    assert(b.receipt.skips.every(s=>s.observed.slot===''));
    assert.equal(b.receipt.independentReadbackPassed,true);
  });
  test('fresh candidate with missing live slot is skipped while another valid candidate enters',async()=>{
    const rows=[source(1),source(2)],r=await fixture(specification(rows),{useQueryAdapter:true,
      beforeQuery:({live})=>{delete live.get(rows[0].phone).slot;}});
    assert.equal(r.receipt.enteredCount,1);assert.equal(r.receipt.sourceChangedCount,1);
    assert.equal(r.receipt.skips[0].observed.slot,'');assert.deepEqual(r.writes[0].ids,['id'+rows[1].phone]);
  });
  test('slot disappears immediately before restoration: detach, never restore blindly',async()=>{
    const a=await fixture(specification([source()]),{useQueryAdapter:true});
    const b=await fixture(specification([],'2026-10-06'),{ledger:a.ledger,live:a.live,useQueryAdapter:true,
      querySlotChanges:[{queryNumber:2,phone:a.live[0].phone,slot:null}]});
    assert.equal(b.writes.length,0);assert.equal(b.receipt.detachedCount,1);
    assert.equal(b.receipt.skips[0].reason,'changed_before_restore');
  });
  test('slot-less unresolved intents remain blocked without another PUT',async()=>{
    for(const status of ['entering','restoring']){
      const a=await fixture(specification([source()]),{useQueryAdapter:true});
      Object.values(a.ledger.records)[0].status=status;delete a.live[0].slot;
      const b=await fixture(specification([],'2026-10-06'),{ledger:a.ledger,live:a.live,useQueryAdapter:true,catchError:true});
      assert.equal(b.writes.length,0);assert.match(b.receipt.error,/UNRESOLVED_INTENT/);
      assert.equal(Object.values(b.ledger.records)[0].status,status);
    }
  });
  test('missing slot after PUT is unconfirmed, not a successful restoration or a detached intent',async()=>{
    const a=await fixture(specification([source()]),{useQueryAdapter:true});
    const b=await fixture(specification([],'2026-10-06'),{ledger:a.ledger,live:a.live,useQueryAdapter:true,catchError:true,
      querySlotChanges:[{queryNumber:3,phone:a.live[0].phone,slot:''}]});
    assert.equal(b.writes.length,1);assert.equal(b.receipt.restoredCount,0);
    assert.match(b.receipt.error,/READBACK_UNCONFIRMED/);assert.equal(Object.values(b.ledger.records)[0].status,'restoring');
  });
  test('nontext slot and duplicate phone with missing slot still stop before writes',()=>{
    const row={id:17,cardNo:'17000000001',cardStore:'089123456789012345678',carrireIdentif:'1'};
    for(const slotIdentif of [0,101,true,{},[]])assert.throws(()=>decodePhoneRows({total:1,rows:[{...row,slotIdentif}]},['7000000001']),/IDENTITY/);
    assert.throws(()=>decodePhoneRows({total:2,rows:[row,{...row,id:18}]},['7000000001','7000000002']),/IDENTITY/);
  });
  test('query adapter rejects duplicate/extra/truncated/numeric-ICCID results',()=>{
    const row={id:17,cardNo:'17000000001',slotIdentif:'101_01',cardStore:'089123456789012345678',carrireIdentif:'1'};
    assert.throws(()=>decodePhoneRows({total:501,rows:[row]},['7000000001']),/INCOMPLETE/);
    assert.throws(()=>decodePhoneRows({total:1,rows:[{...row,cardStore:8912345678901234567}]},['7000000001']),/IDENTITY/);
    assert.throws(()=>decodePhoneRows({total:1,rows:[{...row,cardNo:'17000000002'}]},['7000000001']),/IDENTITY/);
    assert.throws(()=>decodePhoneRows({total:2,rows:[row,row]},['7000000001','7000000002']),/IDENTITY/);
  });
  test('durable JSON persists precise ledger and exclusive receipt cannot overwrite',async()=>{
    const directory=await fs.mkdtemp(path.join(os.tmpdir(),'task-g-test-')),file=path.join(directory,'ledger.json');
    try{const r=await fixture();await atomicJson(file,r.ledger,{exclusive:true});assert.deepEqual(JSON.parse(await fs.readFile(file,'utf8')),r.ledger);
      await assert.rejects(atomicJson(file,{}, {exclusive:true}),/EEXIST/);assert.deepEqual(JSON.parse(await fs.readFile(file,'utf8')),r.ledger);}
    finally{await fs.unlink(file);await fs.rmdir(directory);}
  });
  test('standalone invocation blocked before browser or ledger access',async()=>{await assert.rejects(guardedMain([]),/RUNNER_GATE_MISSING/);});
}
