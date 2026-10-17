// Task D branch 2: complete, bounded OPR-filtered reads and reusable proof.
import { shortCardNo } from '../opensms_protocol_client.mjs';
import { mapReadOnlyBounded } from './opensms_readonly_concurrency.mjs';

export const RETENTION_FILTERS = Object.freeze([
  { oprClass: 'chatr', filter: 'chatr' },
  { oprClass: 'kooda', filter: 'koo' },
  { oprClass: 'kooda', filter: 'telus1' },
  { oprClass: 'kooda', filter: 'kini' },
]);
const PROTECTED = new Set(['快过期续费C', '快过期续费T']);
const PAGE_SIZE = 500;
const text = value => String(value ?? '').trim();
const identity = value => typeof value === 'string' ? value.trim()
  : typeof value === 'number' && Number.isSafeInteger(value) ? String(value) : '';
const card = row => shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
const carrierId = row => identity(row.carrireIdentif || row.carrierIdentif || row.identif);
const carrierName = row => text(row.name || row.carrierName || row.carrireName || row.carrier);
const fail = message => { throw new Error(`任务D筛选证明无效: ${message}`); };
const CARRIER_ID_KEYS = ['carrireIdentif', 'carrierIdentif', 'identif'];
const CARRIER_NAME_KEYS = ['name', 'carrierName', 'carrireName', 'carrier'];
const absent = value => value == null || (typeof value === 'string' && value.trim() === '');
export const DATA_ANOMALY_POLICY = 'skip_row_data_anomalies_v1';
const carrierFields = row => CARRIER_ID_KEYS.filter(key => !absent(row[key])).map(key => row[key]);
function assertCarrierConsistency(row) {
  const values = carrierFields(row);
  if (new Set(values.map(value => identity(value) || JSON.stringify(value))).size > 1) fail('后台运营商身份字段冲突');
  if (row.displayStatus === '未配置运营商' && (values.length || CARRIER_NAME_KEYS.some(key => !absent(row[key])))) fail('未配置运营商状态与身份字段冲突');
}
export function carrierDataAnomalyRow(row, carrierConfigs) {
  if (isUnconfiguredCarrier(row)) return null;
  assertCarrierConsistency(row);
  let reason = !carrierId(row) ? 'carrier_identity_missing' : '';
  if (!reason && carrierConfigs) {
    const hits = carrierConfigs.filter(config => text(config.carrireIdentif || config.carrierIdentif || config.identif || config.id) === carrierId(row));
    if (hits.length > 1) fail('运营商配置存在重复身份，不能作为单条异常跳过');
    if (!hits.length || !carrierName(hits[0]) || hits[0].delData === true) reason = 'source_carrier_unresolved';
  }
  return reason ? { cardNo: card(row), backendId: identity(row.id), slot: text(row.slotIdentif),
    opr: text(row.opr), sourceCarrierIdentif: carrierId(row), reason } : null;
}
// Exact tombstones, verified against saved configuration before B14 deletion.
// They are exclusions only, never live carriers or notification targets.
const RETIRED_CARRIERS = new Map([
  ['2093378431675928577', { name: 'B14-C快下保留卡', oprClass: 'chatr' }],
  ['2088852494502465536', { name: 'B14-T快下保留卡', oprClass: 'kooda' }],
]);
export function retiredCarrierSkipRow(row, carrierConfigs) {
  const id = carrierId(row), retired = RETIRED_CARRIERS.get(id);
  if (!retired) return null;
  if (!Array.isArray(carrierConfigs)) fail('已删除运营商排除缺少本轮完整配置');
  if (carrierConfigs.some(config => text(config.carrireIdentif || config.carrierIdentif || config.identif || config.id) === id)) {
    fail('已删除B14标识仍出现在当前配置，不能安全排除');
  }
  if (CARRIER_ID_KEYS.some(key => !absent(row[key]) && identity(row[key]) !== id)
    || CARRIER_NAME_KEYS.some(key => !absent(row[key]) && text(row[key]) !== retired.name)
    || retentionOprClass(row.opr) !== retired.oprClass
    || !identity(row.id) || !/^\d{10}$/.test(card(row))) fail('已删除B14排除的身份、名称或OPR冲突');
  return { cardNo: card(row), backendId: identity(row.id), slot: text(row.slotIdentif),
    opr: text(row.opr), sourceCarrierIdentif: id, retiredCarrierName: retired.name,
    reason: 'carrier_retired_b14' };
}
// User-authorized exclusion: only an explicit, internally consistent backend
// unconfigured state. Missing/malformed identity alone is never an exclusion.
export function isUnconfiguredCarrier(row) {
  return CARRIER_ID_KEYS.every(key => absent(row[key]))
    && CARRIER_NAME_KEYS.every(key => absent(row[key]))
    && row.displayStatus === '未配置运营商'
    && row.showAble === true && row.showInTable === true && row.userAble === false;
}
export function unconfiguredSkipRow(row) {
  return {cardNo: card(row), backendId: identity(row.id), slot: text(row.slotIdentif),
    opr: text(row.opr), reason: 'carrier_unconfigured'};
}
function failRow(category, message, row) {
  const error = new Error(`任务D筛选证明无效: ${message}；号码${card(row)}，卡槽${text(row.slotIdentif) || '未知'}。未提交通知。`);
  error.code = 'OPENSMS_TASK_D_ROW_INVALID';
  error.category = category;
  error.details = { cardNo: card(row), backendId: identity(row.id), slot: text(row.slotIdentif) };
  throw error;
}
function sameSet(a, b) { return a.size === b.size && [...a].every(value => b.has(value)); }
function uniqueCards(values, label) {
  if (!Array.isArray(values) || values.some(value => !/^\d{10}$/.test(text(value)))) fail(`${label}号码无效`);
  const set = new Set(values);
  if (set.size !== values.length) fail(`${label}号码重复`);
  return set;
}
export function retentionOprClass(value) {
  const lower = text(value).toLowerCase();
  const c = lower.includes('chatr');
  const t = /kooda|koodo|telus1|kini/.test(lower);
  return c === t ? '' : c ? 'chatr' : 'kooda';
}

// Validated both at collection time and immediately before business writes.
export function validateFilteredQueries(sourceCards, proof) {
  const source = uniqueCards(sourceCards, '全部来源');
  if (proof?.version !== 1 || !Array.isArray(proof.queries)) fail('缺少版本或查询回执');
  if (proof.dataAnomalyPolicy !== undefined && proof.dataAnomalyPolicy !== DATA_ANOMALY_POLICY) fail('异常跳过契约无效');
  const skipDataAnomalies = proof.dataAnomalyPolicy === DATA_ANOMALY_POLICY;
  const coverage = new Map(RETENTION_FILTERS.map(item => [item.filter, new Set()]));
  const matched = new Map();
  const identities = new Map();
  for (const query of proof.queries) {
    const spec = RETENTION_FILTERS.find(item => item.filter === query.filter && item.oprClass === query.oprClass);
    if (!spec || query.method !== 'POST' || query.endpoint !== '/phone/config/list') fail('查询字段或分组异常');
    const requested = uniqueCards(query.cardNos, '查询批次');
    if (!requested.size || requested.size > PAGE_SIZE) fail('查询批次必须为1至500个号码');
    for (const phone of requested) {
      if (!source.has(phone) || coverage.get(spec.filter).has(phone)) fail('查询来源越界或批次重叠');
      coverage.get(spec.filter).add(phone);
    }
    if (!Array.isArray(query.pages) || !query.pages.length) fail('查询分页缺失');
    const total = query.pages[0].total;
    if (!Number.isSafeInteger(total) || total < 0 || total > 50000) fail('查询total无效');
    if (query.pages.length !== Math.max(1, Math.ceil(total / PAGE_SIZE))) fail('查询分页不完整');
    const seen = new Set();
    const seenIds = new Set();
    for (const [index, page] of query.pages.entries()) {
      if (page.code !== 200 || page.pageNum !== index + 1 || page.pageSize !== PAGE_SIZE
        || page.total !== total || !Array.isArray(page.rows)
        || page.rows.length !== Math.min(PAGE_SIZE, Math.max(0, total - index * PAGE_SIZE))) fail('查询分页失败、总数漂移或行数不完整');
      for (const row of page.rows) {
        const phone = card(row), id = identity(row.id), oprClass = retentionOprClass(row.opr);
        if (!requested.has(phone)) failRow('outside_requested', '查询返回越界号码', row);
        if (!id) failRow('backend_id_missing', '后台身份缺失：号码记录ID缺失或无效', row);
        if (oprClass !== spec.oprClass || !text(row.opr).toLowerCase().includes(spec.filter)) fail('后台未遵守OPR筛选或OPR冲突');
        if (skipDataAnomalies) assertCarrierConsistency(row);
        if (!skipDataAnomalies && row.displayStatus === '未配置运营商' && !isUnconfiguredCarrier(row)) {
          failRow('carrier_identity_missing', '未配置运营商状态与身份字段冲突，不能安全排除', row);
        }
        if (!skipDataAnomalies && !carrierId(row) && !isUnconfiguredCarrier(row)) {
          failRow('carrier_identity_missing', '后台身份缺失：运营商标识缺失或无效（不能视为筛选未命中）', row);
        }
        if (seen.has(phone) || seenIds.has(id)) fail('查询返回重复号码或后台ID');
        seen.add(phone); seenIds.add(id);
        const previous = matched.get(phone);
        if (previous && (text(previous.id) !== id || carrierId(previous) !== carrierId(row)
          || previous.opr !== row.opr || previous.oprClass !== oprClass
          || (skipDataAnomalies && JSON.stringify(CARRIER_ID_KEYS.map(key => previous[key] ?? null)) !== JSON.stringify(CARRIER_ID_KEYS.map(key => row[key] ?? null)))
          || isUnconfiguredCarrier(previous) !== isUnconfiguredCarrier(row)
          || (isUnconfiguredCarrier(row) && text(previous.slotIdentif) !== text(row.slotIdentif)))) fail('筛选之间后台身份或分组冲突');
        if (identities.has(id) && identities.get(id) !== phone) fail('后台ID绑定多个号码');
        identities.set(id, phone);
        matched.set(phone, { ...row, cardNo: phone, oprClass });
      }
    }
  }
  for (const cards of coverage.values()) if (!sameSet(cards, source)) fail('未完成全部号码的全部OPR筛选');
  return {
    matchedRows: sourceCards.filter(phone => matched.has(phone)).map(phone => matched.get(phone)),
    unmatchedCards: sourceCards.filter(phone => !matched.has(phone)),
    unconfiguredRows: sourceCards.filter(phone => matched.has(phone) && isUnconfiguredCarrier(matched.get(phone)))
      .map(phone => unconfiguredSkipRow(matched.get(phone))),
    dataAnomalyRows: skipDataAnomalies ? [...matched.values()].map(row => carrierDataAnomalyRow(row)).filter(Boolean) : [],
  };
}

export async function collectFilteredRetention(api, sourceCards, { onProof, dataAnomalyPolicy } = {}) {
  uniqueCards(sourceCards, '全部来源');
  const jobs = RETENTION_FILTERS.flatMap(spec => {
    const chunks = [];
    for (let i = 0; i < sourceCards.length; i += PAGE_SIZE) chunks.push({ ...spec, cardNos: sourceCards.slice(i, i + PAGE_SIZE) });
    return chunks;
  });
  const queries = await mapReadOnlyBounded(jobs, async job => {
    const result = { ...job, method: 'POST', endpoint: '/phone/config/list', pages: [] };
    let total;
    for (let pageNum = 1; ; pageNum++) {
      const response = await api('POST', '/phone/config/list', {
        cardNo: job.cardNos.join('\n'), opr: job.filter, pageNum, pageSize: PAGE_SIZE,
      }, { pageNum, pageSize: PAGE_SIZE });
      if (Number(response?.code) !== 200 || !Array.isArray(response.rows)
        || response.total === null || response.total === undefined) fail('查询接口失败或缺少rows/total');
      const pageTotal = Number(response.total);
      if (!Number.isSafeInteger(pageTotal) || pageTotal < 0 || pageTotal > 50000) fail('查询total无效');
      total ??= pageTotal;
      if (pageTotal !== total || response.rows.length !== Math.min(PAGE_SIZE, Math.max(0, total - (pageNum - 1) * PAGE_SIZE))) fail('查询分页漂移或缺行');
      result.pages.push({ pageNum, pageSize: PAGE_SIZE, code: 200, total, rows: response.rows });
      if (pageNum >= Math.max(1, Math.ceil(total / PAGE_SIZE))) break;
    }
    return result;
  });
  const proof = { version: 1, queries, ...(dataAnomalyPolicy === undefined ? {} : {dataAnomalyPolicy}) };
  // Persist raw, complete reads before validation; a rejection must not lose its evidence.
  if (onProof) await onProof(proof);
  return { proof, ...validateFilteredQueries(sourceCards, proof) };
}

export function validateFilteredRetentionProof(receipt) {
  const source = uniqueCards(receipt.cardNos, '卡保留来源');
  if (receipt.task !== 'D' || receipt.sourceProvenance !== 'phone_card_retain_all_pages'
    || receipt.endpoint !== '/phone/cardRetain/list' || !text(receipt.runId)
    || receipt.pageSize !== 200 || receipt.uniqueCardCount !== source.size
    || !Number.isSafeInteger(receipt.totalRecords) || receipt.totalRecords < 0
    || receipt.fetchedRecords !== receipt.totalRecords || !Array.isArray(receipt.records)
    || receipt.records.length !== receipt.totalRecords
    || receipt.records.some(row => !source.has(row.cardNo))
    || !sameSet(new Set(receipt.records.map(row => row.cardNo)), source)) fail('卡保留全部分页来源不完整');
  if (receipt.pageCount !== Math.max(1, Math.ceil(receipt.totalRecords / 200))
    || receipt.pages?.length !== receipt.pageCount
    || receipt.pages.some((page, index) => page.pageNum !== index + 1
      || page.rowCount !== Math.min(200, Math.max(0, receipt.totalRecords - index * 200)))) fail('卡保留分页数量不一致');
  const { matchedRows, unmatchedCards, unconfiguredRows } = validateFilteredQueries(receipt.cardNos, receipt.filteredQueryProof);
  const unconfigured = new Set(unconfiguredRows.map(row => row.cardNo));
  const skippedRows = receipt.unconfiguredCarrierSkippedRows ?? [];
  if (!Array.isArray(skippedRows) || (receipt.unconfiguredCarrierSkippedCount ?? 0) !== unconfiguredRows.length
    || !sameSet(uniqueCards(skippedRows.map(row => row.cardNo), '未配置运营商排除'), unconfigured)
    || !sameSet(uniqueCards(receipt.unconfiguredCarrierSkippedCards ?? [], '未配置运营商排除清单'), unconfigured)) fail('未配置运营商排除集合不一致');
  for (const row of skippedRows) {
    const expected = unconfiguredRows.find(item => item.cardNo === row.cardNo);
    if (['backendId', 'slot', 'opr', 'reason'].some(key => row[key] !== expected[key])) fail('未配置运营商排除身份不一致');
  }
  if (receipt.filteredUnmatchedCount !== unmatchedCards.length
    || !sameSet(uniqueCards(receipt.filteredUnmatchedCards, '未命中清单'), new Set(unmatchedCards))) fail('未命中清单与完整查询不一致');
  if (!Array.isArray(receipt.carrierConfigs)) fail('缺少运营商精确名称证明');
  const retiredRows = matchedRows.filter(row => !unconfigured.has(row.cardNo))
    .map(row => retiredCarrierSkipRow(row, receipt.carrierConfigs)).filter(Boolean);
  const retiredCards = new Set(retiredRows.map(row => row.cardNo));
  const retiredDeclared = receipt.retiredCarrierSkippedRows ?? [];
  if (!Array.isArray(retiredDeclared) || (receipt.retiredCarrierSkippedCount ?? 0) !== retiredRows.length
    || !sameSet(uniqueCards(retiredDeclared.map(row => row.cardNo), '已删除运营商排除'), retiredCards)
    || !sameSet(uniqueCards(receipt.retiredCarrierSkippedCards ?? [], '已删除运营商排除清单'), retiredCards)) fail('已删除运营商排除集合不一致');
  for (const row of retiredDeclared) {
    const expected = retiredRows.find(item => item.cardNo === row.cardNo);
    if (Object.keys(expected).some(key => row[key] !== expected[key])) fail('已删除运营商排除身份不一致');
  }
  const policy = receipt.filteredQueryProof.dataAnomalyPolicy;
  if (policy !== receipt.dataAnomalyPolicy) fail('异常跳过契约未绑定原始查询');
  const dataAnomalyRows = policy === DATA_ANOMALY_POLICY ? matchedRows
    .filter(row => !unconfigured.has(row.cardNo) && !retiredCards.has(row.cardNo))
    .map(row => carrierDataAnomalyRow(row, receipt.carrierConfigs)).filter(Boolean) : [];
  const dataAnomalyCards = new Set(dataAnomalyRows.map(row => row.cardNo));
  const declaredAnomalies = receipt.dataAnomalySkippedRows ?? [];
  if (!Array.isArray(declaredAnomalies) || (receipt.dataAnomalySkippedCount ?? 0) !== dataAnomalyRows.length
    || !sameSet(uniqueCards(declaredAnomalies.map(row => row.cardNo), '异常数据排除'), dataAnomalyCards)
    || !sameSet(uniqueCards(receipt.dataAnomalySkippedCards ?? [], '异常数据排除清单'), dataAnomalyCards)) fail('异常数据排除集合不一致');
  for (const row of declaredAnomalies) {
    const expected = dataAnomalyRows.find(item => item.cardNo === row.cardNo);
    if (Object.keys(expected).some(key => row[key] !== expected[key])) fail('异常数据排除身份或原因不一致');
  }
  const resolved = new Map();
  for (const row of matchedRows) {
    if (unconfigured.has(row.cardNo) || retiredCards.has(row.cardNo) || dataAnomalyCards.has(row.cardNo)) continue;
    const hits = receipt.carrierConfigs.filter(config => text(config.carrireIdentif || config.carrierIdentif || config.identif || config.id) === carrierId(row));
    if (hits.length !== 1 || !carrierName(hits[0]) || hits[0].delData === true) fail('命中号码源运营商无法唯一解析');
    resolved.set(row.cardNo, carrierName(hits[0]));
  }
  const protectedRows = matchedRows.filter(row => PROTECTED.has(resolved.get(row.cardNo))).map(row => ({
    cardNo: row.cardNo, sourceCarrierName: resolved.get(row.cardNo),
    sourceCarrierIdentif: carrierId(row), backendId: row.id,
  }));
  const declared = receipt.neverProcessSourceExcludedRows;
  if (!Array.isArray(declared) || receipt.neverProcessSourceExcludedCount !== protectedRows.length
    || !sameSet(uniqueCards(declared.map(row => row.cardNo), '保护排除'), new Set(protectedRows.map(row => row.cardNo)))) fail('保护排除集合不一致');
  for (const row of declared) {
    const expected = protectedRows.find(item => item.cardNo === row.cardNo);
    if (row.sourceCarrierName !== expected.sourceCarrierName || text(row.backendId) !== text(expected.backendId)
      || row.sourceCarrierIdentif !== expected.sourceCarrierIdentif) fail('保护排除身份不一致');
  }
  const enrichedRows = matchedRows.map(row => ({ ...row, sourceCarrierName: resolved.get(row.cardNo) }));
  return { matchedRows: enrichedRows, unmatchedCards, protectedRows, unconfiguredRows, retiredRows, dataAnomalyRows,
    actionableRows: enrichedRows.filter(row => !unconfigured.has(row.cardNo)
      && !retiredCards.has(row.cardNo) && !dataAnomalyCards.has(row.cardNo) && !PROTECTED.has(row.sourceCarrierName)) };
}
