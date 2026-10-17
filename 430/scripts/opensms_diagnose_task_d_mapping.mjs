#!/usr/bin/env node
// Read-only Task D diagnosis. No carrier update or expiry-marker API is allowed.
// Usage: node scripts/opensms_diagnose_task_d_mapping.mjs output.json [--all-missing]

import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createRawOpenSmsApiSession, shortCardNo } from '../opensms_protocol_client.mjs';
import { collectCardRetention } from './opensms_card_retention_readonly_lib.mjs';
import { getCarrierConfigsReadOnly } from './opensms_carriers_readonly.mjs';
import { createReadOnlyApiLimiter, mapReadOnlyBounded } from './opensms_readonly_concurrency.mjs';

const PROFILE_ID = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
const PORT_FILE = `/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/${PROFILE_ID}/DevToolsActivePort`;
const PAGE_SIZE = 500;
const MAX_QUERY_PAGES = 100;
const ALLOWED_REQUESTS = [
  'GET /carrier/config/listNotDeleted',
  'GET /phone/cardRetain/list',
  'POST /phone/config/list',
];

function backendCard(row) {
  return shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
}

function carrierId(row) {
  return String(row.carrireIdentif || row.carrierIdentif || row.identif || row.id || '').trim();
}

function groupRows(rows) {
  const grouped = new Map();
  for (const row of rows) {
    const card = backendCard(row);
    if (!grouped.has(card)) grouped.set(card, []);
    grouped.get(card).push(row);
  }
  return grouped;
}

export function parseDiagnosticArgs(args) {
  const paths = args.filter(arg => !arg.startsWith('--'));
  const flags = args.filter(arg => arg.startsWith('--'));
  if (paths.length !== 1 || flags.some(flag => flag !== '--all-missing') || new Set(flags).size !== flags.length) {
    throw new Error('usage: opensms_diagnose_task_d_mapping.mjs output.json [--all-missing]');
  }
  return { output: path.resolve(paths[0]), allMissing: flags.includes('--all-missing') };
}

export function validateExact430Endpoint(portText, suppliedEndpoint = '') {
  const port = String(portText).split(/\r?\n/)[0].trim();
  if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
    throw new Error('430 专属 DevToolsActivePort 无效');
  }
  if (suppliedEndpoint) {
    const supplied = new URL(suppliedEndpoint);
    if (
      supplied.protocol !== 'http:'
      || !['127.0.0.1', 'localhost', '[::1]'].includes(supplied.hostname)
      || supplied.port !== port
      || supplied.username || supplied.password || supplied.search || supplied.hash
      || !['', '/'].includes(supplied.pathname)
    ) throw new Error('BITBROWSER_CDP 与 430 专属端口不一致，拒绝邻居会话');
  }
  return `http://127.0.0.1:${port}`;
}

// Keep cardNo strings exactly as supplied, including the leading country code.
// Both body and URL pagination match the shared production protocol helper.
export async function queryRawPhoneNumbers(api, numbers, queryKind, queries) {
  if (!numbers.length || numbers.length > PAGE_SIZE || numbers.some(number => !/^\d{10,11}$/.test(number))) {
    throw new Error('诊断查询仅接受 1 至 500 个 10/11 位原样号码');
  }
  const query = {
    queryIndex: queries.length,
    queryKind,
    numbers: [...numbers],
    pages: [],
    anomalies: [],
    rows: [],
    firstPageRows: [],
  };
  queries.push(query);
  let expectedTotal;
  let pageCount = 1;
  for (let pageNum = 1; pageNum <= pageCount; pageNum += 1) {
    const request = {
      method: 'POST',
      endpoint: '/phone/config/list',
      data: { cardNo: numbers.join('\n'), pageNum, pageSize: PAGE_SIZE },
      searchParams: { pageNum, pageSize: PAGE_SIZE },
    };
    const page = { pageNum, request };
    query.pages.push(page);
    try {
      const response = await api(request.method, request.endpoint, request.data, request.searchParams);
      page.response = response;
      if (Number(response?.code) !== 200 || !Array.isArray(response.rows)) {
        throw new Error(`号码诊断查询响应无效: query=${query.queryIndex}, page=${pageNum}`);
      }
      const total = Number(response.total);
      if (!Number.isInteger(total) || total < 0) throw new Error('号码诊断查询 total 无效');
      if (pageNum === 1) {
        expectedTotal = total;
        query.reportedTotal = total;
        query.firstPageRows = response.rows;
        query.firstPageTruncated = total > response.rows.length;
        const requiredPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
        pageCount = Math.min(requiredPages, MAX_QUERY_PAGES);
        if (requiredPages > MAX_QUERY_PAGES) query.anomalies.push({ reason: 'page_limit_exceeded', requiredPages });
      }
      if (total !== expectedTotal) query.anomalies.push({ reason: 'total_changed', pageNum, expectedTotal, total });
      const expectedRows = Math.min(PAGE_SIZE, Math.max(0, expectedTotal - (pageNum - 1) * PAGE_SIZE));
      if (response.rows.length !== expectedRows) {
        query.anomalies.push({ reason: 'page_row_count_mismatch', pageNum, expectedRows, actualRows: response.rows.length });
      }
      query.rows.push(...response.rows);
    } catch (error) {
      page.error = { name: error.name, message: error.message };
      throw error;
    }
  }
  const wanted = new Set(numbers.map(shortCardNo));
  query.unexpectedRows = query.rows.filter(row => !wanted.has(backendCard(row)));
  const ids = query.rows.map(row => String(row.id ?? '')).filter(Boolean);
  query.duplicateBackendIdCount = ids.length - new Set(ids).size;
  query.complete = query.rows.length === expectedTotal && query.anomalies.length === 0;
  return query;
}

export async function diagnoseTaskDMapping(api, { allMissing = false, report = {} } = {}) {
  Object.assign(report, {
    task: 'D',
    purpose: 'read_only_retention_phone_mapping_diagnosis',
    startedAt: new Date().toISOString(),
    allMissing,
    rawRetentionPages: [],
    rawCarrierPages: [],
    phoneQueries: [],
    perCard: [],
  });
  const captureApi = async (method, endpoint, data, searchParams, ...rest) => {
    const collection = endpoint === '/phone/cardRetain/list' ? report.rawRetentionPages
      : endpoint === '/carrier/config/listNotDeleted' ? report.rawCarrierPages : null;
    const entry = collection ? { request: { method, endpoint, data, searchParams } } : null;
    if (entry) collection.push(entry);
    try {
      const response = await api(method, endpoint, data, searchParams, ...rest);
      if (entry) entry.response = response;
      return response;
    } catch (error) {
      if (entry) entry.error = { name: error.name, message: error.message };
      throw error;
    }
  };
  const limiter = createReadOnlyApiLimiter(captureApi, { allowedRequests: ALLOWED_REQUESTS });
  try {
    const initial = await Promise.allSettled([
      collectCardRetention(limiter.api),
      getCarrierConfigsReadOnly(limiter.api),
    ]);
    const failed = initial.find(result => result.status === 'rejected');
    if (failed) throw failed.reason;
    const retention = initial[0].value;
    report.retention = retention;
    report.carriers = initial[1].value;
    report.rawRetentionPages.sort((a, b) => a.request.searchParams.pageNum - b.request.searchParams.pageNum);
    report.rawCarrierPages.sort((a, b) => a.request.searchParams.pageNum - b.request.searchParams.pageNum);
    const batches = [];
    for (let start = 0; start < retention.cardNos.length; start += PAGE_SIZE) {
      batches.push(retention.cardNos.slice(start, start + PAGE_SIZE));
    }
    const bulk = await mapReadOnlyBounded(batches, numbers => queryRawPhoneNumbers(
      limiter.api, numbers, 'retention_batch_10_digit', report.phoneQueries,
    ));
    const firstPageGrouped = groupRows(bulk.flatMap(query => query.firstPageRows));
    const fullGrouped = groupRows(bulk.flatMap(query => query.rows));
    const missing = retention.cardNos.filter(cardNo => !(fullGrouped.get(cardNo) || []).length);
    report.missingBeforeSingleQueries = missing;
    const sampled = allMissing ? missing : missing.slice(0, 3);
    report.singleQueryCards = sampled;
    const singles = await mapReadOnlyBounded(sampled, async cardNo => {
      const ten = await queryRawPhoneNumbers(limiter.api, [cardNo], 'missing_single_10_digit', report.phoneQueries);
      const eleven = await queryRawPhoneNumbers(limiter.api, [`1${cardNo}`], 'missing_single_11_digit', report.phoneQueries);
      return [cardNo, {
        tenDigitQueryIndex: ten.queryIndex,
        elevenDigitQueryIndex: eleven.queryIndex,
        tenDigitHits: ten.rows.filter(row => backendCard(row) === cardNo),
        elevenDigitHits: eleven.rows.filter(row => backendCard(row) === cardNo),
      }];
    });
    const singlesByCard = new Map(singles);
    const carriersById = new Map(report.carriers.map(row => [carrierId(row), String(row.name || row.carrierName || '')]));
    report.perCard = retention.cardNos.map(cardNo => {
      const firstPageHits = firstPageGrouped.get(cardNo) || [];
      const bulkHits = fullGrouped.get(cardNo) || [];
      const single = singlesByCard.get(cardNo);
      let reason = bulkHits.length === 1 ? 'matched_backend' : bulkHits.length ? 'duplicate_backend' : 'missing_backend_unprobed';
      if (!firstPageHits.length && bulkHits.length) reason = bulkHits.length === 1 ? 'found_on_later_page' : 'duplicate_backend';
      if (!bulkHits.length && single) {
        const tenCount = single.tenDigitHits.length;
        const elevenCount = single.elevenDigitHits.length;
        reason = tenCount ? 'found_with_single_10_digit' : elevenCount ? 'found_only_with_country_code' : 'missing_backend_both_forms';
      }
      return {
        cardNo,
        reason,
        firstPageHitCount: firstPageHits.length,
        bulkHitCount: bulkHits.length,
        bulkRows: bulkHits,
        bulkCarrierNames: bulkHits.map(row => carriersById.get(carrierId(row)) || ''),
        ...(single || {}),
      };
    });
    const reasonCounts = {};
    for (const row of report.perCard) reasonCounts[row.reason] = (reasonCounts[row.reason] || 0) + 1;
    report.summary = {
      retentionRecordCount: retention.totalRecords,
      retentionUniqueCardCount: retention.uniqueCardCount,
      bulkQueryCount: bulk.length,
      bulkMissingCardCount: missing.length,
      singleQueryCardCount: sampled.length,
      totalPhoneQueryCount: report.phoneQueries.length,
      truncatedFirstPageQueryCount: report.phoneQueries.filter(query => query.firstPageTruncated).length,
      incompletePhoneQueryCount: report.phoneQueries.filter(query => !query.complete).length,
      unexpectedPhoneRowCount: report.phoneQueries.reduce((sum, query) => sum + query.unexpectedRows.length, 0),
      reasonCounts,
    };
    report.status = report.summary.incompletePhoneQueryCount ? 'diagnosis_incomplete' : 'diagnosis_completed';
    return report;
  } catch (error) {
    report.status = 'diagnosis_failed';
    report.error = { name: error.name, message: error.message };
    throw error;
  } finally {
    await limiter.drain();
    report.readOnlyConcurrency = { ...limiter.stats };
    report.finishedAt = new Date().toISOString();
  }
}

async function main() {
  const { output, allMissing } = parseDiagnosticArgs(process.argv.slice(2));
  // Pin discovery before creating any session, including when the shared client
  // has a stale default endpoint. An explicit different profile is rejected.
  process.env.OPENSMS_EXACT_CDP_ONLY = '1';
  const endpoint = validateExact430Endpoint(await fs.readFile(PORT_FILE, 'utf8'), process.env.BITBROWSER_CDP);
  await fs.mkdir(path.dirname(output), { recursive: true });
  const handle = await fs.open(output, 'wx');
  const report = {
    output,
    browserProfileId: PROFILE_ID,
    endpoint,
    exactCdpOnly: true,
    allowedRequests: ALLOWED_REQUESTS,
  };
  let session;
  try {
    session = await createRawOpenSmsApiSession({ cdp: endpoint, pathName: '/phoneConfig' });
    report.pageTarget = { id: session.target.id, url: session.target.url };
    await diagnoseTaskDMapping(session.api, { allMissing, report });
  } catch (error) {
    report.status = 'diagnosis_failed';
    report.error = { name: error.name, message: error.message };
    process.exitCode = 1;
  } finally {
    session?.client.close();
    report.finishedAt = new Date().toISOString();
    try {
      await handle.writeFile(`${JSON.stringify(report, null, 2)}\n`);
    } finally {
      await handle.close();
    }
  }
  console.log(JSON.stringify({ output, status: report.status, ...report.summary }));
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
