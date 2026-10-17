import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import test from 'node:test';
import { RETENTION_FILTERS, retiredCarrierSkipRow, carrierDataAnomalyRow, DATA_ANOMALY_POLICY } from '../scripts/opensms_task_d_filtered_retention.mjs';
import { validateTaskDRetentionSourceProof, validateTaskDRetentionNotificationIdentity,
  assertTaskDNotificationOnly, validateManifest } from '../scripts/opensms_execute_task_d_expiry.mjs';
import { validateFilteredRetentionProof } from '../scripts/opensms_task_d_filtered_retention.mjs';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {
  validateTaskDRetentionCarrierProof, validateTaskDRetentionLiveIdentity,
  enforceTaskAuthorization,
} from '../opensms_set_carrier_candidates.mjs';

test('deleted retention transfer rejects even forced or disguised old candidates', async () => {
  for (const candidate of [
    { task: 'D', forceGate: true, automationGate: true },
    { task: 'A', route: 'task_d_retain_chatr_to_b14_c' },
    { task: 'A', targetCarrierName: 'B14-C快下保留卡' },
  ]) await assert.rejects(enforceTaskAuthorization(candidate, 'B1-测试'), /工作已删除/);
  await assert.rejects(enforceTaskAuthorization({ task: 'A' }, 'B14-T快下保留卡'), /工作已删除/);
  assert.throws(() => assertTaskDNotificationOnly({ phase: 'retention_verified' }), /工作已删除/);
});

test('retention expiry verifies original non-B14 identity; drift and protection cannot be bypassed', () => {
  const input = fixture();
  const expected = validateFilteredRetentionProof(input.receipt).actionableRows[0];
  validateTaskDRetentionNotificationIdentity(input.rows[0], expected, 'A21-测试');
  for (const changes of [{ id: 99 }, { cardNo: '2368999999' }, { opr: 'Kooda' },
    { carrireIdentif: 'b14-c' }, { carrireIdentif: 'renew-c' }]) {
    assert.throws(() => validateTaskDRetentionNotificationIdentity({ ...input.rows[0], ...changes },
      expected, 'A21-测试'), /身份、来源或OPR/);
  }
  assert.throws(() => validateTaskDRetentionNotificationIdentity(input.rows[0], expected, '快过期续费C'));
});

test('notification validation needs no B14 targets and accepts exact original query proof', async t => {
  const input = fixture();
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'opensms-notify-only-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const receiptPath = path.join(root, 'retention.json');
  await fs.writeFile(receiptPath, JSON.stringify(input.receipt));
  input.bind();
  const manifest = { ...input.manifest, task: 'D', forceGate: true, automationGate: true,
    phase: 'retention_verified', serverDate: '2026-09-09', expireDate: '2026-09-16',
    sourceProvenance: 'phone_card_retain_all_pages', retentionReceiptPath: receiptPath,
    retentionCarrierTransferEnabled: false, retentionCarrierTransferStatus: 'removed_by_user' };
  const rows = input.rows.filter(row => input.actionable.includes(row.cardNo));
  const result = await validateManifest(manifest, input.actionable, async (method, endpoint) => {
    assert.equal(method, 'POST'); assert.equal(endpoint, '/phone/config/list');
    return { code: 200, rows, total: rows.length };
  }, input.receipt.carrierConfigs);
  assert.equal(result.backendVerifiedCount, 4);
  let readCount = 0;
  const recovered = await validateManifest(manifest, input.actionable, async (method, endpoint) => {
    assert.equal(method, 'POST'); assert.equal(endpoint, '/phone/config/list');
    readCount += 1;
    return { code: 200, rows: readCount === 1
      ? rows.map((row, index) => index === 0 ? { ...row, opr: 'changed' } : row)
      : [rows[0]], total: readCount === 1 ? rows.length : 1 };
  }, input.receipt.carrierConfigs);
  assert.equal(recovered.backendVerifiedCount, 4);
  assert.equal(readCount, 2);
  await assert.rejects(validateManifest(manifest, input.actionable, async () => ({
    code: 200, rows: rows.map(row => ({ ...row, opr: 'changed' })), total: rows.length,
  }), input.receipt.carrierConfigs), /写前独立验证失败/);
});

function fixture() {
  const cardNos = Array.from({ length: 6 }, (_, index) => String(2368000000 + index));
  const rows = [
    { id: 10, cardNo: cardNos[0], opr: 'Chatr', carrireIdentif: 'source-c' },
    { id: 11, cardNo: cardNos[1], opr: 'Chatr', carrireIdentif: 'source-c' },
    { id: 12, cardNo: cardNos[2], opr: 'Kooda', carrireIdentif: 'source-t' },
    { id: 13, cardNo: cardNos[3], opr: 'Chatr', carrireIdentif: 'renew-c' },
    { id: 15, cardNo: cardNos[5], opr: 'Telus1', carrireIdentif: 'source-t' },
  ];
  const excluded = [{ cardNo: cardNos[3], sourceCarrierName: '快过期续费C', sourceCarrierIdentif: 'renew-c', backendId: 13 }];
  const receipt = {
    task: 'D', runId: 'offline-filtered-guards', sourceProvenance: 'phone_card_retain_all_pages',
    endpoint: '/phone/cardRetain/list', pageSize: 200, pageCount: 1,
    totalRecords: 6, fetchedRecords: 6, uniqueCardCount: 6, cardNos,
    records: cardNos.map((cardNo, id) => ({ id, cardNo })), pages: [{ pageNum: 1, rowCount: 6 }],
    filteredQueryProof: { version: 1, queries: RETENTION_FILTERS.map(spec => {
      const matches = rows.filter(row => row.opr.toLowerCase().includes(spec.filter));
      return { ...spec, method: 'POST', endpoint: '/phone/config/list', cardNos: [...cardNos],
        pages: [{ code: 200, pageNum: 1, pageSize: 500, total: matches.length, rows: structuredClone(matches) }] };
    }) },
    carrierConfigs: [
      { carrireIdentif: 'source-c', name: 'A21-测试' },
      { carrireIdentif: 'source-t', name: 'B1-测试' },
      { carrireIdentif: 'renew-c', name: '快过期续费C' },
    ],
    neverProcessSourceExcludedRows: excluded, neverProcessSourceExcludedCount: 1,
    filteredUnmatchedCards: [cardNos[4]], filteredUnmatchedCount: 1,
  };
  const actionable = [cardNos[0], cardNos[1], cardNos[2], cardNos[5]];
  const manifest = {
    runId: receipt.runId, cardNos: actionable,
    neverProcessSourceExcludedCards: [cardNos[3]], neverProcessSourceExcludedCount: 1,
    filteredUnmatchedCards: [cardNos[4]], filteredUnmatchedCount: 1,
  };
  const candidateRows = rows.slice(0, 2).map(row => ({
    '电话': row.cardNo, phone: row.cardNo, cardNo: row.cardNo,
    '运营商': 'A21-测试', sourceCarrierName: 'A21-测试', sourceCarrierIdentif: row.carrireIdentif,
    backendId: row.id, opr: row.opr, oprClass: 'chatr', expectedTargetCarrierName: 'B14-C快下保留卡',
  }));
  const candidate = {
    task: 'D', runId: receipt.runId, targetCarrierName: 'B14-C快下保留卡',
    sourceCarrierNames: ['A21-测试'], count: candidateRows.length, rows: candidateRows,
    candidates: structuredClone(candidateRows),
  };
  const bind = () => {
    const sha = crypto.createHash('sha256').update(JSON.stringify(receipt)).digest('hex');
    candidate.retentionReceiptSha256 = sha; manifest.retentionReceiptSha256 = sha;
    return sha;
  };
  return { cardNos, rows, receipt, manifest, candidate, actionable, bind };
}

test('expiry accepts exactly filtered hits minus protected rows and proves the skipped source numbers', () => {
  const input = fixture();
  assert.deepEqual(validateTaskDRetentionSourceProof(input.manifest, input.actionable, input.receipt, input.bind()), {
    sourceCount: 6, notificationCount: 4, protectedExcludedCount: 1,
    unconfiguredCarrierSkippedCount: 0,
    retiredCarrierSkippedCount: 0,
    filteredUnmatchedCount: 1, filteredQueryProofValidated: true,
  });
});

test('notification partition requires exact unconfigured exclusion and never allows notifying it', () => {
  const input = fixture();
  const raw = input.receipt.filteredQueryProof.queries[0].pages[0].rows[0];
  Object.assign(raw, {carrireIdentif: null, displayStatus: '未配置运营商',
    showAble: true, showInTable: true, userAble: false, slotIdentif: '210_18.01'});
  const phone = raw.cardNo;
  input.receipt.unconfiguredCarrierSkippedRows = [{cardNo: phone, backendId: String(raw.id),
    slot: raw.slotIdentif, opr: raw.opr, reason: 'carrier_unconfigured'}];
  input.receipt.unconfiguredCarrierSkippedCards = [phone];
  input.receipt.unconfiguredCarrierSkippedCount = 1;
  input.manifest.unconfiguredCarrierSkippedCards = [phone];
  input.manifest.unconfiguredCarrierSkippedCount = 1;
  input.actionable = input.actionable.filter(value => value !== phone);
  const check = cards => validateTaskDRetentionSourceProof(input.manifest, cards, input.receipt, input.bind());
  assert.equal(check(input.actionable).unconfiguredCarrierSkippedCount, 1);
  assert.throws(() => check([...input.actionable, phone]), /通知集合/);
  input.manifest.unconfiguredCarrierSkippedCards = [];
  assert.throws(() => check(input.actionable), /未配置运营商排除/);
});

test('retired B14 skip is independently validated before notification; forged or included skips stop', () => {
  const input = fixture();
  const raw = input.receipt.filteredQueryProof.queries[0].pages[0].rows[0];
  raw.carrireIdentif = '2093378431675928577';
  const skipped = retiredCarrierSkipRow(raw, input.receipt.carrierConfigs);
  input.receipt.retiredCarrierSkippedRows = [skipped];
  input.receipt.retiredCarrierSkippedCards = [raw.cardNo];
  input.receipt.retiredCarrierSkippedCount = 1;
  input.manifest.retiredCarrierSkippedCards = [raw.cardNo];
  input.manifest.retiredCarrierSkippedCount = 1;
  input.actionable = input.actionable.filter(phone => phone !== raw.cardNo);
  const check = cards => validateTaskDRetentionSourceProof(input.manifest, cards, input.receipt, input.bind());
  assert.equal(check(input.actionable).retiredCarrierSkippedCount, 1);
  assert.throws(() => check([...input.actionable, raw.cardNo]), /通知集合/);
  input.manifest.retiredCarrierSkippedCards = [];
  assert.throws(() => check(input.actionable), /已删除运营商排除/);
});

test('future anomalous row never reaches notification; missing or forged manifest partition fails before writes', () => {
  const input = fixture();
  const raw = input.receipt.filteredQueryProof.queries[0].pages[0].rows[0];
  raw.carrireIdentif = null;
  input.receipt.filteredQueryProof.dataAnomalyPolicy = DATA_ANOMALY_POLICY;
  const fields = {dataAnomalyPolicy: DATA_ANOMALY_POLICY, dataAnomalySkippedCount: 1,
    dataAnomalySkippedCards: [raw.cardNo], dataAnomalySkippedRows: [carrierDataAnomalyRow(raw, input.receipt.carrierConfigs)]};
  Object.assign(input.receipt, fields);
  Object.assign(input.manifest, fields);
  input.actionable = input.actionable.filter(phone => phone !== raw.cardNo);
  const check = cards => validateTaskDRetentionSourceProof(input.manifest, cards, input.receipt, input.bind());
  assert.equal(check(input.actionable).dataAnomalySkippedCount, 1);
  assert.throws(() => check([...input.actionable, raw.cardNo]), /通知集合/);
  input.manifest.dataAnomalySkippedCards = [];
  assert.throws(() => check(input.actionable), /异常数据排除/);
});

test('expiry rejects omitted hits, forged skip lists, intersecting partitions, and run drift', () => {
  const changes = [
    input => input.actionable.pop(),
    input => { input.manifest.filteredUnmatchedCards = [input.cardNos[0]]; },
    input => { input.manifest.filteredUnmatchedCount = 0; },
    input => { input.manifest.neverProcessSourceExcludedCards = [input.cardNos[0]]; },
    input => { input.manifest.runId = 'other-run'; },
    input => { input.receipt.filteredUnmatchedCards = [input.cardNos[0]]; },
    input => { input.receipt.records[0].cardNo = '2368999999'; },
    input => { input.receipt.filteredQueryProof.queries.pop(); },
    input => { input.receipt.filteredQueryProof.queries[0].cardNos.pop(); },
    input => { input.receipt.neverProcessSourceExcludedRows[0].backendId = 99; },
    input => { input.receipt.filteredQueryProof = null; },
  ];
  for (const change of changes) {
    const input = fixture(); change(input);
    assert.throws(() => validateTaskDRetentionSourceProof(input.manifest, input.actionable, input.receipt, input.bind()));
  }
});

test('carrier proof binds exact target group, receipt SHA, run, and backend identities', () => {
  const input = fixture();
  const result = validateTaskDRetentionCarrierProof(input.candidate, 'B14-C快下保留卡', input.receipt, input.bind());
  assert.equal(result.actionableRows.length, 4);
  assert.equal(result.unmatchedCards.length, 1);
});

test('carrier proof rejects omitted candidates and substitutions from another group, source, or skipped set', () => {
  for (const replacement of [null, '2368999999', '2368000002', '2368000003', '2368000004']) {
    const input = fixture();
    if (replacement === null) input.candidate.rows.pop();
    else input.candidate.rows[0] = { ...input.candidate.rows[0], '电话': replacement, phone: replacement, cardNo: replacement };
    input.candidate.candidates = structuredClone(input.candidate.rows);
    input.candidate.count = input.candidate.rows.length;
    assert.throws(() => validateTaskDRetentionCarrierProof(input.candidate, 'B14-C快下保留卡', input.receipt, input.bind()), /精确覆盖/);
  }
});

test('carrier proof rejects edited identity, source, OPR, target, duplicate, and alternate row evidence', () => {
  for (const change of [
    input => { input.candidate.rows[0].backendId = 99; },
    input => { input.candidate.rows[0].sourceCarrierIdentif = 'other'; },
    input => { input.candidate.rows[0]['运营商'] = '快过期续费C'; },
    input => { input.candidate.rows[0].opr = 'Telus1'; },
    input => { input.candidate.rows[0].oprClass = 'kooda'; },
    input => { input.candidate.rows[0].expectedTargetCarrierName = 'B14-T快下保留卡'; },
    input => { input.candidate.rows[0].cardNo = input.cardNos[2]; },
    input => { input.candidate.candidates[0].backendId = 99; },
    input => { input.candidate.runId = 'other'; },
    input => { input.candidate.rows[1] = { ...input.candidate.rows[0] }; input.candidate.candidates = structuredClone(input.candidate.rows); },
  ]) {
    const input = fixture(); change(input);
    assert.throws(() => validateTaskDRetentionCarrierProof(input.candidate, 'B14-C快下保留卡', input.receipt, input.bind()));
  }
  const input = fixture(); const sha = input.bind();
  delete input.candidate.retentionReceiptSha256;
  assert.throws(() => validateTaskDRetentionCarrierProof(input.candidate, 'B14-C快下保留卡', input.receipt, sha), /SHA-256/);
  input.candidate.retentionReceiptSha256 = sha;
  assert.throws(() => validateTaskDRetentionCarrierProof(input.candidate, 'B14-C快下保留卡', input.receipt, 'f'.repeat(64)), /SHA-256/);
});

test('carrier live read accepts original or exact target identity and rejects backend/OPR/source drift', () => {
  const input = fixture();
  const target = { name: 'B14-C快下保留卡', identif: 'target-c' };
  const live = structuredClone(input.rows.slice(0, 2));
  assert.doesNotThrow(() => validateTaskDRetentionLiveIdentity(input.candidate, live, target));
  live[0].carrireIdentif = 'target-c';
  assert.doesNotThrow(() => validateTaskDRetentionLiveIdentity(input.candidate, live, target));
  for (const change of [row => { row.id = 99; }, row => { row.opr = 'Kooda'; },
    row => { row.opr = 'Chatr Telus1'; }, row => { row.carrireIdentif = 'renew-c'; }]) {
    const rows = structuredClone(live); change(rows[0]);
    assert.throws(() => validateTaskDRetentionLiveIdentity(input.candidate, rows, target), /偏离筛选证明/);
  }
});
