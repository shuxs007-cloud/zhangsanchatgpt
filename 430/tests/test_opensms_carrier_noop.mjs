import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

// Offline only: do not import production entry points. All I/O, including the
// protocol client and artifact writer, is replaced inside an isolated VM.
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sourceNames = {
  set: 'opensms_set_carrier_candidates.mjs',
  verify: 'opensms_verify_carrier_candidates_readonly.mjs',
};
const source = Object.fromEntries(Object.entries(sourceNames).map(([key, name]) => [key, fs.readFileSync(path.join(root, name), 'utf8')]));
const protocolSource = fs.readFileSync(path.join(root, 'opensms_protocol_client.mjs'), 'utf8');
const phoneFunctions = protocolSource.slice(protocolSource.indexOf('export function shortCardNo('), protocolSource.indexOf('\nfunction readDevtoolsPorts(')).replaceAll('export function ', 'function ');
const compactFunction = protocolSource.slice(protocolSource.indexOf('export function compactPhoneRow(')).replace('export function ', 'function ');
const baselineDir = process.env.OPENSMS_CARRIER_NOOP_BASELINE_DIR;
const baseline = baselineDir && Object.fromEntries(Object.entries(sourceNames).map(([key, name]) => [key, fs.readFileSync(path.join(baselineDir, name), 'utf8')]));
const clone = value => JSON.parse(JSON.stringify(value));
const candidatePath = '/offline/candidate.json';
const fixedTime = '2026-09-05T08:00:00.000Z';
class FixedDate extends Date {
  constructor(...args) { super(...(args.length ? args : [fixedTime])); }
  static now() { return Date.parse(fixedTime); }
}
const carriers = [
  { id: 'source-id', identif: 'source', name: 'B1-测试', delData: false },
  { id: 'target-id', identif: 'target', name: '过期下架卡T', delData: false },
  { id: 'swap-id', identif: 'swap', name: '换号区T', delData: false },
  { id: 'other-id', identif: 'other', name: '其他来源', delData: false },
];
const phone = index => String(14160000000 + index);
const candidateRow = index => ({ 电话: phone(index), 运营商: 'B1-测试', opr: 'Kooda' });
const backendRow = (index, identif = 'source') => ({ id: `phone-${index}`, cardNo: phone(index), identif, name: carriers.find(row => row.identif === identif)?.name || identif, opr: 'Kooda' });
function taskB(rows = []) {
  return { task: 'B', forceGate: true, route: 'task_b_expired_to_offline_t', targetCarrierName: '过期下架卡T', sourceCarrierNames: ['B1-测试'], count: rows.length, rows };
}
function taskC(rows = []) {
  return {
    ...taskB(rows), task: 'C', targetCarrierName: '换号区T', route: 'task_c_kooda_4day_reply_rate_lt5_to_swap',
    taskBPriorityLockEnforced: true, serverDate: '2026-09-05', tomorrowDate: '2026-09-06', replyRateSendThresholdInclusive: 50,
    tenDaySendThresholdExclusive: 360, tenDaySendMetric: '10日发送窗口数',
    rows: rows.map(row => ({ '10日发送窗口数': 361, ...row })),
  };
}
const authorized = { OPENSMS_FORCE_TASK_B: '1', OPENSMS_FORCE_TASK_C: '1' };

async function run(kind, candidate, options = {}) {
  const events = [];
  const output = [];
  const artifacts = new Map([[candidatePath, JSON.stringify(candidate)]]);
  const liveRows = clone(options.backendRows || []);
  const liveCarriers = clone(options.carriers || carriers);
  const context = vm.createContext({
    path, Date: FixedDate, SNAPSHOT_DIR: '/offline',
    process: { env: options.env || {}, argv: ['node', sourceNames[kind], candidatePath, options.target || candidate.targetCarrierName || '过期下架卡T', 'noop_mock'], exitCode: 0 },
    console: { log: value => output.push(JSON.parse(value)) },
    fetch: () => { throw new Error('Live network access is forbidden'); },
    ensureArtifactDirs: async () => {},
    fs: {
      readFile: async file => {
        assert.ok(artifacts.has(file), `Unexpected file read: ${file}`);
        return artifacts.get(file);
      },
      writeFile: async (file, content) => { artifacts.set(file, content); },
      open: async () => { throw new Error('Unexpected file lock in offline test'); },
    },
    createOpenSmsApiSession: async () => {
      events.push({ type: 'connect' });
      return { api: {}, apiTransport: 'offline-mock', browser: { close: async () => { events.push({ type: 'close' }); } } };
    },
    getCarrierConfigs: async () => { events.push({ type: 'carriers' }); return clone(liveCarriers); },
    listPhonesByNumbers: async (_api, phones, batchSize) => {
      assert.ok(phones.length > 0 && phones.length <= 500);
      assert.equal(batchSize, 500);
      events.push({ type: 'phones', phones: clone(phones) });
      const keys = new Set(phones.map(value => String(value).slice(-10)));
      return { rows: clone(liveRows.filter(row => keys.has(String(row.cardNo).slice(-10)))) };
    },
    updatePhoneCarrier: async (_api, ids, identif) => {
      events.push({ type: 'write', ids: clone(ids), identif });
      if (options.ignoreWrite) return { code: 200, msg: 'mock-no-change' };
      const target = liveCarriers.find(row => row.identif === identif);
      for (const row of liveRows) if (ids.includes(row.id)) Object.assign(row, { identif, name: target.name });
      return { code: 200, msg: 'offline-mock' };
    },
  });
  vm.runInContext(phoneFunctions, context);
  vm.runInContext(compactFunction, context);
  const script = options.source || source[kind];
  const start = script.indexOf('const CDP =');
  const entryGuard = script.lastIndexOf('\nif (process.argv[1] && import.meta.url ===');
  const end = entryGuard >= 0 ? entryGuard : script.lastIndexOf('\nmain().catch(');
  assert.ok(start >= 0 && end > start);
  vm.runInContext(script.slice(start, end).replace(/^export /gm, ''), context);
  let error;
  try { await context.main(); } catch (caught) { error = caught; }
  const receipt = output.at(-1);
  return {
    error, events, receipt, exitCode: context.process.exitCode, artifacts, liveRows,
    validated: receipt?.validatedCandidatePath ? JSON.parse(artifacts.get(receipt.validatedCandidatePath)) : undefined,
  };
}
function counts(result) {
  return Object.fromEntries(['connect', 'carriers', 'phones', 'write', 'close'].map(type => [type, result.events.filter(event => event.type === type).length]));
}
function success(result) { assert.ifError(result.error); return result.receipt; }

test('empty authorized set/verify creates explicit no-op receipts without any connection', async () => {
  const written = await run('set', taskB(), { env: authorized });
  const receipt = success(written);
  assert.deepEqual(written.events, []);
  assert.equal(receipt.status, 'no_candidate');
  assert.equal(receipt.noWrite, true);
  assert.equal(receipt.actualWriteCount, 0);
  assert.equal(receipt.after, null);
  assert.deepEqual(receipt.updateResponses, []);
  assert.equal(receipt.target.resolved, false);
  assert.equal(receipt.independentReadbackRequired, false);
  assert.equal(receipt.independentReadbackPerformed, false);
  assert.equal(receipt.stageReadbackRequired, false);
  const verified = await run('verify', written.validated);
  assert.equal(success(verified).status, 'no_candidate');
  assert.equal(verified.receipt.verificationStatus, 'not_required');
  assert.equal(verified.receipt.passed, null);
  assert.equal(verified.receipt.independentReadbackPerformed, false);
  assert.equal(verified.exitCode, 0);
  assert.deepEqual(verified.events, []);
});

test('zero actual IDs reads candidates once and preserves every exclusion for the stage collector', async () => {
  const candidate = taskB([1, 2, 3, 4].map(candidateRow));
  const result = await run('set', candidate, { env: authorized, backendRows: [backendRow(1, 'target'), backendRow(2, 'other'), backendRow(4), backendRow(4)] });
  const receipt = success(result);
  assert.deepEqual(counts(result), { connect: 1, carriers: 1, phones: 1, write: 0, close: 1 });
  assert.equal(receipt.noWrite, true);
  assert.equal(receipt.stageReadbackRequired, true);
  assert.equal(receipt.independentReadbackPerformed, false);
  assert.equal(receipt.after, null);
  assert.deepEqual(receipt.updateResponses, []);
  assert.equal(receipt.before.alreadyTargetCount, 1);
  assert.equal(receipt.before.nonSourceCount, 1);
  assert.equal(receipt.before.missingCount, 1);
  assert.equal(receipt.before.duplicateBackendCount, 1);
  assert.equal(result.validated.count, 0);
  const verified = await run('verify', result.validated);
  assert.equal(success(verified).passed, null);
  assert.deepEqual(verified.events, []);
});

test('real candidate writes exact IDs and reads independently after submission and in a new verifier session', async () => {
  const result = await run('set', taskB([1, 2].map(candidateRow)), { env: authorized, backendRows: [backendRow(1), backendRow(2, 'target')] });
  const receipt = success(result);
  assert.deepEqual(result.events.map(event => event.type), ['connect', 'carriers', 'phones', 'write', 'phones', 'close']);
  assert.deepEqual(result.events.find(event => event.type === 'write'), { type: 'write', ids: ['phone-1'], identif: 'target' });
  assert.equal(receipt.noWrite, false);
  assert.equal(receipt.actualWriteCount, 1);
  assert.equal(receipt.independentReadbackRequired, true);
  assert.equal(receipt.independentReadbackPerformed, true);
  assert.equal(receipt.after.targetCount, 2);
  assert.equal(result.validated.rows[0].后台ID, 'phone-1');
  const verified = await run('verify', result.validated, { backendRows: result.liveRows });
  assert.equal(success(verified).passed, true);
  assert.equal(verified.receipt.total, 1);
  assert.equal(verified.receipt.independentReadbackPerformed, true);
  assert.deepEqual(verified.events.map(event => event.type), ['connect', 'carriers', 'phones', 'close']);
});

test('write response alone does not satisfy separate nonempty verification', async () => {
  const result = await run('set', taskB([candidateRow(1)]), { env: authorized, backendRows: [backendRow(1)], ignoreWrite: true });
  assert.equal(success(result).after.stillNotTargetCount, 1);
  const verified = await run('verify', result.validated, { backendRows: result.liveRows });
  assert.equal(success(verified).passed, false);
  assert.equal(verified.exitCode, 1);
  assert.equal(verified.receipt.independentReadbackPerformed, true);
});

test('authorization and task C priority/send gates still run before empty/real no-op decisions', async () => {
  for (const candidate of [taskB(), taskC(), {
    ...taskB(), task: 'D', route: 'task_d_retain_chatr_to_b14_c', targetCarrierName: 'B14-C快下保留卡',
  }]) {
    const result = await run('set', candidate);
    assert.ok(result.error);
    assert.match(result.error.message, /授权|FORCE_TASK/);
    assert.deepEqual(result.events, []);
  }
  for (const candidate of [
    { ...taskB(), forceGate: false },
    { ...taskC(), taskBPriorityLockEnforced: false },
    taskC([{ ...candidateRow(1), '4日发送窗口数': 49 }]),
    taskC([{ ...candidateRow(1), '4日发送窗口数': 50, 过期状态: '已过期' }]),
  ]) {
    const result = await run('set', candidate, { env: authorized });
    assert.ok(result.error);
    assert.deepEqual(result.events, []);
  }
  const valid = await run('set', taskC([{ ...candidateRow(1), '4日发送窗口数': 50 }]), { env: authorized, backendRows: [backendRow(1, 'swap')] });
  assert.equal(success(valid).noWrite, true);
  assert.equal(valid.receipt.before.alreadyTargetCount, 1);
});

test('malformed lists cannot be treated as an empty no-op', async () => {
  for (const candidate of [
    null, {}, { rows: {} }, { rows: [null] }, { rows: [{ 电话: '' }] },
    { rows: [], count: 1 }, { rows: [], candidates: [candidateRow(1)] },
  ]) {
    for (const kind of ['set', 'verify']) {
      const result = await run(kind, candidate, { env: authorized, target: '过期下架卡T' });
      assert.ok(result.error);
      assert.match(result.error.message, /候选清单/);
      assert.deepEqual(result.events, []);
    }
  }
});

test('permanent protection and exact target identity still block no-op/write paths', async () => {
  for (const protectedName of ['快过期续费C', '快过期续费T']) {
    const declared = await run('set', { ...taskB(), sourceCarrierNames: [protectedName] }, { env: authorized });
    assert.match(declared.error?.message || '', /永久保护/);
    assert.deepEqual(declared.events, []);
    const live = await run('set', taskB([candidateRow(1)]), { env: authorized, backendRows: [{ ...backendRow(1), name: protectedName }] });
    assert.match(live.error?.message || '', /永久保护/);
    assert.equal(counts(live).write, 0);
    assert.equal(counts(live).close, 1);
  }
  const duplicate = await run('set', taskB([candidateRow(1)]), { env: authorized, carriers: [...carriers, carriers[1]] });
  assert.match(duplicate.error?.message || '', /精确匹配异常/);
  assert.equal(counts(duplicate).write, 0);
  const disabled = await run('set', taskC([{ ...candidateRow(1), '4日发送窗口数': 50 }]), { env: authorized, carriers: carriers.map(row => row.identif === 'swap' ? { ...row, delData: true } : row) });
  assert.match(disabled.error?.message || '', /已停用/);
  assert.equal(counts(disabled).write, 0);
});

test('task B exact B14 declared-source protection cannot be hidden behind task or source aliases', async () => {
  for (const retainedName of ['B14-C快下保留卡', 'B14-T快下保留卡']) {
    const declarations = [
      { ...taskB(), sourceCarrierNames: [retainedName] },
      { ...taskB(), configuredSourceCarrierNames: [retainedName] },
      ...['运营商', '原运营商', '精确原运营商', 'sourceCarrierName', 'sourceCarrier'].map(key =>
        taskB([{ ...candidateRow(1), [key]: retainedName }])),
      { ...taskB([candidateRow(1)]), candidates: [{ ...candidateRow(1), 运营商: retainedName }] },
    ];
    for (const candidate of declarations) {
      for (const task of ['B', '', 'C', 'D']) {
        const result = await run('set', { ...candidate, task }, { env: authorized });
        assert.match(result.error?.message || '', /任务B保留卡保护/);
        assert.deepEqual(result.events, []);
      }
    }
  }
});

test('task B routes reject missing or forged task before opening a session', async () => {
  for (const task of ['', 'A', 'C', 'D', 'RESTORE_QUICK_RENEW_C']) {
    const result = await run('set', { ...taskB([candidateRow(1)]), task }, { env: authorized });
    assert.match(result.error?.message || '', /任务B路由与task标记不一致/);
    assert.deepEqual(result.events, []);
  }
});

test('task B stops the whole group when live B14 is identified by name or carrier ID', async () => {
  for (const retainedName of ['B14-C快下保留卡', 'B14-T快下保留卡']) {
    const liveCarriers = [...carriers,
      { identif: 'retained', name: retainedName },
      { identif: 'retained', name: 'conflicting-config-name' },
    ];
    for (const heldRow of [
      { ...backendRow(1), name: retainedName },
      { ...backendRow(1), identif: 'retained', name: '' },
      { ...backendRow(1), identif: 'retained', name: 'B1-测试' },
    ]) {
      const result = await run('set', taskB([candidateRow(1), candidateRow(2)]), {
        env: authorized, carriers: liveCarriers, backendRows: [heldRow, backendRow(2)],
      });
      assert.match(result.error?.message || '', /任务B保留卡保护：后台实时来源/);
      assert.equal(counts(result).write, 0);
      assert.equal(counts(result).close, 1);
    }
    const duplicate = await run('set', taskB([candidateRow(1), candidateRow(2)]), {
      env: authorized, carriers: liveCarriers,
      backendRows: [backendRow(1), { ...backendRow(1), identif: 'retained', name: retainedName }, backendRow(2)],
    });
    assert.match(duplicate.error?.message || '', /任务B保留卡保护/);
    assert.equal(counts(duplicate).write, 0);
  }
});

test('both expired and tomorrow C/T task B routes retain exact B14 sources', async () => {
  for (const [route, target] of [
    ['task_b_expired_to_offline_c', '过期下架卡C'],
    ['task_b_expired_to_offline_t', '过期下架卡T'],
    ['task_b_tomorrow_fast_to_isolation_c', '快过期隔离区C'],
    ['task_b_tomorrow_fast_to_isolation_t', '快过期隔离区T'],
  ]) {
    for (const name of ['B14-C快下保留卡', 'B14-T快下保留卡']) {
      const candidate = { ...taskB([candidateRow(1)]), route, targetCarrierName: target };
      const result = await run('set', candidate, {
        env: authorized, backendRows: [{ ...backendRow(1), name }],
        carriers: carriers.map(row => row.identif === 'target' ? { ...row, name: target } : row),
      });
      assert.match(result.error?.message || '', /任务B保留卡保护/);
      assert.equal(counts(result).write, 0);
    }
  }
});

test('task B B14 protection is exact and does not broaden unrelated routing', async () => {
  for (const name of ['B14', 'B14-其它', 'B14-C快下保留卡-备份', 'B14-T快下保留卡其他']) {
    const candidate = { ...taskB([{ ...candidateRow(1), 运营商: name }]), sourceCarrierNames: [name] };
    const result = await run('set', candidate, {
      env: authorized, carriers: carriers.map(row => row.identif === 'source' ? { ...row, name } : row),
      backendRows: [{ ...backendRow(1), name }],
    });
    assert.equal(success(result).actualWriteCount, 1);
  }
  const name = 'B14-C快下保留卡';
  const unrelated = await run('set', {
    ...taskB([{ ...candidateRow(1), 运营商: name }]), task: '', route: 'offline_test_other_route', sourceCarrierNames: [name],
  }, {
    carriers: carriers.map(row => row.identif === 'source' ? { ...row, name } : row),
    backendRows: [{ ...backendRow(1), name }],
  });
  assert.equal(success(unrelated).actualWriteCount, 1);
});

test('500-number batch limit and original phone coverage survive the no-op path', async () => {
  const rows = Array.from({ length: 1001 }, (_, index) => candidateRow(index + 1));
  const result = await run('set', taskB(rows), { env: authorized, backendRows: rows.map((_, index) => backendRow(index + 1, 'target')) });
  assert.equal(success(result).before.alreadyTargetCount, 1001);
  assert.equal(counts(result).phones, 3);
  assert.deepEqual(result.events.filter(event => event.type === 'phones').map(event => event.phones.length), [500, 500, 1]);
  assert.deepEqual(result.events.filter(event => event.type === 'phones').flatMap(event => event.phones), rows.map(row => row.电话));
  assert.equal(result.receipt.after, null);
});

test('saved baseline comparison: same input keeps actual-write/exclusion outputs while removing no-op I/O', { skip: !baseline }, async t => {
  const scenarios = [
    { label: 'empty', candidate: taskB(), backendRows: [] },
    { label: 'already-target', candidate: taskB([candidateRow(1)]), backendRows: [backendRow(1, 'target')] },
    { label: 'non-source', candidate: taskB([candidateRow(1)]), backendRows: [backendRow(1, 'other')] },
    { label: 'real-write', candidate: taskB([candidateRow(1), candidateRow(2)]), backendRows: [backendRow(1), backendRow(2, 'target')] },
  ];
  for (const scenario of scenarios) {
    const options = { env: authorized, backendRows: scenario.backendRows };
    const before = await run('set', scenario.candidate, { ...options, source: baseline.set });
    const after = await run('set', scenario.candidate, options);
    success(before); success(after);
    for (const key of ['eligibleCount', 'alreadyTargetCount', 'missingCount', 'duplicateBackendCount', 'nonSourceCount', 'candidateCount']) {
      assert.equal(after.receipt.before[key], before.receipt.before[key], `${scenario.label}: ${key}`);
    }
    assert.deepEqual(after.validated.rows, before.validated.rows);
    assert.deepEqual(after.receipt.phones, before.receipt.phones);
    assert.deepEqual(after.events.filter(event => event.type === 'write'), before.events.filter(event => event.type === 'write'));
    const oldVerify = await run('verify', before.validated, { backendRows: before.liveRows, source: baseline.verify });
    const newVerify = await run('verify', after.validated, { backendRows: after.liveRows });
    success(oldVerify); success(newVerify);
    if (after.receipt.actualWriteCount) {
      assert.deepEqual(after.receipt.after, before.receipt.after);
      assert.equal(newVerify.receipt.passed, oldVerify.receipt.passed);
      assert.deepEqual(counts(after), counts(before));
      assert.deepEqual(counts(newVerify), counts(oldVerify));
    } else {
      assert.equal(after.receipt.after, null);
      assert.equal(newVerify.receipt.passed, null);
      assert.deepEqual(newVerify.events, []);
      assert.ok(counts(after).carriers + counts(after).phones < counts(before).carriers + counts(before).phones);
    }
    t.diagnostic(JSON.stringify({ scenario: scenario.label, baselineSet: counts(before), optimizedSet: counts(after), baselineVerify: counts(oldVerify), optimizedVerify: counts(newVerify), businessOutputsEqual: true }));
  }
});
