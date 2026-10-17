import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { validateExpiryReceipt } from '../scripts/opensms_expiry_receipt.mjs';
import { main as runE } from '../scripts/opensms_execute_task_e_expiry.mjs';

const phones = ['2368000000', '2368000001'];
const full = () => ({ code: 200, data: { inputCount: 2, matchedCardCount: 2, updatedChatCount: 0, notFound: [] } });
test('D/E reject missing, malformed, coerced and nonfinite transaction evidence', () => {
  const invalid = [undefined, { data: full().data }, { code: 200 }, { ...full(), code: 500 }];
  for (const key of ['inputCount', 'matchedCardCount', 'updatedChatCount']) {
    for (const value of [undefined, null, false, '', '2', -1, NaN, Infinity, 0.5]) {
      const raw = full(); raw.data[key] = value; invalid.push(raw);
    }
  }
  for (const value of [undefined, null, {}, ['bad'], [phones[0], phones[0]], ['2368000099']]) {
    const raw = full(); raw.data.notFound = value; invalid.push(raw);
  }
  for (const raw of invalid) for (const allowNotFound of [false, true]) {
    assert.throws(() => validateExpiryReceipt(raw, phones, { allowNotFound }), /业务回执不完整/);
  }
});

test('E allows exactly reconciled not-found subsets, D retains full-match gate', () => {
  const raw = full(); raw.data.matchedCardCount = 1; raw.data.notFound = ['1' + phones[1]];
  assert.deepEqual(validateExpiryReceipt(raw, phones, { allowNotFound: true }).matchedCards, [phones[0]]);
  assert.throws(() => validateExpiryReceipt(raw, phones));
});

async function fixture(t, marker) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'opensms-de-regression-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const env = { ...process.env };
  for (const key of ['OPENSMS_ENABLE_TASK_E', 'OPENSMS_FORCE_TASK_E', 'OPENSMS_EXECUTE_TASK_E']) process.env[key] = '1';
  t.after(() => { process.env = env; });
  const source = path.join(root, 'source.fixture');
  const sourceBytes = Buffer.from('offline fixture, no real workbook');
  await fs.writeFile(source, sourceBytes);
  const ledger = path.join(root, 'ledger.json');
  const manifest = path.join(root, 'manifest.json');
  const cards = Array.from({ length: 21 }, (_, i) => String(2368000000 + i));
  await fs.writeFile(manifest, JSON.stringify({ task: 'E', forceGate: true, automationGate: true,
    sourcePath: source, sourceSha256: crypto.createHash('sha256').update(sourceBytes).digest('hex'),
    ledgerPath: ledger, ledgerSha256Before: null, ledgerDedupeKey: 'phone', eligibleExpiryStatuses: ['6天', '快过期', '已过期'],
    candidateCount: cards.length, dateGroupCount: 1, groups: [{ expireDate: '2026-09-13', count: cards.length, cardNos: cards,
      rows: cards.map(phone => ({ 电话: phone, 截止日: '2026-09-13', 过期状态: '已过期', 运营商: '普通T' })) }] }));
  let calls = 0, active = 0, maximum = 0;
  const createSession = async () => ({ client: { close() {} }, api: async (_method, endpoint, data) => {
    if (endpoint === '/carrier/config/listNotDeleted') return { code: 200, rows: [{ identif: 't', name: '普通T' }], total: 1 };
    if (endpoint === '/phone/config/list') return { code: 200, total: cards.length,
      rows: cards.map((cardNo, i) => ({ id: String(i + 1), cardNo, carrireIdentif: 't' })) };
    assert.equal(endpoint, '/phone/config/batchSetChatExpire');
    calls++; active++; maximum = Math.max(maximum, active);
    try { return await marker(data.cardNos, calls); } finally { active--; }
  } });
  return { root, cards, ledger, args: [manifest, 'offline', root, ledger], createSession,
    counts: () => ({ calls, maximum }), progress: path.join(root, 'opensms_task_e_offline_progress.json'),
    receipt: path.join(root, 'opensms_task_e_offline_receipt.json') };
}

test('E records zero-update matches only, skips explicit misses, and continues serially', async t => {
  const f = await fixture(t, async (cards, call) => ({ code: 200, data: { inputCount: cards.length,
    matchedCardCount: cards.length - (call === 1 ? 1 : 0), updatedChatCount: 0, notFound: call === 1 ? [cards[0]] : [] } }));
  await runE(f);
  const receipt = JSON.parse(await fs.readFile(f.receipt));
  const ledger = JSON.parse(await fs.readFile(f.ledger));
  assert.equal(receipt.status, 'passed_with_not_found');
  assert.equal(receipt.submittedCount, 20); assert.equal(receipt.matchedNoChangeCount, 20);
  assert.equal(receipt.notWrittenCount, 1); assert.equal(Object.keys(ledger.submissions).length, 20);
  assert.equal(ledger.submissions[f.cards[0]], undefined);
  assert.deepEqual(f.counts(), { calls: 2, maximum: 1 });
  await assert.rejects(runE(f), /禁止自动重复提交/);
  assert.equal(f.counts().calls, 2);
});

test('E saves invalid responses and blocks all unfinished submissions without regex guesses', async t => {
  for (const marker of [async () => { throw new Error('连接已断开'); },
    async cards => ({ code: 200, data: { inputCount: cards.length, matchedCardCount: cards.length, updatedChatCount: 0 } })]) {
    const f = await fixture(t, marker);
    await assert.rejects(runE(f));
    const bytes = await fs.readFile(f.progress);
    const progress = JSON.parse(bytes);
    assert.equal(progress.uncertainSubmission, true); assert.equal(progress.automaticRetryBlocked, true);
    assert.equal(progress.uncertainBatch.inputCount, 20);
    assert.equal(f.counts().calls, 1);
    await assert.rejects(fs.access(f.ledger));
    await assert.rejects(runE(f), /禁止自动重复提交/);
    assert.deepEqual(await fs.readFile(f.progress), bytes);
  }
});

test('E losing the ledger lock preserves its owner and creates no submission evidence', async t => {
  const f = await fixture(t, () => assert.fail('must never submit'));
  await fs.writeFile(f.ledger + '.lock', 'another process');
  await assert.rejects(runE(f), /EEXIST/);
  assert.equal(await fs.readFile(f.ledger + '.lock', 'utf8'), 'another process');
  await assert.rejects(fs.access(f.progress));
  assert.equal(f.counts().calls, 0);
});
