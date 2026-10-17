import fs from 'node:fs/promises';
import { constants as fsConstants } from 'node:fs';
import path from 'node:path';
import { withReadRetry } from '../opensms_read_retry.mjs';
import { readExportClockBeforeSubmit } from './opensms_export_clock_recovery.mjs';
import { submissionReadyConfig, publishSubmissionReady } from './opensms_export_submission_ready.mjs';
import { withOpenSmsRequestPacing } from '../opensms_request_pacing.mjs';
import { validateExportDownloadResponse } from './opensms_export_download_response.mjs';
import { fetchExportDownloadBounded } from '../opensms_bounded_status_fetch.mjs';

import {
  createRawBackgroundPage,
  createRawOpenSmsApiSession,
} from '../opensms_protocol_client.mjs';
import {
  EXPORT_ARCHIVE_DIR,
  SNAPSHOT_DIR,
  ensureArtifactDirs,
} from '../opensms_artifact_paths.mjs';

const PRIMARY_EXPORT_DIR = '/Volumes/zs01/430';
const FALLBACK_EXPORT_DIR = '/Users/zhangsan/Downloads/430';
const SILENT_BROWSER_MODE = true;
let exportDirectory = null;

function nowIso() {
  return new Date().toISOString();
}

function receiptStamp() {
  return new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
}

function localFileStamp(mtimeMs) {
  const value = new Date(mtimeMs);
  const pad = number => String(number).padStart(2, '0');
  const offsetMinutes = -value.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? '+' : '-';
  const offset = Math.abs(offsetMinutes);
  return `${value.getFullYear()}${pad(value.getMonth() + 1)}${pad(value.getDate())}`
    + `T${pad(value.getHours())}${pad(value.getMinutes())}${pad(value.getSeconds())}`
    + `${sign}${pad(Math.floor(offset / 60))}${pad(offset % 60)}`;
}

async function isWritableDirectory(directory) {
  try {
    const stat = await fs.stat(directory);
    if (!stat.isDirectory()) return false;
    await fs.access(directory, fsConstants.W_OK);
    return true;
  } catch {
    return false;
  }
}

async function resolveExportDirectory() {
  if (process.env.OPENSMS_EXPORT_DIR) {
    const directory = path.resolve(process.env.OPENSMS_EXPORT_DIR);
    await fs.mkdir(directory, { recursive: true });
    if (!await isWritableDirectory(directory)) throw new Error(`OPENSMS_EXPORT_DIR 不可写: ${directory}`);
    return { directory, source: 'environment_override', fallbackReason: null };
  }
  if (await isWritableDirectory(PRIMARY_EXPORT_DIR)) {
    return { directory: PRIMARY_EXPORT_DIR, source: 'external_primary', fallbackReason: null };
  }
  await fs.mkdir(FALLBACK_EXPORT_DIR, { recursive: true });
  if (!await isWritableDirectory(FALLBACK_EXPORT_DIR)) {
    throw new Error(`本地回退下载目录不可写: ${FALLBACK_EXPORT_DIR}`);
  }
  return {
    directory: FALLBACK_EXPORT_DIR,
    source: 'local_fallback',
    fallbackReason: `${PRIMARY_EXPORT_DIR} 未挂载、目录不存在或不可写`,
  };
}

async function listExports(directory = exportDirectory) {
  const rows = [];
  for (const name of await fs.readdir(directory)) {
    if (!/^export4day(?: \(\d+\))?\.xlsx$/.test(name)) continue;
    const filePath = path.join(directory, name);
    const stat = await fs.stat(filePath);
    rows.push({ name, path: filePath, mtimeMs: stat.mtimeMs, size: stat.size });
  }
  rows.sort((left, right) => right.mtimeMs - left.mtimeMs);
  return rows;
}

async function archiveCurrentExport() {
  const currentPath = path.join(exportDirectory, 'export4day.xlsx');
  let currentStat;
  try {
    currentStat = await fs.stat(currentPath);
  } catch (error) {
    if (error?.code === 'ENOENT') return null;
    throw error;
  }
  const archiveDirectory = exportDirectory === FALLBACK_EXPORT_DIR ? EXPORT_ARCHIVE_DIR : exportDirectory;
  let targetPath = path.join(archiveDirectory, `export4day_${localFileStamp(currentStat.mtimeMs)}.xlsx`);
  let suffix = 1;
  while (true) {
    try {
      await fs.access(targetPath);
      targetPath = path.join(
        archiveDirectory,
        `export4day_${localFileStamp(currentStat.mtimeMs)}_${suffix}.xlsx`,
      );
      suffix += 1;
    } catch (error) {
      if (error?.code === 'ENOENT') break;
      throw error;
    }
  }
  await fs.rename(currentPath, targetPath);
  return {
    sourcePath: currentPath,
    archivedPath: targetPath,
    mtimeMs: currentStat.mtimeMs,
    size: currentStat.size,
  };
}

async function moveAcrossFilesystems(source, target) {
  try {
    await fs.rename(source, target);
  } catch (error) {
    if (error?.code !== 'EXDEV') throw error;
    await fs.copyFile(source, target);
    await fs.unlink(source);
  }
}

async function archiveFreshFallbackExtras(beforeMax) {
  const files = (await listExports(FALLBACK_EXPORT_DIR))
    .filter(file => file.mtimeMs > beforeMax + 500);
  for (const file of files) {
    const stamp = localFileStamp(file.mtimeMs);
    let target = path.join(EXPORT_ARCHIVE_DIR, `export4day_duplicate_${stamp}.xlsx`);
    let suffix = 1;
    while (true) {
      try {
        await fs.access(target);
        target = path.join(EXPORT_ARCHIVE_DIR, `export4day_duplicate_${stamp}_${suffix}.xlsx`);
        suffix += 1;
      } catch (error) {
        if (error?.code === 'ENOENT') break;
        throw error;
      }
    }
    await moveAcrossFilesystems(file.path, target);
  }
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function hasExpectedPath(href, pagePath) {
  try {
    const url = new URL(href);
    return url.origin === 'https://adminsms.smsoug.com' && url.pathname === pagePath;
  } catch {
    return false;
  }
}

async function pageSnapshot(client) {
  return client.evaluate(`(() => ({
    href: location.href,
    bodyText: String(document.body?.innerText || '').slice(0, 20000),
    loginVisible: !!document.querySelector('input[placeholder="账号"]'),
  }))()`, 15000);
}

async function waitForPage(client, ready, label, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      last = await pageSnapshot(client);
      if (/\/login(?:$|[?#])/.test(last.href) || last.loginVisible) {
        throw new Error(`OpenSMS 登录已失效，无法${label}`);
      }
      if (ready(last)) return last;
    } catch (error) {
      lastError = error;
      if (/登录已失效/.test(error.message)) throw error;
    }
    await sleep(250);
  }
  const error = new Error(`${label}超时: ${lastError?.message || JSON.stringify(last)}`);
  if (label === '读取平台服务器日期' && !lastError && !last?.loginVisible
      && hasExpectedPath(last?.href, '/export4day')) error.code = 'EXPORT_SERVER_CLOCK_MISSING';
  throw error;
}

async function navigate(client, pagePath) {
  const started = Date.now();
  const destination = new URL(pagePath, 'https://adminsms.smsoug.com');
  if (destination.origin !== 'https://adminsms.smsoug.com' || destination.username || destination.password) {
    throw new Error('OpenSMS 导出导航地址必须为同源地址');
  }
  const current = new URL(await client.evaluate('String(location.href)', 15000));
  if (current.origin !== destination.origin || current.username || current.password) {
    throw new Error('OpenSMS 导出页进入意外来源，已停止导航');
  }
  const receipt = {
    requestedUrl: `${destination.origin}${destination.pathname}`,
    lastUrl: `${current.origin}${current.pathname}`,
    navigationCount: 1,
    status: 'command-pending',
  };
  // A new hidden target already navigates to its requested route. Reissuing
  // Page.navigate here can abort an in-flight load; readiness is checked below
  // by the caller, not inferred from the URL alone.
  if (current.pathname === destination.pathname && current.search === destination.search
    && current.hash === destination.hash) {
    return { ...receipt, navigationCount: 0, status: 'already-at-destination',
      elapsedMs: Date.now() - started, navigationResult: null };
  }
  let result;
  try {
    result = await client.send('Page.navigate', { url: destination.href }, 15000);
  } catch {
    const error = new Error('OpenSMS 导出页导航命令失败');
    error.navigationReceipt = { ...receipt, status: 'failed', elapsedMs: Date.now() - started };
    throw error;
  }
  receipt.elapsedMs = Date.now() - started;
  receipt.navigationResult = {
    accepted: !result?.errorText && result?.isDownload !== true,
    isDownload: result?.isDownload === true,
    errorText: result?.errorText
      ? String(result.errorText).match(/net::ERR_[A-Z0-9_]+/)?.[0] || '[redacted navigation error]'
      : null,
  };
  if (!receipt.navigationResult.accepted) {
    const error = new Error(result?.isDownload === true
      ? 'OpenSMS 导出页导航意外触发下载'
      : `OpenSMS 导出页导航失败: ${receipt.navigationResult.errorText}`);
    error.navigationReceipt = { ...receipt, status: 'failed' };
    throw error;
  }
  return { ...receipt, status: 'command-accepted' };
}

async function readServerDate(client, pagePath = '/day') {
  if (!['/day', '/export4day'].includes(pagePath)) throw new Error('服务器日期读取路径不受支持');
  const state = await waitForPage(
    client,
    row => hasExpectedPath(row.href, pagePath)
      && /20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}/.test(row.bodyText),
    '读取平台服务器日期',
  );
  const candidates = await client.evaluate(`(() => {
    const rows = [];
    for (const element of document.querySelectorAll('body *')) {
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      const text = String(element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim();
      // Fractional layout noise can put the real top header a fraction of a
      // CSS pixel above zero. Keep the top-header bound; never use local time.
      if (!rect.width || !rect.height || rect.top < -1 || rect.top > 180) continue;
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
      if (!text || text.length > 80 || !/20\\d{2}[-/.年]\\d{1,2}[-/.月]\\d{1,2}/.test(text)) continue;
      rows.push({ text, top: rect.top, left: rect.left, right: rect.right, width: rect.width });
    }
    return rows;
  })()`);
  const normalize = value => {
    const match = String(value || '').match(/(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?/);
    return match ? `${match[1]}-${match[2].padStart(2, '0')}-${match[3].padStart(2, '0')}` : null;
  };
  const normalized = candidates
    .map(row => ({ ...row, date: normalize(row.text) }))
    .filter(row => row.date)
    .sort((left, right) => (right.left - left.left) || (left.top - right.top) || (left.width - right.width));
  const dates = [...new Set(normalized.map(row => row.date))];
  if (dates.length !== 1) throw new Error(`平台顶部服务器日期无法唯一解析: ${JSON.stringify(normalized)}`);
  return { serverDate: dates[0], candidates: normalized, page: state.href };
}

async function inspectExportPage(client) {
  const navigationReceipt = await navigate(client, '/export4day');
  const state = await waitForPage(
    client,
    row => hasExpectedPath(row.href, '/export4day') && (row.bodyText.includes('后台生成全部号码近10日统计')
      || row.bodyText.includes('后台生成中...')
      || row.bodyText.includes('生成完成，可以下载')),
    '检查导出页面',
  );
  return {
    page: state.href,
    ready: true,
    bodySample: state.bodyText.slice(0, 500),
    navigationReceipt: { ...navigationReceipt, status: 'ready', lastUrl: 'https://adminsms.smsoug.com/export4day' },
  };
}

async function submitFreshExport(client) {
  const navigationReceipt = await navigate(client, '/export4day');
  let state = await waitForPage(
    client,
    row => hasExpectedPath(row.href, '/export4day') && (row.bodyText.includes('后台生成全部号码近10日统计')
      || row.bodyText.includes('后台生成中...')
      || row.bodyText.includes('生成完成，可以下载')),
    '加载导出页面',
  );
  if (state.bodyText.includes('正在加载系统资源')) throw new Error('OpenSMS 静默后台页资源未加载');
  if (state.bodyText.includes('后台生成选中的')) {
    const cleared = await client.evaluate(`(() => {
      const visible = element => {
        const rect = element?.getBoundingClientRect?.();
        const style = element ? getComputedStyle(element) : null;
        return !!rect && rect.width > 0 && rect.height > 0 && style?.display !== 'none' && style?.visibility !== 'hidden';
      };
      const button = [...document.querySelectorAll('button, [role="button"]')]
        .filter(visible)
        .find(element => String(element.innerText || element.textContent || '').replace(/\\s+/g, '').includes('清空'));
      if (!button) return false;
      button.click();
      return true;
    })()`);
    if (!cleared) throw new Error('导出页存在号码筛选，但未找到清空按钮');
    await sleep(500);
    state = await pageSnapshot(client);
  }
  const existingGeneration = state.bodyText.includes('后台生成中...') && /状态：\s*生成中/.test(state.bodyText);
  const previousTaskId = state.bodyText.match(/任务ID：\s*([a-f0-9]{32})/i)?.[1] || null;
  if (!existingGeneration) {
    const clicked = await client.evaluate(`(() => {
      window.confirm = () => true;
      window.alert = () => {};
      const visible = element => {
        const rect = element?.getBoundingClientRect?.();
        const style = element ? getComputedStyle(element) : null;
        return !!rect && rect.width > 0 && rect.height > 0 && style?.display !== 'none' && style?.visibility !== 'hidden';
      };
      const button = [...document.querySelectorAll('button, [role="button"]')]
        .filter(visible)
        .find(element => String(element.innerText || element.textContent || '').replace(/\\s+/g, '').includes('后台生成全部号码近10日统计'));
      if (!button) return false;
      button.click();
      return true;
    })()`);
    if (!clicked) throw new Error('未找到后台生成全部号码按钮');
    await sleep(750);
    await client.evaluate(`(() => {
      const visible = element => {
        const rect = element?.getBoundingClientRect?.();
        const style = element ? getComputedStyle(element) : null;
        return !!rect && rect.width > 0 && rect.height > 0 && style?.display !== 'none' && style?.visibility !== 'hidden';
      };
      const button = [...document.querySelectorAll('button, [role="button"]')]
        .filter(visible)
        .find(element => String(element.innerText || element.textContent || '').replace(/\\s+/g, '').includes('确认生成'));
      if (button) button.click();
      return !!button;
    })()`);
  }
  state = await waitForPage(
    client,
    row => {
      const candidate = row.bodyText.match(/任务ID：\s*([a-f0-9]{32})/i)?.[1] || null;
      return !!candidate && (existingGeneration || !previousTaskId || candidate !== previousTaskId);
    },
    '读取本轮导出任务ID',
    15000,
  );
  const taskId = state.bodyText.match(/任务ID：\s*([a-f0-9]{32})/i)?.[1] || null;
  if (!taskId) throw new Error(`未能读取本轮导出任务ID: ${state.bodyText.slice(-800)}`);
  return {
    taskId,
    existingGeneration,
    page: state.href,
    bodyText: state.bodyText.slice(0, 1800),
    navigationReceipt: { ...navigationReceipt, status: 'ready', lastUrl: 'https://adminsms.smsoug.com/export4day' },
  };
}

async function downloadExport(client, token, taskId, onAttempt = () => {}) {
  if (!/^[a-f0-9]{32}$/i.test(String(taskId || ''))) throw new Error('下载缺少已确认的本轮任务ID');
  const authorization = token.startsWith('Bearer ') ? token : `Bearer ${token}`;
  const downloadAttempts = [];
  let downloadDiagnostic = null;
  const response = await withReadRetry(() => withOpenSmsRequestPacing(async () => {
    downloadDiagnostic = null;
    try {
      const result = await fetchExportDownloadBounded(client, {
        requestUrl: `https://adminsms.smsoug.com/api/adminExport/export4day/async/download?taskId=${taskId}`,
        requestMethod: 'GET', requestBody: null, authorization,
        extraHeaders: { Accept: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, */*' },
        readTimeoutMs: 120000,
      });
      if (result?.transportError) {
        throw Object.assign(new Error('Export download transport failed'), {
          requestCompletionConfirmed: true, requestCompletionUnconfirmed: false,
          downloadFetchDiagnostic: { mode: 'host-polled-same-page', timeoutMs: 120000,
            completionConfirmed: true, category: 'download_transport_error' },
        });
      }
      const checked = validateExportDownloadResponse(result);
      downloadDiagnostic = checked.diagnostic;
      return { ...result, payload: checked.payload };
    } catch (error) {
      if (error.downloadFetchDiagnostic) error.downloadDiagnostic = error.downloadFetchDiagnostic;
      downloadDiagnostic = error.downloadDiagnostic ?? null;
      throw error;
    }
  }), {
    method: 'GET', apiPath: '/adminExport/export4day/async/download', searchParams: { taskId },
    onAttempt: async attempt => {
      const receipt = { ...attempt, response: downloadDiagnostic };
      downloadAttempts.push(receipt);
      await onAttempt(receipt);
    },
  });
  const payload = response.payload;
  const target = path.join(exportDirectory, 'export4day.xlsx');
  await fs.writeFile(target, payload, { flag: 'wx' });
  const stat = await fs.stat(target);
  return {
    name: path.basename(target),
    path: target,
    mtimeMs: stat.mtimeMs,
    size: stat.size,
    via: 'raw-cdp-same-origin-fetch',
    contentDisposition: response.contentDisposition,
    contentType: response.contentType,
    downloadAttempts,
    retryWaitMs: downloadAttempts.reduce((total, attempt) => total + (attempt.waitMs || 0), 0),
  };
}

async function main() {
  const scheduling = submissionReadyConfig();
  const processStartedAt = nowIso();
  const processStartedMs = Date.now();
  await ensureArtifactDirs();
  const exportDirectorySelection = await resolveExportDirectory();
  exportDirectory = exportDirectorySelection.directory;
  if (process.env.OPENSMS_EXPORT_DIRECTORY_CHECK_ONLY === '1') {
    console.log(JSON.stringify(exportDirectorySelection, null, 2));
    return;
  }
  if (process.env.OPENSMS_EXPORT_PREFLIGHT_ONLY === '1') {
    const session = await createRawOpenSmsApiSession({
      cdp: process.env.BITBROWSER_CDP,
      pathName: '/export4day',
    });
    let backgroundPage;
    try {
      backgroundPage = await createRawBackgroundPage({
        cdp: process.env.BITBROWSER_CDP,
        url: 'https://adminsms.smsoug.com/export4day',
      });
      const serverDate = await readServerDate(backgroundPage.client, '/export4day');
      const exportPage = await inspectExportPage(backgroundPage.client);
      console.log(JSON.stringify({
        status: exportPage.ready ? 'passed' : 'failed',
        mode: 'read-only-preflight',
        silentBrowserMode: true,
        browserForegroundActivationAttempted: false,
        playwrightBrowserConnectionUsed: false,
        transport: process.env.OPENSMS_BROWSER_KIND === 'cloakbrowser' ? 'exact-cloakbrowser-raw-cdp-background-target' : 'exact-bitbrowser-430-raw-cdp-background-target',
        elapsedMs: Date.now() - processStartedMs,
        navigationReceipt: backgroundPage.navigationReceipt ?? null,
        startupRecovery: backgroundPage.startupRecovery ?? null,
        viewportReceipt: backgroundPage.viewportReceipt ?? null,
        serverDate,
        exportPage,
      }, null, 2));
      if (!exportPage.ready) process.exitCode = 2;
    } finally {
      if (backgroundPage) await backgroundPage.close().catch(() => {});
      await session.browser.close().catch(() => {});
    }
    return;
  }
  const archivedBeforeDownload = await archiveCurrentExport();
  const before = await listExports();
  const fallbackBefore = exportDirectory === FALLBACK_EXPORT_DIR ? [] : await listExports(FALLBACK_EXPORT_DIR);
  const fallbackBeforeMax = fallbackBefore[0]?.mtimeMs ?? 0;
  const session = await createRawOpenSmsApiSession({
    cdp: process.env.BITBROWSER_CDP,
    pathName: '/export4day',
  });
  let backgroundPage;
  try {
    backgroundPage = await createRawBackgroundPage({
      cdp: process.env.BITBROWSER_CDP,
      url: 'https://adminsms.smsoug.com/export4day',
    });
    const setupMs = Date.now() - processStartedMs;
    // Read the export page's actual server header without a /day -> /export4day
    // reload. The runner independently reads /day and /phoneConfig and requires
    // all three server dates to match before generating documents or writing.
    const serverDateStartedMs = Date.now();
    const serverDateReadback = await withOpenSmsRequestPacing(() => readExportClockBeforeSubmit({
      read: () => readServerDate(backgroundPage.client, '/export4day'),
      reload: () => backgroundPage.client.send('Page.reload', {ignoreCache: false}, 15000),
      record: async row => console.error(JSON.stringify(row)),
    }));
    const serverDateReadMs = Date.now() - serverDateStartedMs;
    const submitStartedMs = Date.now();
    const exportProgressPath = path.join(SNAPSHOT_DIR, `opensms_export_progress_${process.env.OPENSMS_RUN_ID || receiptStamp()}.json`);
    const progress = {runId: process.env.OPENSMS_RUN_ID || null, status: 'submission_pending',
      serverDate: serverDateReadback.serverDate, submissionReplayBlocked: true, createdAt: nowIso()};
    await fs.writeFile(exportProgressPath, JSON.stringify(progress, null, 2) + '\n', {flag:'wx'});
    const navigationReceipt = backgroundPage.navigationReceipt ?? null;
    const startupRecovery = backgroundPage.startupRecovery ?? null;
    const submitted = await withOpenSmsRequestPacing(async () => {
      const submission = await submitFreshExport(backgroundPage.client);
      Object.assign(progress, {status:'submitted', taskId:submission.taskId, submittedAt:nowIso()});
      await fs.writeFile(exportProgressPath, JSON.stringify(progress, null, 2) + '\n');
      // Keep the lane until the owned page's automatic polling has stopped.
      await backgroundPage.close();
      backgroundPage = null;
      return submission;
    });
    const submitMs = Date.now() - submitStartedMs;
    // Only after confirmed submission, persisted progress, owned-page close and
    // release of the paced lane may the runner dispatch independent reads.
    await publishSubmissionReady(scheduling, {taskId: submitted.taskId,
      serverDate: serverDateReadback.serverDate, exportPageClosed: backgroundPage === null});
    const generationStartedMs = Date.now();
    const completionDeadline = generationStartedMs + 600000;
    let generationStatus = null;
    let statusPollCount = 0;
    let statusRequestMs = 0;
    let pollingSleepMs = 0;
    try {
      while (Date.now() < completionDeadline) {
        const statusRequestStartedMs = Date.now();
        generationStatus = await session.api(
          'GET',
          '/adminExport/export4day/async/status',
          null,
          { taskId: submitted.taskId },
        );
        statusRequestMs += Date.now() - statusRequestStartedMs;
        statusPollCount += 1;
        Object.assign(progress, {status: 'awaiting_generation', statusPollCount,
          taskStatus: generationStatus?.status ?? null, lastStatusPollAt: nowIso()});
        await fs.writeFile(exportProgressPath, JSON.stringify(progress, null, 2) + '\n');
        if (generationStatus?.status === 'SUCCESS') break;
        if (generationStatus?.status === 'FAILED') {
          throw Object.assign(new Error('后台明确返回本轮导出生成失败'),
            {exportFailureCategory: 'server_generation_failed'});
        }
        const pollingSleepStartedMs = Date.now();
        await new Promise(resolve => setTimeout(resolve, 15000));
        pollingSleepMs += Date.now() - pollingSleepStartedMs;
      }
      if (generationStatus?.status !== 'SUCCESS') {
        throw Object.assign(new Error('等待本轮导出任务完成超时'),
          {exportFailureCategory: 'generation_timeout'});
      }
    } catch (error) {
      const read = error.readDiagnostic;
      const category = error.exportFailureCategory || read?.category
        || (error.code === 'CDP_COMMAND_TIMEOUT' ? 'browser_runtime_timeout' : 'status_query_error');
      Object.assign(progress, {status: 'status_query_failed', failedAt: nowIso(),
        generationFailure: {category, stage: 'generation_status', statusPollCount,
          browserCommand: error.cdpMethod === 'Runtime.evaluate' ? 'Runtime.evaluate' : null,
          completionConfirmed: error.requestCompletionConfirmed ?? null,
          submissionReplayBlocked: true,
          attempts: (error.readAttempts || []).map(row => Object.fromEntries(
            ['attempt', 'success', 'category', 'retry', 'durationMs', 'waitMs', 'completionConfirmed']
              .filter(key => key in row).map(key => [key, row[key]]))),
        }});
      await fs.writeFile(exportProgressPath, JSON.stringify(progress, null, 2) + '\n');
      console.error(JSON.stringify({exportGenerationFailure: progress.generationFailure}));
      throw error;
    }
    const pollingWaitMs = Date.now() - generationStartedMs;
    // User-confirmed server cooldown: after SUCCESS, wait before the first
    // download instead of deliberately provoking the export frequency limit.
    // Keep the same task and persist the checkpoint before sleeping.
    const cooldownStartedMs = Date.now();
    Object.assign(progress, {
      status: 'download_cooldown', taskStatus: 'SUCCESS', downloadAttempts: [],
      preDownloadWaitMs: 60000,
      downloadNotBefore: new Date(cooldownStartedMs + 60000).toISOString(),
    });
    await fs.writeFile(exportProgressPath, JSON.stringify(progress, null, 2) + '\n');
    await new Promise(resolve => setTimeout(resolve, 60000));
    const preDownloadWaitMs = Date.now() - cooldownStartedMs;
    const downloadStartedMs = Date.now();
    Object.assign(progress, {status:'downloading', taskStatus:'SUCCESS', downloadAttempts:[]});
    await fs.writeFile(exportProgressPath, JSON.stringify(progress, null, 2) + '\n');
    let saved;
    try {
      saved = await downloadExport(session.client, session.token, submitted.taskId, async attempt => {
        progress.downloadAttempts.push(attempt);
        progress.status = attempt.retry ? 'download_backoff' : 'downloading';
        await fs.writeFile(exportProgressPath, JSON.stringify(progress, null, 2) + '\n');
      });
    } catch (error) {
      Object.assign(progress, {status:'download_failed', failedAt:nowIso(),
        httpStatus:error.downloadDiagnostic?.httpStatus ?? error.status ?? null,
        downloadFailure:error.downloadDiagnostic ?? null});
      await fs.writeFile(exportProgressPath, JSON.stringify(progress, null, 2) + '\n');
      throw error;
    }
    const downloadMs = Date.now() - downloadStartedMs;
    const postDownloadArchiveStartedMs = Date.now();
    if (exportDirectory !== FALLBACK_EXPORT_DIR) await archiveFreshFallbackExtras(fallbackBeforeMax);
    const after = await listExports();
    const postDownloadArchiveMs = Date.now() - postDownloadArchiveStartedMs;
    const result = {
      startedAt: processStartedAt,
      completedAt: nowIso(),
      silentBrowserMode: SILENT_BROWSER_MODE,
      browserForegroundActivationAttempted: false,
      transport: process.env.OPENSMS_BROWSER_KIND === 'cloakbrowser' ? 'exact-cloakbrowser-raw-cdp-background-target' : 'exact-bitbrowser-430-raw-cdp-background-target',
      playwrightBrowserConnectionUsed: false,
      navigationReceipt,
      startupRecovery,
      exportProgressPath,
      statusPollingMode: 'single-reader-after-owned-export-page-close',
      statusPollingIntervalMs: 15000,
      exportNavigationReceipt: submitted.navigationReceipt ?? null,
      exportDirectory,
      exportDirectorySelection,
      loadedAppFallback: false,
      archivedBeforeDownload,
      taskId: submitted.taskId,
      taskStatus: generationStatus.status,
      phoneCount: generationStatus.phoneCount,
      elapsedMs: generationStatus.elapsedMs,
      runnerElapsedMs: Date.now() - processStartedMs,
      // Preserve the historical field; it includes download/archive time.
      // Use pollingWaitMs for the wait ending at the first SUCCESS receipt.
      generationWaitMs: Date.now() - generationStartedMs,
      backendGenerationMs: generationStatus.elapsedMs,
      pollingWaitMs,
      preDownloadWaitMs,
      downloadMs,
      downloadAttempts: saved.downloadAttempts ?? [],
      downloadRetryWaitMs: saved.retryWaitMs ?? 0,
      statusPollCount,
      timingsMs: {
        setupMs,
        serverDateReadMs,
        submitMs,
        pollingWaitMs,
        preDownloadWaitMs,
        downloadMs,
        postDownloadArchiveMs,
        backendGenerationMs: generationStatus.elapsedMs,
        statusRequestMs,
        pollingSleepMs,
      },
      timingNotes: {
        backendGenerationMs: '后台返回的生成耗时；与pollingWaitMs重叠，不得相加',
        pollingWaitMs: '取得任务ID后至首次SUCCESS；包含状态请求和轮询间隔，不包含下载',
        preDownloadWaitMs: '确认生成成功后主动等待至少60秒，再首次下载同一个taskId',
        generationWaitMs: '兼容旧字段：等待、下载前冷却、下载及下载后归档合计',
      },
      downloadedAt: nowIso(),
      serverDate: serverDateReadback.serverDate,
      serverDatePage: serverDateReadback.page,
      serverDateCandidates: serverDateReadback.candidates,
      url: submitted.page,
      beforeLatest: before[0] || null,
      saved,
      afterLatest: after[0] || null,
      bodyText: submitted.bodyText,
    };
    const receiptPath = path.join(SNAPSHOT_DIR, `opensms_export_once_${receiptStamp()}.json`);
    result.receiptPath = receiptPath;
    Object.assign(progress, {status:'downloaded', receiptPath, savedPath:saved.path, completedAt:nowIso()});
    await fs.writeFile(exportProgressPath, JSON.stringify(progress, null, 2) + '\n');
    await fs.writeFile(receiptPath, `${JSON.stringify(result, null, 2)}\n`, { flag: 'wx' });
    console.log(JSON.stringify(result, null, 2));
  } finally {
    if (backgroundPage) await backgroundPage.close().catch(() => {});
    await session.browser.close().catch(() => {});
  }
}

main().catch(error => {
  if (error.downloadDiagnostic) console.error(JSON.stringify({exportDownloadFailure:error.downloadDiagnostic}));
  if (error.startupRecovery) console.error(JSON.stringify({startupRecovery: error.startupRecovery}));
  if (error.navigationReceipt) console.error(JSON.stringify({
    event: 'export_navigation_failed', navigationReceipt: error.navigationReceipt,
  }));
  console.error(error.stack || String(error));
  process.exit(1);
});
