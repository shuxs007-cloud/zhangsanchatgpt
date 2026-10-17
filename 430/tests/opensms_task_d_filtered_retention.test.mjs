import test from 'node:test';
import assert from 'node:assert/strict';
import {
  collectFilteredRetention, validateFilteredQueries, validateFilteredRetentionProof,
  retiredCarrierSkipRow, carrierDataAnomalyRow, DATA_ANOMALY_POLICY,
} from '../scripts/opensms_task_d_filtered_retention.mjs';

const phone = index => String(2000000000 + index);
const backend = (index, opr, carrier = 'ordinary') => ({ id: `id-${index}`, cardNo: `1${phone(index)}`, opr, carrireIdentif: carrier });
const unconfigured = () => ({ ...backend(1, 'chatr', null), slotIdentif: '210_18.01',
  displayStatus: '未配置运营商', showAble: true, showInTable: true, userAble: false });
test('future contract quarantines missing and unresolved carriers, preserves all raw hits, never guesses a target', async () => {
  const source = [phone(1), phone(2), phone(3), phone(4)];
  const rows = [backend(1, 'chatr', null), backend(2, 'chatr', 'unknown'), backend(3, 'chatr')];
  const result = await collectFilteredRetention(async (_, __, data) => ({code:200,
    total:data.opr === 'chatr' ? rows.length : 0, rows:data.opr === 'chatr' ? rows : []}), source,
    {dataAnomalyPolicy: DATA_ANOMALY_POLICY});
  const receipt = receiptFor(source, result);
  const anomalies = result.matchedRows.map(row => carrierDataAnomalyRow(row, receipt.carrierConfigs)).filter(Boolean);
  Object.assign(receipt, {dataAnomalyPolicy: DATA_ANOMALY_POLICY, dataAnomalySkippedRows: anomalies,
    dataAnomalySkippedCount: anomalies.length, dataAnomalySkippedCards: anomalies.map(row => row.cardNo)});
  const verified = validateFilteredRetentionProof(receipt);
  assert.equal(verified.matchedRows.length, 3);
  assert.deepEqual(verified.actionableRows.map(row => row.cardNo), [phone(3)]);
  assert.deepEqual(verified.dataAnomalyRows.map(row => row.reason), ['carrier_identity_missing', 'source_carrier_unresolved']);
  assert.deepEqual(verified.unmatchedCards, [phone(4)]);
  for (const mutate of [
    r => { r.dataAnomalySkippedCount = 0; },
    r => { r.dataAnomalySkippedRows[0].reason = 'unmatched'; },
    r => { r.dataAnomalySkippedRows[0].backendId = 'invented'; },
    r => { delete r.filteredQueryProof.dataAnomalyPolicy; },
    r => { r.filteredQueryProof.queries[0].pages[0].rows[0].carrierIdentif = 'other'; },
    r => { r.carrierConfigs.push({identif:'ordinary', name:'duplicate'}); },
  ]) {const changed = structuredClone(receipt); mutate(changed); assert.throws(() => validateFilteredRetentionProof(changed));}
});
function declareRetired(receipt, result) {
  const rows = result.matchedRows.map(row => retiredCarrierSkipRow(row, receipt.carrierConfigs)).filter(Boolean);
  Object.assign(receipt, {retiredCarrierSkippedCount: rows.length,
    retiredCarrierSkippedRows: rows, retiredCarrierSkippedCards: rows.map(row => row.cardNo)});
  return receipt;
}
test('only exact removed B14 IDs become a fifth partition; no notification or reassignment', async () => {
  const source = [phone(1), phone(2), phone(3), phone(4), phone(5)];
  const result = await fixture(source, [
    {...backend(1, 'chatr', '2093378431675928577'), slotIdentif: '210_18.01'},
    backend(2, 'Koodo', '2088852494502465536'), backend(3, 'chatr'),
    backend(4, 'Koodo', 'protected'),
  ]);
  const receipt = declareRetired(receiptFor(source, result), result);
  const proof = validateFilteredRetentionProof(receipt);
  assert.deepEqual(proof.retiredRows.map(row => row.cardNo), [phone(1), phone(2)]);
  assert.deepEqual(proof.actionableRows.map(row => row.cardNo), [phone(3)]);
  assert.deepEqual(proof.unmatchedCards, [phone(5)]);
  assert.equal(proof.protectedRows.length, 1);
  for (const mutate of [
    r => { r.retiredCarrierSkippedRows = []; },
    r => { r.retiredCarrierSkippedCount = 0; },
    r => { r.retiredCarrierSkippedCards = [phone(3), phone(2)]; },
    r => { r.retiredCarrierSkippedRows[0].backendId = 'invented'; },
    r => { r.retiredCarrierSkippedRows[0].slot = 'changed'; },
    r => { r.retiredCarrierSkippedRows[0].retiredCarrierName = 'B1'; },
    r => { r.retiredCarrierSkippedRows[0].reason = 'unmatched'; },
    r => { r.carrierConfigs.push({identif:'2093378431675928577', name:'B14-C快下保留卡'}); },
    r => { r.filteredQueryProof.queries[0].pages[0].rows[0].carrierIdentif = 'ordinary'; },
    r => { r.filteredQueryProof.queries[0].pages[0].rows[0].name = '快过期续费C'; },
    r => { r.filteredQueryProof.queries[0].pages[0].rows[0].carrireIdentif = '2088852494502465536'; },
  ]) { const changed = structuredClone(receipt); mutate(changed); assert.throws(() => validateFilteredRetentionProof(changed)); }
});
test('unknown non-B14 IDs still stop and cannot be declared retired', async () => {
  const source = [phone(1)];
  const result = await fixture(source, [backend(1, 'chatr', 'unknown')]);
  const receipt = receiptFor(source, result);
  assert.throws(() => validateFilteredRetentionProof(receipt), /无法唯一解析/);
  receipt.retiredCarrierSkippedCards = source;
  receipt.retiredCarrierSkippedCount = 1;
  receipt.retiredCarrierSkippedRows = [{cardNo:phone(1)}];
  assert.throws(() => validateFilteredRetentionProof(receipt), /已删除运营商排除/);
});
test('saved failing run replays locally without API or business writes', {
  skip: !process.env.OPENSMS_TASK_D_REPLAY_MAPPING,
}, async () => {
  const fs = await import('node:fs/promises');
  const mapping = JSON.parse(await fs.readFile(process.env.OPENSMS_TASK_D_REPLAY_MAPPING, 'utf8'));
  const snapshot = JSON.parse(await fs.readFile(process.env.OPENSMS_TASK_D_REPLAY_CARRIER_SNAPSHOT, 'utf8'));
  assert.equal(mapping.runId, snapshot.payload.runId);
  assert.equal(mapping.errorCount, 12);
  const result = validateFilteredQueries(mapping.retention.cardNos, mapping.filteredQueryProof);
  const carrierConfigs = snapshot.payload.rows.map(row => ({identif:row.carrierIdentif, name:row.carrierName}));
  const names = new Map(carrierConfigs.map(row => [row.identif, row.name]));
  const protectedRows = result.matchedRows.filter(row => ['快过期续费C', '快过期续费T'].includes(names.get(row.carrireIdentif)))
    .map(row => ({cardNo:row.cardNo, sourceCarrierName:names.get(row.carrireIdentif),
      sourceCarrierIdentif:row.carrireIdentif, backendId:row.id}));
  const receipt = declareRetired({...mapping.retention, runId:mapping.runId, task:'D',
    sourceProvenance:'phone_card_retain_all_pages', endpoint:'/phone/cardRetain/list',
    carrierConfigs, filteredQueryProof:mapping.filteredQueryProof,
    filteredUnmatchedCards:result.unmatchedCards, filteredUnmatchedCount:result.unmatchedCards.length,
    unconfiguredCarrierSkippedRows:result.unconfiguredRows,
    unconfiguredCarrierSkippedCards:result.unconfiguredRows.map(row => row.cardNo),
    unconfiguredCarrierSkippedCount:result.unconfiguredRows.length,
    neverProcessSourceExcludedRows:protectedRows, neverProcessSourceExcludedCount:protectedRows.length}, result);
  const verified = validateFilteredRetentionProof(receipt);
  assert.equal(verified.retiredRows.length, 12);
  assert.deepEqual(new Set(verified.retiredRows.map(row => row.cardNo)), new Set(mapping.errors.map(row => row.cardNo)));
  assert.equal(verified.actionableRows.length + verified.protectedRows.length + verified.unmatchedCards.length
    + verified.unconfiguredRows.length + verified.retiredRows.length, receipt.uniqueCardCount);
  console.log(JSON.stringify({offlineReplay:true, runId:mapping.runId, sourceCount:receipt.uniqueCardCount,
    retiredSkipped:verified.retiredRows.length, actionableCount:verified.actionableRows.length, businessWrites:0}));
});
async function rejectRow(row, expectedCategory, requested = [phone(1)]) {
  let saved;
  await assert.rejects(() => collectFilteredRetention(async (_, __, data) => ({
    code: 200, total: data.opr === 'chatr' ? 1 : 0, rows: data.opr === 'chatr' ? [row] : [],
  }), requested, {onProof: async proof => { saved = proof; }}), error => {
    assert.equal(error.code, 'OPENSMS_TASK_D_ROW_INVALID');
    assert.equal(error.category, expectedCategory);
    assert.equal(error.details.slot, row.slotIdentif || '');
    assert.match(error.message, /未提交通知/);
    return true;
  });
  assert.equal(saved.queries.length, 4);
}
test('explicit unconfigured carrier is separately excluded, never unmatched or actionable', async () => {
  const source = [phone(1), phone(2), phone(3)];
  const result = await fixture(source, [unconfigured(), backend(2, 'Koodo')]);
  const receipt = receiptFor(source, result);
  const verified = validateFilteredRetentionProof(receipt);
  assert.equal(verified.matchedRows.length, 2);
  assert.deepEqual(verified.actionableRows.map(row => row.cardNo), [phone(2)]);
  assert.deepEqual(verified.unmatchedCards, [phone(3)]);
  assert.deepEqual(verified.unconfiguredRows, [{cardNo: phone(1), backendId: 'id-1',
    slot: '210_18.01', opr: 'chatr', reason: 'carrier_unconfigured'}]);
  for (const mutate of [
    r => { r.unconfiguredCarrierSkippedRows = []; },
    r => { r.unconfiguredCarrierSkippedCount = 0; },
    r => { r.unconfiguredCarrierSkippedCards = [phone(2)]; },
    r => { r.unconfiguredCarrierSkippedRows[0].backendId = 'fake'; },
    r => { r.filteredUnmatchedCards.push(phone(1)); r.filteredUnmatchedCount++; },
  ]) { const changed = structuredClone(receipt); mutate(changed); assert.throws(() => validateFilteredRetentionProof(changed)); }
});
test('malformed or conflicting fields never masquerade as explicitly unconfigured', async () => {
  for (const patch of [
    {displayStatus: '注册失败'}, {showAble: false}, {showInTable: false}, {userAble: 'false'},
    {carrireIdentif: {}}, {carrireIdentif: 0}, {carrireIdentif: false}, {name: '快过期续费C'},
    {carrireIdentif: 'protected'},
  ]) await rejectRow({...unconfigured(), ...patch}, 'carrier_identity_missing');
});
test('outside input and missing backend ID errors are distinct from missing carrier', async () => {
  await rejectRow({...unconfigured(), cardNo: phone(99)}, 'outside_requested');
  await rejectRow({...unconfigured(), id: null}, 'backend_id_missing');
});
test('complete raw proof is preserved before rejecting missing carrier identity', async () => {
  let saved;
  await assert.rejects(() => collectFilteredRetention(async (_, __, data) => {
    const rows = data.opr === 'koo' ? [{...backend(1, 'Koodo'), carrireIdentif: null}] : [];
    return {code: 200, total: rows.length, rows};
  }, [phone(1)], {onProof: async proof => { saved = structuredClone(proof); }}), /后台身份缺失/);
  assert.equal(saved.queries.length, 4);
  assert.equal(saved.queries.find(query => query.filter === 'koo').pages[0].rows[0].carrireIdentif, null);
});
test('evidence persistence failure cannot publish successful collection', async () => {
  await assert.rejects(() => collectFilteredRetention(async () => ({code:200,total:0,rows:[]}),
    [phone(1)], {onProof: async () => { throw new Error('evidence write failed'); }}), /evidence write failed/);
});
async function fixture(source, rows) {
  return collectFilteredRetention(async (method, endpoint, data, query) => {
    assert.equal(method, 'POST'); assert.equal(endpoint, '/phone/config/list');
    assert.deepEqual(Object.keys(data).sort(), ['cardNo', 'opr', 'pageNum', 'pageSize']);
    assert.deepEqual(query, { pageNum: data.pageNum, pageSize: 500 });
    const requested = new Set(data.cardNo.split('\n'));
    assert.ok(requested.size <= 500);
    const hits = rows.filter(row => requested.has(row.cardNo.slice(-10)) && row.opr.toLowerCase().includes(data.opr));
    return { code: 200, total: hits.length, rows: hits.slice((data.pageNum - 1) * 500, data.pageNum * 500) };
  }, source);
}
function receiptFor(source, result) {
  const protectedRows = result.matchedRows.filter(row => row.carrireIdentif === 'protected').map(row => ({
    cardNo: row.cardNo, sourceCarrierName: '快过期续费C', sourceCarrierIdentif: 'protected', backendId: row.id,
  }));
  return {
    task: 'D', runId: 'test-run', sourceProvenance: 'phone_card_retain_all_pages', endpoint: '/phone/cardRetain/list',
    cardNos: source, uniqueCardCount: source.length, totalRecords: source.length, fetchedRecords: source.length,
    pageSize: 200, pageCount: Math.max(1, Math.ceil(source.length / 200)),
    pages: Array.from({ length: Math.max(1, Math.ceil(source.length / 200)) }, (_, i) => ({ pageNum: i + 1, rowCount: Math.min(200, Math.max(0, source.length - i * 200)) })),
    records: source.map(cardNo => ({ cardNo })),
    carrierConfigs: [{ identif: 'ordinary', name: 'B1' }, { identif: 'protected', name: '快过期续费C' }],
    filteredQueryProof: result.proof, filteredUnmatchedCards: result.unmatchedCards, filteredUnmatchedCount: result.unmatchedCards.length,
    unconfiguredCarrierSkippedCount: result.unconfiguredRows.length,
    unconfiguredCarrierSkippedRows: result.unconfiguredRows,
    unconfiguredCarrierSkippedCards: result.unconfiguredRows.map(row => row.cardNo),
    neverProcessSourceExcludedRows: protectedRows, neverProcessSourceExcludedCount: protectedRows.length,
  };
}

test('398 retained: 254 supported hits, 143 absent plus 0 TELUS skip; no business endpoints', async () => {
  const source = Array.from({ length: 398 }, (_, i) => phone(i));
  const rows = Array.from({ length: 254 }, (_, i) => backend(i, i < 183 ? '302220 Koodo' : '302720 chatr chatr'));
  rows.push(backend(254, '0 TELUS'));
  const result = await fixture(source, rows);
  const verified = validateFilteredRetentionProof(receiptFor(source, result));
  assert.equal(result.proof.queries.length, 4);
  assert.equal(verified.actionableRows.length, 254);
  assert.equal(verified.unmatchedCards.length, 144);
  assert.equal(verified.matchedRows.filter(row => row.oprClass === 'kooda').length, 183);
});
test('bounded parallel chunks <=500, deterministic order, no UI status/carrier leakage', async () => {
  let active = 0, maximum = 0;
  const source = Array.from({ length: 2100 }, (_, i) => phone(i));
  const result = await collectFilteredRetention(async (method, endpoint, data) => {
    assert.equal(data.status, undefined); assert.equal(data.carrireIdentif, undefined);
    assert.ok(data.cardNo.split('\n').length <= 500);
    active++; maximum = Math.max(maximum, active);
    await new Promise(resolve => setTimeout(resolve, 2)); active--;
    return { code: 200, total: 0, rows: [] };
  }, source);
  assert.equal(maximum, 8); assert.equal(result.proof.queries.length, 20);
  assert.deepEqual(result.unmatchedCards, source);
});
test('protected renewal hit remains excluded; aliases deduplicate only identical identities', async () => {
  const source = [phone(1), phone(2)];
  const result = await fixture(source, [backend(1, 'Koodo telus1'), backend(2, 'chatr', 'protected')]);
  const verified = validateFilteredRetentionProof(receiptFor(source, result));
  assert.equal(verified.matchedRows.length, 2); assert.equal(verified.actionableRows.length, 1);
  assert.equal(verified.protectedRows[0].cardNo, phone(2));
});
test('empty source performs no query and verifies an empty partition', async () => {
  const result = await collectFilteredRetention(() => { throw Error('must not call'); }, []);
  assert.equal(validateFilteredRetentionProof(receiptFor([], result)).actionableRows.length, 0);
});
test('forged skip, omitted filter, incomplete retention pages and protected identity fail', async () => {
  const source = [phone(1), phone(2)];
  const result = await fixture(source, [backend(1, 'Koodo'), backend(2, 'chatr', 'protected')]);
  const receipt = receiptFor(source, result);
  for (const mutate of [
    r => { r.filteredUnmatchedCards = [phone(1)]; r.filteredUnmatchedCount = 1; },
    r => { r.filteredQueryProof.queries.pop(); },
    r => { r.pages[0].rowCount--; },
    r => { r.neverProcessSourceExcludedRows[0].backendId = 'wrong'; },
    r => { r.filteredQueryProof.queries[1].pages[0].rows[0].id = {}; },
  ]) { const changed = structuredClone(receipt); mutate(changed); assert.throws(() => validateFilteredRetentionProof(changed)); }
});
test('failed/malformed/truncated responses never become an empty successful filter', async () => {
  for (const response of [
    { code: 500, total: 0, rows: [] }, { code: 200, rows: [] },
    { code: 200, total: 1, rows: [] }, { code: 200, total: 0, rows: [backend(1, 'Koodo')] },
  ]) await assert.rejects(() => collectFilteredRetention(async () => response, [phone(1)]));
});
test('outside phones, ignored OPR filters, ambiguous groups and duplicate backend IDs fail', async () => {
  for (const rows of [
    [backend(99, 'chatr')], [backend(1, 'Koodo')], [backend(1, 'chatr Koodo')],
    [backend(1, 'chatr'), { ...backend(2, 'chatr'), id: 'id-1' }],
  ]) await assert.rejects(() => collectFilteredRetention(async (_, __, data) => ({
    code: 200, total: data.opr === 'chatr' ? rows.length : 0, rows: data.opr === 'chatr' ? rows : [],
  }), [phone(1), phone(2)]));
});
test('alias conflicts and page total drift are rejected by offline proof validator', async () => {
  const source = [phone(1)];
  const result = await fixture(source, [backend(1, 'Koodo telus1')]);
  result.proof.queries.find(q => q.filter === 'telus1').pages[0].rows[0] = { ...backend(1, 'Koodo telus1'), id: 'another' };
  assert.throws(() => validateFilteredQueries(source, result.proof), /身份/);
});
