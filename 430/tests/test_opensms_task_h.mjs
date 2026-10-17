import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {validateSettings,selectCarriers,makePayload,assertReadback,assertSameConfig,assertSameScope,executeTaskH} from '../scripts/opensms_task_h_engine.mjs';
import {parseTaskHArgs,runTaskH} from '../scripts/opensms_task_h.mjs';

const settings={version:1,executionMode:'explicit-current-user-only',dailyDispatchLimit:150,dailySendAmount:130};
const row=(name='A1-精普',id='123')=>({id,identif:id+'1',name,delData:false,
  sendLimitByDay:140,sendNewLimitByDay:120,sendScore:0.05,renewAble:true,
  userSendSizeByDay:225,userSendNewChatSizeByDay:125,checkeStartTime:8,
  dailyRemaining:400,dailyTotal:1500,params:{},updateTime:'old'});

function fakeWorkflow(rows=[row()]){
  const current=new Map(rows.map(r=>[r.id,structuredClone(r)])),events=[],entries=[];
  let writes=0,pending=null;
  const deps={rows,settings,
    readDetail:async id=>{events.push('GET');return structuredClone(current.get(id));},
    assertScope:async before=>{events.push('scope');assertSameScope(before,[...current.values()]);},
    savePlan:async plan=>{events.push('plan');assert.equal(plan.length,rows.length);},
    persistIntent:async(entry,payload)=>{events.push('intent');assert.equal(pending,null);pending={entry,payload};},
    submit:async payload=>{events.push('PUT');assert.ok(pending);writes++;current.set(payload.id,structuredClone(payload));return {code:200};},
    saveEntry:async e=>{events.push('receipt');entries.push(structuredClone(e));},
    resolveIntent:async e=>{events.push('resolve');assert.ok(e.independentVerified);pending=null;}};
  return {deps,current,events,entries,get writes(){return writes;},get pending(){return pending;}};
}

test('H includes all numeric A/B groups including A2/A21 and V, excludes unrelated/deleted/lookalikes',()=>{
  const names=['A1-精','A2-股票','A21-股票','A3','B1V-精','B3-精','B22-new','B1VX-伪','B11V2','AB1','C2','换号区T','B14-old'];
  const rows=names.map((name,i)=>({...row(name,String(i+1)),delData:name==='B14-old'}));
  assert.deepEqual(selectCarriers(rows).map(r=>r.name),['A1-精','A2-股票','A3','A21-股票','B1V-精','B3-精','B22-new']);
});
test('ambiguous group names, duplicate IDs, missing deletion flag and rounded IDs stop',()=>{
  for(const rows of [[row(),row('A1-another','456')],[row(),row('B1','123')],
    [{...row(),delData:undefined}],[{...row(),id:123}]])assert.throws(()=>selectCarriers(rows));
});
test('public settings reject invalid limits, extra behavior, and automatic execution',()=>{
  assert.equal(validateSettings(settings),settings);
  for(const change of [{dailyDispatchLimit:0},{dailySendAmount:'130'},{dailySendAmount:151},
    {executionMode:'automatic'},{taskF:true}])assert.throws(()=>validateSettings({...settings,...change}));
});
test('H payload changes exactly the two requested fields and preserves source types and values',()=>{
  const before=row(),payload=makePayload(before,settings);
  assert.deepEqual(Object.keys(before).filter(k=>!assertValueEqual(before[k],payload[k])),['sendLimitByDay','sendNewLimitByDay']);
  assert.equal(before.sendLimitByDay,140);assert.equal(payload.userSendSizeByDay,225);
  assert.deepEqual(payload.params,{});assert.notEqual(payload.params,before.params);
});
function assertValueEqual(a,b){try{assert.deepEqual(a,b);return true;}catch{return false;}}
test('readback tolerates live counters/audit time only, rejects config changes, identity and target mismatch',()=>{
  const before=row(),after={...makePayload(before,settings),dailyRemaining:15,dailyTotal:999,updateTime:'new'};
  assert.doesNotThrow(()=>assertReadback(before,after,settings));
  for(const change of [{sendLimitByDay:149},{name:'A2'},{id:'234'},{sendScore:0.06},{renewAble:false}])
    assert.throws(()=>assertReadback(before,{...after,...change},settings));
  assert.throws(()=>assertSameConfig(before,{...before,userSendSizeByDay:220}));
});
test('explicit current-user CLI required, inherited enable flags cannot turn preview into writes',()=>{
  assert.equal(parseTaskHArgs(['preview','--json']),'preview');
  assert.equal(parseTaskHArgs(['run','--force-task-h','--authorization-source','current-user','--json']),'run');
  for(const args of [[],['--json'],['run','--json'],['run','--force-task-h','--authorization-source','timer','--json'],
    ['preview','--json','--force-task-h']])assert.throws(()=>parseTaskHArgs(args));
});
test('durable intent before each single write, independent readback before resolution, final sweep',async()=>{
  const f=fakeWorkflow([row(),row('B1V','456')]),result=await executeTaskH(f.deps);
  assert.equal(f.writes,2);assert.equal(result.modified,2);assert.equal(result.independentlyVerified,2);
  assert.equal(f.pending,null);assert.equal(f.events.filter(e=>e==='PUT').length,2);
  assert.ok(f.events.indexOf('plan')<f.events.indexOf('intent'));
  assert.ok(f.events.indexOf('intent')<f.events.indexOf('PUT'));
  assert.ok(f.events.indexOf('GET',f.events.indexOf('PUT'))<f.events.indexOf('resolve'));
});
test('already correct carriers are independently reread without rewrite',async()=>{
  const f=fakeWorkflow([makePayload(row(),settings)]),r=await executeTaskH(f.deps);
  assert.equal(f.writes,0);assert.equal(r.modified,0);assert.equal(r.alreadyCorrect,1);
  assert.equal(r.independentlyVerified,1);assert.ok(f.events.filter(e=>e==='GET').length>=3);
});
test('all details must pass before first write',async()=>{
  const f=fakeWorkflow([row(),row('B1','456')]);f.current.get('456').delData=true;
  await assert.rejects(executeTaskH(f.deps));assert.equal(f.writes,0);
});
test('configuration changed after planning stops before submit',async()=>{
  const f=fakeWorkflow();f.deps.assertScope=async()=>{f.current.get('123').sendScore=0.2;};
  await assert.rejects(executeTaskH(f.deps),/CONFIG_CHANGED_BEFORE_WRITE/);assert.equal(f.writes,0);
});
test('request timeout leaves the intent and never repeats the mutation',async()=>{
  const f=fakeWorkflow();let attempted=0;
  f.deps.submit=async()=>{attempted++;throw Error('timeout');};
  await assert.rejects(executeTaskH(f.deps),/timeout/);assert.equal(attempted,1);assert.ok(f.pending);
  assert.equal(f.events.filter(e=>e==='resolve').length,0);
});
test('accepted is not completed: mismatching readback retains intent, next carrier is untouched',async()=>{
  const f=fakeWorkflow([row(),row('B1','456')]);let attempted=0;
  f.deps.submit=async()=>{attempted++;return {code:200};};
  await assert.rejects(executeTaskH(f.deps),/INDEPENDENT_READBACK_FAILED/);assert.equal(attempted,1);assert.ok(f.pending);
});
test('a final independent sweep catches a later unrelated configuration change',async()=>{
  const f=fakeWorkflow();let checks=0;
  f.deps.assertScope=async()=>{if(++checks===2)f.current.get('123').sendScore=0.9;};
  await assert.rejects(executeTaskH(f.deps),/INDEPENDENT_READBACK_FAILED/);assert.equal(f.writes,1);
});

async function integration(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'opensms-h-test-'));
  // Test-only files live exclusively under this freshly allocated directory.
  t.after(()=>fs.rm(root,{recursive:true,force:true}));
  const stateRoot=path.join(root,'state'),settingsPath=path.join(root,'settings.json');
  await fs.writeFile(settingsPath,JSON.stringify(settings));
  const current=new Map([['123',row()]]);let writes=0,connections=0,closed=0;
  const api=async(method,endpoint,payload)=>{
    if(method==='GET'&&endpoint==='/carrier/config/listNotDeleted')return {code:200,total:1,rows:[...current.values()].map(r=>structuredClone(r))};
    if(method==='GET'&&endpoint==='/carrier/config/123')return {code:200,data:structuredClone(current.get('123'))};
    if(method==='PUT'&&endpoint==='/carrier/config'){writes++;current.set(payload.id,structuredClone(payload));return {code:200};}
    throw Error('unexpected endpoint');
  };
  const deps={stateRoot,settingsPath,connect:async()=>{connections++;return {api,close:async()=>{closed++;}};}};
  return {root,current,api,deps,get writes(){return writes;},get connections(){return connections;},get closed(){return closed;}};
}

test('standalone preview generates a fresh receipt/Chinese summary with no write and no F',async t=>{
  const f=await integration(t),r=await runTaskH('preview',f.deps);
  assert.equal(r.status,'previewed_no_write');assert.equal(r.toModify,1);assert.equal(f.writes,0);assert.equal(f.closed,1);
  assert.equal(r.taskFInvoked,false);assert.match(await fs.readFile(r.summaryPath,'utf8'),/任务F未调用/);
});
test('H persistence independently verifies, archives resolved intent, repeat run writes nothing',async t=>{
  const f=await integration(t),first=await runTaskH('run',f.deps),second=await runTaskH('run',f.deps);
  assert.equal(first.status,'verified');assert.equal(first.modified,1);assert.equal(first.submissionAttempts,1);
  assert.equal(second.status,'verified');assert.equal(second.modified,0);assert.equal(second.alreadyCorrect,1);assert.equal(f.writes,1);
  assert.notEqual(first.runId,second.runId);
  await assert.rejects(fs.stat(path.join(f.deps.stateRoot,'pending.json')),{code:'ENOENT'});
  assert.ok((await fs.readdir(path.dirname(first.receiptPath))).includes('intent_123_resolved.json'));
});
test('unresolved or corrupt pending stops before browser connection, leaves bytes unchanged',async t=>{
  const f=await integration(t);await fs.mkdir(f.deps.stateRoot,{recursive:true});
  const pending=path.join(f.deps.stateRoot,'pending.json');await fs.writeFile(pending,'broken prior intent');
  const r=await runTaskH('run',f.deps);assert.equal(r.error,'TASK_H_PRIOR_SUBMISSION_UNRESOLVED');
  assert.equal(f.connections,0);assert.equal(f.writes,0);assert.equal(await fs.readFile(pending,'utf8'),'broken prior intent');
});
test('runner records uncertain writes, leaves barrier, blocks all later runs without resubmission',async t=>{
  const f=await integration(t);let attempts=0;
  const connect=async()=>({api:async(...args)=>{
    if(args[0]==='PUT'){attempts++;throw Error('HTTP 502');}return f.api(...args);
  },close:async()=>{}});
  const r=await runTaskH('run',{...f.deps,connect});assert.equal(r.status,'stopped_no_retry');
  assert.equal(r.submissionAttempts,1);assert.equal(r.modified,0);assert.equal(r.pendingIntent,true);
  assert.equal(r.diagnostic.httpStatus,502);
  const again=await runTaskH('run',f.deps);assert.equal(again.error,'TASK_H_PRIOR_SUBMISSION_UNRESOLVED');
  assert.equal(attempts,1);assert.equal(f.connections,0);
});
test('settings drift prevents any subsequent intent or submit',async t=>{
  const f=await integration(t);let gets=0;
  const connect=async()=>({api:async(...args)=>{
    if(args[1]==='/carrier/config/123'&&++gets===2)await fs.writeFile(f.deps.settingsPath,JSON.stringify({...settings,dailyDispatchLimit:160}));
    return f.api(...args);
  },close:async()=>{}});
  const r=await runTaskH('run',{...f.deps,connect});assert.equal(r.error,'TASK_H_SETTINGS_CHANGED');assert.equal(f.writes,0);
});
test('H lock contention does not connect or clear another owner',async t=>{
  const f=await integration(t),lock=path.join(f.deps.stateRoot,'lock');await fs.mkdir(lock,{recursive:true});
  await fs.writeFile(path.join(lock,'owner.json'),'other-run');
  const r=await runTaskH('run',f.deps);assert.equal(r.error,'TASK_H_LOCK_BUSY');assert.equal(f.connections,0);
  assert.equal(await fs.readFile(path.join(lock,'owner.json'),'utf8'),'other-run');
});
