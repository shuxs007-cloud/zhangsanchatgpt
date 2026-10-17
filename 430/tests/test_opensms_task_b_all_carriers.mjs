import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { assertTemporaryCarrierAllowed } from '../scripts/opensms_temporary_carrier_hold.mjs';
import { enforceVGroupBoundary } from '../scripts/opensms_v_group_guard.mjs';
import { isUnexpiredRestore, enforceUnexpiredRestore, validateUnexpiredLiveIdentity } from '../scripts/opensms_unexpired_a11_guard.mjs';

// Run the real writer main with ONLY memory-backed protocol/filesystem mocks.
// OS sandbox in the fixed-rule entry additionally denies every network request.
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = fs.readFileSync(path.join(root, 'opensms_set_carrier_candidates.mjs'), 'utf8');
const protocol = fs.readFileSync(path.join(root, 'opensms_protocol_client.mjs'), 'utf8');
const phoneFunctions = protocol.slice(protocol.indexOf('export function shortCardNo('), protocol.indexOf('\nfunction readDevtoolsPorts(')).replaceAll('export function ', 'function ');
const compactFunction = protocol.slice(protocol.indexOf('export function compactPhoneRow(')).replace('export function ', 'function ');
const candidatePath = '/offline/candidate.json';
const clone = value => JSON.parse(JSON.stringify(value));
const names = ['C2-测试区1-C120', 'B1V-精-Te-120', 'B14-C快下保留卡', 'B14-T快下保留卡',
  'A11-精普-C120', 'A21-股票-C120', 'A2-股票-T120', '快过期续费C', '快过期续费T', '三方隔离', '其它来源'];
const targets = ['过期下架卡C', '过期下架卡T', '快过期隔离区C', '快过期隔离区T'];
const carriers = [...names, ...targets].map((name, i) => ({ name, id: `carrier-${i}`, identif: `identif-${i}`, delData: false }));
const phone = i => String(14160000000 + i);
function fixture(branch = 'expired', family = 'C') {
  const target = `${branch === 'expired' ? '过期下架卡' : '快过期隔离区'}${family}`;
  const rows = names.map((name, i) => ({ 电话: phone(i), 运营商: name,
    opr: family === 'C' ? 'Chatr' : ['Kooda', 'Koodo', 'Kini', 'Telus1', 'Lucky'][i % 5],
    截止日: branch === 'expired' ? '2026-10-02' : '2026-10-03',
    过期状态: branch === 'expired' ? '已过期' : '快过期', 距截止日天数: branch === 'expired' ? 0 : 1 }));
  return { task: 'B', forceGate: true, sourceCarrierScope: 'all_carriers_v1', sourceCarrierExclusions: [],
    route: branch === 'expired' ? `task_b_expired_to_offline_${family.toLowerCase()}` : `task_b_tomorrow_fast_to_isolation_${family.toLowerCase()}`,
    targetCarrierName: target, serverDate: '2026-10-02', tomorrowDate: '2026-10-03',
    count: rows.length, candidateCount: rows.length, sourceCarrierNames: names, rows, candidates: clone(rows) };
}
function backend(candidate) {
  return candidate.rows.map((row, i) => ({ id: `phone-${i}`, cardNo: row.电话, opr: row.opr,
    identif: carriers.find(c => c.name === row.运营商).identif, name: row.运营商 }));
}
async function run(candidate, options = {}) {
  const events = [], output = [], artifacts = new Map([[candidatePath, JSON.stringify(candidate)]]);
  const liveRows = clone(options.liveRows ?? backend(candidate));
  const configs = clone(options.carriers ?? carriers);
  const context = vm.createContext({ path, crypto, Date, Intl,
    assertTemporaryCarrierAllowed, enforceVGroupBoundary, isUnexpiredRestore, enforceUnexpiredRestore, validateUnexpiredLiveIdentity,
    SNAPSHOT_DIR: '/offline', ensureArtifactDirs: async () => {},
    process: { env: options.env ?? { OPENSMS_FORCE_TASK_B: '1' },
      argv: ['node', 'offline', candidatePath, candidate.targetCarrierName, 'all_source'], exitCode: 0 },
    console: { log: value => output.push(JSON.parse(value)) },
    fetch: () => { throw new Error('Network is forbidden'); },
    fs: { readFile: async file => { assert.ok(artifacts.has(file)); return artifacts.get(file); },
      writeFile: async (file, value) => artifacts.set(file, value),
      open: async () => { throw new Error('Unexpected business lock'); } },
    createOpenSmsApiSession: async () => {
      events.push('connect');
      return { api: {}, apiTransport: 'offline-mock', browser: { close: async () => events.push('close') } };
    },
    getCarrierConfigs: async () => { events.push('carriers'); return clone(configs); },
    listPhonesByNumbers: async (_api, phones) => {
      events.push('read'); const keys = new Set(phones.map(p => String(p).slice(-10)));
      return { rows: clone(liveRows.filter(row => keys.has(String(row.cardNo).slice(-10)))) };
    },
    updatePhoneCarrier: async (_api, ids, identif) => {
      events.push('write'); const target = configs.find(row => row.identif === identif);
      for (const row of liveRows) if (ids.includes(row.id)) Object.assign(row, { identif, name: target.name });
      return { code: 200, msg: 'offline-mock' };
    },
  });
  vm.runInContext(phoneFunctions, context); vm.runInContext(compactFunction, context);
  const start = source.indexOf('const CDP =');
  const end = source.lastIndexOf('\nif (process.argv[1] && import.meta.url ===');
  assert.ok(end > start);
  vm.runInContext(source.slice(start, end).replace(/^export /gm, ''), context);
  let error;
  try { await context.main(); } catch (caught) { error = caught; }
  return { error, events, receipt: output.at(-1), liveRows };
}

for (const branch of ['expired', 'tomorrow']) for (const family of ['C', 'T']) {
  test(`${branch}/${family}: every source, including B14/renewal/isolation/V, writes and reads back`, async () => {
    const candidate = fixture(branch, family), result = await run(candidate);
    assert.ifError(result.error);
    assert.equal(result.receipt.actualWriteCount, names.length);
    assert.equal(result.receipt.after.targetCount, names.length);
    assert.equal(result.receipt.before.nonSourceCount, 0);
    assert.deepEqual(result.events, ['connect', 'carriers', 'read', 'write', 'read', 'close']);
    assert.ok(result.liveRows.every(row => row.name === candidate.targetCarrierName));
  });
}
test('all-source scope does not bypass authorization, date or OPR proof before connection', async () => {
  for (const change of [c => { c.forceGate = false; }, c => { c.sourceCarrierExclusions = ['三方隔离']; },
    c => { c.rows[0].截止日 = '2026-10-04'; }, c => { c.rows[0].opr = 'Chatr Lucky'; },
    c => { c.rows[0].运营商 = ''; }, c => { c.rows[1].电话 = '+1 ' + c.rows[0].电话.slice(-10); c.candidates = clone(c.rows); }]) {
    const candidate = fixture(); change(candidate); const result = await run(candidate, { liveRows: [] });
    assert.ok(result.error); assert.deepEqual(result.events, []);
  }
  const result = await run(fixture(), { env: {} });
  assert.match(result.error.message, /OPENSMS_FORCE_TASK_B/); assert.deepEqual(result.events, []);
});
test('missing, duplicate, wrong OPR, exact source drift or deleted target stops whole group before write', async () => {
  const candidate = fixture();
  for (const change of [rows => rows.pop(), rows => rows.push(clone(rows[0])),
    rows => { rows[0].opr = 'Kini'; }, rows => { rows[0].id = ''; },
    rows => { rows[0].name = names[1]; rows[0].identif = carriers[1].identif; }]) {
    const liveRows = backend(candidate); change(liveRows); const result = await run(candidate, { liveRows });
    assert.ok(result.error); assert.ok(!result.events.includes('write')); assert.equal(result.events.at(-1), 'close');
  }
  const deleted = clone(carriers); deleted.find(c => c.name === candidate.targetCarrierName).delData = true;
  const result = await run(candidate, { carriers: deleted });
  assert.match(result.error.message, /停用或删除/); assert.ok(!result.events.includes('write'));
});
test('already-target numbers are accounted for without duplicate writes', async () => {
  const candidate = fixture(), liveRows = backend(candidate), target = carriers.find(c => c.name === candidate.targetCarrierName);
  Object.assign(liveRows[0], { name: target.name, identif: target.identif });
  const result = await run(candidate, { liveRows }); assert.ifError(result.error);
  assert.equal(result.receipt.before.alreadyTargetCount, 1);
  assert.equal(result.receipt.actualWriteCount, names.length - 1);
  assert.equal(result.receipt.after.targetCount, names.length);
});
test('ordinary routes still cannot leave renewal or isolation through all-source metadata', async () => {
  for (const name of ['三方隔离', '快过期续费C', '快过期续费T']) {
    const candidate = fixture(); candidate.task = 'A'; candidate.route = 'ordinary_test';
    candidate.rows = [{ 电话: phone(0), 运营商: name, opr: 'Chatr' }];
    candidate.candidates = clone(candidate.rows); candidate.count = 1;
    candidate.sourceCarrierNames = [name]; candidate.targetCarrierName = 'A11-精普-C120';
    const result = await run(candidate); assert.ok(result.error); assert.deepEqual(result.events, []);
  }
});
