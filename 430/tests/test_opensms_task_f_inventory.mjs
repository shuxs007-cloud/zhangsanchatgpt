import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { collect as productionCollect, collectWithEvidence as productionCollectWithEvidence, requireAuthorization, inventoryShowAbleFilter } from '../scripts/opensms_collect_task_f_kooda_cards.mjs';
// Simulated pagination failures must not spend production cooldown wall time.
const collect = (api, options = {}) => productionCollect(api, {sleep: async () => {}, ...options});
const collectWithEvidence = (api, output, options = {}) => productionCollectWithEvidence(api, output, {sleep: async () => {}, ...options});

test('report inventory includes off-shelf rows without expanding independent F', () => {
  assert.equal(inventoryShowAbleFilter({ OPENSMS_SYSTEM_KOODA_SNAPSHOT: '1' }), null);
  assert.equal(inventoryShowAbleFilter({ OPENSMS_TASK_F_POLICY_CATALOG_SYNC: '1' }), null);
  assert.equal(inventoryShowAbleFilter({ OPENSMS_FORCE_TASK_F: '1' }), 1);
  assert.equal(inventoryShowAbleFilter({}), 1);
  assert.equal(inventoryShowAbleFilter({ OPENSMS_SYSTEM_KOODA_SNAPSHOT: 'true' }), 1);
});

test('all report pages and serial retries retain all-state filter and off-shelf identity', async () => {
  const { api, state } = fakeApi(1001, { alter: (response, page, attempt) => {
    response.rows.forEach(row => { row.showAble = false; });
    if (attempt === 1 && page === 2) response.rows[0].id = 1;
  } });
  const result = await collect(async (...args) => {
    assert.equal(args[2].showAble, null);
    for (const name of ['cardNo', 'cardStore', 'status', 'cardStatus', 'userAble']) assert.equal(args[2][name], null);
    return api(...args);
  }, { showAble: null });
  assert.equal(result.total, 1001);
  assert.ok(result.rows.every(row => row.showAble === false));
  assert.equal(result.showAbleFilter, null);
  assert.equal(result.inventoryVisibilityScope, 'all-including-off-shelf');
  assert.equal(result.paginationAttemptCount, 2);
  assert.ok(result.paginationAttempts.every(attempt => attempt.showAbleFilter === null));
  assert.equal(state.active, 0);
});

test('independent F stays visible-only and invalid filters fail before requests', async () => {
  const { api, state } = fakeApi(0);
  const result = await collect(async (...args) => {
    assert.equal(args[2].showAble, 1);
    return api(...args);
  });
  assert.equal(result.inventoryVisibilityScope, 'visible-only');
  for (const showAble of [0, false, true, '1', 'all']) {
    await assert.rejects(collect(api, { showAble }));
  }
  assert.equal(state.calls.length, 1);
});

test('system snapshot read gate works independently of all task F gates', () => {
  assert.doesNotThrow(() => requireAuthorization({ OPENSMS_SYSTEM_KOODA_SNAPSHOT: '1' }));
  assert.throws(() => requireAuthorization({}), /授权门禁不足/);
  assert.throws(() => requireAuthorization({ OPENSMS_SYSTEM_KOODA_SNAPSHOT: 'true' }), /授权门禁不足/);
  assert.doesNotThrow(() => requireAuthorization({ OPENSMS_TASK_F_POLICY_CATALOG_SYNC: '1' }));
  assert.doesNotThrow(() => requireAuthorization({
    OPENSMS_FORCE_TASK_F: '1', OPENSMS_ENABLE_TASK_F: '1', OPENSMS_EXECUTE_TASK_F: '1',
  }));
  assert.throws(() => requireAuthorization({ OPENSMS_FORCE_TASK_F: '1' }), /授权门禁不足/);
});

function fakeApi(total, { alter = null, failPage = null, delay = page => page % 2 ? 8 : 2 } = {}) {
  const state = { active: 0, maxActive: 0, calls: [], completed: [], attemptCount: 0, attempts: [] };
  const api = async (method, endpoint, body, query) => {
    assert.equal(method, 'POST');
    assert.equal(endpoint, '/phone/config/list');
    assert.equal(body.opr, 'koo');
    assert.equal(body.pageSize, 500);
    assert.equal(body.pageNum, query.pageNum);
    const page = body.pageNum;
    if (page === 1) {
      state.attemptCount++;
      state.attempts.push({ activeBeforeFirstPage: state.active, maxActive: 0, calls: [], completed: [] });
    }
    const attempt = state.attemptCount;
    const attemptState = state.attempts[attempt - 1];
    const pageTotal = typeof total === 'function' ? total(attempt) : total;
    attemptState.calls.push(page);
    state.calls.push(page);
    state.active++;
    state.maxActive = Math.max(state.maxActive, state.active);
    attemptState.maxActive = Math.max(attemptState.maxActive, state.active);
    try {
      await new Promise(resolve => setTimeout(resolve, delay(page, attempt)));
      if (page === failPage) throw new Error('mock page failure: token=DO_NOT_PERSIST');
      const start = (page - 1) * 500;
      const rows = Array.from({ length: Math.min(500, Math.max(0, pageTotal - start)) }, (_, i) => ({ id: start + i + 1 }));
      const response = { code: 200, total: pageTotal, rows };
      if (alter) alter(response, page, attempt);
      return response;
    } finally {
      state.active--;
      state.completed.push(page);
      attemptState.completed.push(page);
    }
  };
  return { api, state };
}

test('first page establishes total; bounded pages merge deterministically', async () => {
  const { api, state } = fakeApi(3104);
  const result = await collect(api, { workers: 4 });
  assert.equal(result.total, 3104);
  assert.equal(result.pageCount, 7);
  assert.equal(state.maxActive, 4);
  assert.equal(state.calls[0], 1);
  assert.deepEqual(result.rows.map(row => row.id), Array.from({ length: 3104 }, (_, i) => i + 1));
  assert.equal(result.paginationEffectiveWorkers, 4);
  assert.equal(result.paginationAttemptCount, 1);
  assert.equal(result.paginationRecovered, false);
  assert.equal(result.paginationTransactionalSnapshotGuaranteed, false);
});

test('empty inventory makes exactly one request and no worker pool', async () => {
  const { api, state } = fakeApi(0);
  const result = await collect(api);
  assert.deepEqual(result.rows, []);
  assert.deepEqual(state.calls, [1]);
  assert.equal(result.paginationAttemptCount, 1);
  assert.equal(result.paginationAttempts[0].uniqueBackendIdCount, 0);
});

test('partial pages, total drift, and duplicate backend identities fail', async () => {
  for (const alter of [
    (response, page) => { if (page === 2) response.rows.pop(); },
    (response, page) => { if (page === 2) response.total++; },
    (response, page) => { if (page === 2) response.rows[0].id = 1; },
    (response, page) => { if (page === 2) delete response.rows[0].id; },
  ]) {
    const { api, state } = fakeApi(1501, { alter });
    await assert.rejects(collect(api));
    assert.equal(state.active, 0);
  }
});

test('a failing page drains outstanding requests and stops issuing new pages', async () => {
  const { api, state } = fakeApi(6000, { failPage: 2 });
  await assert.rejects(collect(api, { workers: 2 }), /读取请求失败/);
  assert.equal(state.active, 0);
  assert.ok(state.calls.length <= 3);
  assert.equal(state.attemptCount, 1);
});

test('invalid worker count never reaches backend', async () => {
  const { api, state } = fakeApi(0);
  await assert.rejects(collect(api, { workers: 0 }));
  assert.deepEqual(state.calls, []);
});

test('system dates and raw ICCID survive concurrent collection without extra requests', async () => {
  const source = {
    cardStore: '8912230200278240607F', opr: 'Kooda', cardNo: '17052749359',
    slotIdentif: '20.01', chatrActivateDate: '2026-08-24',
    chatrPlanExpire: '2026-10-01', extraSourceField: { keep: true },
  };
  const { api, state } = fakeApi(2001, {
    alter: response => response.rows.forEach(row => Object.assign(row, source)),
  });
  const result = await collect(api, { workers: 4 });
  assert.deepEqual(state.calls.sort((a, b) => a - b), [1, 2, 3, 4, 5]);
  assert.equal(state.maxActive, 4);
  for (const row of result.rows) {
    for (const [key, value] of Object.entries(source)) assert.deepEqual(row[key], value);
  }
});

test('one duplicate triggers a complete serial reread with fresh totals and rows', async () => {
  const recorded = [];
  const { api, state } = fakeApi(attempt => attempt === 1 ? 1501 : 2001, {
    alter: (response, page, attempt) => {
      response.rows.forEach(row => { row.sourceAttempt = attempt; });
      if (attempt === 1 && page === 2) response.rows[0].id = 1;
    },
  });
  const result = await collect(api, { workers: 4, onAttempt: attempt => recorded.push(attempt) });
  assert.equal(result.total, 2001);
  assert.equal(result.rows.length, 2001);
  assert.equal(new Set(result.rows.map(row => row.id)).size, 2001);
  assert.ok(result.rows.every(row => row.sourceAttempt === 2));
  assert.equal(result.paginationAttemptCount, 2);
  assert.equal(result.paginationRecovered, true);
  assert.equal(result.paginationEffectiveWorkers, 1);
  assert.equal(result.paginationTransactionalSnapshotGuaranteed, false);
  assert.deepEqual(state.attempts[0].calls.sort((a, b) => a - b), [1, 2, 3, 4]);
  assert.deepEqual(state.attempts[1].calls, [1, 2, 3, 4, 5]);
  assert.equal(state.attempts[1].maxActive, 1);
  assert.deepEqual(recorded[0].duplicates, [{
    backendId: '1', first: { pageNum: 1, rowNumber: 1 }, duplicate: { pageNum: 2, rowNumber: 1 },
  }]);
  assert.equal(recorded[0].failure.code, 'duplicate-backend-id');
  assert.equal(recorded[0].pages[1].backendIds.length, 500);
  assert.equal(recorded[1].uniqueBackendIdCount, 2001);
  for (const attempt of recorded) {
    assert.ok(attempt.startedAt && attempt.completedAt);
    for (const page of attempt.pages) assert.ok(page.startedAt && page.completedAt);
  }
});

test('total drift and incomplete pages recover only by starting again at page one', async () => {
  for (const kind of ['total-drift', 'incomplete-page']) {
    const { api, state } = fakeApi(attempt => attempt === 1 ? 2001 : 1001, {
      alter: (response, page, attempt) => {
        if (attempt !== 1 || page !== 2) return;
        if (kind === 'total-drift') response.total++;
        else response.rows.pop();
      },
    });
    const result = await collect(api);
    assert.equal(result.total, 1001);
    assert.equal(result.rows.length, 1001);
    assert.equal(result.paginationAttemptCount, 2);
    assert.equal(result.paginationAttempts[0].failure.code, kind);
    assert.equal(result.paginationAttempts[1].total, 1001);
    assert.deepEqual(state.attempts[1].calls, [1, 2, 3]);
    assert.ok(state.attempts.every(attempt => attempt.activeBeforeFirstPage === 0));
  }
});

test('persistent consistency failures stop after three independent attempts', async () => {
  const { api, state } = fakeApi(2001, {
    alter: (response, page) => { if (page === 2) response.rows[0].id = 1; },
  });
  await assert.rejects(collect(api, { workers: 4 }), error => {
    assert.equal(error.code, 'duplicate-backend-id');
    assert.equal(error.paginationAttemptCount, 3);
    assert.deepEqual(error.paginationAttempts.map(attempt => attempt.requestedWorkers), [4, 1, 1]);
    assert.ok(error.paginationAttempts.every(attempt => attempt.status === 'failed'));
    return true;
  });
  assert.equal(state.attemptCount, 3);
  assert.equal(state.attempts[0].maxActive, 4);
  for (const attempt of state.attempts.slice(1)) {
    assert.equal(attempt.maxActive, 1);
    assert.deepEqual(attempt.calls, [1, 2, 3, 4, 5]);
  }
  assert.equal(state.active, 0);
});

test('the third complete read may succeed after two failed attempts', async () => {
  const { api, state } = fakeApi(501, {
    alter: (response, page, attempt) => { if (attempt < 3 && page === 2) response.rows[0].id = 1; },
  });
  const result = await collect(api);
  assert.equal(result.paginationAttemptCount, 3);
  assert.equal(result.paginationRecovered, true);
  assert.equal(result.rows.length, 501);
  assert.equal(state.attemptCount, 3);
});

test('drain barrier also precedes evidence callbacks and recovery after a fast failure', async () => {
  const recorded = [];
  const { api, state } = fakeApi(3001, {
    delay: (page, attempt) => attempt === 1 && page === 3 ? 35 : 1,
    alter: (response, page, attempt) => { if (attempt === 1 && page === 2) response.total++; },
  });
  const result = await collect(api, {
    workers: 2,
    onAttempt: attempt => {
      assert.equal(state.active, 0);
      recorded.push(attempt);
      assert.equal(state.attempts[attempt.attemptNumber - 1].completed.length, attempt.pages.length);
    },
  });
  assert.equal(result.paginationRecovered, true);
  assert.deepEqual(state.attempts[0].calls, [1, 2, 3]);
  assert.equal(state.attempts[1].activeBeforeFirstPage, 0);
  assert.deepEqual(recorded[0].pages.map(page => page.pageNum), [1, 2, 3]);
});

test('fatal in-flight request failure wins over earlier total drift', async () => {
  const { api, state } = fakeApi(3001, {
    failPage: 3,
    delay: page => page === 3 ? 25 : 1,
    alter: (response, page) => { if (page === 2) response.total++; },
  });
  await assert.rejects(collect(api, { workers: 2 }), error => {
    assert.equal(error.code, 'request-failed');
    assert.equal(error.paginationAttemptCount, 1);
    assert.equal(error.paginationAttempts[0].pages[1].failure.code, 'total-drift');
    assert.equal(error.paginationAttempts[0].pages[2].failure.code, 'request-failed');
    assert.ok(!JSON.stringify(error.paginationAttempts).includes('DO_NOT_PERSIST'));
    return true;
  });
  assert.equal(state.active, 0);
  assert.equal(state.attemptCount, 1);
});

test('missing IDs, unsuccessful responses, and malformed responses are never retried', async () => {
  const cases = [
    ['missing-backend-id', response => { delete response.rows[0].id; }],
    ['missing-backend-id', response => { response.rows[0].id = {}; }],
    ['api-response-failed', response => { response.code = 401; response.token = 'DO_NOT_PERSIST'; }],
    ['malformed-response', response => { delete response.rows; }],
    ['malformed-response', response => { delete response.total; }],
    ['invalid-total', response => { response.total = true; }],
    ['invalid-total', response => { response.total = ''; }],
    ['invalid-total', response => { response.total = -1; }],
    ['invalid-total', response => { response.total = 0.5; }],
  ];
  for (const [code, alter] of cases) {
    const { api, state } = fakeApi(501, { alter });
    await assert.rejects(collect(api), error => {
      assert.equal(error.code, code);
      assert.equal(error.paginationAttemptCount, 1);
      assert.ok(!JSON.stringify(error.paginationAttempts).includes('DO_NOT_PERSIST'));
      return true;
    });
    assert.deepEqual(state.calls, [1]);
  }
});

test('missing ID on a later page fails without retry and retains its exact position', async () => {
  const { api, state } = fakeApi(1001, {
    alter: (response, page) => { if (page === 2) delete response.rows[31].id; },
  });
  await assert.rejects(collect(api), error => {
    assert.equal(error.code, 'missing-backend-id');
    assert.equal(error.paginationAttemptCount, 1);
    assert.equal(error.details.pageNum, 2);
    assert.equal(error.details.rowNumber, 32);
    return true;
  });
  assert.equal(state.active, 0);
  assert.equal(state.attemptCount, 1);
});

test('evidence callback failure is fatal and prevents any recovery attempt', async () => {
  const { api, state } = fakeApi(1001, {
    alter: (response, page) => { if (page === 2) response.rows[0].id = 1; },
  });
  await assert.rejects(collect(api, {
    onAttempt: () => { throw new Error('mock evidence failure token=DO_NOT_PERSIST'); },
  }), error => {
    assert.equal(error.code, 'evidence-write-failed');
    assert.equal(error.paginationAttemptCount, 1);
    return true;
  });
  assert.equal(state.attemptCount, 1);
  assert.equal(state.active, 0);
});

async function temporaryOutput(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'opensms-pagination-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return path.join(directory, 'inventory.json');
}

test('successful recovery persists independent attempt files and a verifiable summary', async t => {
  const outputPath = await temporaryOutput(t);
  const { api } = fakeApi(501, {
    alter: (response, page, attempt) => { if (attempt === 1 && page === 2) response.rows[0].id = 1; },
  });
  const result = await collectWithEvidence(api, outputPath);
  assert.equal(result.paginationEvidencePath, `${outputPath}.pagination.json`);
  const content = await fs.readFile(result.paginationEvidencePath, 'utf8');
  assert.equal(createHash('sha256').update(content).digest('hex'), result.paginationEvidenceSha256);
  const summary = JSON.parse(content);
  assert.equal(summary.status, 'passed');
  assert.equal(summary.runId, process.env.OPENSMS_RUN_ID || null);
  assert.equal(summary.attemptCount, 2);
  assert.equal(summary.recovered, true);
  assert.equal(summary.paginationTransactionalSnapshotGuaranteed, false);
  assert.equal(summary.attemptEvidencePaths.length, 2);
  for (const [index, attemptPath] of summary.attemptEvidencePaths.entries()) {
    assert.deepEqual(JSON.parse(await fs.readFile(attemptPath, 'utf8')), summary.attempts[index]);
  }
  await assert.rejects(fs.stat(outputPath), { code: 'ENOENT' });
});

test('exhausted attempts still persist all failure evidence and the summary', async t => {
  const outputPath = await temporaryOutput(t);
  const { api } = fakeApi(501, {
    alter: (response, page) => { if (page === 2) response.rows[0].id = 1; },
  });
  await assert.rejects(collectWithEvidence(api, outputPath), error => {
    assert.equal(error.code, 'duplicate-backend-id');
    assert.equal(error.paginationEvidencePath, `${outputPath}.pagination.json`);
    assert.equal(error.paginationAttemptCount, 3);
    assert.equal(error.paginationRecovered, false);
    assert.match(error.paginationEvidenceSha256, /^[0-9a-f]{64}$/);
    return true;
  });
  const summary = JSON.parse(await fs.readFile(`${outputPath}.pagination.json`, 'utf8'));
  assert.equal(summary.status, 'failed');
  assert.equal(summary.attemptCount, 3);
  assert.equal(summary.recovered, false);
  assert.equal(summary.attemptEvidencePaths.length, 3);
  assert.ok(summary.attempts.every(attempt => attempt.failure.code === 'duplicate-backend-id'));
});

test('historical summary evidence is never overwritten and prevents backend reads', async t => {
  const outputPath = await temporaryOutput(t);
  const evidencePath = `${outputPath}.pagination.json`;
  await fs.writeFile(evidencePath, 'historical evidence', { flag: 'wx' });
  const { api, state } = fakeApi(501);
  await assert.rejects(collectWithEvidence(api, outputPath), { code: 'EEXIST' });
  assert.deepEqual(state.calls, []);
  assert.equal(await fs.readFile(evidencePath, 'utf8'), 'historical evidence');
});

test('attempt evidence write collision fails closed and preserves the old attempt file', async t => {
  const outputPath = await temporaryOutput(t);
  const attemptPath = `${outputPath}.pagination.json.attempt-1.json`;
  await fs.writeFile(attemptPath, 'historical attempt', { flag: 'wx' });
  const { api, state } = fakeApi(501, {
    alter: (response, page) => { if (page === 2) response.rows[0].id = 1; },
  });
  await assert.rejects(collectWithEvidence(api, outputPath), error => {
    assert.equal(error.code, 'evidence-write-failed');
    assert.equal(error.paginationAttemptCount, 1);
    return true;
  });
  assert.equal(state.attemptCount, 1);
  assert.equal(await fs.readFile(attemptPath, 'utf8'), 'historical attempt');
  const summary = JSON.parse(await fs.readFile(`${outputPath}.pagination.json`, 'utf8'));
  assert.equal(summary.status, 'failed');
  assert.equal(summary.failure.code, 'evidence-write-failed');
  assert.deepEqual(summary.attemptEvidencePaths, []);
  assert.equal(summary.attempts.length, 1);
});

function movingInventoryApi(firstPageSamples, { alter = null } = {}) {
  const calls = [];
  let firstPageRead = 0;
  let current;
  const api = async (method, endpoint, body) => {
    assert.equal(method, 'POST');
    assert.equal(endpoint, '/phone/config/list');
    assert.equal(body.opr, 'koo');
    assert.equal(body.pageSize, 500);
    assert.equal(body.showAble, null);
    if (body.pageNum === 1) {
      current = firstPageSamples[Math.min(firstPageRead++, firstPageSamples.length - 1)];
    }
    calls.push(body.pageNum);
    const start = (body.pageNum - 1) * 500;
    const response = {
      code: 200, total: current.total,
      rows: Array.from({length: Math.min(500, Math.max(0, current.total - start))}, (_, i) => ({
        id: (current.offset || 0) + start + i + 1, sourceSample: firstPageRead,
      })),
    };
    if (alter) alter(response, body.pageNum, firstPageRead);
    return response;
  };
  return { api, calls };
}

test('coverage reread waits for matching total and IDs then reuses only the last first page', async () => {
  const {api, calls} = movingInventoryApi([{total: 1501}, {total: 1001}, {total: 1001}]);
  const waits = [];
  const result = await collect(api, {
    workers: 1, showAble: null, stabilizeBeforeFirstRead: true,
    sleep: async ms => waits.push(ms),
  });
  assert.deepEqual(calls, [1, 1, 1, 2, 3]);
  assert.deepEqual(waits, [10000, 10000]);
  assert.equal(result.total, 1001);
  assert.ok(result.rows.every(row => row.sourceSample === 3));
  assert.equal(result.paginationAttemptCount, 1);
  assert.equal(result.paginationStabilityProbeCount, 3);
  const attempt = result.paginationAttempts[0];
  assert.equal(attempt.stabilityCheck.status, 'passed');
  assert.equal(attempt.receivedRowCount, 1001);
  assert.equal(attempt.uniqueBackendIdCount, 1001);
  assert.deepEqual(attempt.pages.map(page => page.pageNum), [1, 2, 3]);
  assert.deepEqual(attempt.duplicates, []);
});

test('unchanged totals with changing first-page identities do not count as stable', async () => {
  const {api, calls} = movingInventoryApi([
    {total: 501, offset: 0}, {total: 501, offset: 1000}, {total: 501, offset: 1000},
  ]);
  const result = await collect(api, {showAble: null, workers: 1, stabilizeBeforeFirstRead: true});
  assert.deepEqual(calls, [1, 1, 1, 2]);
  assert.equal(result.rows[0].id, 1001);
  assert.equal(result.rows.at(-1).id, 1501);
  assert.equal(result.paginationStabilityProbeCount, 3);
});

test('persistent movement exhausts bounded probes and three attempts without partial inventory', async () => {
  const {api, calls} = movingInventoryApi(Array.from({length: 18}, (_, i) => ({total: 1001 + i})));
  const waits = [];
  await assert.rejects(collect(api, {
    showAble: null, stabilizeBeforeFirstRead: true, stabilizeOnConsistencyFailure: true,
    sleep: async ms => waits.push(ms),
  }), error => {
    assert.equal(error.code, 'inventory-not-stable');
    assert.equal(error.paginationAttemptCount, 3);
    assert.ok(error.paginationAttempts.every(attempt =>
      attempt.stabilityProbes.length === 6 && attempt.pages.length === 0
      && attempt.receivedRowCount === 0 && attempt.stabilityCheck.status === 'not_stable'));
    return true;
  });
  assert.equal(calls.length, 18);
  assert.ok(calls.every(page => page === 1));
  assert.equal(waits.filter(ms => ms === 10000).length, 15);
  assert.deepEqual(waits.filter(ms => ms !== 10000), [30000, 60000]);
});

test('stability observation never replaces full-page total and uniqueness verification', async () => {
  for (const kind of ['total-drift', 'duplicate-backend-id', 'incomplete-page']) {
    const {api} = movingInventoryApi([{total: 501}], {alter: (response, page) => {
      if (page !== 2) return;
      if (kind === 'total-drift') response.total++;
      if (kind === 'duplicate-backend-id') response.rows[0].id = 1;
      if (kind === 'incomplete-page') response.rows.pop();
    }});
    await assert.rejects(collect(api, {
      showAble: null, stabilizeBeforeFirstRead: true, stabilizeOnConsistencyFailure: true,
    }), error => {
      assert.equal(error.code, kind);
      assert.equal(error.paginationAttemptCount, 3);
      assert.ok(error.paginationAttempts.every(attempt => attempt.stabilityCheck.status === 'passed'));
      return true;
    });
  }
});

test('normal successful reads add no stability requests; a failed full read gets fresh probes', async () => {
  const stable = movingInventoryApi([{total: 501}]);
  await collect(stable.api, {showAble: null, stabilizeOnConsistencyFailure: true});
  assert.deepEqual(stable.calls, [1, 2]);
  const moving = movingInventoryApi([{total: 1001}, {total: 501}, {total: 501}], {
    alter: (response, page, sample) => {if (sample === 1 && page === 2) response.total++;},
  });
  const result = await collect(moving.api, {showAble: null, workers: 1, stabilizeOnConsistencyFailure: true});
  assert.deepEqual(moving.calls, [1, 2, 1, 1, 2]);
  assert.equal(result.paginationAttemptCount, 2);
  assert.equal(result.paginationAttempts[0].failure.code, 'total-drift');
  assert.equal(result.paginationAttempts[1].requestedWorkers, 1);
  assert.equal(result.total, 501);
  assert.ok(result.rows.every(row => row.sourceSample === 3));
});

test('authentication, malformed responses, missing IDs and evidence collisions stop probes', async t => {
  for (const alter of [response => {response.code = 401;}, response => {delete response.rows;},
    response => {delete response.rows[0].id;}]) {
    const {api, calls} = movingInventoryApi([{total: 501}], {alter});
    await assert.rejects(collect(api, {showAble: null, stabilizeBeforeFirstRead: true}), error => {
      assert.equal(error.paginationAttemptCount, 1);
      assert.equal(error.retryable, false);
      return true;
    });
    assert.deepEqual(calls, [1]);
  }
  const outputPath = await temporaryOutput(t);
  await fs.writeFile(`${outputPath}.pagination.json`, 'history', {flag: 'wx'});
  const {api, calls} = movingInventoryApi([{total: 501}]);
  await assert.rejects(collectWithEvidence(api, outputPath, {showAble: null, stabilizeBeforeFirstRead: true}), {code: 'EEXIST'});
  assert.deepEqual(calls, []);
});

test('probe evidence is independently archived and hash-bound without duplicating inventory counts', async t => {
  const outputPath = await temporaryOutput(t);
  const {api} = movingInventoryApi([{total: 501}, {total: 501}]);
  const result = await collectWithEvidence(api, outputPath, {showAble: null, stabilizeBeforeFirstRead: true});
  const content = await fs.readFile(result.paginationEvidencePath, 'utf8');
  assert.equal(createHash('sha256').update(content).digest('hex'), result.paginationEvidenceSha256);
  const summary = JSON.parse(content);
  const persisted = JSON.parse(await fs.readFile(summary.attemptEvidencePaths[0], 'utf8'));
  assert.deepEqual(persisted, summary.attempts[0]);
  assert.equal(persisted.stabilityProbes.length, 2);
  assert.equal(persisted.receivedRowCount, 501);
  assert.equal(persisted.uniqueBackendIdCount, 501);
  assert.equal(result.paginationTransactionalSnapshotGuaranteed, false);
});
