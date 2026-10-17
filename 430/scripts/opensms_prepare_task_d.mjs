#!/usr/bin/env node
// Read-only Task D preparation.  Workbook metric analysis and retention-page
// collection run concurrently; no carrier or expiry-marker write is made here.

import { spawn } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  createRawOpenSmsApiSession,
  listPhonesByNumbers,
  shortCardNo,
} from '../opensms_protocol_client.mjs';
import { collectCardRetention } from './opensms_card_retention_readonly_lib.mjs';
import { createReadOnlyApiLimiter, mapReadOnlyBounded } from './opensms_readonly_concurrency.mjs';
import { getCarrierConfigsReadOnly } from './opensms_carriers_readonly.mjs';
import { collectFilteredRetention, validateFilteredRetentionProof, retiredCarrierSkipRow, carrierDataAnomalyRow, DATA_ANOMALY_POLICY } from './opensms_task_d_filtered_retention.mjs';

const PHONE_BATCH_SIZE = 500;
const SWAP_CARRIER_NAMES = ['换号区C', '换号区T'];
const NEVER_PROCESS_SOURCE_CARRIERS = ['快过期续费C', '快过期续费T'];

function isoNow() {
  return new Date().toISOString();
}

function carrierName(row) {
  return String(row.name || row.carrierName || row.carrireName || row.carrier || '').trim();
}

function carrierIdentif(row) {
  return String(row.carrireIdentif || row.carrierIdentif || row.identif || row.id || '').trim();
}

function backendCard(row) {
  return shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
}

function addDays(dateText, days) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateText || ''));
  if (!match) throw new Error(`无效服务器日期: ${dateText}`);
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

async function sha256(file) {
  const data = await fs.readFile(file);
  return crypto.createHash('sha256').update(data).digest('hex');
}

async function writeExclusive(file, payload) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  const handle = await fs.open(file, 'wx');
  try {
    await handle.writeFile(`${JSON.stringify(payload, null, 2)}\n`);
  } finally {
    await handle.close();
  }
}

function runMetricAnalyzer({ python, script, workbook, serverDate, tag, output }) {
  return new Promise((resolve, reject) => {
    const child = spawn(python, [
      script,
      workbook,
      '--server-date', serverDate,
      '--tag', tag,
      '--output', output,
    ], {
      env: { ...process.env, OPENSMS_ENABLE_TASK_D: '1' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.on('error', reject);
    child.on('close', code => {
      if (code !== 0) {
        reject(new Error(`任务D指标分析失败(${code}): ${stderr || stdout}`));
      } else {
        resolve({ stdout, stderr });
      }
    });
  });
}

export async function listPhonesBatched(api, cards) {
  const chunks = [];
  for (let index = 0; index < cards.length; index += PHONE_BATCH_SIZE) {
    chunks.push(cards.slice(index, index + PHONE_BATCH_SIZE));
  }
  const responses = await mapReadOnlyBounded(chunks, chunk => listPhonesByNumbers(api, chunk, PHONE_BATCH_SIZE));
  return responses.flatMap(item => item.rows || []);
}

export async function prepareTaskDInputs({ analyzeMetrics, createSession, collectLive }) {
  // Both branches start after authorization. Filesystem analysis overlaps CDP
  // setup and reads, while all failures are drained before session cleanup.
  let session;
  const results = await Promise.allSettled([
    Promise.resolve().then(analyzeMetrics),
    Promise.resolve().then(createSession).then(async value => {
      session = value;
      return collectLive(value);
    }),
  ]);
  const failed = results.find(result => result.status === 'rejected');
  if (failed) {
    session?.client.close();
    throw failed.reason;
  }
  return { session, live: results[1].value };
}

function exactCarrier(carriers, expectedName) {
  const matches = carriers.filter(item => carrierName(item) === expectedName);
  if (matches.length !== 1) {
    throw new Error(`任务D运营商精确匹配异常: ${expectedName}, count=${matches.length}`);
  }
  const identif = carrierIdentif(matches[0]);
  if (!identif) throw new Error(`任务D运营商缺少ID: ${expectedName}`);
  if (matches[0].delData === true) throw new Error(`任务D运营商已停用或删除: ${expectedName}`);
  return { name: expectedName, identif, id: String(matches[0].id || '') };
}

function groupBackendRows(rows) {
  const grouped = new Map();
  for (const row of rows) {
    const card = backendCard(row);
    if (!grouped.has(card)) grouped.set(card, []);
    grouped.get(card).push(row);
  }
  return grouped;
}

async function main() {
  const [workbookArg, serverDate, tag, outputDirArg] = process.argv.slice(2);
  if (!workbookArg || !serverDate || !tag || !outputDirArg) {
    throw new Error('usage: opensms_prepare_task_d.mjs workbook serverDate tag outputDir');
  }
  if (process.env.OPENSMS_ENABLE_TASK_D !== '1') {
    throw new Error('OPENSMS_ENABLE_TASK_D 未严格等于 1，拒绝准备任务D');
  }
  if (process.env.OPENSMS_FORCE_TASK_D !== '1') {
    throw new Error('OPENSMS_FORCE_TASK_D 未严格等于 1，缺少当前真实用户授权，拒绝准备任务D');
  }
  const workbook = path.resolve(workbookArg);
  const outputDir = path.resolve(outputDirArg);
  const python = process.env.OPENSMS_PYTHON || 'python3';
  const analyzer = path.resolve(path.dirname(new URL(import.meta.url).pathname), 'opensms_generate_task_d_zero_metrics.py');
  const metricPath = path.join(outputDir, `opensms_task_d_swap_zero_metric_${tag}.json`);
  const retentionReceiptPath = path.join(outputDir, `opensms_task_d_retention_${tag}_receipt.json`);
  const mappingReceiptPath = path.join(outputDir, `opensms_task_d_mapping_${tag}_receipt.json`);
  const swapManifestPath = path.join(outputDir, `opensms_task_d_swap_zero_${tag}_notification.json`);
  const retentionNotificationPath = path.join(outputDir, `opensms_task_d_retention_${tag}_notification.json`);
  const manifestPath = path.join(outputDir, `opensms_task_d_${tag}_manifest.json`);

  let readLimiter;
  const preparationStartedAt = Date.now();
  const { session, live } = await prepareTaskDInputs({
    analyzeMetrics: () => runMetricAnalyzer({
      python,
      script: analyzer,
      workbook,
      serverDate,
      tag,
      output: metricPath,
    }),
    createSession: () => createRawOpenSmsApiSession({
      cdp: process.env.BITBROWSER_CDP,
      pathName: '/phoneConfig',
    }),
    collectLive: async currentSession => {
      readLimiter = createReadOnlyApiLimiter(currentSession.api, {
        allowedRequests: [
          'GET /carrier/config/listNotDeleted',
          'GET /phone/cardRetain/list',
          'POST /phone/config/list',
        ],
      });
      const results = await Promise.allSettled([
        getCarrierConfigsReadOnly(readLimiter.api),
        collectCardRetention(readLimiter.api),
      ]);
      await readLimiter.drain();
      const failed = results.find(result => result.status === 'rejected');
      if (failed) throw failed.reason;
      return results.map(result => result.value);
    },
  });
  try {
    const metric = JSON.parse(await fs.readFile(metricPath, 'utf8'));
    const [carriers, retention] = live;
    for (const name of SWAP_CARRIER_NAMES) exactCarrier(carriers, name);
    const carrierNameByIdentif = new Map();
    for (const row of carriers) {
      const identif = carrierIdentif(row);
      if (identif) carrierNameByIdentif.set(identif, carrierName(row));
    }

    const metricCards = (metric.rows || []).map(row => shortCardNo(row['电话'])).filter(Boolean);
    // Independent read branches overlap, but do not reuse the UI's stale status
    // or carrier filters. Retention hits come ONLY from explicit OPR queries.
    const queryResults = await Promise.allSettled([
      listPhonesBatched(readLimiter.api, [...new Set(metricCards)]),
      collectFilteredRetention(readLimiter.api, retention.cardNos, {
        dataAnomalyPolicy: DATA_ANOMALY_POLICY,
        onProof: proof => writeExclusive(path.join(outputDir, `opensms_task_d_filtered_queries_${tag}.json`), {
          task: 'D', runId: tag, readOnly: true, businessWrites: 0,
          createdAt: isoNow(), cardNos: retention.cardNos, proof,
        }),
      }),
    ]);
    await readLimiter.drain();
    const failedQuery = queryResults.find(result => result.status === 'rejected');
    if (failedQuery) throw failedQuery.reason;
    const backendRows = queryResults[0].value;
    const filtered = queryResults[1].value;
    const grouped = groupBackendRows(backendRows);

    const metricByCard = new Map((metric.rows || []).map(row => [shortCardNo(row['电话']), row]));
    const swapRows = [];
    const swapMissing = [];
    const swapDuplicate = [];
    for (const cardNo of metricCards) {
      const hits = grouped.get(cardNo) || [];
      if (!hits.length) {
        swapMissing.push(cardNo);
        continue;
      }
      if (hits.length !== 1) {
        swapDuplicate.push(cardNo);
        continue;
      }
      const row = hits[0];
      const liveCarrierName = carrierNameByIdentif.get(carrierIdentif(row)) || carrierName(row);
      if (!SWAP_CARRIER_NAMES.includes(liveCarrierName)) continue;
      swapRows.push({
        ...metricByCard.get(cardNo),
        cardNo,
        liveCarrierName,
        liveCarrierIdentif: carrierIdentif(row),
        backendId: row.id,
      });
    }

    const retentionRows = [];
    const retentionProtectedRows = [];
    const retentionErrors = [];
    const retiredRows = filtered.matchedRows.map(row => retiredCarrierSkipRow(row, carriers)).filter(Boolean);
    const retiredCards = new Set(retiredRows.map(row => row.cardNo));
    const unconfiguredCards = new Set(filtered.unconfiguredRows.map(row => row.cardNo));
    const dataAnomalyRows = filtered.matchedRows.filter(row => !unconfiguredCards.has(row.cardNo) && !retiredCards.has(row.cardNo))
      .map(row => carrierDataAnomalyRow(row, carriers)).filter(Boolean);
    const dataAnomalyCards = new Set(dataAnomalyRows.map(row => row.cardNo));
    const unconfiguredFields = {
      dataAnomalyPolicy: DATA_ANOMALY_POLICY,
      dataAnomalySkippedCount: dataAnomalyRows.length,
      dataAnomalySkippedCards: [...dataAnomalyCards],
      dataAnomalySkippedRows: dataAnomalyRows,
      unconfiguredCarrierSkippedCount: unconfiguredCards.size,
      unconfiguredCarrierSkippedCards: [...unconfiguredCards],
      unconfiguredCarrierSkippedRows: filtered.unconfiguredRows,
      retiredCarrierSkippedCount: retiredRows.length,
      retiredCarrierSkippedCards: [...retiredCards],
      retiredCarrierSkippedRows: retiredRows,
    };
    for (const row of filtered.matchedRows) {
      const cardNo = row.cardNo;
      if (unconfiguredCards.has(cardNo) || retiredCards.has(cardNo) || dataAnomalyCards.has(cardNo)) continue;
      const oprClass = row.oprClass;
      const currentCarrierName = carrierNameByIdentif.get(carrierIdentif(row)) || carrierName(row);
      if (NEVER_PROCESS_SOURCE_CARRIERS.includes(currentCarrierName)) {
        retentionProtectedRows.push({
          cardNo,
          sourceCarrierName: currentCarrierName,
          sourceCarrierIdentif: carrierIdentif(row),
          backendId: row.id,
        });
        continue;
      }
      if (!oprClass) {
        retentionErrors.push({ cardNo, reason: 'unsupported_opr', opr: row.opr || '' });
        continue;
      }
      if (!currentCarrierName) {
        retentionErrors.push({ cardNo, reason: 'unresolved_carrier', carrireIdentif: carrierIdentif(row) });
        continue;
      }
      retentionRows.push({
        '电话': cardNo,
        phone: cardNo,
        cardNo,
        '运营商': currentCarrierName,
        sourceCarrierName: currentCarrierName,
        sourceCarrierIdentif: carrierIdentif(row),
        opr: String(row.opr || ''),
        oprClass,
        backendId: row.id,
      });
    }
    await writeExclusive(mappingReceiptPath, {
      createdAt: isoNow(), runId: tag, task: 'D', readOnly: true,
      status: retentionErrors.length ? 'failed' : 'passed',
      retention, queriedCardNos: retention.cardNos, backendRows: filtered.matchedRows,
      carrierConfigs: carriers,
      filteredQueryProof: filtered.proof,
      filteredUnmatchedCount: filtered.unmatchedCards.length,
      filteredUnmatchedCards: filtered.unmatchedCards,
      unmatchedReason: 'not_returned_by_supported_opr_filters',
      retentionMatchedCardCount: filtered.matchedRows.length,
      ...unconfiguredFields,
      mappedRetentionCount: retentionRows.length,
      protectedExcludedCount: retentionProtectedRows.length,
      errorCount: retentionErrors.length,
      errorCountsByReason: Object.fromEntries([...new Set(retentionErrors.map(row => row.reason))]
        .map(reason => [reason, retentionErrors.filter(row => row.reason === reason).length])),
      errors: retentionErrors,
    });
    if (retentionErrors.length) {
      throw new Error(`任务D筛选命中号码存在${retentionErrors.length}条身份异常；完整只读映射回执: ${mappingReceiptPath}`);
    }
    if (retentionRows.length + retentionProtectedRows.length + filtered.unmatchedCards.length + unconfiguredCards.size + retiredCards.size + dataAnomalyCards.size !== retention.uniqueCardCount) {
      throw new Error(`任务D卡保留记录数量不一致: retain=${retention.uniqueCardCount}, mapped=${retentionRows.length}, protected=${retentionProtectedRows.length}`);
    }

    const retentionReceipt = {
      createdAt: isoNow(),
      task: 'D',
      runId: tag,
      sourceProvenance: 'phone_card_retain_all_pages',
      endpoint: '/phone/cardRetain/list',
      ...retention,
      carrierConfigs: carriers,
      ...unconfiguredFields,
      filteredQueryProof: filtered.proof,
      filteredUnmatchedCount: filtered.unmatchedCards.length,
      filteredUnmatchedCards: filtered.unmatchedCards,
      neverProcessSourceCarrierNames: NEVER_PROCESS_SOURCE_CARRIERS,
      neverProcessSourceExcludedCount: retentionProtectedRows.length,
      neverProcessSourceExcludedRows: retentionProtectedRows,
    };
    validateFilteredRetentionProof(retentionReceipt);
    await writeExclusive(retentionReceiptPath, retentionReceipt);
    const retentionReceiptSha256 = await sha256(retentionReceiptPath);

    const swapNotification = {
      createdAt: isoNow(),
      task: 'D',
      forceGate: true,
      automationGate: true,
      phase: 'swap_zero',
      route: 'task_d_swap_two_day_zero_reply_expire_today',
      sourcePath: metric.sourcePath,
      sourceSha256: metric.sourceSha256,
      metricReceiptPath: path.resolve(metricPath),
      serverDate,
      expireDate: serverDate,
      latestReplyHeader: metric.latestReplyHeader,
      previousReplyHeader: metric.previousReplyHeader,
      latestReplyDate: metric.latestReplyDate,
      previousReplyDate: metric.previousReplyDate,
      allowedCarrierNames: SWAP_CARRIER_NAMES,
      metricCandidateCount: metric.metricCandidateCount,
      backendMissingCount: swapMissing.length,
      backendDuplicateCount: swapDuplicate.length,
      candidateCount: swapRows.length,
      count: swapRows.length,
      cardNos: swapRows.map(row => row.cardNo),
      rows: swapRows,
      candidates: swapRows,
    };
    await writeExclusive(swapManifestPath, swapNotification);

    const retentionNotification = {
      createdAt: isoNow(),
      task: 'D',
      forceGate: true,
      automationGate: true,
      phase: 'retention_verified',
      runId: tag,
      route: 'task_d_retention_expire_plus_7',
      sourceProvenance: 'phone_card_retain_all_pages',
      retentionReceiptPath: path.resolve(retentionReceiptPath),
      retentionReceiptSha256,
      serverDate,
      expireDate: addDays(serverDate, 7),
      retentionCarrierTransferEnabled: false,
      retentionCarrierTransferStatus: 'removed_by_user',
      ...unconfiguredFields,
      neverProcessSourceCarrierNames: NEVER_PROCESS_SOURCE_CARRIERS,
      neverProcessSourceExcludedCount: retentionProtectedRows.length,
      neverProcessSourceExcludedCards: retentionProtectedRows.map(row => row.cardNo),
      filteredUnmatchedCount: filtered.unmatchedCards.length,
      filteredUnmatchedCards: filtered.unmatchedCards,
      candidateCount: retentionRows.length,
      count: retentionRows.length,
      cardNos: retentionRows.map(row => row.cardNo),
      rows: retentionRows,
      candidates: retentionRows,
    };
    await writeExclusive(retentionNotificationPath, retentionNotification);

    const manifest = {
      createdAt: isoNow(),
      task: 'D',
      runId: tag,
      forceGate: true,
      automationGate: true,
      serverDate,
      sourcePath: workbook,
      sourceSha256: await sha256(workbook),
      readOnlyPreparationConcurrent: true,
      metricAnalysisOverlapsBrowserConnection: true,
      readOnlyPreparationElapsedMs: Date.now() - preparationStartedAt,
      readOnlyConcurrency: { ...readLimiter.stats },
      metricAnalysisPath: path.resolve(metricPath),
      unconfiguredCarrierPolicy: metric.unconfiguredCarrierPolicy,
      swapMetricUnconfiguredCarrierSkippedCount: metric.unconfiguredCarrierSkippedCount,
      retentionReceiptPath: path.resolve(retentionReceiptPath),
      swapNotificationPath: path.resolve(swapManifestPath),
      mappingReceiptPath: path.resolve(mappingReceiptPath),
      swapMetricCandidateCount: metric.metricCandidateCount,
      swapNotificationCount: swapRows.length,
      swapBackendMissingCount: swapMissing.length,
      swapBackendDuplicateCount: swapDuplicate.length,
      retentionCarrierTransferEnabled: false,
      retentionCarrierTransferStatus: 'removed_by_user',
      carrierCandidateCount: 0,
      chatrCandidateCount: 0,
      koodaCandidateCount: 0,
      retentionNotificationPath: path.resolve(retentionNotificationPath),
      retentionUniqueCardCount: retention.uniqueCardCount,
      retentionMatchedCardCount: filtered.matchedRows.length,
      retentionActionableCardCount: retentionRows.length,
      ...unconfiguredFields,
      filteredUnmatchedCount: filtered.unmatchedCards.length,
      filteredUnmatchedCards: filtered.unmatchedCards,
      neverProcessSourceCarrierNames: NEVER_PROCESS_SOURCE_CARRIERS,
      neverProcessSourceExcludedCount: retentionProtectedRows.length,
      neverProcessSourceExcludedCounts: Object.fromEntries(
        NEVER_PROCESS_SOURCE_CARRIERS.map(name => [
          name,
          retentionProtectedRows.filter(row => row.sourceCarrierName === name).length,
        ]),
      ),
      neverProcessSourceExcludedCards: retentionProtectedRows.map(row => row.cardNo),
      retentionExpireDate: retentionNotification.expireDate,
      swapExpireDate: serverDate,
      manifestPath: path.resolve(manifestPath),
    };
    await writeExclusive(manifestPath, manifest);
    console.log(JSON.stringify(manifest, null, 2));
  } finally {
    await readLimiter.drain();
    session.client.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => {
    if (error.code === 'OPENSMS_TASK_D_ROW_INVALID') {
      console.error(JSON.stringify({ taskDPreparationFailure: {
        version: 1, runId: process.argv[4], task: 'D', stage: 'task_d_prepare',
        code: error.code, category: error.category, ...error.details,
        businessWrites: 0, notificationSubmitted: false,
      } }));
    }
    console.error(error.stack || String(error));
    process.exit(1);
  });
}
