import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import test from 'node:test';
import { createReadOnlyApiLimiter, mapReadOnlyBounded } from '../scripts/opensms_readonly_concurrency.mjs';
import { collectCarrierConfigPagesReadOnly, getCarrierConfigsReadOnly } from '../scripts/opensms_carriers_readonly.mjs';
import { listPhonesBatched, prepareTaskDInputs } from '../scripts/opensms_prepare_task_d.mjs';
import { collectCardRetention } from '../scripts/opensms_card_retention_readonly_lib.mjs';

test('bounded reads preserve input order while overlapping up to the limit', async () => {
  let active = 0;
  let maximum = 0;
  const result = await mapReadOnlyBounded([3, 1, 2, 0, 4], async value => {
    active += 1;
    maximum = Math.max(maximum, active);
    await delay(value + 1);
    active -= 1;
    return value * 2;
  }, { concurrency: 3 });
  assert.deepEqual(result, [6, 2, 4, 0, 8]);
  assert.equal(maximum, 3);
});

test('empty reads never start a worker and invalid concurrency is rejected', async () => {
  assert.deepEqual(await mapReadOnlyBounded([], () => assert.fail('unexpected call')), []);
  for (const concurrency of [0, -1, 1.5, 33, NaN]) {
    await assert.rejects(mapReadOnlyBounded([], () => {}, { concurrency }), /只读并发数/);
  }
});

test('first failed read stops queued work and waits for already dispatched work', async () => {
  const started = [];
  let siblingFinished = false;
  await assert.rejects(mapReadOnlyBounded([0, 1, 2, 3], async value => {
    started.push(value);
    if (value === 0) throw new Error('read failed');
    await delay(5);
    siblingFinished = true;
  }, { concurrency: 2 }), /read failed/);
  assert.deepEqual(started, [0, 1]);
  assert.equal(siblingFinished, true);
});

test('API limiter rejects writes and unknown reads before calling the API', async () => {
  const limiter = createReadOnlyApiLimiter(() => assert.fail('unexpected API call'), {
    allowedRequests: ['GET /phone/cardRetain/list'],
  });
  await assert.rejects(limiter.api('PUT', '/phone/config/updateCarrireIdentif'), /非白名单/);
  await assert.rejects(limiter.api('POST', '/phone/config/batchSetChatExpire'), /非白名单/);
  await assert.rejects(limiter.api('GET', '/unknown'), /非白名单/);
  assert.equal(limiter.stats.submitted, 0);
});

test('API limiter bounds eager pagination and drains on failure', async () => {
  const started = [];
  let lastFinished = false;
  const limiter = createReadOnlyApiLimiter(async (_method, _path, value) => {
    started.push(value);
    if (value === 0) throw new Error('API failure');
    await delay(5);
    lastFinished = true;
    return value;
  }, { concurrency: 2, allowedRequests: ['GET /read'] });
  const settled = Promise.allSettled([0, 1, 2, 3].map(value => limiter.api('GET', '/read', value)));
  await settled;
  await limiter.drain();
  assert.deepEqual(started, [0, 1]);
  assert.equal(lastFinished, true);
  assert.equal(limiter.stats.maxActive, 2);
  assert.equal(limiter.stats.cancelled, 2);
  await assert.rejects(limiter.api('GET', '/read', 4), /API failure/);
});

function carrierFixture({ total = 181, mutate } = {}) {
  const all = Array.from({ length: total }, (_, index) => ({ id: index + 1, carrireIdentif: `c-${index}`, name: `运营商${index}` }));
  const called = [];
  let active = 0;
  let maxActive = 0;
  return {
    all,
    called,
    get maxActive() { return maxActive; },
    async api(method, endpoint, data, { pageNum, pageSize }) {
      assert.equal(method, 'GET');
      assert.equal(endpoint, '/carrier/config/listNotDeleted');
      assert.equal(data, null);
      assert.equal(pageSize, 20);
      called.push(pageNum);
      active += 1;
      maxActive = Math.max(maxActive, active);
      await delay(pageNum % 3 + 1);
      active -= 1;
      const response = { code: 200, total, rows: all.slice((pageNum - 1) * pageSize, pageNum * pageSize) };
      return mutate ? mutate(response, pageNum) : response;
    },
  };
}

test('carrier config reads all exact pages in parallel and preserves page order', async () => {
  const fixture = carrierFixture();
  const result = await collectCarrierConfigPagesReadOnly(fixture.api, { concurrency: 4 });
  assert.deepEqual(result.rows, fixture.all);
  assert.deepEqual(fixture.called, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  assert.equal(fixture.maxActive, 4);
  assert.equal(result.pageCount, 10);
  assert.equal(result.pages.at(-1).rowCount, 1);
});

test('carrier config handles zero and exactly one full page without extra requests', async () => {
  for (const total of [0, 20]) {
    const fixture = carrierFixture({ total });
    assert.deepEqual(await getCarrierConfigsReadOnly(fixture.api), fixture.all);
    assert.deepEqual(fixture.called, [1]);
  }
});

test('carrier reads reject incomplete pages, changing totals, duplicate ids and API errors', async () => {
  for (const mutate of [
    (response, page) => page === 2 ? { ...response, rows: response.rows.slice(1) } : response,
    (response, page) => page === 2 ? { ...response, total: response.total + 1 } : response,
    (response, page) => page === 2 ? { ...response, rows: response.rows.map((row, index) => index ? row : { ...row, carrireIdentif: 'c-0' }) } : response,
    response => ({ ...response, total: 'invalid' }),
    response => ({ ...response, code: 500 }),
  ]) {
    const fixture = carrierFixture({ mutate });
    await assert.rejects(getCarrierConfigsReadOnly(fixture.api));
  }
});

test('retention library retains full pagination with 8 shared read slots', async () => {
  const total = 2010;
  const limiter = createReadOnlyApiLimiter(async (_method, _endpoint, _data, { pageNum, pageSize }) => {
    assert.equal(pageSize, 200);
    await delay(2);
    return {
      code: 200,
      total,
      rows: Array.from({ length: Math.min(pageSize, total - (pageNum - 1) * pageSize) }, (_, index) => ({
        cardNo: String(2000000000 + (pageNum - 1) * pageSize + index),
        expireBefore: '2026-08-31',
        expireAfter: '2026-09-07',
        chatId: `chat-${pageNum}-${index}`,
        createTime: '2026-09-01 01:02:03',
      })),
    };
  }, { allowedRequests: ['GET /phone/cardRetain/list'] });
  const result = await collectCardRetention(limiter.api);
  await limiter.drain();
  assert.equal(result.fetchedRecords, total);
  assert.equal(result.uniqueCardCount, total);
  assert.equal(result.pageCount, 11);
  assert.equal(result.records.at(-1).expireAfter, '2026-09-07');
  assert.equal(limiter.stats.maxActive, 8);
});

test('Task D starts analysis and CDP setup independently and preserves the barrier', async () => {
  const events = [];
  let releaseAnalysis;
  const analysis = new Promise(resolve => { releaseAnalysis = resolve; });
  const session = { client: { close() { assert.fail('should remain open'); } } };
  const result = await prepareTaskDInputs({
    analyzeMetrics: async () => { events.push('analysis-start'); await analysis; events.push('analysis-done'); },
    createSession: async () => { events.push('session-start'); return session; },
    collectLive: async value => { assert.equal(value, session); events.push('live-read'); releaseAnalysis(); return ['carriers', 'retention']; },
  });
  assert.equal(result.session, session);
  assert.deepEqual(result.live, ['carriers', 'retention']);
  assert.deepEqual(events, ['analysis-start', 'session-start', 'live-read', 'analysis-done']);
});

test('Task D analysis failure still waits for reads and closes its session', async () => {
  const events = [];
  await assert.rejects(prepareTaskDInputs({
    analyzeMetrics: async () => { throw new Error('metric failure'); },
    createSession: async () => ({ client: { close() { events.push('closed'); } } }),
    collectLive: async () => { await delay(5); events.push('read-done'); return []; },
  }), /metric failure/);
  assert.deepEqual(events, ['read-done', 'closed']);
});

test('Task D phone queries stay under 500 per batch and return stable ordering', async () => {
  const cards = Array.from({ length: 5100 }, (_, index) => String(2000000000 + index));
  let active = 0;
  let maxActive = 0;
  const rows = await listPhonesBatched(async (method, endpoint, params) => {
    assert.equal(method, 'POST');
    assert.equal(endpoint, '/phone/config/list');
    const batch = params.cardNo.split('\n');
    assert.ok(batch.length <= 500);
    active += 1;
    maxActive = Math.max(maxActive, active);
    await delay(2);
    active -= 1;
    return { code: 200, total: batch.length, rows: batch.map(cardNo => ({ cardNo })) };
  }, cards);
  assert.deepEqual(rows.map(row => row.cardNo), cards);
  assert.equal(maxActive, 8);
  assert.deepEqual(await listPhonesBatched(() => assert.fail('empty API read'), []), []);
});

test('Task D and E authorization gates reject before any live session can start', () => {
  for (const [script, args] of [
    ['opensms_prepare_task_d.mjs', ['unused.xlsx', '2026-09-05', 'test', '/tmp/unused']],
    ['opensms_collect_task_e_card_retention.mjs', ['test', '/tmp/unused.json']],
  ]) {
    const env = { ...process.env, OPENSMS_ENABLE_TASK_D: '0', OPENSMS_FORCE_TASK_D: '0', OPENSMS_ENABLE_TASK_E: '0', OPENSMS_FORCE_TASK_E: '0' };
    const result = spawnSync(process.execPath, [new URL(`../scripts/${script}`, import.meta.url).pathname, ...args], { env, encoding: 'utf8' });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /拒绝准备任务D|拒绝读取卡保留记录/);
  }
});
