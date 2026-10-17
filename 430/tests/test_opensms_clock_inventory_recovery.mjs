import test from 'node:test';
import assert from 'node:assert/strict';
import {readExportClockBeforeSubmit} from '../scripts/opensms_export_clock_recovery.mjs';
import {collect} from '../scripts/opensms_collect_task_f_kooda_cards.mjs';

test('clock recovery reloads once, only for explicit missing clock', async () => {
  let reads = 0, reloads = 0;
  const events = [];
  const value = await readExportClockBeforeSubmit({
    read: async () => { if (++reads === 1) throw Object.assign(new Error('missing'), {code:'EXPORT_SERVER_CLOCK_MISSING'}); return '2026-09-21'; },
    reload: async () => { reloads++; }, record: async e => events.push(e),
  });
  assert.equal(value, '2026-09-21'); assert.equal(reloads, 1); assert.equal(events.length, 2);
});
test('clock unknown/auth failure never reloads; persistent missing stops', async () => {
  for (const code of ['AUTH', 'UNKNOWN', 'EXPORT_SERVER_CLOCK_MISSING']) {
    let reloads = 0;
    await assert.rejects(readExportClockBeforeSubmit({read:async () => {throw Object.assign(new Error('stop'), {code});}, reload:async () => {reloads++;}, record:async () => {}}));
    assert.equal(reloads, code === 'EXPORT_SERVER_CLOCK_MISSING' ? 1 : 0);
  }
});
test('inventory classified network failure recovers with fresh serial pass', async () => {
  let calls = 0;
  const waits = [], attempts = [];
  await collect(async () => { if (++calls === 1) throw Object.assign(new Error('network'), {readDiagnostic:{category:'network_fetch_failed'}}); return {code:200,total:0,rows:[]}; },
    {workers:4,sleep:async ms => waits.push(ms),onAttempt:async a => attempts.push(a)});
  assert.equal(calls, 2); assert.deepEqual(waits,[30000]); assert.equal(attempts[1].requestedWorkers,1);
});
test('inventory unknown/auth failure does not retry; network bounded at three', async () => {
  for (const category of ['unknown','http_401','http_403','network_fetch_failed']) {
    let calls = 0;
    await assert.rejects(collect(async () => {calls++; throw Object.assign(new Error('stop'),{readDiagnostic:{category}});},{sleep:async () => {}}));
    assert.equal(calls,category === 'network_fetch_failed' ? 3 : 1);
  }
});
test('inventory keeps HTTP 403 evidence, never labels it unknown or retries it',async()=>{
  let calls=0;
  await assert.rejects(collect(async()=>{calls++;
    throw Object.assign(new Error('HTTP 403 nil'),{status:403,readDiagnostic:{category:'http_403'}});
  },{sleep:async()=>assert.fail('no retry')}),error=>
    error.paginationAttempts[0].failure.requestCategory==='http_403'
    && error.paginationAttempts[0].failure.httpStatus===403);
  assert.equal(calls,1);
});
