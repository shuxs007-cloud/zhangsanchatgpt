import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {identityKey,listingState,isExpired,selectCandidates,assertSame,decide,validateRecoveryAcceptance} from '../scripts/opensms_expired_card_probe_lib.mjs';
import {runProbe,collectPopulation,readPlatformDate,createProbeJournal} from '../scripts/opensms_expired_card_probe.mjs';
const zones=[{name:'过期下架卡T',identif:'10'},{name:'过期下架卡C',identif:'20'}];
const row=(i=1,online=true)=>({id:String(i),cardNo:'647000000'+i,cardStore:'1234567890123456789'+i+'F',slotIdentif:'101_'+i,carrireIdentif:'10',chatrPlanExpire:'2026-10-01',displayStatus:online?'已上架':'未上架',userAble:online});
function fixture({pending=false,failOffline=false,failRestore=false,initialRows,journal:providedJournal}={}){
  let rows=initialRows||[row(1),row(2,false)],time=0;
  const records=new Map(),evidenceRows=new Map(),calls=[];
  let journal=providedJournal||{read:async key=>records.get(key)||null,write:async(key,value)=>records.set(key,structuredClone(value))};
  const api=async(method,url,body)=>{
    calls.push({method,url,body});
    if(url==='/carrier/config/listNotDeleted')return {code:200,total:2,rows:zones.map(z=>({name:z.name,identif:z.identif}))};
    if(url==='/phone/config/list'){
      const selected=body.cardNo?rows.filter(r=>body.cardNo.split('\n').includes(r.cardNo)):rows.filter(r=>r.carrireIdentif===body.carrireIdentif);
      return {code:200,total:selected.length,rows:structuredClone(selected.slice((body.pageNum-1)*body.pageSize,body.pageNum*body.pageSize))};
    }
    if(url==='/phone/config/batchOffline'){
      for(const id of body.ids)assert.equal((await journal.read(identityKey(rows.find(r=>r.id===id)))).status,'pending_offline');
      if(failOffline)throw Error('transport outcome unknown');
      rows=rows.map(r=>body.ids.includes(r.id)?{...r,displayStatus:'未上架',userAble:false}:r);
      return {code:200};
    }
    if(url==='/phone/config/offlineRecover'){
      for(const id of body.phoneIds)assert.equal((await journal.read(identityKey(rows.find(r=>r.id===id)))).status,'pending_restore');
      if(failRestore)throw Error('submission unknown');
      if(!pending)rows=rows.map(r=>body.phoneIds.includes(r.id)?{...r,displayStatus:'已上架',userAble:true}:r);
      return {code:200,data:{acceptedCount:body.phoneIds.length,skippedCount:0}};
    }
    throw Error('unexpected URL');
  };
  const run=(dryRun=false,runId='current')=>runProbe({api,today:'2026-10-02',runId,journal,evidence:async(k,v)=>evidenceRows.set(k,structuredClone(v)),checkDate:async()=>{throw Error('date must not affect new scope');},clock:()=>time,wait:async ms=>{time+=ms;},maxWaitMs:10000,dryRun});
  return {run,api,journal,records,calls,evidenceRows,replaceJournal:value=>{journal=value;}};
}
test('yesterday included; today, future and invalid date excluded',()=>{
  assert.equal(isExpired('2026-10-01','2026-10-02'),true);
  assert.equal(isExpired('2026-09-30','2026-10-02'),true);
  for(const d of ['2026-10-02','2026-10-03','2026-02-30',null])assert.equal(isExpired(d,'2026-10-02'),false);
  assert.throws(()=>isExpired('2026-10-01','invalid'));
});
test('exact two carriers; displayed and boolean statuses must agree',()=>{
  assert.equal(listingState({...row(),userAble:false}),'ineligible_status');
  const selection=selectCandidates([row(),{...row(2),chatrPlanExpire:null},{...row(3),displayStatus:'卡槽异常'}],zones,'2026-10-02');
  assert.equal(selection.candidates.length,2);assert.equal(selection.deadlineFilterEnabled,false);assert.equal(selection.ineligibleStatusCount,1);
  assert.throws(()=>selectCandidates([{...row(),carrireIdentif:'30'}],zones,'2026-10-02'));
  assert.throws(()=>selectCandidates([row(),row()],zones,'2026-10-02'));
  const invalid=selectCandidates([{...row(),cardNo:'123456789'}],zones,'2026-10-02');
  assert.equal(invalid.invalidIdentityCount,1);assert.equal(invalid.candidates.length,0);
  assert.throws(()=>identityKey({...row(),cardStore:8912230200000000000}),/IDENTITY_INVALID/);
});
test('all dates including absent, invalid and future are eligible in both exact zones',()=>{
  const rows=[null,'invalid','2026-10-02','2027-10-03'].map((d,i)=>({...row(i+1),carrireIdentif:i%2?'20':'10',chatrPlanExpire:d}));
  const selection=selectCandidates(rows,zones,null);
  assert.equal(selection.candidates.length,4);
  assert.equal(selection.deadlineFilterEnabled,false);
  const item=selection.candidates[0];
  assert.equal(assertSame(item,{...rows[0],chatrPlanExpire:'2099-12-31'},'invalid'),'online');
  assert.throws(()=>assertSame(item,{...rows[0],carrireIdentif:'30'}),/SCOPE_CHANGED/);
});
test('missing population identities never enter restore intents or the one-shot ledger',async()=>{
  const invalid=[2,3,4].map(i=>row(i,false));
  delete invalid[0].cardNo;delete invalid[1].cardStore;delete invalid[2].slotIdentif;
  const f=fixture({initialRows:[row(1,false),...invalid]}),result=await f.run();
  assert.equal(result.invalidIdentityCount,3);assert.equal(result.candidateCount,1);
  assert.equal(result.acceptedCount,1);assert.equal(f.records.size,1);
  assert.deepEqual(f.calls.filter(c=>c.url==='/phone/config/offlineRecover').map(c=>c.body.phoneIds),[['1']]);
});
test('platform date waits for layout only, never falls back to local date',async()=>{
  let calls=0,time=0;
  const client={evaluate:async()=>++calls===1?[]:['2026-10-02']};
  assert.equal(await readPlatformDate(client,{clock:()=>time,wait:async ms=>{time+=ms;}}),'2026-10-02');
  await assert.rejects(readPlatformDate({evaluate:async()=>['2026-10-01','2026-10-02']}),/AMBIGUOUS/);
  time=0;await assert.rejects(readPlatformDate({evaluate:async()=>[]},{clock:()=>time,wait:async ms=>{time+=ms;}}),/UNCONFIRMED/);
});
test('one-shot offline ledger; online repeat allowed; uncertainty blocks replay',()=>{
  const offline={initialState:'offline',key:'a'},online={...offline,initialState:'online'};
  assert.equal(decide(offline,null),'restore_once');
  assert.equal(decide(offline,{key:'a',attempts:1,status:'online_verified'}),'skip_offline_already_tested');
  assert.equal(decide(online,{key:'a',attempts:1,status:'online_verified'}),'offline_then_restore');
  assert.equal(decide(offline,{key:'a',attempts:0,status:'pending_offline'}),'blocked_uncertain_offline');
  assert.equal(decide(online,{key:'a',attempts:1,status:'pending_restore'}),'read_only_pending');
  assert.equal(decide(online,{key:'a',attempts:1,status:'online_verified',runId:'A'},'A'),'skip_same_run_already_tested');
  assert.equal(decide(online,{key:'a',attempts:1,status:'online_verified',runId:'A'},'B'),'offline_then_restore');
  assert.equal(decide(online,{key:'a',attempts:1,status:'awaiting_async_readback',runId:'A'},'B'),'offline_then_restore');
  assert.notEqual(identityKey(row()),identityKey({...row(),slotIdentif:'different'}));
});
test('same run never repeats online tests; next A may test independently confirmed online cards',async()=>{
  const f=fixture();await f.run();
  const second=await f.run();assert.equal(second.acceptedCount,0);assert.equal(second.alreadyTestedSkippedCount,2);
  const next=await f.run(false,'next-A');assert.equal(next.offlineSubmittedCount,2);assert.equal(next.acceptedCount,2);
});
test('normal flow independently verifies offline before restore; no carrier writes',async()=>{
  const f=fixture(),r=await f.run();
  assert.equal(r.status,'passed');assert.equal(r.offlineSubmittedCount,1);assert.equal(r.acceptedCount,2);assert.equal(r.onlineVerifiedCount,2);
  assert.deepEqual(f.calls.filter(c=>/batchOffline|offlineRecover/.test(c.url)).map(c=>c.url),['/phone/config/batchOffline','/phone/config/offlineRecover']);
  assert.equal(f.evidenceRows.get('offline_readback_0').rows[0].displayStatus,'未上架');
});
test('async offline is pending, not dead; future runs read only without replay',async()=>{
  const f=fixture({pending:true}),first=await f.run();
  assert.equal(first.status,'pending');assert.equal(first.onlineVerifiedCount,0);assert.equal(first.pendingCount,2);
  const writeCount=f.calls.filter(c=>/batchOffline|offlineRecover/.test(c.url)).length;
  const next=await f.run();assert.equal(next.recoverySubmittedCount,0);assert.equal(next.pendingCount,2);
  assert.equal(f.calls.filter(c=>/batchOffline|offlineRecover/.test(c.url)).length,writeCount);
});
test('unknown write never replays; intent survives exceptions',async()=>{
  for(const options of [{failOffline:true},{failRestore:true}]){
    const f=fixture(options);await assert.rejects(f.run());
    const before=f.calls.filter(c=>/batchOffline|offlineRecover/.test(c.url)).length;
    if(options.failRestore){const next=await f.run();assert.equal(next.pendingCount,2);}
    else {const next=await f.run();assert.equal(next.blockedCount,1);} // No old offline write repeats.
    assert.equal(f.calls.filter(c=>c.url==='/phone/config/batchOffline').length,1);
    assert.ok(f.records.size>0);assert.ok(before>0);
  }
});
test('dry run does not touch journal or any mutation endpoint',async()=>{
  const f=fixture(),r=await f.run(true);assert.equal(r.status,'dry_run');assert.equal(f.records.size,0);
  assert.equal(f.calls.some(c=>/batchOffline|offlineRecover/.test(c.url)),false);
});
test('acceptance counts and population coverage are mandatory',async()=>{
  assert.equal(validateRecoveryAcceptance({code:200,data:{acceptedCount:1,skippedCount:0}},1).completed,false);
  assert.throws(()=>validateRecoveryAcceptance({code:200,data:{acceptedCount:0,skippedCount:1}},1));
  await assert.rejects(collectPopulation(async()=>({code:200,total:1,rows:[]}),zones));
});
test('durable local history survives a recreated worker and prevents another offline test',async t=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'expired-probe-ledger-'));
  t.after(()=>fs.rm(directory,{recursive:true,force:true}));
  const f=fixture({pending:true,journal:createProbeJournal(directory)});
  const first=await f.run(false,'A-1');assert.equal(first.acceptedCount,2);
  const files=await fs.readdir(directory);assert.equal(files.length,2);
  for(const file of files)assert.equal((await fs.stat(path.join(directory,file))).mode&0o777,0o600);
  f.replaceJournal(createProbeJournal(directory));
  const before=f.calls.filter(c=>/batchOffline|offlineRecover/.test(c.url)).length;
  const next=await f.run(false,'A-2');assert.equal(next.acceptedCount,0);assert.equal(next.pendingCount,2);
  assert.equal(f.calls.filter(c=>/batchOffline|offlineRecover/.test(c.url)).length,before);
  assert.deepEqual(await fs.readdir(directory),files);
});
test('unknown restoration remains consumed on disk after a worker restart',async t=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'expired-probe-intent-'));
  t.after(()=>fs.rm(directory,{recursive:true,force:true}));
  const f=fixture({failRestore:true,journal:createProbeJournal(directory)});
  await assert.rejects(f.run(false,'A-1'));
  f.replaceJournal(createProbeJournal(directory));
  const next=await f.run(false,'A-2');assert.equal(next.acceptedCount,0);assert.equal(next.pendingCount,2);
  assert.equal(f.calls.filter(c=>c.url==='/phone/config/offlineRecover').length,1);
});
test('local history rejects corrupt identities instead of resetting them to untested',async t=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'expired-probe-corrupt-'));
  t.after(()=>fs.rm(directory,{recursive:true,force:true}));
  const journal=createProbeJournal(directory),key=identityKey(row());
  assert.equal(await journal.read(key),null);
  assert.throws(()=>createProbeJournal('relative'),/LEDGER_INVALID/);
  assert.throws(()=>journal.write('../bad',{key:'../bad'}),/LEDGER_INVALID/);
  await fs.writeFile(path.join(directory,key+'.json'),JSON.stringify({key:'wrong',attempts:1,status:'online_verified'}));
  await assert.rejects(journal.read(key),/LEDGER_INVALID/);
  await fs.writeFile(path.join(directory,key+'.json'),'{invalid');
  await assert.rejects(journal.read(key));
});
test('multi-selection uses exact backend IDs, batches of at most 500, and never includes an already-tested offline card',async()=>{
  const initialRows=Array.from({length:503},(_,i)=>({...row(),id:String(i+1),cardNo:String(6471000000+i),
    cardStore:'891223020000'+String(i).padStart(8,'0')+'F',slotIdentif:'101_'+i,
    displayStatus:i%2?'未上架':'已上架',userAble:i%2===0}));
  const f=fixture({initialRows});
  const skipped=initialRows[1],key=identityKey(skipped);
  f.records.set(key,{key,runId:'old',attempts:1,status:'online_verified'});
  const result=await f.run(false,'current');
  assert.equal(result.alreadyTestedSkippedCount,1);assert.equal(result.acceptedCount,502);
  const offline=f.calls.filter(c=>c.url==='/phone/config/batchOffline');
  const restore=f.calls.filter(c=>c.url==='/phone/config/offlineRecover');
  assert.equal(offline.length,2);assert.equal(restore.length,2);
  assert.equal(offline.flatMap(c=>c.body.ids).length,252);
  assert.deepEqual(new Set(offline.flatMap(c=>c.body.ids)),new Set(initialRows.filter(r=>r.userAble).map(r=>r.id)));
  assert.deepEqual(new Set(restore.flatMap(c=>c.body.phoneIds)),new Set(initialRows.filter(r=>r.id!==skipped.id).map(r=>r.id)));
  assert.ok(restore.every(c=>c.body.phoneIds.length<=500));
  assert.ok(f.calls.filter(c=>c.url==='/phone/config/list'&&c.body.cardNo).every(c=>c.body.showAble===null));
});
