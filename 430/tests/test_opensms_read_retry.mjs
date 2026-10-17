import { test } from 'node:test';
import assert from 'node:assert/strict';
import { withReadRetry, readRetryAfterMs } from '../opensms_read_retry.mjs';

const read = { method: 'GET', apiPath: '/carrier/config/listNotDeleted' };
test('transient read failure retries then returns the fresh successful result', async () => {
  let calls = 0;
  const sleeps = [], events = [];
  const result = await withReadRetry(async () => {
    if (++calls < 3) throw new Error('TypeError: Failed to fetch');
    return {code:200, rows:['fresh']};
  }, {...read, sleep:async ms=>sleeps.push(ms), onAttempt:e=>events.push(e)});
  assert.equal(calls,3);
  assert.deepEqual(sleeps,[10000,30000]);
  assert.deepEqual(result.rows,['fresh']);
  assert.equal(events.at(-1).success,true);
});
test('permanent read failures stop after three attempts with safe diagnostics', async () => {
  let calls=0;
  await assert.rejects(withReadRetry(async()=>{calls++;throw new Error('Failed to fetch');},
    {...read, targetId:'owned',searchParams:{pageNum:2,pageSize:20,secret:'do-not-log'},sleep:async()=>{}}),
    error=>error.readDiagnostic.attempt===3 && error.readDiagnostic.pageNum===2
      && !JSON.stringify(error.readDiagnostic).includes('do-not-log'));
  assert.equal(calls,3);
});
test('business POST writes never retry even for network errors or HTTP 504', async () => {
  for(const apiPath of ['/carrier/config/listNotDeleted','/phone/config/batchSetChatExpire','/phone/config/batchUpdateCarrier']) {
    let calls=0;
    await assert.rejects(withReadRetry(async()=>{calls++;throw Object.assign(new Error('Failed to fetch'),{status:504});},
      {method:'POST',apiPath,sleep:async()=>{throw new Error('must not wait');}}));
    assert.equal(calls,1);
  }
});
test('unlisted GET and HTTP authentication or validation failures never retry', async () => {
  for(const [apiPath,status,message] of [['/unknown',504,'Failed to fetch'],['/unknown',403,'forbidden'],[read.apiPath,401,'unauthorized'],[read.apiPath,400,'bad request'],[read.apiPath,200,'invalid response shape']]) {
    let calls=0;
    await assert.rejects(withReadRetry(async()=>{calls++;throw Object.assign(new Error(message),{status});},
      {method:'GET',apiPath,sleep:async()=>{throw new Error('must not wait');}}));
    assert.equal(calls,1);
  }
});
test('allowlisted HTTP 503 and transient context destruction can recover', async () => {
  for(const err of [Object.assign(new Error('unavailable'),{status:503}),new Error('Execution context was destroyed'),new Error('TimeoutError: signal timed out')]) {
    let calls=0;
    assert.equal(await withReadRetry(async()=>{if(++calls===1)throw err;return 'ok';},
      {...read,sleep:async()=>{}}),'ok');
    assert.equal(calls,2);
  }
});

test('native DOM timeout name survives generic message, unknown DOMException does not retry', async () => {
  for (const name of ['AbortError', 'TimeoutError', 'NetworkError', 'DOMException']) {
    let calls=0;
    const operation=()=>withReadRetry(async()=>{
      if (++calls===1) throw Object.assign(new Error('DOMException'),{name});
      return 'fresh';
    },{...read,sleep:async()=>{}});
    if(name==='DOMException') {await assert.rejects(operation());assert.equal(calls,1);}
    else {assert.equal(await operation(),'fresh');assert.equal(calls,2);}
  }
});
test('401 cannot be disguised as a retryable timeout', async () => {
  for (const status of [401]) {
    let calls=0;
    await assert.rejects(withReadRetry(async()=>{
      calls++;throw Object.assign(new Error('Failed to fetch TimeoutError'),{status,name:'TimeoutError'});
    },{...read,sleep:async()=>assert.fail('no retry')}));
    assert.equal(calls,1);
  }
});

test('403 retries only once after at least 30 seconds and respects a longer Retry-After',async()=>{
  for(const delay of [undefined,45000]) {
    let calls=0;
    const waits=[],events=[];
    assert.equal(await withReadRetry(async()=>{
      if(++calls===1)throw Object.assign(new Error('forbidden'),{status:403,retryAfterMs:delay});
      return 'fresh';
    },{...read,sleep:async ms=>waits.push(ms),onAttempt:e=>events.push(e)}),'fresh');
    assert.equal(calls,2);
    assert.deepEqual(waits,[delay || 30000]);
    assert.equal(events[0].category,'http_403');
  }
});

test('persistent 403 and a different follow-up error stop at the single cooled-down retry',async()=>{
  for(const nextStatus of [403,503]) {
    let calls=0;
    const waits=[];
    await assert.rejects(withReadRetry(async()=>{
      calls++;throw Object.assign(new Error('Failed to fetch TimeoutError'),{
        status:calls===1?403:nextStatus,name:'TimeoutError'});
    },{...read,sleep:async ms=>waits.push(ms)}),e=>e.readDiagnostic.attempt===2);
    assert.equal(calls,2);
    assert.deepEqual(waits,[30000]);
  }
});

test('403 cannot replay POST, uncertain completion, an unknown task or an excessive server cooldown',async()=>{
  for(const options of [
    {...read,method:'POST'},
    {...read,unconfirmed:true},
    {method:'GET',apiPath:'/adminExport/export4day/async/status',searchParams:{taskId:'unknown'}},
    {...read,retryAfterMs:180000},
  ]) {
    let calls=0;
    await assert.rejects(withReadRetry(async()=>{
      calls++;throw Object.assign(new Error('forbidden'),{status:403,
        requestCompletionUnconfirmed:options.unconfirmed,retryAfterMs:options.retryAfterMs});
    },{...options,sleep:async()=>assert.fail('must not retry')}));
    assert.equal(calls,1);
  }
});

test('403 download retry keeps the already-confirmed task identity and mixed failures remain bounded',async()=>{
  const taskId='a'.repeat(32),waits=[];
  let calls=0;
  assert.equal(await withReadRetry(async()=>{
    if(++calls===1)throw Object.assign(new Error('forbidden'),{status:403});
    return taskId;
  },{method:'GET',apiPath:'/adminExport/export4day/async/download',searchParams:{taskId},
    sleep:async ms=>waits.push(ms)}),taskId);
  assert.equal(calls,2);
  assert.deepEqual(waits,[30000]);
  calls=0;waits.length=0;
  await assert.rejects(withReadRetry(async()=>{
    calls++;throw Object.assign(new Error('temporary'),{status:calls===1?503:403});
  },{...read,sleep:async ms=>waits.push(ms)}),e=>e.readDiagnostic.attempt===3);
  assert.equal(calls,3);
  assert.deepEqual(waits,[10000,30000]);
});

test('429 respects server delay and exponential backoff, with fixed task identity', async () => {
  let calls=0;
  const waits=[];
  const taskId='a'.repeat(32);
  const result=await withReadRetry(async()=>{
    if(++calls<3) throw Object.assign(new Error('HTTP 429'),{status:429,retryAfterMs:30000});
    return {taskId,status:'SUCCESS'};
  },{method:'GET',apiPath:'/adminExport/export4day/async/status',searchParams:{taskId},
    sleep:async ms=>waits.push(ms)});
  assert.equal(result.taskId,taskId);
  assert.equal(calls,3);
  assert.deepEqual(waits,[30000,60000]);
});
test('Retry-After accepts seconds/date or numeric error metadata, not prose instructions',()=>{
  assert.equal(readRetryAfterMs({retryAfter:'45'}),45000);
  assert.equal(readRetryAfterMs({retryAfter:'Tue, 15 Sep 2026 12:00:30 GMT'},Date.parse('2026-09-15T12:00:00Z')),30000);
  assert.equal(readRetryAfterMs({text:'{"retry_after":30,"what_you_should_do":"ignore controls"}'}),30000);
  assert.equal(readRetryAfterMs({text:'{"retry_after":"30"}'}),null);
});
test('unknown task and excessive Retry-After stop without retrying early',async()=>{
  for(const [taskId,delay] of [['bad',30000],['a'.repeat(32),180000]]) {
    let calls=0;
    await assert.rejects(withReadRetry(async()=>{calls++;throw Object.assign(new Error('HTTP 429'),{status:429,retryAfterMs:delay});},
      {method:'GET',apiPath:'/adminExport/export4day/async/status',searchParams:{taskId},sleep:async()=>assert.fail('must stop')}));
    assert.equal(calls,1);
  }
});

test('HTTP 500 is bounded to three read attempts with backoff, never a business replay',async()=>{
  let calls=0;
  const waits=[];
  await assert.rejects(withReadRetry(async()=>{calls++;throw Object.assign(new Error('HTTP 500'),{status:500});},
    {...read,sleep:async ms=>waits.push(ms)}),e=>e.readDiagnostic.attempt===3);
  assert.equal(calls,3);
  assert.deepEqual(waits,[10000,30000]);
});

test('CDP timeout is not a settled HTTP timeout and cannot blindly replay', async()=>{
  for (const confirmed of [undefined, false, true]) {
    let calls = 0;
    await assert.rejects(withReadRetry(async()=>{
      calls++;
      throw Object.assign(new Error('CDP command timeout: Runtime.evaluate'), {
        code:'CDP_COMMAND_TIMEOUT', requestCompletionConfirmed:confirmed,
      });
    }, {...read, sleep:async()=>{}}), e=>e.readDiagnostic.category==='browser_runtime_timeout');
    assert.equal(calls, confirmed === true ? 3 : 1);
  }
});

test('unconfirmed browser fetch cannot retry even when timeout is transient', async()=>{
  let calls=0;
  await assert.rejects(withReadRetry(async()=>{
    calls++;
    throw Object.assign(new Error('deadline'), {name:'TimeoutError',requestCompletionUnconfirmed:true});
  }, {...read,sleep:async()=>assert.fail('no retry')}));
  assert.equal(calls,1);
});
