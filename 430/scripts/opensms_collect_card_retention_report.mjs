import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { setTimeout as sleep } from 'node:timers/promises';
import { createRawOpenSmsApiSession } from '../opensms_protocol_client.mjs';
import { collectCardRetention } from './opensms_card_retention_readonly_lib.mjs';
import { createReadOnlyApiLimiter } from './opensms_readonly_concurrency.mjs';

const ENDPOINT = '/phone/cardRetain/list';

async function collectSnapshotAttempt(api) {
  const passes = [];
  const limiter = createReadOnlyApiLimiter(api, { concurrency: 1, allowedRequests: [`GET ${ENDPOINT}`] });
  try {
    for (let pass = 1; pass <= 2; pass++) {
      const pages = [];
      const startedAt = new Date().toISOString();
      const retention = await collectCardRetention(async (method, endpoint, body, query) => {
        const response = await limiter.api(method, endpoint, body, query, { timeoutMs: 45000 });
        if (response?.code !== 200) throw new Error(`任务A卡保留第${pass}遍第${query.pageNum}页业务响应无效`);
        pages.push({ pageNum: query.pageNum, pageSize: query.pageSize, receivedAt: new Date().toISOString(), response });
        return response;
      });
      pages.sort((a, b) => a.pageNum - b.pageNum);
      passes.push({ pass, startedAt, completedAt: new Date().toISOString(), pages, retention });
    }
    const rows = item => item.pages.flatMap(page => page.response.rows);
    if (!isDeepStrictEqual(rows(passes[0]), rows(passes[1]))
      || !isDeepStrictEqual(passes[0].retention, passes[1].retention)) {
      throw new Error('任务A卡保留两遍全量读取不一致；列表期间发生变化，未交付不完整文件');
    }
    return { passes, retention: passes[0].retention, readConcurrency: { ...limiter.stats },
      independentSourceReadbackPassed: true, transactionalSnapshotGuaranteed: false };
  } catch (error) {
    error.completedPasses = passes;
    throw error;
  } finally { await limiter.drain(); }
}

// Restart only a read snapshot, never the runner or a business submission.
export async function collectCardRetentionSnapshot(api, { wait = sleep } = {}) {
  const attempts = [];
  for (let attempt = 1; attempt <= 3; attempt++) {
    const startedAt = new Date().toISOString();
    try {
      const result = await collectSnapshotAttempt(api);
      attempts.push({ attempt, startedAt, completedAt: new Date().toISOString(), status: 'passed' });
      return { ...result, snapshotAttempts: attempts };
    } catch (error) {
      const retryable = /^卡保留记录分页期间total变化:/.test(error.message)
        || /^任务A卡保留两遍全量读取不一致；/.test(error.message);
      const delayMs = retryable && attempt < 3 ? attempt * 2000 : 0;
      attempts.push({ attempt, startedAt, completedAt: new Date().toISOString(),
        status: 'failed', error: error.message, retryable, delayMs,
        completedPasses: error.completedPasses || [] });
      if (!delayMs) { error.snapshotAttempts = attempts; throw error; }
      // collectSnapshotAttempt drains every in-flight read before returning.
      await wait(delayMs);
    }
  }
}

export async function main(argv = process.argv.slice(2)) {
  const [runId, output, ...extra] = argv;
  const targetId = process.env.OPENSMS_RAW_API_TARGET_ID;
  if (extra.length || !/^\d{8}T\d{6}\+0800$/.test(runId || '') || !path.isAbsolute(output || '')
    || process.env.OPENSMS_CARD_RETENTION_REPORT_RUN_ID !== runId || !targetId
    || process.env.OPENSMS_RUN_ID !== runId) {
    throw new Error('任务A卡保留读取需要本轮授权、运行编号及精确自有API页');
  }
  const handle = await fs.open(output, 'wx', 0o600);
  const receipt = { version: 1, task: 'A', scope: 'card_retention_report', runId,
    status: 'running', readOnly: true, targetId, endpoint: ENDPOINT, pageSize: 200,
    backendWrites: 0, silentBrowserMode: true, foregroundActivationAttempted: false,
    startedAt: new Date().toISOString() };
  let session, failure;
  try {
    session = await createRawOpenSmsApiSession({ cdp: process.env.BITBROWSER_CDP, pathName: '/index' });
    Object.assign(receipt, await collectCardRetentionSnapshot(session.api));
    receipt.status = 'passed';
  } catch (error) {
    failure = error;
    receipt.status = 'failed';
    receipt.error = error.message;
    receipt.completedPasses = error.completedPasses || [];
    receipt.snapshotAttempts = error.snapshotAttempts || [];
  } finally {
    session?.client.close();
    receipt.clientClosed = true;
    receipt.sharedTargetOwnedByRunner = true;
    receipt.completedAt = new Date().toISOString();
    try { await handle.writeFile(JSON.stringify(receipt, null, 2) + '\n'); await handle.sync(); }
    finally { await handle.close(); }
  }
  if (failure) throw new Error(`任务A卡保留只读采集未通过：${failure.message}`);
  console.log(JSON.stringify({ status: receipt.status, runId, receipt: output,
    totalRecords: receipt.retention.totalRecords, pageCount: receipt.retention.pageCount,
    uniqueCardCount: receipt.retention.uniqueCardCount, backendWrites: 0 }));
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
