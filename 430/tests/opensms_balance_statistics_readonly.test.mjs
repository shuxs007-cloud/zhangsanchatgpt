import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import vm from 'node:vm';
import { setTimeout as delay } from 'node:timers/promises';
import test from 'node:test';
import { collectBalanceStatisticsReadOnly } from '../scripts/opensms_balance_statistics_readonly.mjs';

const GROUPS = ['A1', 'A12', 'A11', 'B1', 'B11', 'B12', 'C2', 'A3', 'B3', 'B1V', 'B11V', 'B12V'];
const NOW = new Date('2026-09-07T15:59:59.999Z');
const RECORDS = GROUPS.map((group, index) => ({ group, name: `${group}-fixture`, identif: `id-${index}` }));

function fixture({ waits = [12, 4, 8, 2, 10, 3, 6, 1, 5, 7, 9, 11], fail } = {}) {
  const called = [];
  const finished = [];
  let active = 0;
  let maxActive = 0;
  return {
    called,
    finished,
    get active() { return active; },
    get maxActive() { return maxActive; },
    async api(method, endpoint, data, query) {
      assert.equal(method, 'GET');
      assert.equal(endpoint, '/admin/statisticsList');
      assert.equal(data, null);
      const index = RECORDS.findIndex(item => item.identif === query.carrireIdentif);
      assert.ok(index >= 0);
      assert.deepEqual(query, {
        pageNum: 1, pageSize: 40, userId: -1, proxyUserId: -1, phoneId: -1,
        startTime: '2026-08-07 00:00:00', endTime: '2026-09-07 23:59:59',
        carrireIdentif: RECORDS[index].identif, dayliModel: -1,
      });
      called.push(index);
      active += 1;
      maxActive = Math.max(maxActive, active);
      try {
        await delay(waits[index]);
        if (fail && index === 0) return fail();
        return { code: 200, data: { data: { content: [
          { days: '2026-09-07', firstReplyRate: `${index + 1}.25%`, sendSize: 1000 + index },
          { days: '2026-09-06', firstReplyRate: '99%', sendSize: 9000 },
        ] } } };
      } finally {
        active -= 1;
        finished.push(index);
      }
    },
  };
}

test('statistics reads keep all twelve groups in order and preserve raw values at at most four requests', async () => {
  const mock = fixture();
  const result = await collectBalanceStatisticsReadOnly(mock.api, RECORDS, { now: NOW });
  assert.deepEqual(result.replyRates.map(item => item.group), GROUPS);
  assert.deepEqual(result.replyRates.map(item => item.replyRatePct), GROUPS.map((_group, index) => index + 1.25));
  assert.ok(result.replyRates.every(item => item.status === 'ok' && item.requestDispatched && !item.cancelledBeforeDispatch));
  assert.equal(result.replyRates[6].dailyRow.sendSize, 1006);
  assert.equal(result.replyRates[6].replyRateRaw, '7.25%');
  assert.equal(result.performance.configuredConcurrency, 4);
  assert.equal(result.performance.maxActive, 4);
  assert.equal(result.performance.completed, GROUPS.length);
  assert.equal(result.performance.failed, 0);
  assert.equal(mock.active, 0);
  assert.equal(mock.maxActive, 4);
  assert.equal(mock.finished.length, GROUPS.length);
});

test('empty groups never dispatch requests and invalid limits reject before API access', async () => {
  const api = () => assert.fail('must remain offline and undispatched');
  const result = await collectBalanceStatisticsReadOnly(api, [], { now: NOW });
  assert.deepEqual(result.replyRates, []);
  assert.equal(result.performance.submitted, 0);
  assert.equal(result.performance.maxActive, 0);
  for (const concurrency of [0, -1, 1.5, 5, 32, NaN, '4']) {
    await assert.rejects(collectBalanceStatisticsReadOnly(api, RECORDS, { concurrency, now: NOW }), /1至4/);
  }
});

test('transport and invalid-result failures stop queued queries, keep sibling successes, and drain before returning', async () => {
  for (const fail of [
    () => { throw new Error('transport_failed'); },
    () => ({ code: 500, message: 'business_failed' }),
    () => ({ code: 200, data: { content: [] } }),
    () => ({ code: 200, data: { content: [
      { days: '2026-09-07', firstReplyRate: '1%' },
      { days: '2026-09-07', firstReplyRate: '2%' },
    ] } }),
    () => ({ code: 200, data: { content: [{ days: '2026-09-07', firstReplyRate: 'invalid' }] } }),
  ]) {
    const mock = fixture({ waits: [1, ...Array(GROUPS.length - 1).fill(15)], fail });
    const result = await collectBalanceStatisticsReadOnly(mock.api, RECORDS, { now: NOW });
    assert.deepEqual(mock.called, [0, 1, 2, 3]);
    assert.equal(mock.active, 0);
    assert.equal(mock.finished.length, 4);
    assert.equal(result.replyRates.length, GROUPS.length);
    assert.equal(result.replyRates[0].status, 'error');
    assert.equal(result.replyRates[0].requestDispatched, true);
    assert.ok(result.replyRates.slice(1, 4).every(item => item.status === 'ok'));
    assert.ok(result.replyRates.slice(4).every(item => item.status === 'error'
      && item.statDate === null && item.replyRatePct === null
      && item.cancelledBeforeDispatch && !item.requestDispatched
      && item.reason.includes('未派发')));
    assert.equal(result.performance.failed, 1);
    assert.equal(result.performance.completed, 3);
    assert.equal(result.performance.cancelled, GROUPS.length - 4);
  }
});

// Run the real entry body with every external capability replaced in a VM.
// Its protocol client is never imported and no browser or filesystem is opened.
async function runEntryOffline(carrierRows, mock) {
  const source = await fs.readFile(new URL('../scripts/opensms_balance_readonly_raw.mjs', import.meta.url), 'utf8');
  const body = source.replace(/^import .*;\r?\n/gm, '');
  assert.doesNotMatch(body, /^import /m);
  let output;
  let closed = false;
  const isolatedProcess = {
    env: { OPENSMS_EXACT_CDP_ONLY: '1', OPENSMS_FORCE_RAW_CDP: '1', BITBROWSER_CDP: 'http://127.0.0.1:1' },
    exitCode: undefined,
    exit: code => assert.fail(`unexpected fatal exit ${code}`),
  };
  await vm.runInNewContext(body, {
    fs: { writeFile: async (_file, text, options) => {
      assert.equal(options.flag, 'wx');
      assert.equal(mock.active, 0);
      output = JSON.parse(text);
    } },
    path,
    SNAPSHOT_DIR: '/offline-fixture-no-files-created',
    ensureArtifactDirs: async () => {},
    getCarrierConfigs: async () => carrierRows,
    createOpenSmsApiSession: async options => {
      assert.equal(options.cdp, 'http://127.0.0.1:1');
      assert.equal(options.pathName, '/day');
      return { api: mock.api, apiTransport: 'offline_fixture', browser: { close: async () => {
        assert.equal(mock.active, 0);
        closed = true;
      } } };
    },
    collectBalanceStatisticsReadOnly: (api, records, options) => collectBalanceStatisticsReadOnly(api, records, { ...options, now: NOW }),
    process: isolatedProcess,
    console: { log: () => {}, error: text => assert.fail(text) },
  });
  assert.equal(closed, true);
  return { output, exitCode: isolatedProcess.exitCode };
}

test('entry keeps missing and ambiguous carrier errors in all twelve positions without treating them as zero', async () => {
  const rows = RECORDS.map((item, index) => ({ name: item.name, identif: item.identif, dailyRemaining: 25 + index, dailyTotal: 100 }));
  rows.splice(1, 1);
  rows.push({ ...rows[1], identif: 'ambiguous-id' });
  const mock = fixture();
  const { output, exitCode } = await runEntryOffline(rows, mock);
  assert.equal(exitCode, 2);
  assert.deepEqual(output.requiredGroups, GROUPS);
  assert.deepEqual(output.replyRates.map(item => item.group), GROUPS);
  assert.deepEqual(output.missingRequiredGroups, ['A12', 'A11']);
  assert.equal(output.records.length, GROUPS.length - 2);
  assert.equal(output.replyRates[1].carrierMatchCount, 0);
  assert.equal(output.replyRates[2].carrierMatchCount, 2);
  assert.equal(output.replyRates[1].replyRatePct, null);
  assert.equal(output.replyRates[2].replyRatePct, null);
  // Ambiguous group aliases remain unknown in the twelve-group view, while
  // the all-carrier view can independently query each unique backend ID.
  assert.deepEqual(mock.called, [0, 3, 4, 5, 6, 7, 8, 9, 10, 11, 2]);
  assert.equal(output.systemIssues.length, 2);
  assert.equal(output.c2LatestDailySendSize, 1006);
});

test('entry persists partial errors and actual balances, then closes the drained session with the existing exit code', async () => {
  const rows = RECORDS.map(item => ({ name: item.name, identif: item.identif, dailyRemaining: 25, dailyTotal: 100 }));
  const mock = fixture({ waits: [1, ...Array(GROUPS.length - 1).fill(15)], fail: () => { throw new Error('transport_failed'); } });
  const { output, exitCode } = await runEntryOffline(rows, mock);
  assert.equal(exitCode, 2);
  assert.equal(output.records.length, GROUPS.length);
  assert.ok(output.records.every(item => item.dailyRemaining === 25 && item.percentRemaining === 25));
  assert.equal(output.systemIssues.length, GROUPS.length - 3);
  assert.equal(output.replyRates.filter(item => item.status === 'ok').length, 3);
  assert.equal(output.performance.statisticsRead.cancelled, GROUPS.length - 4);
  assert.equal(output.c2LatestDailyDate, null);
  assert.equal(output.c2LatestDailySendSize, null);
});

test('offline fixed-input benchmark reports serial and four-way timing with identical output', async t => {
  const waits = Array(GROUPS.length).fill(20);
  const serial = await collectBalanceStatisticsReadOnly(fixture({ waits }).api, RECORDS, { concurrency: 1, now: NOW });
  const parallel = await collectBalanceStatisticsReadOnly(fixture({ waits }).api, RECORDS, { concurrency: 4, now: NOW });
  assert.deepEqual(parallel.replyRates, serial.replyRates);
  assert.equal(serial.performance.maxActive, 1);
  assert.equal(parallel.performance.maxActive, 4);
  const speedup = serial.performance.durationSeconds / parallel.performance.durationSeconds;
  // Timer-based evidence is reported, not a fragile machine-load pass criterion.
  t.diagnostic(JSON.stringify({
    mode: 'offline_mock_only', groups: GROUPS.length, simulatedRequestDelayMs: 20,
    serialSeconds: serial.performance.durationSeconds,
    concurrentSeconds: parallel.performance.durationSeconds,
    serialMaxActive: serial.performance.maxActive,
    concurrentMaxActive: parallel.performance.maxActive,
    speedup, outputsEqual: true, livePipelineMeasured: false,
  }));
});

test('B3 uses exact carrier identity and the existing strict low20 and low10 thresholds', async () => {
  for (const [remaining, low20, low10] of [[20, false, false], [19, true, false], [10, true, false], [9, true, true], [0, true, true]]) {
    const rows = RECORDS.map(item => ({ ...item, dailyRemaining: item.group === 'B3' ? remaining : 50, dailyTotal: 100 }));
    for (const name of ['B31', 'B32-fixture', 'B33 fixture', 'B3V-fixture']) {
      rows.push({ name, identif: `excluded-${name}`, dailyRemaining: 0, dailyTotal: 100 });
    }
    const { output, exitCode } = await runEntryOffline(rows, fixture());
    assert.equal(exitCode, undefined);
    assert.deepEqual(output.requiredGroups, GROUPS);
    assert.deepEqual(output.alertGroups, GROUPS);
    assert.equal(output.records.find(item => item.group === 'B3').percentRemaining, remaining);
    assert.equal(output.low20.some(item => item.group === 'B3'), low20);
    assert.equal(output.low10.some(item => item.group === 'B3'), low10);
    assert.equal(output.autoQuota.enabled, false);
    assert.equal(output.autoBinding.attempted, false);
  }
});

test('B3 unknown or zero-denominator balance never becomes a zero-percent alert', async () => {
  for (const [remaining, total] of [[null, 100], [undefined, 100], ['', 100], [0, 0]]) {
    const rows = RECORDS.map(item => ({ ...item, dailyRemaining: 50, dailyTotal: 100 }));
    Object.assign(rows.find(item => item.group === 'B3'), { dailyRemaining: remaining, dailyTotal: total });
    const { output } = await runEntryOffline(rows, fixture());
    assert.equal(output.records.find(item => item.group === 'B3').percentRemaining, null);
    assert.equal(output.low20.some(item => item.group === 'B3'), false);
    assert.equal(output.low10.some(item => item.group === 'B3'), false);
  }
});

test('missing or ambiguous B3 is reported without using similarly prefixed groups', async () => {
  const rows = RECORDS.filter(item => item.group !== 'B3').map(item => ({ ...item, dailyRemaining: 50, dailyTotal: 100 }));
  rows.push({ name: 'B31-fixture', identif: 'not-b3', dailyRemaining: 0, dailyTotal: 100 });
  const missing = await runEntryOffline(rows, fixture());
  assert.equal(missing.exitCode, 2);
  assert.deepEqual(missing.output.missingRequiredGroups, ['B3']);
  assert.equal(missing.output.replyRates.find(item => item.group === 'B3').replyRatePct, null);
  assert.equal(missing.output.low10.some(item => item.group === 'B3'), false);
  rows.push({ ...RECORDS.find(item => item.group === 'B3'), dailyRemaining: 9, dailyTotal: 100 },
    { name: 'B3 duplicate', identif: 'duplicate-b3', dailyRemaining: 8, dailyTotal: 100 });
  const ambiguous = await runEntryOffline(rows, fixture());
  assert.equal(ambiguous.exitCode, 2);
  assert.deepEqual(ambiguous.output.missingRequiredGroups, ['B3']);
  assert.equal(ambiguous.output.replyRates.find(item => item.group === 'B3').carrierMatchCount, 2);
});

test('V balances use exact independent identities, strict thresholds and unknown safeguards', async () => {
  for (const [remaining, total, low20, low10] of [
    [20, 100, false, false], [19, 100, true, false], [10, 100, true, false],
    [9, 100, true, true], [0, 100, true, true], [null, 100, false, false], [0, 0, false, false],
  ]) {
    const rows = RECORDS.map(item => ({ ...item, dailyRemaining: item.group.endsWith('V') ? remaining : 50, dailyTotal: item.group.endsWith('V') ? total : 100 }));
    for (const name of ['B1VX', 'B11V2', 'B12VV', 'B1Vextra-fixture']) {
      rows.push({ name, identif: `excluded-${name}`, dailyRemaining: 0, dailyTotal: 100 });
    }
    const { output, exitCode } = await runEntryOffline(rows, fixture());
    assert.equal(exitCode, undefined);
    assert.deepEqual(output.requiredGroups, GROUPS);
    assert.deepEqual(output.alertGroups, GROUPS);
    for (const group of ['B1V', 'B11V', 'B12V']) {
      assert.equal(output.records.find(item => item.group === group).percentRemaining, total && remaining !== null ? remaining : null);
      assert.equal(output.low20.some(item => item.group === group), low20);
      assert.equal(output.low10.some(item => item.group === group), low10);
    }
    for (const group of ['B1', 'B11', 'B12']) {
      assert.equal(output.records.find(item => item.group === group).percentRemaining, 50);
    }
    assert.equal(output.autoQuota.enabled, false);
    assert.equal(output.autoBinding.attempted, false);
    assert.equal(output.performance.statisticsRead.maxActive, 4);
  }
});

test('missing or ambiguous V carriers cannot borrow standard group records', async () => {
  for (const group of ['B1V', 'B11V', 'B12V']) {
    const rows = RECORDS.filter(item => item.group !== group).map(item => ({ ...item, dailyRemaining: 50, dailyTotal: 100 }));
    const missing = await runEntryOffline(rows, fixture());
    assert.equal(missing.exitCode, 2);
    assert.deepEqual(missing.output.missingRequiredGroups, [group]);
    rows.push({ ...RECORDS.find(item => item.group === group), name: group, dailyRemaining: 9, dailyTotal: 100 },
      { name: `${group} duplicate`, identif: 'duplicate-v', dailyRemaining: 9, dailyTotal: 100 });
    const ambiguous = await runEntryOffline(rows, fixture());
    assert.equal(ambiguous.exitCode, 2);
    assert.deepEqual(ambiguous.output.missingRequiredGroups, [group]);
    assert.equal(ambiguous.output.replyRates.find(item => item.group === group).carrierMatchCount, 2);
  }
});

test('all-carrier report reuses required queries and includes other exact IDs with actual observation times', async () => {
  const mock = fixture();
  const original = mock.api;
  const extraCalls = [];
  mock.api = async (method, endpoint, data, query) => {
    if (query.carrireIdentif.startsWith('other-')) {
      extraCalls.push(query.carrireIdentif);
      return { code: 200, data: { content: [{ days: '2026-09-07', firstReplyRate: '3%' }] } };
    }
    return original(method, endpoint, data, query);
  };
  const rows = [...RECORDS, { name: '其它运营商', identif: 'other-one' }, { name: '其它运营商', identif: 'other-two' }];
  const { output } = await runEntryOffline(rows, mock);
  assert.equal(output.carrierReplyRates.length, GROUPS.length + 2);
  assert.equal(mock.called.length, GROUPS.length);
  assert.deepEqual(extraCalls, ['other-one', 'other-two']);
  assert.ok(output.carrierReplyRates.every(row => row.metric === 'firstReplyRate' && !isNaN(Date.parse(row.observedAt))));
  assert.equal(output.replyRates.length, GROUPS.length);
});

test('extra carrier read failures are unknown and do not change required-group success contract', async () => {
  const rows = [...RECORDS, { name: '无统计行运营商', identif: 'other' }];
  const { output, exitCode } = await runEntryOffline(rows, fixture());
  assert.equal(exitCode, undefined);
  assert.equal(output.systemIssues.length, 0);
  assert.equal(output.carrierReplyRates.at(-1).status, 'error');
  assert.equal(output.carrierReplyRates.at(-1).replyRatePct, null);
});

test('display-only carriers with no statistics do not cancel following carriers', async () => {
  let called = 0;
  const api = async () => {
    called += 1;
    return { code: 200, data: { content: called === 1 ? [] : [{ days: '2026-09-07', firstReplyRate: '0%' }] } };
  };
  const result = await collectBalanceStatisticsReadOnly(api, RECORDS, {
    now: NOW, concurrency: 1, continueOnDataErrors: true,
  });
  assert.equal(called, GROUPS.length);
  assert.equal(result.replyRates[0].status, 'error');
  assert.ok(result.replyRates.slice(1).every(row => row.status === 'ok' && row.replyRatePct === 0));
});
