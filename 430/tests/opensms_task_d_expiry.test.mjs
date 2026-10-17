import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import test from 'node:test';
import { assertNoTaskDReplay, MARKER_BATCH_SIZE, submitTaskDMarkerBatches, validateTaskDRetentionSourceProof, validateTaskDContinuation } from '../scripts/opensms_execute_task_d_expiry.mjs';

async function fixture(t, count = 45) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'opensms-task-d-offline-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const cards = Array.from({ length: count }, (_, index) => String(2368000000 + index));
  const manifest = { task: 'D', phase: 'swap_zero', expireDate: '2026-09-05' };
  const receipt = { task: 'D', phase: manifest.phase, candidateCount: count, markerBatchSize: MARKER_BATCH_SIZE, batches: [] };
  let clockMs = 0;
  return { root, cards, manifest, receipt, progressPath: path.join(root, 'progress.json'),
    submissionOptions: { clock: () => clockMs, wait: async ms => { clockMs += ms; } } };
}

function success(batch) {
  return { code: 200, data: { inputCount: batch.length, matchedCardCount: batch.length, updatedChatCount: batch.length, notFound: [] } };
}

function continuationFixture(cards) {
  const manifestPath = '/private/fixture/manifest.json';
  const manifest = { task: 'D', phase: 'retention_verified', serverDate: '2026-10-10', expireDate: '2026-10-17' };
  const completed = cards.slice(0, 20), pending = cards.slice(20, 40);
  const digest = crypto.createHash('sha256').update(pending.join('\n')).digest('hex');
  const prior = { task: 'D', phase: manifest.phase, status: 'failed', serverDate: manifest.serverDate,
    expireDate: manifest.expireDate, manifestPath, candidateCount: cards.length, markerBatchSize: 20,
    failureClass: 'submission_outcome_uncertain', uncertainSubmission: true, uncertainBatchCount: 1,
    error: 'POST /phone/config/batchSetChatExpire HTTP 502；未将拒绝响应当作登录失效',
    confirmedInputCount: 20, uncertainBatch: { inputCount: pending.length, cardNosSha256: digest },
    pendingBatch: { batchNumber: 2, cardNos: pending, inputCount: pending.length, expireDate: manifest.expireDate, cardNosSha256: digest },
    batches: [{ batchNumber: 1, cardNos: completed, expireDate: manifest.expireDate,
      ...success(completed).data, serverResponse: success(completed), businessReceiptPassed: true,
      transactionalVerificationPassed: true }] };
  return { manifestPath, manifest, prior };
}

test('manual D continuation sends only the unconfirmed tail, preserving prior bytes and total counts', async t => {
  const input = await fixture(t), spec = continuationFixture(input.cards);
  const before = JSON.stringify(spec.prior);
  const proof = validateTaskDContinuation(spec.prior, spec.manifest, input.cards, spec.manifestPath);
  assert.equal(proof.confirmedCount, 20);
  assert.deepEqual(proof.remainingCards, input.cards.slice(20));
  input.receipt.batches = proof.batches;
  const sent = [];
  await submitTaskDMarkerBatches({ ...input, cards: proof.remainingCards, manifest: spec.manifest,
    api: async (_m, _e, data) => { sent.push(...data.cardNos); return success(data.cardNos); } });
  assert.deepEqual(sent, input.cards.slice(20));
  assert.deepEqual(input.receipt.batches.map(b => b.batchNumber), [1, 2, 3]);
  assert.equal(input.receipt.batches.reduce((n, b) => n + b.inputCount, 0), 45);
  assert.equal(JSON.stringify(spec.prior), before);
});

test('manual D continuation rejects wrong dates, phases, counts, prefix evidence, payloads and hashes', () => {
  const cards = Array.from({ length: 45 }, (_, n) => String(2368000000 + n));
  const mutations = [
    p => { p.error = 'HTTP 504'; }, p => { p.phase = 'swap_zero'; },
    p => { p.serverDate = '2026-10-11'; }, p => { p.confirmedInputCount = 0; },
    p => { p.batches[0].serverResponse.data.matchedCardCount = 19; },
    p => { p.batches[0].cardNos.reverse(); }, p => { p.batches[0].transactionalVerificationPassed = false; },
    p => { p.pendingBatch.cardNos[0] = cards[0]; }, p => { p.pendingBatch.cardNosSha256 = '0'.repeat(64); },
    p => { p.uncertainBatch.cardNosSha256 = '0'.repeat(64); }, p => { p.candidateCount--; },
    p => { p.pendingBatch.batchNumber++; }, p => { p.manifestPath = '/another/manifest.json'; },
  ];
  for (const mutate of mutations) {
    const spec = continuationFixture(cards); mutate(spec.prior);
    assert.throws(() => validateTaskDContinuation(spec.prior, spec.manifest, cards, spec.manifestPath));
  }
});

test('Task D uses 20/20/5 strictly serial write batches, preserving confirmed receipt counts', async t => {
  const input = await fixture(t);
  const sizes = [];
  let active = 0;
  let maximum = 0;
  await submitTaskDMarkerBatches({ ...input, api: async (method, endpoint, data) => {
    assert.equal(method, 'POST');
    assert.equal(endpoint, '/phone/config/batchSetChatExpire');
    assert.equal(data.expireDate, input.manifest.expireDate);
    sizes.push(data.cardNos.length);
    active += 1;
    maximum = Math.max(maximum, active);
    const pending = JSON.parse(await fs.readFile(input.progressPath, 'utf8'));
    assert.equal(pending.status, 'submitting');
    assert.deepEqual(pending.pendingBatch.cardNos, data.cardNos);
    await delay(2);
    active -= 1;
    return success(data.cardNos);
  } });
  assert.equal(MARKER_BATCH_SIZE, 20);
  assert.deepEqual(sizes, [20, 20, 5]);
  assert.equal(maximum, 1);
  assert.equal(input.receipt.batches.reduce((total, row) => total + row.inputCount, 0), 45);
  assert.ok(input.receipt.batches.every(row => row.transactionalVerificationPassed && row.businessReceiptPassed));
  const progress = JSON.parse(await fs.readFile(input.progressPath, 'utf8'));
  assert.equal(progress.status, 'in_progress');
  assert.equal(progress.batches.length, 3);
});

test('HTTP 504 preserves the uncertain batch and stops without retrying or starting the next batch', async t => {
  const input = await fixture(t);
  let calls = 0;
  await assert.rejects(submitTaskDMarkerBatches({ ...input, api: async (_method, _endpoint, data) => {
    calls += 1;
    if (calls === 2) throw new Error('HTTP 504 Gateway Timeout');
    return success(data.cardNos);
  } }), /HTTP 504/);
  assert.equal(calls, 2);
  const before = await fs.readFile(input.progressPath, 'utf8');
  const progress = JSON.parse(before);
  assert.equal(progress.status, 'failed');
  assert.equal(progress.failureClass, 'submission_outcome_uncertain');
  assert.equal(progress.uncertainSubmission, true);
  assert.equal(progress.automaticRetryBlocked, true);
  assert.equal(progress.confirmedInputCount, 20);
  assert.equal(progress.uncertainBatchCount, 1);
  assert.equal(progress.uncertainBatch.inputCount, 20);
  assert.deepEqual(progress.pendingBatch.cardNos, input.cards.slice(20, 40));
  assert.equal(progress.uncertainBatch.cardNosSha256, crypto.createHash('sha256').update(input.cards.slice(20, 40).join('\n')).digest('hex'));
  await assert.rejects(submitTaskDMarkerBatches({ ...input, api: () => assert.fail('must never replay') }), /禁止自动重复提交/);
  assert.equal(await fs.readFile(input.progressPath, 'utf8'), before);
});

test('D confirmed HTTP 502 retries only the current frozen batch after cooldown and recheck', async t => {
  const input = await fixture(t);
  let calls = 0, rechecks = 0;
  input.submissionOptions.beforeRetry = async ({ cards }) => {
    assert.deepEqual(cards, input.cards.slice(20, 40)); rechecks++; return { passed: true };
  };
  await submitTaskDMarkerBatches({ ...input, api: async (_m, _e, data) => {
    calls++;
    if (calls === 2) throw Object.assign(Error('HTTP 502'), { status: 502 });
    return { code: 200, data: { ...success(data.cardNos).data, updatedChatCount: 0 } };
  } });
  assert.equal(calls, 4); assert.equal(rechecks, 1);
  assert.deepEqual(input.receipt.batches.map(b => b.inputCount), [20, 20, 5]);
  assert.equal(input.receipt.batches[1].submission.retryCount, 1);
  assert.equal(input.receipt.batches[1].submission.waits[1].actualMs, 60000);
  assert.equal(input.receipt.batches.reduce((n, b) => n + b.matchedCardCount, 0), 45);
});

test('D exhausted HTTP 502 keeps uncertainty, confirmed batches, and replay barrier', async t => {
  const input = await fixture(t);
  let calls = 0;
  input.submissionOptions.beforeRetry = async () => ({ passed: true });
  await assert.rejects(submitTaskDMarkerBatches({ ...input, api: async (_m, _e, data) => {
    if (++calls === 1) return success(data.cardNos);
    throw Object.assign(Error('HTTP 502'), { status: 502 });
  } }), /502/);
  assert.equal(calls, 4);
  const progress = JSON.parse(await fs.readFile(input.progressPath));
  assert.equal(progress.confirmedInputCount, 20);
  assert.equal(progress.pendingBatch.submission.attempts.length, 3);
  assert.equal(progress.uncertainBatch.inputCount, 20);
  assert.equal(progress.uncertainSubmission, true);
  await assert.rejects(submitTaskDMarkerBatches({ ...input, api: () => assert.fail('old run must not retry') }), /禁止自动重复提交/);
});

test('network errors and invalid business receipts preserve raw evidence and never count success', async t => {
  for (const response of [
    new Error('socket hang up'),
    { code: 200, data: { inputCount: 20, matchedCardCount: 20, updatedChatCount: -1, notFound: [] } },
    { code: 200, data: { inputCount: 20, matchedCardCount: 19, updatedChatCount: 19, notFound: ['2368000000'] } },
    { code: 500, message: 'backend failed' },
  ]) {
    const input = await fixture(t, 20);
    await assert.rejects(submitTaskDMarkerBatches({ ...input, api: async () => {
      if (response instanceof Error) throw response;
      return response;
    } }));
    const progress = JSON.parse(await fs.readFile(input.progressPath, 'utf8'));
    assert.equal(progress.confirmedInputCount, 0);
    assert.equal(progress.batches.length, 0);
    assert.equal(progress.failureClass, 'submission_outcome_uncertain');
    assert.equal(progress.automaticRetryBlocked, true);
    if (!(response instanceof Error)) assert.deepEqual(progress.pendingBatch.response, response);
  }
});

test('D accepts full matches with zero or fewer changed conversations in both branches', async t => {
  for (const phase of ['swap_zero', 'retention_verified']) {
    for (const updated of [0, 1, 19]) {
      const input = await fixture(t, 21);
      input.manifest.phase = input.receipt.phase = phase;
      let calls = 0;
      await submitTaskDMarkerBatches({ ...input, api: async (_m, _e, data) => {
        calls++;
        const response = success(data.cardNos);
        response.data.updatedChatCount = updated;
        return response;
      } });
      assert.equal(calls, 2);
      assert.equal(input.receipt.batches.reduce((n, b) => n + b.matchedCardCount, 0), 21);
      assert.equal(input.receipt.batches.reduce((n, b) => n + b.matchedNoChangeCount, 0), updated === 0 ? 21 : 0);
    }
  }
});

test('existing progress and successful receipts block before any browser access', async t => {
  const input = await fixture(t, 0);
  const manifestPath = path.join(input.root, 'manifest.json');
  const receiptPath = path.join(input.root, 'opensms_task_d_swap_zero_replay_receipt.json');
  await fs.writeFile(manifestPath, JSON.stringify({ ...input.manifest, cardNos: [], count: 0 }));
  await fs.writeFile(receiptPath, JSON.stringify({ status: 'passed', inputCount: 20 }));
  await assert.rejects(assertNoTaskDReplay(input.progressPath, receiptPath), /禁止自动重复提交/);
  const before = await fs.readFile(receiptPath, 'utf8');
  const result = spawnSync(process.execPath, [
    new URL('../scripts/opensms_execute_task_d_expiry.mjs', import.meta.url).pathname,
    manifestPath, 'replay', input.root,
  ], {
    encoding: 'utf8',
    env: { ...process.env, OPENSMS_ENABLE_TASK_D: '1', OPENSMS_FORCE_TASK_D: '1', OPENSMS_EXECUTE_TASK_D: '1', BITBROWSER_CDP: 'http://127.0.0.1:1' },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /禁止自动重复提交/);
  assert.equal(await fs.readFile(receiptPath, 'utf8'), before);
  assert.equal((await fs.readdir(input.root)).some(name => name.endsWith('.lock')), false);
});

test('another process lock blocks immediately and is never removed by the losing process', async t => {
  const input = await fixture(t, 0);
  const manifestPath = path.join(input.root, 'manifest.json');
  const lockPath = path.join(input.root, 'opensms_task_d_swap_zero_locked_progress.json.lock');
  await fs.writeFile(manifestPath, JSON.stringify({ ...input.manifest, cardNos: [], count: 0 }));
  await fs.writeFile(lockPath, 'owned by another process');
  const result = spawnSync(process.execPath, [
    new URL('../scripts/opensms_execute_task_d_expiry.mjs', import.meta.url).pathname,
    manifestPath, 'locked', input.root,
  ], {
    encoding: 'utf8',
    env: { ...process.env, OPENSMS_ENABLE_TASK_D: '1', OPENSMS_FORCE_TASK_D: '1', OPENSMS_EXECUTE_TASK_D: '1', BITBROWSER_CDP: 'http://127.0.0.1:1' },
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /EEXIST/);
  assert.equal(await fs.readFile(lockPath, 'utf8'), 'owned by another process');
});

function retentionProofFixture() {
  const cards = ['2368000000', '2368000001'];
  const excluded = [
    { cardNo: '2368000002', sourceCarrierName: '快过期续费C', sourceCarrierIdentif: 'renew-c', backendId: 12 },
    { cardNo: '2368000003', sourceCarrierName: '快过期续费T', sourceCarrierIdentif: 'renew-t', backendId: 13 },
  ];
  const receipt = {
    task: 'D',
    sourceProvenance: 'phone_card_retain_all_pages',
    endpoint: '/phone/cardRetain/list',
    cardNos: [...cards, ...excluded.map(row => row.cardNo)],
    uniqueCardCount: 4,
    neverProcessSourceExcludedCount: excluded.length,
    neverProcessSourceExcludedRows: excluded,
  };
  const manifest = {
    neverProcessSourceExcludedCount: excluded.length,
    neverProcessSourceExcludedCards: excluded.map(row => row.cardNo),
  };
  function bindReceipt() {
    const sha = crypto.createHash('sha256').update(JSON.stringify(receipt)).digest('hex');
    manifest.retentionReceiptSha256 = sha;
    return sha;
  }
  return { cards, excluded, receipt, manifest, bindReceipt };
}

test('retention source proof accepts only the exact union of notifications and protected exclusions', () => {
  const input = retentionProofFixture();
  const result = validateTaskDRetentionSourceProof(input.manifest, input.cards, input.receipt, input.bindReceipt());
  assert.deepEqual(result, { sourceCount: 4, notificationCount: 2, protectedExcludedCount: 2 });
  input.receipt.neverProcessSourceExcludedRows = [];
  input.receipt.neverProcessSourceExcludedCount = 0;
  input.receipt.cardNos = input.cards;
  input.receipt.uniqueCardCount = 2;
  input.manifest.neverProcessSourceExcludedCards = [];
  input.manifest.neverProcessSourceExcludedCount = 0;
  assert.deepEqual(validateTaskDRetentionSourceProof(input.manifest, input.cards, input.receipt, input.bindReceipt()), {
    sourceCount: 2, notificationCount: 2, protectedExcludedCount: 0,
  });
});

test('retention source proof rejects partial subsets, outside numbers and intersecting sets', () => {
  for (const modify of [
    input => { input.cards.pop(); },
    input => { input.cards[0] = '2368999999'; },
    input => { input.cards[0] = input.excluded[0].cardNo; },
    input => { input.cards.push(input.cards[0]); },
  ]) {
    const input = retentionProofFixture();
    modify(input);
    assert.throws(() => validateTaskDRetentionSourceProof(input.manifest, input.cards, input.receipt, input.bindReceipt()), /来源不满足|相交|重复/);
  }
});

test('retention source proof rejects forged non-protected source names or missing backend identities', () => {
  for (const modify of [
    input => { input.excluded[0].sourceCarrierName = 'A1'; },
    input => { input.excluded[0].sourceCarrierName = '快过期续费C-其它'; },
    input => { input.excluded[0].sourceCarrierIdentif = ''; },
    input => { delete input.excluded[0].backendId; },
  ]) {
    const input = retentionProofFixture();
    modify(input);
    assert.throws(() => validateTaskDRetentionSourceProof(input.manifest, input.cards, input.receipt, input.bindReceipt()), /精确续费保护区|后台身份/);
  }
});

test('retention source proof rejects missing hashes, changed bytes and old missing exclusion evidence', () => {
  const input = retentionProofFixture();
  const sha = input.bindReceipt();
  delete input.manifest.retentionReceiptSha256;
  assert.throws(() => validateTaskDRetentionSourceProof(input.manifest, input.cards, input.receipt, sha), /SHA-256/);
  input.manifest.retentionReceiptSha256 = sha;
  assert.throws(() => validateTaskDRetentionSourceProof(input.manifest, input.cards, input.receipt, 'f'.repeat(64)), /SHA-256/);
  delete input.receipt.neverProcessSourceExcludedRows;
  assert.throws(() => validateTaskDRetentionSourceProof(input.manifest, input.cards, input.receipt, input.bindReceipt()), /逐号保护排除证明/);
});

test('retention source proof rejects count drift, duplicate source cards and undeclared exclusions', () => {
  for (const modify of [
    input => { input.manifest.neverProcessSourceExcludedCount = 1; },
    input => { input.receipt.neverProcessSourceExcludedCount = 1; },
    input => { input.receipt.cardNos.push(input.receipt.cardNos[0]); },
    input => { input.manifest.neverProcessSourceExcludedCards[0] = '2368999999'; },
  ]) {
    const input = retentionProofFixture();
    modify(input);
    assert.throws(() => validateTaskDRetentionSourceProof(input.manifest, input.cards, input.receipt, input.bindReceipt()), /不一致|重复/);
  }
});
