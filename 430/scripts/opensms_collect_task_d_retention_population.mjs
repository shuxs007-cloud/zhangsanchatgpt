#!/usr/bin/env node
// Post-Task-D independent population read. Only exact carrier-filtered reads;
// no cardRetain logs, workbook subsets, operator writes, or expiry writes.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { assertOk, createRawOpenSmsApiSession } from '../opensms_protocol_client.mjs';
import { collectCarrierConfigPagesReadOnly } from './opensms_carriers_readonly.mjs';
import { createReadOnlyApiLimiter, mapReadOnlyBounded } from './opensms_readonly_concurrency.mjs';

export const RETENTION_CARRIER_NAMES = Object.freeze(['B14-C快下保留卡', 'B14-T快下保留卡']);
const PAGE_SIZE = 500;
const MAX_PAGES_PER_CARRIER = 1000;
const ALLOWED_READS = Object.freeze(['GET /carrier/config/listNotDeleted', 'POST /phone/config/list']);
const str = value => String(value ?? '').trim();
const carrierName = row => str(row.name || row.carrierName || row.carrireName || row.carrier);
const carrierIdentif = row => str(row.carrireIdentif || row.carrierIdentif || row.identif);
const now = () => new Date().toISOString();

export function requireAuthorization(runId, env = process.env) {
  const gates = ['OPENSMS_FORCE_TASK_D', 'OPENSMS_ENABLE_TASK_D', 'OPENSMS_EXECUTE_TASK_D'];
  const missing = gates.filter(name => env[name] !== '1');
  if (missing.length) throw new Error(`任务D保留区统计授权门禁不足: ${missing.join(',')}`);
  if (!str(runId) || str(runId) !== runId || !str(env.OPENSMS_RUN_ID) || runId !== env.OPENSMS_RUN_ID) {
    throw new Error('任务D保留区统计 runId 与本轮 OPENSMS_RUN_ID 不一致或为空');
  }
  if (!str(env.BITBROWSER_CDP) || !str(env.OPENSMS_RAW_API_TARGET_ID)) {
    throw new Error('任务D保留区统计缺少本轮430专属CDP/页面，拒绝回退其他会话');
  }
}

export function normalizePhone10(value) {
  // Do not truncate arbitrary long values/ICCID into plausible phone numbers.
  if (typeof value === 'number' && !Number.isSafeInteger(value)) return '';
  const digits = str(value).replace(/[\s()+-]/g, '');
  if (/^\d{10}$/.test(digits)) return digits;
  if (/^1\d{10}$/.test(digits)) return digits.slice(1);
  return '';
}

function exactCarrier(configs, name) {
  const matches = configs.filter(row => carrierName(row) === name);
  if (matches.length !== 1) throw new Error(`任务D保留区精确运营商匹配异常: ${name}, count=${matches.length}`);
  const identif = carrierIdentif(matches[0]);
  if (!identif || [true, 1, '1', 'true'].includes(matches[0].delData)) {
    throw new Error(`任务D保留区运营商标识缺失或已删除: ${name}`);
  }
  return { carrierName: name, carrierIdentif: identif };
}

export async function collectRetentionPopulation(api, { runId, concurrency = 4 } = {}) {
  if (!str(runId)) throw new Error('任务D保留区统计缺少 runId');
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 4) {
    throw new Error('任务D保留区只读并发必须为1至4的整数');
  }
  const createdAt = now();
  const startedAt = performance.now();
  const limiter = createReadOnlyApiLimiter(api, { concurrency, allowedRequests: ALLOWED_READS });
  try {
    const carrierConfig = await collectCarrierConfigPagesReadOnly(limiter.api, { concurrency });
    const groups = RETENTION_CARRIER_NAMES.map(name => ({ ...exactCarrier(carrierConfig.rows, name), pages: [] }));
    if (new Set(groups.map(group => group.carrierIdentif)).size !== groups.length) {
      throw new Error('任务D两个保留区使用相同运营商标识，拒绝重叠统计');
    }

    async function readPage(group, pageNum) {
      // No showAble/status/OPR filter: this is the whole carrier population.
      const body = { pageNum, pageSize: PAGE_SIZE, carrireIdentif: group.carrierIdentif };
      const response = await limiter.api('POST', '/phone/config/list', body, { pageNum, pageSize: PAGE_SIZE });
      assertOk(`任务D保留区 ${group.carrierName} 第${pageNum}页`, response);
      if (!Array.isArray(response.rows) || response.total === undefined || response.total === null || response.total === '') {
        throw new Error(`任务D保留区 ${group.carrierName} 第${pageNum}页缺少 rows/total`);
      }
      const total = Number(response.total);
      if (!['number', 'string'].includes(typeof response.total)
        || (typeof response.total === 'string' && !/^\d+$/.test(response.total.trim()))
        || !Number.isSafeInteger(total) || total < 0 || total > PAGE_SIZE * MAX_PAGES_PER_CARRIER) {
        throw new Error(`任务D保留区 ${group.carrierName} total 无效或超限`);
      }
      if (pageNum === 1) group.firstPageTotal = total;
      if (total !== group.firstPageTotal) {
        throw new Error(`任务D保留区 ${group.carrierName} 分页期间 total 变化`);
      }
      const expectedRowCount = Math.min(PAGE_SIZE, Math.max(0, total - (pageNum - 1) * PAGE_SIZE));
      if (response.rows.length !== expectedRowCount) {
        throw new Error(`任务D保留区 ${group.carrierName} 第${pageNum}页不完整: expected=${expectedRowCount}, received=${response.rows.length}`);
      }
      const rows = response.rows.map(row => {
        const backendId = str(row?.id);
        const phone10 = normalizePhone10(row?.cardNo ?? row?.phone ?? row?.cardPhone ?? row?.tel);
        if (!backendId || !phone10 || !['string', 'number'].includes(typeof row.id)
          || (typeof row.id === 'number' && !Number.isSafeInteger(row.id))) {
          throw new Error(`任务D保留区 ${group.carrierName} 缺少有效后台ID/手机号`);
        }
        const identif = carrierIdentif(row);
        const returnedName = str(row.carrierName || row.carrireName || row.carrier);
        if (identif !== group.carrierIdentif || (returnedName && returnedName !== group.carrierName)) {
          throw new Error(`任务D保留区 ${group.carrierName} 返回非精确目标运营商`);
        }
        return { phone10, backendId, carrierIdentif: identif };
      });
      group.pages[pageNum - 1] = {
        rows,
        proof: { pageNum, pageSize: PAGE_SIZE, reportedTotal: total, rowCount: rows.length, expectedRowCount, verifiedAt: now() },
      };
    }

    await mapReadOnlyBounded(groups, group => readPage(group, 1), { concurrency });
    const remainingPages = groups.flatMap(group => {
      group.pageCount = Math.max(1, Math.ceil(group.firstPageTotal / PAGE_SIZE));
      return Array.from({ length: group.pageCount - 1 }, (_, index) => ({ group, pageNum: index + 2 }));
    });
    await mapReadOnlyBounded(remainingPages, ({ group, pageNum }) => readPage(group, pageNum), { concurrency });

    const allBackendIds = new Set();
    const allPhones = new Set();
    const resultGroups = groups.map(group => {
      const rows = group.pages.flatMap(page => page.rows);
      if (group.pages.length !== group.pageCount || rows.length !== group.firstPageTotal) {
        throw new Error(`任务D保留区 ${group.carrierName} 全量分页覆盖不足`);
      }
      const phones = new Set();
      for (const row of rows) {
        if (allBackendIds.has(row.backendId)) throw new Error(`任务D保留区存在重复后台ID: ${row.backendId}`);
        allBackendIds.add(row.backendId);
        phones.add(row.phone10);
      }
      for (const phone10 of phones) {
        if (allPhones.has(phone10)) throw new Error(`任务D保留区手机号跨区重叠: ${phone10}`);
        allPhones.add(phone10);
      }
      return {
        carrierName: group.carrierName, carrierIdentif: group.carrierIdentif,
        firstPageTotal: group.firstPageTotal, rawRowCount: rows.length,
        uniquePhoneCount: phones.size, duplicatePhoneRowCount: rows.length - phones.size,
        pageCount: group.pageCount, pageSize: PAGE_SIZE,
        paginationTotalStable: true, backendIdsUnique: true, exactCarrierValidated: true,
        pageProofs: group.pages.map(page => page.proof), rows,
      };
    });
    await limiter.drain();
    return {
      schemaVersion: 1, task: 'D', status: 'passed', phase: 'post_task_d_retention_population',
      runId, createdAt, completedAt: now(), scope: 'exact_b14_c_t_all_pages',
      sourceProvenance: 'phone_config_exact_carriers_all_pages',
      endpoint: '/phone/config/list', countingUnit: 'unique_phone10', pageSize: PAGE_SIZE,
      totalUniquePhoneCount: allPhones.size, totalRawRowCount: allBackendIds.size,
      totalDuplicatePhoneRowCount: allBackendIds.size - allPhones.size,
      crossGroupPhoneOverlapCount: 0, backendIdsUnique: true,
      groups: resultGroups,
      carrierConfigurationProof: {
        pageSize: carrierConfig.pageSize, pageCount: carrierConfig.pageCount,
        totalRecords: carrierConfig.totalRecords, pages: carrierConfig.pages,
      },
      readOnly: true, silentBrowserMode: true, browserForegroundActivationAttempted: false,
      readOnlyConcurrency: { ...limiter.stats },
      elapsedSeconds: Number(((performance.now() - startedAt) / 1000).toFixed(3)),
    };
  } finally {
    // Includes failed requests and cancelled queued reads; no read may outlive
    // the caller's session, even when one carrier/page rejects early.
    await limiter.drain();
  }
}

async function writeExclusive(outputPath, payload) {
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.writeFile(outputPath, `${JSON.stringify(payload, null, 2)}\n`, { flag: 'wx' });
}

export async function runCollector({ runId, outputPath, env = process.env,
  createSession = createRawOpenSmsApiSession, writeReceipt = writeExclusive } = {}) {
  // Deleted carriers must never cause a live browser connection. Injected
  // offline adapters retain historical receipt regression capability only.
  if (createSession === createRawOpenSmsApiSession) {
    throw new Error('B14运营商已删除，保留区存量查询入口已停用');
  }
  requireAuthorization(runId, env);
  if (!str(outputPath)) throw new Error('任务D保留区统计缺少输出路径');
  const concurrency = Number(env.OPENSMS_TASK_D_RETENTION_READ_WORKERS || 4);
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 4) throw new Error('任务D保留区只读并发必须为1至4的整数');
  const session = await createSession({ cdp: env.BITBROWSER_CDP, pathName: '/phoneConfig' });
  try {
    const payload = await collectRetentionPopulation(session.api, { runId, concurrency });
    payload.authorization = { forceGate: true, enableGate: true, executeGate: true, runIdMatched: true };
    payload.browserSession = { profile: '430', dedicatedTargetId: env.OPENSMS_RAW_API_TARGET_ID };
    const output = path.resolve(outputPath);
    await writeReceipt(output, payload);
    return { ...payload, path: output };
  } finally {
    // Disconnect only. Never close or foreground the shared browser/page.
    if (session.browser?.close) await session.browser.close();
    else session.client?.close();
  }
}

async function main() {
  const [runId, outputPath, ...extra] = process.argv.slice(2);
  if (extra.length || !runId || !outputPath) throw new Error('usage: opensms_collect_task_d_retention_population.mjs RUN_ID OUTPUT.json');
  const result = await runCollector({ runId, outputPath });
  console.log(JSON.stringify({ ...result, groups: result.groups.map(({ rows, pageProofs, ...group }) => group) }));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(error?.stack || String(error)); process.exitCode = 1; });
}
