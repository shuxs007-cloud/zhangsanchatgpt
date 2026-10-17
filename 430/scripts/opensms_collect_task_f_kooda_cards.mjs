#!/usr/bin/env node
// Task F read-only OpenSMS inventory collector. No carrier or expiry write.

import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';

import {
  createRawOpenSmsApiSession,
  shortCardNo,
} from '../opensms_protocol_client.mjs';

const PAGE_SIZE = 500;
const KOODA_QUERY = 'koo';
const KOODA_OPR_PATTERN = /kooda|koodo|telus1/i;
const MAX_PAGINATION_ATTEMPTS = 3;
const STABILITY_PROBE_LIMIT = 6;
const STABILITY_PROBE_INTERVAL_MS = 10000;
const CONSISTENCY_FAILURES = new Set([
  'total-drift', 'incomplete-page', 'incomplete-inventory', 'duplicate-backend-id',
  'inventory-not-stable',
]);

export function requireAuthorization(env = process.env) {
  if (env.OPENSMS_SYSTEM_KOODA_SNAPSHOT === '1') return;
  if (env.OPENSMS_TASK_F_POLICY_CATALOG_SYNC === '1') return;
  const gates = [
    'OPENSMS_FORCE_TASK_F',
    'OPENSMS_ENABLE_TASK_F',
    'OPENSMS_EXECUTE_TASK_F',
  ];
  const missing = gates.filter(name => env[name] !== '1');
  if (missing.length) throw new Error(`任务F授权门禁不足: ${missing.join(',')}`);
}

function text(value) {
  return String(value ?? '').trim();
}

function displayPhone(row) {
  const value = shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
  return value.length === 10 ? `1${value}` : value;
}

function carrierName(row) {
  return text(row.carrierName || row.carrireName || row.carrier);
}

function carrierIdentif(row) {
  return text(row.carrireIdentif || row.carrierIdentif || row.identif);
}

function cardStoreValue(row) {
  return text(
    row.cardStore
    || row.cardStorage
    || row.cardSave
    || row.iccid
    || row.simCardNumber
    || row.simCardNo,
  );
}

function normalizeQueryIccid(value) {
  const compact = text(value).replace(/\s+/g, '').replace(/[Ff]$/, '');
  return /^\d{18,22}$/.test(compact) ? compact : '';
}

export function inventoryShowAbleFilter(env = process.env) {
  // Reports include off-shelf historical numbers too. Independent F keeps its
  // existing visible inventory scope; it must not expand to historical cards.
  return env.OPENSMS_SYSTEM_KOODA_SNAPSHOT === '1'
    || env.OPENSMS_TASK_F_POLICY_CATALOG_SYNC === '1' ? null : 1;
}

function queryBody(pageNum, showAble) {
  return {
    pageNum,
    pageSize: PAGE_SIZE,
    cardNo: null,
    cardStore: null,
    carrireIdentif: null,
    carrireIdentifBound: null,
    machineName: null,
    cardType: null,
    status: null,
    cardStatus: null,
    opr: KOODA_QUERY,
    userAble: null,
    showAble,
    faultCard: null,
  };
}

class PaginationError extends Error {
  constructor(code, message, { retryable = false, ...details } = {}) {
    super(message);
    this.code = code;
    this.retryable = retryable;
    this.details = details;
  }
}

function failureEvidence(error) {
  // API exceptions and response bodies can contain credentials. Evidence only
  // includes our fixed error descriptions and explicitly selected safe fields.
  return error instanceof PaginationError
    ? { code: error.code, message: error.message, retryable: error.retryable, ...error.details }
    : { code: 'unexpected-error', message: '任务F库存读取出现未分类错误', retryable: false };
}

function summarizeAttempt(attempt) {
  attempt.pages.sort((a, b) => a.pageNum - b.pageNum);
  const firstPositions = new Map();
  const duplicates = [];
  let rowCount = 0;
  for (const page of attempt.pages) {
    rowCount += page.rowCount || 0;
    for (const [index, id] of (page.backendIds || []).entries()) {
      if (id === null) continue;
      const position = { pageNum: page.pageNum, rowNumber: index + 1 };
      if (firstPositions.has(id)) {
        duplicates.push({ backendId: id, first: firstPositions.get(id), duplicate: position });
      } else firstPositions.set(id, position);
    }
  }
  attempt.receivedRowCount = rowCount;
  attempt.uniqueBackendIdCount = firstPositions.size;
  attempt.duplicates = duplicates;
}

async function readInventoryAttempt(api, workers, attempt, showAble, { stabilize, sleep }) {
  let total;
  const pages = [];
  async function readPage(pageNum, { probe = false, suppliedResponse } = {}) {
    const page = { pageNum, startedAt: new Date().toISOString() };
    (probe ? attempt.stabilityProbes : attempt.pages).push(page);
    try {
      let response;
      try {
        response = suppliedResponse ?? await api('POST', '/phone/config/list', queryBody(pageNum, showAble), { pageNum, pageSize: PAGE_SIZE });
      } catch (error) {
        const category = error?.readDiagnostic?.category;
        const retryable = ['network_fetch_failed', 'read_timeout'].includes(category);
        throw new PaginationError('request-failed', `任务F Kooda第${pageNum}页读取请求失败`, {
          pageNum, retryable, requestCategory: category || 'unknown',
          httpStatus: Number.isSafeInteger(error?.status) ? error.status : null,
        });
      }
      if (!response || response.code !== 200) {
        throw new PaginationError('api-response-failed', `任务F Kooda第${pageNum}页接口未返回成功`, {
          pageNum, responseCode: Number.isSafeInteger(response?.code) ? response.code : null,
        });
      }
      if (!Array.isArray(response.rows) || response.total === undefined || response.total === null) {
        throw new PaginationError('malformed-response', `任务F Kooda第${pageNum}页缺少rows或total`, { pageNum });
      }
      page.rowCount = response.rows.length;
      page.backendIds = response.rows.map(row => {
        const id = row?.id;
        return (typeof id === 'string' && text(id)) || (typeof id === 'number' && Number.isSafeInteger(id))
          ? text(id) : null;
      });
      const missingIdRow = page.backendIds.indexOf(null);
      if (missingIdRow !== -1) {
        throw new PaginationError('missing-backend-id', '任务F Kooda分页记录缺少有效后台ID，拒绝无法核对的库存', {
          pageNum, rowNumber: missingIdRow + 1,
        });
      }
      const pageTotal = Number(response.total);
      if (!['number', 'string'].includes(typeof response.total) || text(response.total) === ''
          || !Number.isSafeInteger(pageTotal) || pageTotal < 0 || pageTotal > PAGE_SIZE * 1000) {
        throw new PaginationError('invalid-total', `任务F Kooda第${pageNum}页total无效`, { pageNum });
      }
      page.total = pageTotal;
      if (!probe && total === undefined) attempt.total = total = pageTotal;
      if (!probe && pageTotal !== total) {
        throw new PaginationError('total-drift', `任务F Kooda分页期间总数变化: expected=${total}, received=${pageTotal}`, {
          retryable: true, pageNum, expectedTotal: total, receivedTotal: pageTotal,
        });
      }
      const expectedRows = Math.min(PAGE_SIZE, Math.max(0, pageTotal - (pageNum - 1) * PAGE_SIZE));
      if (response.rows.length !== expectedRows) {
        throw new PaginationError('incomplete-page', `任务F Kooda第${pageNum}页不完整: expected=${expectedRows}, received=${response.rows.length}`, {
          retryable: true, pageNum, expectedRows, receivedRows: response.rows.length,
        });
      }
      if (probe && new Set(page.backendIds).size !== page.backendIds.length) {
        throw new PaginationError('duplicate-backend-id', '任务F Kooda稳定性探测发现重复后台ID', { retryable: true, pageNum });
      }
      if (!probe) pages[pageNum - 1] = response.rows;
      page.status = 'passed';
      return { response, page };
    } catch (error) {
      page.status = 'failed';
      page.failure = failureEvidence(error);
      throw error;
    } finally {
      page.completedAt = new Date().toISOString();
    }
  }
  if (stabilize) {
    attempt.stabilityProbes = [];
    attempt.stabilityCheck = {
      status: 'checking', intervalMs: STABILITY_PROBE_INTERVAL_MS,
      maxProbes: STABILITY_PROBE_LIMIT, matchRule: 'same_total_and_ordered_first_page_backend_ids',
    };
    let previousSignature;
    let stableResponse;
    for (let probe = 0; probe < STABILITY_PROBE_LIMIT; probe++) {
      if (probe) await sleep(STABILITY_PROBE_INTERVAL_MS);
      const sampled = await readPage(1, { probe: true });
      const signature = JSON.stringify([sampled.page.total, sampled.page.backendIds]);
      if (signature === previousSignature) {
        stableResponse = sampled.response;
        break;
      }
      previousSignature = signature;
    }
    attempt.stabilityCheck.probeCount = attempt.stabilityProbes.length;
    if (!stableResponse) {
      attempt.stabilityCheck.status = 'not_stable';
      throw new PaginationError('inventory-not-stable', '任务F Kooda库存持续变化，稳定性探测耗尽，未开始完整分页', {
        retryable: true, probeCount: attempt.stabilityProbes.length,
      });
    }
    attempt.stabilityCheck.status = 'passed';
    // Reuse the last validated first-page response within this attempt only.
    // Probes are separate evidence, never rows to concatenate or deduplicate.
    await readPage(1, { suppliedResponse: stableResponse });
  } else {
    await readPage(1);
  }
  const pageCount = attempt.pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const effectiveWorkers = Math.min(workers, pageCount - 1);
  attempt.effectiveWorkers = Math.max(1, effectiveWorkers);
  let nextPage = 2;
  const failures = [];
  async function worker() {
    while (!failures.length && nextPage <= pageCount) {
      const pageNum = nextPage++;
      try { await readPage(pageNum); } catch (error) { failures.push(error); }
    }
  }
  // Drain every in-flight request before recording evidence or starting a new
  // attempt. A fatal error must win even if a consistency error arrived first.
  await Promise.all(Array.from({ length: effectiveWorkers }, () => worker()));
  if (failures.length) throw failures.find(error => !error.retryable) || failures[0];
  const rows = pages.flat();
  if (rows.length !== total) {
    throw new PaginationError('incomplete-inventory', `任务F Kooda分页不完整: expected=${total}, received=${rows.length}`, {
      retryable: true, expectedRows: total, receivedRows: rows.length,
    });
  }
  summarizeAttempt(attempt);
  if (attempt.duplicates.length || attempt.uniqueBackendIdCount !== total) {
    throw new PaginationError('duplicate-backend-id', '任务F Kooda分页存在重复后台ID，拒绝不稳定库存', {
      retryable: true, duplicateCount: attempt.duplicates.length, firstDuplicate: attempt.duplicates[0] || null,
    });
  }
  return { rows, total, pageCount };
}

export async function collect(api, {
  workers = 4, showAble = 1, stabilizeBeforeFirstRead = false, stabilizeOnConsistencyFailure = false,
  onAttempt = async () => {}, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
} = {}) {
  if (!Number.isInteger(workers) || workers < 1 || workers > 8) {
    throw new Error('任务F库存只读分页并发数必须为1至8的整数');
  }
  if (showAble !== null && showAble !== 1) throw new Error('库存筛选仅允许全状态或原独立F可见范围');
  const startedAt = performance.now();
  const attempts = [];
  let previousFailure;
  for (let attemptNumber = 1; attemptNumber <= MAX_PAGINATION_ATTEMPTS; attemptNumber++) {
    const attemptWorkers = attemptNumber === 1 ? workers : 1;
    const attempt = {
      attemptNumber, requestedWorkers: attemptWorkers, effectiveWorkers: 1, showAbleFilter: showAble,
      startedAt: new Date().toISOString(), pages: [],
    };
    let result;
    let failure;
    try {
      // All state belongs to this attempt; no rows or totals survive a retry.
      result = await readInventoryAttempt(api, attemptWorkers, attempt, showAble, {
        stabilize: (attemptNumber === 1 && stabilizeBeforeFirstRead)
          || (stabilizeOnConsistencyFailure && CONSISTENCY_FAILURES.has(previousFailure?.code)),
        sleep,
      });
      attempt.status = 'passed';
    } catch (error) {
      failure = error;
      attempt.status = 'failed';
      attempt.failure = failureEvidence(error);
    }
    summarizeAttempt(attempt);
    attempt.completedAt = new Date().toISOString();
    attempt.retryWaitMs = failure?.retryable && attemptNumber < MAX_PAGINATION_ATTEMPTS
      ? 30000 * attemptNumber : 0;
    attempts.push(attempt);
    try {
      await onAttempt(structuredClone(attempt));
    } catch {
      failure = new PaginationError('evidence-write-failed', '任务F分页证据持久化失败，拒绝继续');
    }
    if (failure) {
      if (failure.retryable && attemptNumber < MAX_PAGINATION_ATTEMPTS) {
        previousFailure = failure;
        await sleep(attempt.retryWaitMs);
        continue;
      }
      failure.paginationAttemptCount = attempts.length;
      failure.paginationAttempts = attempts;
      throw failure;
    }
    return {
      ...result,
      showAbleFilter: showAble,
      inventoryVisibilityScope: showAble === null ? 'all-including-off-shelf' : 'visible-only',
      paginationRequestedWorkers: workers,
      paginationEffectiveWorkers: attempt.effectiveWorkers,
      paginationMode: attemptNumber === 1
        ? 'first-page-then-bounded-concurrent-read-only' : 'serial-full-reread-after-consistency-failure',
      paginationTotalStable: true,
      paginationBackendIdsUnique: true,
      // Passing total/row/ID checks is evidence of a complete read, not a
      // transactional snapshot or proof that every field stayed unchanged.
      paginationTransactionalSnapshotGuaranteed: false,
      paginationAttemptCount: attempts.length,
      paginationRecovered: attempts.length > 1,
      paginationAttempts: attempts,
      paginationStabilityProbeCount: attempts.reduce((count, item) => count + (item.stabilityProbes?.length || 0), 0),
      paginationElapsedSeconds: Number(((performance.now() - startedAt) / 1000).toFixed(3)),
    };
  }
}

export async function collectWithEvidence(api, outputPath, options = {}) {
  const evidencePath = `${path.resolve(outputPath)}.pagination.json`;
  await fs.mkdir(path.dirname(evidencePath), { recursive: true });
  // Claim the exact path before reading; historical evidence is never replaced.
  const summaryFile = await fs.open(evidencePath, 'wx');
  const attemptEvidencePaths = [];
  let collected;
  let failure;
  try {
    try {
      collected = await collect(api, {
        ...options,
        onAttempt: async attempt => {
          const attemptPath = `${evidencePath}.attempt-${attempt.attemptNumber}.json`;
          const attemptFile = await fs.open(attemptPath, 'wx');
          try {
            await attemptFile.writeFile(`${JSON.stringify(attempt, null, 2)}\n`);
            await attemptFile.sync();
          } finally { await attemptFile.close(); }
          attemptEvidencePaths.push(attemptPath);
          if (options.onAttempt) await options.onAttempt(attempt);
        },
      });
    } catch (error) { failure = error; }
    const attempts = collected?.paginationAttempts || failure?.paginationAttempts || [];
    const summary = {
      schemaVersion: 1, status: failure ? 'failed' : 'passed', createdAt: new Date().toISOString(),
      runId: process.env.OPENSMS_RUN_ID || null,
      apiEndpoint: '/api/phone/config/list', pageSize: PAGE_SIZE, oprSearch: KOODA_QUERY,
      showAbleFilter: options.showAble === undefined ? 1 : options.showAble,
      maxAttempts: MAX_PAGINATION_ATTEMPTS, attemptCount: attempts.length, recovered: !failure && attempts.length > 1,
      paginationAttemptCount: attempts.length,
      paginationRecovered: !failure && attempts.length > 1,
      paginationTransactionalSnapshotGuaranteed: false,
      attemptEvidencePaths, attempts, ...(failure ? { failure: failureEvidence(failure) } : {}),
    };
    const serialized = `${JSON.stringify(summary, null, 2)}\n`;
    try {
      await summaryFile.writeFile(serialized);
      await summaryFile.sync();
    } catch {
      throw new PaginationError('evidence-write-failed', '任务F分页汇总证据持久化失败，拒绝继续');
    }
    const metadata = {
      paginationEvidencePath: evidencePath,
      paginationEvidenceSha256: createHash('sha256').update(serialized).digest('hex'),
      paginationAttemptCount: attempts.length,
      paginationRecovered: summary.paginationRecovered,
    };
    if (failure) {
      Object.assign(failure, metadata);
      throw failure;
    }
    return { ...collected, ...metadata };
  } catch (error) {
    error.paginationEvidencePath ||= evidencePath;
    error.paginationAttemptCount ??= collected?.paginationAttemptCount || failure?.paginationAttemptCount || 0;
    error.paginationRecovered = false;
    throw error;
  } finally { await summaryFile.close(); }
}

async function main() {
  requireAuthorization();
  const outputPath = process.argv[2];
  if (!outputPath) throw new Error('usage: node opensms_collect_task_f_kooda_cards.mjs OUTPUT.json');
  for (const target of [outputPath, `${outputPath}.pagination.json`]) {
    try { await fs.lstat(target); } catch (error) {
      if (error.code === 'ENOENT') continue;
      throw error;
    }
    throw new Error(`任务F输出或分页证据文件已存在，拒绝覆盖: ${path.resolve(target)}`);
  }
  const session = await createRawOpenSmsApiSession({
    cdp: process.env.BITBROWSER_CDP,
    pathName: '/phoneConfig',
  });
  try {
    const collected = await collectWithEvidence(session.api, outputPath, {
      workers: Number(process.env.OPENSMS_SYSTEM_KOODA_INVENTORY_READ_WORKERS || process.env.OPENSMS_TASK_F_INVENTORY_READ_WORKERS || 4),
      showAble: inventoryShowAbleFilter(),
      stabilizeOnConsistencyFailure: inventoryShowAbleFilter() === null,
      stabilizeBeforeFirstRead: inventoryShowAbleFilter() === null
        && process.env.OPENSMS_SYSTEM_KOODA_STABILIZE_BEFORE_READ === '1',
    });
    const invalidOpr = [];
    const missingCardStore = [];
    const invalidCardStore = [];
    const unique = new Map();
    for (const raw of collected.rows) {
      const opr = text(raw.opr);
      const sourceIccid = cardStoreValue(raw);
      const queryIccid = normalizeQueryIccid(sourceIccid);
      const compact = {
        backendId: raw.id ?? null,
        phone: displayPhone(raw),
        slot: text(raw.slot || raw.cardSlot || raw.slotIdentif),
        cardStore: sourceIccid,
        queryIccid,
        opr,
        carrierName: carrierName(raw),
        carrierIdentif: carrierIdentif(raw),
        status: text(raw.status || raw.cardStatus),
        // Keep the complete OpenSMS source object. Task F receipts and the
        // persistent database must remain independently auditable even when
        // the backend later adds fields that are not part of this compact
        // index.
        sourceData: raw,
      };
      if (!KOODA_OPR_PATTERN.test(opr)) {
        invalidOpr.push(compact);
        continue;
      }
      if (!sourceIccid) {
        missingCardStore.push(compact);
        continue;
      }
      if (!queryIccid) {
        invalidCardStore.push(compact);
        continue;
      }
      const existing = unique.get(queryIccid);
      if (existing) {
        existing.sourceOccurrenceCount += 1;
        existing.sourceOccurrences.push(compact);
      } else {
        unique.set(queryIccid, {
          ...compact,
          sourceOccurrenceCount: 1,
          sourceOccurrences: [compact],
        });
      }
    }
    if (invalidOpr.length) {
      throw new Error(`任务F OPR=koo查询返回${invalidOpr.length}条非Kooda/Telus1记录，拒绝继续`);
    }
    const payload = {
      status: 'passed',
      runId: process.env.OPENSMS_RUN_ID || null,
      createdAt: new Date().toISOString(),
      mode: process.env.OPENSMS_SYSTEM_KOODA_SNAPSHOT === '1'
        ? 'opensms-system-kooda-snapshot' : 'task-f-opensms-read-only-protocol-api',
      apiEndpoint: '/api/phone/config/list',
      pageSize: PAGE_SIZE,
      oprSearch: KOODA_QUERY,
      showAbleFilter: collected.showAbleFilter,
      inventoryVisibilityScope: collected.inventoryVisibilityScope,
      oprAcceptedPattern: 'kooda|koodo|telus1 (case-insensitive)',
      silentBrowserMode: true,
      browserForegroundActivationAttempted: false,
      totalRows: collected.total,
      pageCount: collected.pageCount,
      paginationRequestedWorkers: collected.paginationRequestedWorkers,
      paginationEffectiveWorkers: collected.paginationEffectiveWorkers,
      paginationMode: collected.paginationMode,
      paginationTotalStable: collected.paginationTotalStable,
      paginationBackendIdsUnique: collected.paginationBackendIdsUnique,
      paginationElapsedSeconds: collected.paginationElapsedSeconds,
      paginationAttemptCount: collected.paginationAttemptCount,
      paginationRecovered: collected.paginationRecovered,
      paginationTransactionalSnapshotGuaranteed: collected.paginationTransactionalSnapshotGuaranteed,
      paginationEvidencePath: collected.paginationEvidencePath,
      paginationEvidenceSha256: collected.paginationEvidenceSha256,
      uniqueIccidCount: unique.size,
      duplicateOccurrenceCount: [...unique.values()].reduce((sum, row) => sum + row.sourceOccurrenceCount - 1, 0),
      missingCardStoreCount: missingCardStore.length,
      invalidCardStoreCount: invalidCardStore.length,
      trailingFNormalization: 'strip_exactly_one_final_F_case_insensitive',
      fullSourceDataIncluded: true,
      // One immutable inventory serves both document dates and Task F gaps.
      // Keep unregistered rows too; document matching is stricter than the
      // ICCID-only catalog index and must not silently lose source records.
      systemRows: collected.rows,
      systemDocumentFields: {
        ICCID: 'cardStore', activationDate: 'chatrActivateDate', cutoffDate: 'chatrPlanExpire',
        openDate: null,
      },
      rows: [...unique.values()],
      missingCardStore,
      invalidCardStore,
    };
    await fs.mkdir(path.dirname(path.resolve(outputPath)), { recursive: true });
    await fs.writeFile(outputPath, `${JSON.stringify(payload, null, 2)}\n`, { flag: 'wx' });
    console.log(JSON.stringify({ ...payload, rows: undefined, systemRows: undefined, missingCardStore: undefined, invalidCardStore: undefined, path: path.resolve(outputPath) }));
  } finally {
    await session.browser.close().catch(() => {});
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(JSON.stringify({ inventoryFailure: {
      schemaVersion: 1, runId: process.env.OPENSMS_RUN_ID || null,
      businessWrites: 0, ...failureEvidence(error),
      paginationAttemptCount: error.paginationAttemptCount ?? null,
    } }));
    if (error.paginationEvidencePath) console.error(JSON.stringify({
      status: 'failed', paginationEvidencePath: error.paginationEvidencePath,
      paginationEvidenceSha256: error.paginationEvidenceSha256,
      paginationAttemptCount: error.paginationAttemptCount, paginationRecovered: false,
    }));
    console.error(error?.stack || String(error));
    process.exitCode = 1;
  });
}
