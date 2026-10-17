import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createExpirySubmitter, loadExpirySubmissionSettings, validateExpirySubmissionSettings,
  expiryIdentitySnapshot, verifyExpiryRetryIdentity } from '../scripts/opensms_expiry_submission.mjs';
import { main as taskE } from '../scripts/opensms_execute_task_e_expiry.mjs';

const payload = { cardNos: ['2368000000'], expireDate: '2026-10-17' };
const response = cards => ({ code: 200, data: { inputCount: cards.length, matchedCardCount: cards.length, updatedChatCount: 0, notFound: [] } });
const gateway = () => Object.assign(Error('HTTP 502'), { status: 502 });
function timing() { let now = 0; return { clock: () => now, wait: async ms => { now += ms; } }; }
async function fixture(api, extra = {}) {
  return createExpirySubmitter({ task: 'D', api, beforeRetry: async () => ({ passed: true }), ...timing(), ...extra });
}

test('public settings are scoped to D/E, >=5 second gap, bounded real-502 attempts', async () => {
  const { settings } = await loadExpirySubmissionSettings();
  assert.equal(settings.completionGapMs, 5000); assert.equal(settings.maxAttempts, 3);
  for (const changes of [{ completionGapMs: 0 }, { maxAttempts: 5 }, { retryHttpStatus: 504 }, { tasks: ['F'] },
    { endpoint: '/phone/config/importChatExpire' }, { retryDelayMs: [1000, 120000] }]) {
    assert.throws(() => validateExpirySubmissionSettings({ ...settings, ...changes }));
  }
  await assert.rejects(createExpirySubmitter({ task: 'F', api: async () => {} }), /SCOPE/);
});

test('HTTP 502 waits 60/120 seconds, rechecks, keeps same payload and all attempts', async () => {
  let calls = 0, checks = 0; const payloads = [], events = [], audit = {};
  const submitter = await fixture(async (method, endpoint, data) => {
    assert.equal(method, 'POST'); assert.equal(endpoint, '/phone/config/batchSetChatExpire');
    payloads.push(JSON.stringify(data)); if (++calls < 3) throw gateway(); return response(data.cardNos);
  }, { beforeRetry: async () => { checks++; return { passed: true }; } });
  assert.deepEqual(await submitter.submit(payload, audit, async () => { events.push(structuredClone(audit)); }), response(payload.cardNos));
  assert.equal(calls, 3); assert.equal(checks, 2); assert.equal(new Set(payloads).size, 1);
  assert.deepEqual(audit.waits.map(w => w.actualMs), [5000, 60000, 120000]);
  assert.deepEqual(audit.attempts.map(a => a.httpStatus ?? a.status), [502, 502, 'response_received']);
  assert.ok(events.some(e => e.phase === 'waiting_502_retry'));
  assert.ok(events.some(e => e.phase === 'rechecking_502_retry'));
});

test('non-502, text-only 502, authentication, transport and unconfirmed requests never replay', async () => {
  for (const error of [Error('HTTP 502'), Object.assign(Error('502'), { status: '502' }),
    ...[401, 403, 429, 500, 503, 504].map(status => Object.assign(Error('HTTP'), { status })),
    Object.assign(gateway(), { requestCompletionUnconfirmed: true }), Error('socket hang up')]) {
    let calls = 0;
    const submitter = await fixture(async () => { calls++; throw error; });
    await assert.rejects(submitter.submit(payload, {}, async () => {})); assert.equal(calls, 1);
  }
});

test('three failed attempts stop with no fourth call; Retry-After takes precedence', async () => {
  let calls = 0; const audit = {};
  const submitter = await fixture(async () => { calls++; throw Object.assign(gateway(), { retryAfterMs: 180000 }); });
  await assert.rejects(submitter.submit(payload, audit, async () => {}), /502/);
  assert.equal(calls, 3); assert.deepEqual(audit.waits.map(w => w.actualMs), [5000, 180000, 180000]);
  assert.equal(audit.attempts.length, 3);
});

test('unconfirmed recheck, missing callback or excessive Retry-After stop before second submission', async () => {
  for (const extra of [{ beforeRetry: undefined }, { beforeRetry: async () => ({ passed: false }) },
    { beforeRetry: async () => { throw Error('identity changed'); } }, { tooLong: true }]) {
    let calls = 0;
    const submitter = await fixture(async () => { calls++; throw Object.assign(gateway(), extra.tooLong ? { retryAfterMs: 900001 } : {}); }, extra);
    await assert.rejects(submitter.submit(payload, {}, async () => {})); assert.equal(calls, 1);
  }
});

test('persistence errors with status=502 never enter HTTP retry catch', async () => {
  let calls = 0; const audit = {};
  const submitter = await fixture(async () => { calls++; return response(payload.cardNos); });
  await assert.rejects(submitter.submit(payload, audit, async () => {
    if (audit.phase === 'response_received') throw Object.assign(Error('disk failure'), { status: 502 });
  }), /disk failure/);
  assert.equal(calls, 1); assert.equal(audit.response.code, 200);
});

test('the scoped submitter refuses overlapping submissions', async () => {
  let release; const gate = new Promise(resolve => { release = resolve; });
  const submitter = await fixture(async () => { await gate; return response(payload.cardNos); });
  const first = submitter.submit(payload, {}, async () => {});
  await assert.rejects(submitter.submit(payload, {}, async () => {}), /CONCURRENT/);
  release(); await first;
});

function phones() { return [{ id: 11, cardNo: '2368000000', carrireIdentif: 'b1', opr: 'Kini', slotIdentif: 'slot1', cardStore: 'iccid1' }]; }
const configs = [{ identif: 'b1', name: 'B1' }];
test('retry readback requires exact date, identity, OPR, carrier and renewal protection', async () => {
  const baseline = expiryIdentitySnapshot(phones(), payload.cardNos, configs);
  for (const change of [null, { id: 12 }, { opr: 'Koodo' }, { cardStore: 'iccid2' }, { slotIdentif: 'slot2' }, { carrireIdentif: 'b2' },
    { name: '快过期续费T' }, { name: 'B11' }, { date: '2026-10-11' }, { missing: true }]) {
    const api = async (_m, endpoint) => endpoint === '/phone/config/list'
      ? { code: 200, rows: change?.missing ? [] : phones().map(r => ({ ...r, ...change })), total: 1 }
      : { code: 200, rows: change?.name ? [{ identif: 'b1', name: change.name }] : configs, total: 1 };
    const operation = verifyExpiryRetryIdentity({ api, cards: payload.cardNos, snapshot: baseline,
      serverDate: '2026-10-10', readDate: async () => change?.date || '2026-10-10' });
    if (change) await assert.rejects(operation); else assert.equal((await operation).passed, true);
  }
});

async function eFixture(t, behavior) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'opensms-e-502-offline-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const source = path.join(root, 'source.xlsx'), ledger = path.join(root, 'ledger.json'), manifestPath = path.join(root, 'manifest.json');
  const bytes = Buffer.from('offline source hash proof'); await fs.writeFile(source, bytes);
  const cards = Array.from({ length: 21 }, (_, i) => String(2368000000 + i));
  const manifest = { task: 'E', forceGate: true, automationGate: true, serverDate: '2026-10-10',
    sourcePath: source, sourceSha256: crypto.createHash('sha256').update(bytes).digest('hex'), ledgerPath: ledger, ledgerSha256Before: null,
    ledgerDedupeKey: 'phone', eligibleExpiryStatuses: ['6天', '快过期', '已过期'], candidateCount: 21, dateGroupCount: 1,
    groups: [{ expireDate: '2026-10-09', count: 21, cardNos: cards, rows: cards.map(card => ({ '电话': card, '运营商': 'B1', '过期状态': '已过期', '截止日': '2026-10-09' })) }] };
  await fs.writeFile(manifestPath, JSON.stringify(manifest));
  const previous = { ...process.env }; Object.assign(process.env, { OPENSMS_ENABLE_TASK_E: '1', OPENSMS_FORCE_TASK_E: '1', OPENSMS_EXECUTE_TASK_E: '1' });
  t.after(() => { process.env = previous; });
  let calls = 0, closed = 0;
  const api = async (_m, endpoint, data) => {
    if (endpoint === '/carrier/config/listNotDeleted') return { code: 200, rows: configs, total: 1 };
    if (endpoint === '/phone/config/list') {
      const requested = data.cardNo.split('\n');
      return { code: 200, total: requested.length, rows: requested.map(p => ({ ...phones()[0], id: cards.indexOf(p) + 1, cardNo: p })) };
    }
    assert.equal(endpoint, '/phone/config/batchSetChatExpire'); calls++;
    return behavior(calls, data, ledger);
  };
  const execute = () => taskE({ args: [manifestPath, 'offline-502', root, ledger],
    createSession: async () => ({ api, client: { close: () => { closed++; } } }),
    submissionRuntime: timing(), readDate: async () => '2026-10-10' });
  return { root, cards, ledger, execute, calls: () => calls, closed: () => closed,
    progress: path.join(root, 'opensms_task_e_offline-502_progress.json'), receipt: path.join(root, 'opensms_task_e_offline-502_receipt.json') };
}

test('E retries same 502 batch then records zero-change success only once in permanent ledger', async t => {
  const input = await eFixture(t, async (calls, data, ledger) => {
    if (calls === 1) throw gateway();
    if (calls === 2) await assert.rejects(fs.access(ledger), /ENOENT/);
    return response(data.cardNos);
  });
  await input.execute();
  const r = JSON.parse(await fs.readFile(input.receipt)), l = JSON.parse(await fs.readFile(input.ledger));
  assert.equal(input.calls(), 3); assert.equal(input.closed(), 1);
  assert.equal(r.matchedCardCount, 21); assert.equal(r.matchedNoChangeCount, 21);
  assert.equal(r.batches[0].submission.retryCount, 1); assert.equal(Object.keys(l.submissions).length, 21);
  await assert.rejects(input.execute(), /禁止自动重复提交/); assert.equal(input.calls(), 3);
});

test('E exhausted 502 keeps pending evidence and does not run next batch or mark ledger success', async t => {
  const input = await eFixture(t, async () => { throw gateway(); });
  await assert.rejects(input.execute(), /502/);
  const r = JSON.parse(await fs.readFile(input.progress));
  assert.equal(input.calls(), 3); assert.equal(r.uncertainSubmission, true); assert.equal(r.automaticRetryBlocked, true);
  assert.equal(r.confirmedInputCount, 0); assert.equal(r.pendingBatch.submission.attempts.length, 3);
  await assert.rejects(fs.access(input.ledger), /ENOENT/);
});

test('E malformed successful HTTP business response is not retried or entered in ledger', async t => {
  const input = await eFixture(t, async (_n, data) => ({ code: 200, data: { ...response(data.cardNos).data, matchedCardCount: 19 } }));
  await assert.rejects(input.execute(), /业务回执不完整/); assert.equal(input.calls(), 1);
  assert.equal(JSON.parse(await fs.readFile(input.progress)).uncertainSubmission, true);
  await assert.rejects(fs.access(input.ledger), /ENOENT/);
});
