import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import vm from 'node:vm';
import { createRequestPacer, requestTimeouts, REQUEST_PACING } from '../opensms_request_pacing.mjs';
import { validateOpenSmsApiResponse } from '../opensms_api_response.mjs';
import { withReadRetry } from '../opensms_read_retry.mjs';

test('approved server pacing is fixed and required by the normal runner', async () => {
  assert.deepEqual(REQUEST_PACING, {concurrency:1, completionGapMs:1000,
    failureCooldownMs:30000, readTimeoutMs:45000, queueTimeoutMs:900000});
  assert.ok(Object.isFrozen(REQUEST_PACING));
  const read = file => fs.readFile(new URL(file, import.meta.url), 'utf8');
  const runner = await read('../scripts/opensms_patrol_runner.py');
  assert.match(runner, /ROOT \/ "opensms_request_pacing\.mjs"/);
  const protocol = await read('../opensms_protocol_client.mjs');
  assert.match(protocol, /withReadRetry\(\(\) => withOpenSmsRequestPacing\(/);
  const exporter = await read('../scripts/opensms_export_fast_raw.mjs');
  assert.match(exporter, /const submitted = await withOpenSmsRequestPacing\(/);
  assert.match(exporter, /const response = await withReadRetry\(\(\) => withOpenSmsRequestPacing\(/);
  assert.match(exporter, /setTimeout\(resolve, 15000\)/);
  assert.match(exporter, /preDownloadWaitMs: 60000/);
  assert.match(exporter, /setTimeout\(resolve, 60000\)/);
});

async function temp(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(),'opensms-pacing-test-'));
  t.after(() => fs.rm(dir,{recursive:true,force:true})); return dir;
}
test('same-process calls serialize across separate pacers with completion gap', async t => {
  const directory = await temp(t), receipts = [], spans = [];
  const opts = {directory,gapMs:30,cooldownMs:40,onReceipt:r=>receipts.push(r)};
  const a = createRequestPacer(opts), b = createRequestPacer(opts);
  let active = 0, peak = 0;
  const op = async () => {
    peak = Math.max(peak,++active); const start = Date.now();
    await new Promise(r=>setTimeout(r,20)); spans.push({start,end:Date.now()}); active--;
  };
  await Promise.all([a(op),a(op),b(op)]);
  assert.equal(peak,1); spans.sort((x,y)=>x.start-y.start);
  for(let i=1;i<spans.length;i++) assert.ok(spans[i].start-spans[i-1].end>=30);
  assert.equal(receipts.length,3);
});
test('failure releases lane, persists cooldown, never replays operation',async t=>{
  const directory=await temp(t); let now=1000,calls=0;
  const pace=createRequestPacer({directory,gapMs:2,cooldownMs:30,clock:()=>now,sleep:async ms=>{now+=ms;}});
  const error=Object.assign(new Error('network'),{retryAfterMs:90});
  await assert.rejects(pace(async()=>{calls++;throw error;}),e=>e===error);
  assert.equal(JSON.parse(await fs.readFile(path.join(directory,'next.json'))).nextAllowedAt,1090);
  await pace(async()=>{assert.equal(now,1090);calls++;}); assert.equal(calls,2);
});
test('production completion gap is one second across pacers, without an environment speed override',async t=>{
  const directory=await temp(t),receipts=[];let now=1000;
  const opts={directory,clock:()=>now,sleep:async ms=>{now+=ms;},onReceipt:r=>receipts.push(r)};
  await createRequestPacer(opts)(async()=>{now+=150;});
  await createRequestPacer(opts)(async()=>assert.equal(now,2150));
  assert.equal(receipts[0].gapMs,1000);
  assert.equal(receipts[1].gapMs,1000);
});
test('legacy page transport cools down a read 403 once and never repeats writes',async t=>{
  const source=await fs.readFile(new URL('../opensms_protocol_client.mjs',import.meta.url),'utf8');
  const start=source.lastIndexOf('  async function api(');
  const end=source.indexOf('\n  return { ...session',start);
  assert.ok(start>=0 && end>start);
  assert.ok(!source.includes('bodySample'));
  for(const method of ['GET','POST']) {
    const directory=await temp(t),receipts=[];let now=1000,calls=0;
    const context=vm.createContext({
      withReadRetry:(op,opts)=>withReadRetry(op,{...opts,sleep:async ms=>{now+=ms;}}),
      validateOpenSmsApiResponse,payloadCodec:{},
      withOpenSmsRequestPacing:createRequestPacer({directory,clock:()=>now,
        sleep:async ms=>{now+=ms;},onReceipt:r=>receipts.push(r)}),
      fetchFromAdminPage:async()=>{calls++;return {status:403,ok:false,text:'secret-cookie'};},
      parseResponseText:()=>assert.fail('denial must not be decrypted or logged'),
      readAdminToken:()=>assert.fail('denial must not refresh credentials'),
      console:{error:()=>{}},
    });
    const api=vm.runInContext(`${source.slice(start,end)}\napi`,context);
    await assert.rejects(api(method,'/carrier/config/listNotDeleted'),e=>
      e.status===403 && e.readDiagnostic.category==='http_403' && !e.message.includes('secret-cookie'));
    assert.equal(calls,method==='GET'?2:1);
    assert.equal(receipts[0].success,false);
    assert.equal(receipts[0].gapMs,30000);
    assert.equal(JSON.parse(await fs.readFile(path.join(directory,'next.json'))).nextAllowedAt,
      method==='GET'?61000:31000);
  }
});
test('unconfirmed browser request retains the lane and stops queued readers promptly',async t=>{
  const directory=await temp(t);
  const pace=createRequestPacer({directory});
  const error=Object.assign(new Error('unknown completion'),{requestCompletionUnconfirmed:true});
  await assert.rejects(pace(async()=>{throw error;}),e=>e===error);
  assert.ok(await fs.stat(path.join(directory,'request.lock','owner')));
  assert.equal(JSON.parse(await fs.readFile(path.join(directory,'request.lock','blocked.json'))).reason,
    'browser_request_completion_unconfirmed');
  await assert.rejects(pace(async()=>assert.fail('no extra network')),/REQUEST_UNCONFIRMED/);
});
test('HTTP Retry-After is shared even on nonretryable paths',async t=>{
  const directory=await temp(t);let now=1000;
  const pace=createRequestPacer({directory,gapMs:2,cooldownMs:30,clock:()=>now,sleep:async ms=>{now+=ms;}});
  await pace(async()=>({status:429,retryAfter:'120'}));
  await pace(async()=>assert.equal(now,121000));
});
test('HTTP denials are failed receipts and cool down the shared lane without any replay',async t=>{
  const directory=await temp(t),receipts=[];let now=1000,calls=0;
  const pace=createRequestPacer({directory,gapMs:2,cooldownMs:30,clock:()=>now,
    sleep:async ms=>{now+=ms;},onReceipt:r=>receipts.push(r)});
  await pace(async()=>{calls++;return {status:403,ok:false,text:'nil\n'};});
  assert.equal(receipts[0].success,false);
  assert.equal(receipts[0].httpStatus,403);
  assert.equal(receipts[0].gapMs,30);
  await pace(async()=>{assert.equal(now,1030);calls++;});
  assert.equal(calls,2);
});
test('production validation failure is inside cooldown and retry boundaries, not recorded as success',async t=>{
  for (const response of [{status:403,ok:false,text:'nil\n'},
    {status:200,ok:true,text:'do-not-log-cookie'}]) {
    const directory=await temp(t),receipts=[];let now=1000,calls=0;
    const pace=createRequestPacer({directory,gapMs:2,cooldownMs:30,clock:()=>now,
      sleep:async ms=>{now+=ms;},onReceipt:r=>receipts.push(r)});
    await assert.rejects(withReadRetry(()=>pace(async()=>{calls++;
      return validateOpenSmsApiResponse(response,JSON.parse,{method:'GET',apiPath:'/carrier/config/listNotDeleted'});
    }),{method:'GET',apiPath:'/carrier/config/listNotDeleted',sleep:async ms=>{now+=ms;}}));
    assert.equal(calls,response.status===403?2:1);assert.equal(receipts[0].success,false);assert.equal(receipts[0].gapMs,30);
    assert.equal(JSON.parse(await fs.readFile(path.join(directory,'next.json'))).nextAllowedAt,
      response.status===403?31030:1030);
    assert.ok(!JSON.stringify(receipts).includes('cookie'));
  }
});
test('held or corrupted locks fail closed without sending',async t=>{
  const directory=await temp(t);await fs.mkdir(path.join(directory,'request.lock'));
  let now=0,sent=false;
  const pace=createRequestPacer({directory,queueTimeoutMs:10,clock:()=>now,sleep:async ms=>{now+=ms;}});
  await assert.rejects(pace(async()=>{sent=true;}),/QUEUE_TIMEOUT/);assert.equal(sent,false);
  assert.ok((await fs.stat(path.join(directory,'request.lock'))).isDirectory());
});
test('corrupt persisted cooldown is not replaced or ignored',async t=>{
  const directory=await temp(t);await fs.writeFile(path.join(directory,'next.json'),'{}');
  await assert.rejects(createRequestPacer({directory})(async()=>assert.fail('must not send')),/STATE_INVALID/);
});
test('queue waiting does not consume read timeout; write timeout not shortened',()=>{
  assert.deepEqual(requestTimeouts('GET','/carrier/config/listNotDeleted'),{readTimeoutMs:45000,evaluationTimeoutMs:60000});
  assert.equal(requestTimeouts('POST','/phone/config/list',{timeoutMs:45000}).evaluationTimeoutMs,60000);
  assert.equal(requestTimeouts('GET','/phone/cardRetain/list').readTimeoutMs,45000);
  assert.equal(requestTimeouts('POST','/phone/config/batchSetChatExpire').readTimeoutMs,null);
  assert.equal(requestTimeouts('POST','/phone/config/batchSetChatExpire').evaluationTimeoutMs,180000);
  assert.equal(REQUEST_PACING.concurrency,1);
});
test('real separate Node processes share a single lane',async t=>{
  const directory=await temp(t);
  const moduleUrl=new URL('../opensms_request_pacing.mjs',import.meta.url).href;
  const code=`import {createRequestPacer} from ${JSON.stringify(moduleUrl)};
    const pace=createRequestPacer({directory:process.argv[1],gapMs:50});
    await pace(async()=>{const start=Date.now();await new Promise(r=>setTimeout(r,40));console.log(JSON.stringify({start,end:Date.now()}));});`;
  const child=()=>new Promise((resolve,reject)=>{
    const proc=spawn(process.execPath,['--input-type=module','-e',code,directory],{stdio:['ignore','pipe','pipe']});
    let out='',err='';proc.stdout.on('data',d=>out+=d);proc.stderr.on('data',d=>err+=d);
    proc.on('error',reject);proc.on('close',n=>n?reject(new Error(err)):resolve(JSON.parse(out)));
  });
  const rows=(await Promise.all([child(),child(),child()])).sort((a,b)=>a.start-b.start);
  for(let i=1;i<rows.length;i++) assert.ok(rows[i].start-rows[i-1].end>=50);
});

async function waitUntil(predicate) {
  const deadline = Date.now() + 3000;
  while (!await predicate()) {
    if (Date.now() > deadline) throw new Error('test queue registration timed out');
    await new Promise(resolve => setTimeout(resolve, 5));
  }
}

test('an already waiting exporter cannot be overtaken by successive collection requests', async t => {
  const directory = await temp(t), order = [];
  const collector = createRequestPacer({directory, gapMs:5});
  const exporter = createRequestPacer({directory, gapMs:5});
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const first = collector(async () => { order.push('first'); await gate; });
  await waitUntil(async () => order.length === 1);
  const status = exporter(async () => order.push('export-status'));
  await waitUntil(async () => (await fs.readdir(path.join(directory, 'waiters'))).length === 2);
  const later = collector(async () => order.push('later-collection'));
  release();
  await Promise.all([first, status, later]);
  assert.deepEqual(order, ['first', 'export-status', 'later-collection']);
  assert.deepEqual(await fs.readdir(path.join(directory, 'waiters')), []);
});

test('a registered exporter in another Node process precedes the next collection request', async t => {
  const directory = await temp(t), order = [];
  const collector = createRequestPacer({directory, gapMs:10});
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const first = collector(async () => { order.push('first'); await gate; });
  await waitUntil(async () => order.length === 1);
  const moduleUrl = new URL('../opensms_request_pacing.mjs', import.meta.url).href;
  const code = `import {createRequestPacer} from ${JSON.stringify(moduleUrl)};
    const pace=createRequestPacer({directory:process.argv[1],gapMs:10});
    await pace(async()=>console.log(JSON.stringify({start:Date.now()})));`;
  const child = spawn(process.execPath, ['--input-type=module', '-e', code, directory],
    {stdio:['ignore','pipe','pipe']});
  let output = '', stderr = '';
  child.stdout.on('data', data => { output += data; });
  child.stderr.on('data', data => { stderr += data; });
  const completed = new Promise((resolve, reject) => {
    child.on('error', reject);
    child.on('close', code => code ? reject(new Error(stderr)) : resolve(JSON.parse(output)));
  });
  t.after(() => { if (child.exitCode === null) child.kill(); });
  await waitUntil(async () => (await fs.readdir(path.join(directory, 'waiters')))
    .some(name => name.startsWith(`${child.pid}-`) && name.endsWith('.json')));
  let laterStarted;
  const later = collector(async () => { laterStarted = Date.now(); order.push('later'); });
  release();
  const [, exported] = await Promise.all([first, completed, later]);
  assert.ok(laterStarted - exported.start >= 10);
  assert.deepEqual(order, ['first', 'later']);
  assert.deepEqual(await fs.readdir(path.join(directory, 'waiters')), []);
});

test('a crashed waiter fails closed and only the unsent caller ticket is removed', async t => {
  const directory = await temp(t), waiters = path.join(directory, 'waiters');
  await fs.mkdir(waiters);
  const owner = '999999-00000000-0000-0000-0000-000000000000';
  const ticket = JSON.stringify({version:1, owner, queuedAt:1});
  await fs.writeFile(path.join(waiters, `${owner}.json`), ticket);
  let now = 1000;
  const pace = createRequestPacer({directory, queueTimeoutMs:40,
    clock:() => now, sleep:async ms => { now += ms; }});
  await assert.rejects(pace(async () => assert.fail('no network')), /QUEUE_TIMEOUT/);
  assert.deepEqual(await fs.readdir(waiters), [`${owner}.json`]);
  assert.equal(await fs.readFile(path.join(waiters, `${owner}.json`), 'utf8'), ticket);
});

test('incomplete or corrupt queue evidence is not ignored or repaired', async t => {
  for (const [name, expected] of [['old.pending', /QUEUE_TIMEOUT/], ['invalid.json', /QUEUE_INVALID/]]) {
    const directory = await temp(t), waiters = path.join(directory, 'waiters');
    await fs.mkdir(waiters); await fs.writeFile(path.join(waiters, name), '{}');
    let now = 1000;
    const pace = createRequestPacer({directory, queueTimeoutMs:40,
      clock:() => now, sleep:async ms => { now += ms; }});
    await assert.rejects(pace(async () => assert.fail('no network')), expected);
    assert.deepEqual(await fs.readdir(waiters), [name]);
  }
});

test('unknown request completion retains its FIFO ownership as well as the original lock', async t => {
  const directory = await temp(t);
  const pace = createRequestPacer({directory});
  await assert.rejects(pace(async () => {
    throw Object.assign(new Error('unconfirmed'), {requestCompletionUnconfirmed:true});
  }), /unconfirmed/);
  const names = await fs.readdir(path.join(directory, 'waiters'));
  assert.equal(names.length, 1);
  const owner = await fs.readFile(path.join(directory, 'request.lock', 'owner'), 'utf8');
  assert.equal(names[0], `${owner}.json`);
  await assert.rejects(pace(async () => assert.fail('no replay')), /REQUEST_UNCONFIRMED/);
  assert.deepEqual(await fs.readdir(path.join(directory, 'waiters')), names);
});
