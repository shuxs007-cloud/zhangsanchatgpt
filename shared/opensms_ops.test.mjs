import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {split,carrier,exactUser,checkAccount,checkSets,validateBatch,exactCreatedBindings,assertCancelTime,validCheck,beijing} from './opensms_ops_core.mjs';

test('separators and deduplication',()=>assert.deepEqual(split('A1、B1，B11;A1'),['A1','B1','B11']));
test('B12 never selects B12V',()=>assert.equal(carrier([{name:'B12V-X',identif:'1'},{name:'B12-X',identif:'2'}],'b12').identif,'2'));
test('A1 never selects A11',()=>assert.throws(()=>carrier([{name:'A11-X',identif:'1'}],'A1')));
test('carrier ambiguity fails',()=>assert.throws(()=>carrier([{name:'B1-X',identif:'1'},{name:'B1-Y',identif:'2'}],'B1')));
test('account fuzzy matching forbidden',()=>assert.throws(()=>exactUser([{proxyUserName:'gali'}],'gail')));
test('case insensitive exact account',()=>assert.equal(exactUser([{proxyUserName:'syt'}],'SYT').proxyUserName,'syt'));
test('duplicate account fails',()=>assert.throws(()=>exactUser([{proxyUserName:'syt'},{proxyUserName:'SYT'}],'syt')));
const old=[{id:'100',carrireIdentif:'1'}];
test('additive retains old ID',()=>assert.ok(validCheck(checkAccount(old,[...old,{id:'200',carrireIdentif:'2'}],['2']))));
test('replacing old ID fails additive verification',()=>assert.equal(checkAccount(old,[{id:'101',carrireIdentif:'1'}],['1']).lost.length,1));
test('keep-only catches extras and missing',()=>{const c=checkAccount([],old,['2'],true);assert.equal(c.extra.length,1);assert.equal(c.missing.length,1);});
test('keep-only catches duplicate relationships',()=>assert.equal(checkAccount([],[...old,...old],['1'],true).duplicates,1));
test('source-target coverage deduplicates',()=>assert.deepEqual(checkSets(['1','1','2'],[{proxyUserId:'1'}]),['2']));
test('batch preserves large IDs as strings',()=>assert.equal(validateBatch({bindings:[{id:'2103218924563861504',proxyUserId:'74',carrireIdentif:'2083885777233776641'}]}).bindings.length,1));
test('unsafe numeric batch ID rejected',()=>assert.throws(()=>validateBatch({bindings:[{id:2103218924563861504,proxyUserId:'74',carrireIdentif:'2'}]})));
test('duplicate batch rejected',()=>assert.throws(()=>validateBatch({bindings:[{id:'1',proxyUserId:'2',carrireIdentif:'3'},{id:'1',proxyUserId:'2',carrireIdentif:'3'}]})));
test('A3 rollback records only declared new exact B12 IDs',()=>{
  const old={id:'100',proxyUserId:'1',carrireIdentif:'12'};
  const added={id:'200',proxyUserId:'2',proxyUserName:'A3-account',carrireIdentif:'12'};
  const result=exactCreatedBindings([old],[old,added],['1','2'],[{createdBindings:[added]}],'12');
  assert.deepEqual(result.bindings,[{id:'200',proxyUserId:'2',proxyUserName:'A3-account',carrireIdentif:'12',carrierName:''}]);
  assert.deepEqual(result.unexplainedNewTargetIds,[]);
});
test('A3 rollback rejects old, unrelated, and B12V IDs',()=>{
  const old={id:'100',proxyUserId:'1',carrireIdentif:'12'};
  const other={id:'200',proxyUserId:'9',carrireIdentif:'12'};
  const variant={id:'300',proxyUserId:'2',carrireIdentif:'12v'};
  assert.throws(()=>exactCreatedBindings([old],[old],['1'],[{createdBindings:[old]}],'12'));
  assert.throws(()=>exactCreatedBindings([],[other],['2'],[{createdBindings:[other]}],'12'));
  assert.throws(()=>exactCreatedBindings([],[variant],['2'],[{createdBindings:[variant]}],'12'));
});
test('A3 rollback exposes unrecorded new IDs for manual review',()=>{
  const added={id:'200',proxyUserId:'2',carrireIdentif:'12'};
  assert.deepEqual(exactCreatedBindings([],[added],['2'],[],'12').unexplainedNewTargetIds,['200']);
});
test('cancellation cannot run before 13:00 or wrong day',()=>{assert.throws(()=>assertCancelTime('20260926','2026-09-26 12:59:59'));assert.throws(()=>assertCancelTime('20260925','2026-09-26 13:00:00'));assert.doesNotThrow(()=>assertCancelTime('20260926','2026-09-26 13:00:00'));});
test('Beijing timezone deterministic',()=>assert.equal(beijing(new Date('2026-09-25T17:00:00Z')),'2026-09-26 01:00:00'));
test('current manual and automatic presets',()=>{const r=JSON.parse(fs.readFileSync(new URL('./opensms_rules.json',import.meta.url)));assert.deepEqual(r.presets['a3-only'].targets,['A3','B1','B11','B12','B3']);assert.equal(r.automaticB12.thresholdStrictlyBelow,0.1);assert.equal(r.automaticB12.writeEnd,'10:00:00');assert.equal(r.automaticB12.cancelTime,'13:00:00');});
test('old C2 auto command is disabled and A3 auto command requires the current checkpoint',()=>{
  const script=fileURLToPath(new URL('./opensms_ops.mjs',import.meta.url));
  const old=spawnSync(process.execPath,[script,'auto-c2-b12','20261005_0930','--apply'],{encoding:'utf8'});
  assert.notEqual(old.status,0);
  assert.match(old.stderr,/OpenSMS commands/);
  const stale=spawnSync(process.execPath,[script,'auto-a3-b12','20200101_0100','--apply'],{encoding:'utf8'});
  assert.notEqual(stale.status,0);
  assert.match(stale.stderr,/current monitoring checkpoint/);
});

// A fake local backend proves command routing without contacting a browser.
test('CLI preview, mutation, independent readback and duplicate request protection',()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'opensms-test-'));
  try{
    for(const name of ['opensms_ops.mjs','opensms_ops_core.mjs','opensms_rules.json','opensms_binding_protocol.mjs','opensms_site_lock.mjs'])fs.copyFileSync(new URL(`./${name}`,import.meta.url),path.join(dir,name));
    fs.writeFileSync(path.join(dir,'opensms_raw_cdp_binding_tmp.mjs'),`import fs from 'node:fs';
const [mode,a,b]=process.argv.slice(2);const file='state.json';let state=fs.existsSync(file)?JSON.parse(fs.readFileSync(file)):false;let r;
const cs=[{name:'B1-X',identif:'1'},{name:'B12-X',identif:'2'}];
if(mode==='query-carriers')r={ok:true,matches:cs};
else if(mode==='query-source-identif')r={ok:true,count:a==='1'||state?1:0,bindings:a==='1'?[{id:'11',proxyUserId:'5',carrireIdentif:'1'}]:state?[{id:'22',proxyUserId:'5',carrireIdentif:'2'}]:[]};
else if(mode==='add-sources-union'){if(a!=='B1-X'||b!=='B12-X')throw Error('wrong route');fs.writeFileSync(file,'true');r={ok:true,addCount:1,deleteCount:0};}
else throw Error(mode);console.log(JSON.stringify(r));`);
    const run=(...args)=>spawnSync(process.execPath,['opensms_ops.mjs',...args],{cwd:dir,encoding:'utf8',env:{...process.env,HOME:dir}});
    let p=run('preset','b1-b12','--request-id','preview');assert.equal(p.status,0,p.stderr);assert.equal(fs.existsSync(path.join(dir,'state.json')),false);
    p=run('add-groups','B1','B12','--apply','--request-id','live');assert.equal(p.status,0,p.stderr);const r=JSON.parse(fs.readFileSync(path.join(dir,'opensms_runs/live/result.json')));assert.equal(r.status,'verified');assert.equal(r.verifiedCount,1);assert.ok(r.calls.some(c=>c.write));assert.ok(r.calls.filter(c=>!c.write).length>=5);
    p=run('add-groups','B1','B12','--apply','--request-id','live');assert.notEqual(p.status,0);assert.match(p.stderr,/already recorded/);
    fs.writeFileSync(path.join(dir,'.opensms_ops.lock'),'{}');p=run('query-group','B1');assert.notEqual(p.status,0);assert.match(p.stderr,/lock exists/);
  }finally{fs.rmSync(dir,{recursive:true,force:true});}
});
