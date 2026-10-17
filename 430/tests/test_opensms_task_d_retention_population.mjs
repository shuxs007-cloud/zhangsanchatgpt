import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  collectRetentionPopulation, normalizePhone10, requireAuthorization, RETENTION_CARRIER_NAMES, runCollector,
} from '../scripts/opensms_collect_task_d_retention_population.mjs';

const RUN_ID = 'offline-task-d-retention-population';
const AUTH = Object.freeze({ OPENSMS_FORCE_TASK_D: '1', OPENSMS_ENABLE_TASK_D: '1', OPENSMS_EXECUTE_TASK_D: '1',
  OPENSMS_RUN_ID: RUN_ID, BITBROWSER_CDP: 'http://127.0.0.1:1', OPENSMS_RAW_API_TARGET_ID: 'offline-owned-target' });
const NAMES = RETENTION_CARRIER_NAMES;
function sourceRows(count, group) {
  return Array.from({ length: count }, (_, index) => ({ id: `${group}-${index}`, cardNo: String((group === 'C' ? 3000000000 : 5000000000) + index),
    carrireIdentif: group, carrierName: NAMES[group === 'C' ? 0 : 1] }));
}
function fakeApi({ C = [], T = [], transform, delay = 0, configRows } = {}) {
  const calls = [];
  const stats = { active: 0, maxActive: 0 };
  const configs = configRows || NAMES.map((name, i) => ({ id: i + 1, name, carrireIdentif: i === 0 ? 'C' : 'T' }));
  const api = async (method, endpoint, body, query) => {
    calls.push({ method, endpoint, body, query });
    stats.active += 1;
    stats.maxActive = Math.max(stats.maxActive, stats.active);
    try {
      if (delay) await new Promise(resolve => setTimeout(resolve, delay));
      if (endpoint === '/carrier/config/listNotDeleted') {
        assert.equal(method, 'GET');
        assert.equal(query.pageSize, 20);
        return { code: 200, total: configs.length, rows: configs.slice((query.pageNum - 1) * 20, query.pageNum * 20) };
      }
      assert.equal(method, 'POST');
      assert.equal(endpoint, '/phone/config/list');
      assert.deepEqual(Object.keys(body).sort(), ['carrireIdentif', 'pageNum', 'pageSize']);
      assert.equal(body.pageSize, 500);
      assert.deepEqual(query, { pageNum: body.pageNum, pageSize: 500 });
      assert.ok(['C', 'T'].includes(body.carrireIdentif));
      const rows = body.carrireIdentif === 'C' ? C : T;
      const result = { code: 200, total: rows.length, rows: rows.slice((body.pageNum - 1) * 500, body.pageNum * 500) };
      return transform ? await transform(result, body) : result;
    } finally { stats.active -= 1; }
  };
  return { api, calls, stats };
}

test('deleted live collector stops before any browser or output writes', async () => {
  await assert.rejects(runCollector({ runId: RUN_ID, outputPath: 'offline.json', env: AUTH }), /入口已停用/);
});

test('authorization and exact run-owned page gates reject before any connection', async () => {
  let connections = 0;
  const createSession = async () => { connections++; throw new Error('must not connect'); };
  for (const key of Object.keys(AUTH)) {
    const env = { ...AUTH }; delete env[key];
    await assert.rejects(runCollector({ runId: RUN_ID, outputPath: 'offline.json', env, createSession }));
  }
  await assert.rejects(runCollector({ runId: 'other-run', outputPath: 'offline.json', env: AUTH, createSession }), /runId/);
  await assert.rejects(runCollector({ runId: RUN_ID, outputPath: '', env: AUTH, createSession }), /输出路径/);
  assert.equal(connections, 0);
  assert.doesNotThrow(() => requireAuthorization(RUN_ID, AUTH));
});

test('phone normalization accepts only canonical ten digits or eleven with country prefix one', () => {
  assert.equal(normalizePhone10('1234567890'), '1234567890');
  assert.equal(normalizePhone10('+1 (234) 567-8901'), '2345678901');
  assert.equal(normalizePhone10(12345678901), '2345678901');
  for (const bad of ['22345678901', '991234567890', '8912230200123456789F', 'abc1234567890', '', null, NaN, 1.5]) {
    assert.equal(normalizePhone10(bad), '', String(bad));
  }
});

test('all exact carrier pages use one global maximum of four reads and unique phone counts', async () => {
  const C = sourceRows(1502, 'C');
  C[1501].cardNo = `1${C[0].cardNo}`; // same phone, a distinct backend row across pages.
  const fake = fakeApi({ C, T: sourceRows(1001, 'T'), delay: 3 });
  const result = await collectRetentionPopulation(fake.api, { runId: RUN_ID });
  assert.equal(fake.stats.active, 0);
  assert.equal(fake.stats.maxActive, 4);
  assert.equal(result.readOnlyConcurrency.configuredConcurrency, 4);
  assert.equal(result.readOnlyConcurrency.maxActive, 4);
  assert.equal(result.readOnlyConcurrency.failed, 0);
  assert.equal(result.totalRawRowCount, 2503);
  assert.equal(result.totalUniquePhoneCount, 2502);
  assert.equal(result.totalDuplicatePhoneRowCount, 1);
  assert.equal(result.groups[0].pageCount, 4);
  assert.deepEqual(result.groups[0].pageProofs.map(row => row.rowCount), [500, 500, 500, 2]);
  assert.deepEqual(result.groups[1].pageProofs.map(row => row.reportedTotal), [1001, 1001, 1001]);
  assert.deepEqual(result.groups.map(row => row.uniquePhoneCount), [1501, 1001]);
  assert.deepEqual(result.groups.map(row => row.rawRowCount), [1502, 1001]);
  assert.ok(result.groups.every(group => group.rows.every(row => /^\d{10}$/.test(row.phone10) && row.carrierIdentif === group.carrierIdentif)));
  assert.equal(result.scope, 'exact_b14_c_t_all_pages');
  assert.equal(result.countingUnit, 'unique_phone10');
  assert.ok(Date.parse(result.completedAt) >= Date.parse(result.createdAt));
});

test('empty carriers still each have an independently checked empty first page', async () => {
  const fake = fakeApi();
  const result = await collectRetentionPopulation(fake.api, { runId: RUN_ID });
  assert.equal(result.totalUniquePhoneCount, 0);
  assert.equal(result.totalRawRowCount, 0);
  assert.equal(result.groups.length, 2);
  for (const group of result.groups) {
    assert.equal(group.pageCount, 1);
    assert.equal(group.firstPageTotal, 0);
    assert.equal(group.pageProofs[0].rowCount, 0);
    assert.deepEqual(group.rows, []);
  }
  assert.equal(fake.calls.filter(call => call.endpoint === '/phone/config/list').length, 2);
});

test('exact carrier definitions must each exist once and cannot share one identif', async () => {
  const variants = [
    [{ name: NAMES[0], carrireIdentif: 'C' }],
    [{ name: `${NAMES[0]}副本`, carrireIdentif: 'C' }, { name: NAMES[1], carrireIdentif: 'T' }],
    [{ name: NAMES[0], carrireIdentif: 'C' }, { name: NAMES[0], carrireIdentif: 'X' }, { name: NAMES[1], carrireIdentif: 'T' }],
    [{ name: NAMES[0], carrireIdentif: 'C' }, { name: NAMES[1], carrireIdentif: 'C' }],
    [{ name: NAMES[0], carrireIdentif: 'C', delData: true }, { name: NAMES[1], carrireIdentif: 'T' }],
  ];
  for (const configRows of variants) {
    const fake = fakeApi({ configRows });
    await assert.rejects(collectRetentionPopulation(fake.api, { runId: RUN_ID }));
    assert.equal(fake.calls.some(call => call.endpoint === '/phone/config/list'), false);
  }
});

test('population pages cannot change total or silently omit rows', async () => {
  for (const kind of ['changed-total', 'short-page']) {
    const fake = fakeApi({ C: sourceRows(501, 'C'), transform(result, body) {
      if (body.carrireIdentif === 'C' && body.pageNum === 2) {
        if (kind === 'changed-total') result.total += 1;
        else result.rows = [];
      }
      return result;
    } });
    await assert.rejects(collectRetentionPopulation(fake.api, { runId: RUN_ID }), /total 变化|页不完整/);
    assert.equal(fake.stats.active, 0);
  }
});

test('missing or invalid backend totals never mean zero', async () => {
  for (const total of [undefined, null, '', -1, 1.5, 'invalid', 500001, false, [], {}, ' ']) {
    const fake = fakeApi({ transform: response => ({ ...response, total }) });
    await assert.rejects(collectRetentionPopulation(fake.api, { runId: RUN_ID }), /rows\/total|total 无效/);
  }
});

test('every row must retain a backend id, valid phone and exact carrier identity', async () => {
  const variants = [row => { delete row.id; }, row => { row.id = {}; }, row => { row.id = 1.5; },
    row => { row.cardNo = 'invalid'; }, row => { row.carrireIdentif = 'OTHER'; },
    row => { delete row.carrireIdentif; }, row => { row.carrierName = '换号区C'; }];
  for (const mutate of variants) {
    const C = sourceRows(1, 'C'); mutate(C[0]);
    await assert.rejects(collectRetentionPopulation(fakeApi({ C }).api, { runId: RUN_ID }), /后台ID\/手机号|非精确目标运营商/);
  }
});

test('duplicate backend ids within or across carriers are rejected', async () => {
  for (const across of [false, true]) {
    const C = sourceRows(2, 'C'); const T = sourceRows(1, 'T');
    if (across) T[0].id = C[0].id; else C[1].id = C[0].id;
    await assert.rejects(collectRetentionPopulation(fakeApi({ C, T }).api, { runId: RUN_ID }), /重复后台ID/);
  }
});

test('the same normalized phone in both carrier zones is a failed snapshot, not a deduplicated success', async () => {
  const C = sourceRows(1, 'C'); const T = sourceRows(1, 'T'); T[0].cardNo = `1${C[0].cardNo}`;
  await assert.rejects(collectRetentionPopulation(fakeApi({ C, T }).api, { runId: RUN_ID }), /手机号跨区重叠/);
});

test('invalid parallelism is rejected without any API read or connection', async () => {
  for (const concurrency of [0, 5, 1.5, NaN]) {
    const fake = fakeApi();
    await assert.rejects(collectRetentionPopulation(fake.api, { runId: RUN_ID, concurrency }), /只读并发/);
    assert.equal(fake.calls.length, 0);
    await assert.rejects(runCollector({ runId: RUN_ID, outputPath: 'offline.json', env: { ...AUTH, OPENSMS_TASK_D_RETENTION_READ_WORKERS: String(concurrency) },
      createSession: async () => { assert.fail('must not connect'); } }), /只读并发/);
  }
});

test('success writes one authorized run-bound receipt then disconnects without browser mutation', async () => {
  const fake = fakeApi({ C: sourceRows(2, 'C'), T: sourceRows(1, 'T') });
  const events = [];
  const result = await runCollector({ runId: RUN_ID, outputPath: '/tmp/offline-retention-receipt.json', env: AUTH,
    createSession: async options => {
      assert.deepEqual(options, { cdp: AUTH.BITBROWSER_CDP, pathName: '/phoneConfig' });
      return { api: fake.api, browser: { close: async () => { assert.equal(fake.stats.active, 0); events.push('disconnect'); } } };
    }, writeReceipt: async (file, payload) => {
      events.push('write');
      assert.equal(file, '/tmp/offline-retention-receipt.json');
      assert.equal(payload.runId, RUN_ID);
      assert.equal(payload.readOnly, true);
      assert.equal(payload.silentBrowserMode, true);
      assert.equal(payload.browserForegroundActivationAttempted, false);
      assert.deepEqual(payload.authorization, { forceGate: true, enableGate: true, executeGate: true, runIdMatched: true });
      assert.deepEqual(payload.browserSession, { profile: '430', dedicatedTargetId: AUTH.OPENSMS_RAW_API_TARGET_ID });
    } });
  assert.deepEqual(events, ['write', 'disconnect']);
  assert.equal(result.totalUniquePhoneCount, 3);
});

test('first failure stops dispatch, drains in-flight reads before disconnect and writes no success receipt', async () => {
  const fake = fakeApi({ C: sourceRows(3001, 'C'), T: sourceRows(3001, 'T'),
    async transform(result, body) {
      if (body.pageNum > 1) {
        await new Promise(resolve => setTimeout(resolve, body.pageNum === 2 ? 1 : 20));
        if (body.carrireIdentif === 'C' && body.pageNum === 2) throw new Error('offline simulated read failure');
      }
      return result;
    } });
  let closed = 0;
  await assert.rejects(runCollector({ runId: RUN_ID, outputPath: 'offline.json', env: AUTH,
    createSession: async () => ({ api: fake.api, browser: { close: async () => { assert.equal(fake.stats.active, 0); closed++; } } }),
    writeReceipt: async () => { assert.fail('no partial result may be written as passed'); },
  }), /offline simulated read failure/);
  assert.equal(closed, 1);
  assert.equal(fake.stats.active, 0);
  assert.equal(fake.calls.filter(call => call.endpoint === '/phone/config/list' && call.body.pageNum > 1).length, 4);
});

test('receipt write failure still disconnects after read drain', async () => {
  const fake = fakeApi(); let closed = 0;
  await assert.rejects(runCollector({ runId: RUN_ID, outputPath: 'offline.json', env: AUTH,
    createSession: async () => ({ api: fake.api, browser: { close: async () => { assert.equal(fake.stats.active, 0); closed++; } } }),
    writeReceipt: async () => { throw new Error('offline output already exists'); },
  }), /output already exists/);
  assert.equal(closed, 1);
});
