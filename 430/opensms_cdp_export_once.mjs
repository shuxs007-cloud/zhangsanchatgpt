import fs from 'node:fs/promises';
import { constants as fsConstants } from 'node:fs';
import path from 'node:path';
import {
  createOpenSmsApiSession,
  createSilentSameOriginFrame,
} from './opensms_protocol_client.mjs';
import {
  EXPORT_ARCHIVE_DIR,
  SNAPSHOT_DIR,
  ensureArtifactDirs,
} from './opensms_artifact_paths.mjs';

const PRIMARY_EXPORT_DIR = '/Volumes/zs01/430';
const FALLBACK_EXPORT_DIR = '/Users/zhangsan/Downloads/430';
let BASE = null;
const CDP = process.env.BITBROWSER_CDP;
const EXPORT_URL = 'https://adminsms.smsoug.com/export4day';
const DAY_URL = 'https://adminsms.smsoug.com/day';
const SILENT_BROWSER_MODE = true;

async function downloadExportFromPage(page, token, taskId) {
  const authorization = token.startsWith('Bearer ') ? token : `Bearer ${token}`;
  const response = await page.evaluate(async ({ taskId: currentTaskId, authorization: auth }) => {
    const requestUrl = `/api/adminExport/export4day/async/download?taskId=${encodeURIComponent(currentTaskId)}`;
    const result = await fetch(requestUrl, {
      method: 'GET',
      credentials: 'include',
      headers: {
        Accept: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, */*',
        Authorization: auth,
      },
    });
    if (!result.ok) {
      return { ok: false, status: result.status, text: (await result.text()).slice(0, 1000) };
    }
    const bytes = new Uint8Array(await result.arrayBuffer());
    let binary = '';
    const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
    }
    return {
      ok: true,
      status: result.status,
      contentDisposition: result.headers.get('content-disposition'),
      contentType: result.headers.get('content-type'),
      base64: btoa(binary),
    };
  }, { taskId, authorization });

  if (!response.ok) {
    throw new Error(`下载本轮导出失败: HTTP ${response.status} ${response.text || ''}`);
  }
  const payload = Buffer.from(response.base64, 'base64');
  if (payload.length < 100000 || payload[0] !== 0x50 || payload[1] !== 0x4b) {
    throw new Error(`本轮导出不是完整XLSX: size=${payload.length}`);
  }

  let target = path.join(BASE, 'export4day.xlsx');
  let suffix = 1;
  while (true) {
    try {
      await fs.access(target);
      target = path.join(BASE, `export4day (${suffix}).xlsx`);
      suffix += 1;
    } catch (error) {
      if (error?.code === 'ENOENT') break;
      throw error;
    }
  }
  await fs.writeFile(target, payload, { flag: 'wx' });
  const stat = await fs.stat(target);
  return {
    name: path.basename(target),
    path: target,
    mtimeMs: stat.mtimeMs,
    size: stat.size,
    via: 'same-origin-page-fetch',
    contentDisposition: response.contentDisposition,
    contentType: response.contentType,
  };
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
    if (!await isWritableDirectory(directory)) {
      throw new Error(`OPENSMS_EXPORT_DIR 不可写: ${directory}`);
    }
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

async function archiveCurrentExport() {
  const currentPath = path.join(BASE, 'export4day.xlsx');
  let currentStat;
  try {
    currentStat = await fs.stat(currentPath);
  } catch (error) {
    if (error?.code === 'ENOENT') return null;
    throw error;
  }

  const stamp = localFileStamp(currentStat.mtimeMs);
  const archiveDirectory = BASE === FALLBACK_EXPORT_DIR ? EXPORT_ARCHIVE_DIR : BASE;
  let targetPath = path.join(archiveDirectory, `export4day_${stamp}.xlsx`);
  let suffix = 1;
  while (true) {
    try {
      await fs.access(targetPath);
      targetPath = path.join(archiveDirectory, `export4day_${stamp}_${suffix}.xlsx`);
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

async function listExports(directory = BASE) {
  const names = await fs.readdir(directory);
  const rows = [];
  for (const name of names) {
    if (!/^export4day(?: \(\d+\))?\.xlsx$/.test(name)) continue;
    const p = path.join(directory, name);
    const st = await fs.stat(p);
    rows.push({ name, path: p, mtimeMs: st.mtimeMs, size: st.size });
  }
  rows.sort((a, b) => b.mtimeMs - a.mtimeMs);
  return rows;
}

async function newestAfter(beforeMax, deadlineMs = 90000, directory = BASE) {
  const start = Date.now();
  while (Date.now() - start < deadlineMs) {
    const files = await listExports(directory);
    const hit = files.find(f => f.mtimeMs > beforeMax + 500 && f.size > 100000);
    if (hit) return hit;
    await new Promise(r => setTimeout(r, 1000));
  }
  return null;
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

function normalizeDateText(value) {
  const match = String(value || '').match(/(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?/);
  if (!match) return null;
  return `${match[1]}-${match[2].padStart(2, '0')}-${match[3].padStart(2, '0')}`;
}

async function navigateWithinLoadedApp(page, href, menuText) {
  const current = new URL(page.url());
  if (current.pathname !== href) {
    const entry = page.locator(`a[href="${href}"]`).filter({ hasText: menuText }).first();
    if (!await entry.count()) {
      throw new Error(`已加载后台页缺少站内入口: ${menuText} (${href})`);
    }
    await entry.click({ timeout: 30000 });
    await page.waitForURL(url => new URL(url).pathname === href, { timeout: 45000 });
  }
  await page.waitForTimeout(1200);
}

async function readPlatformServerDate(page, { loadedApp = false } = {}) {
  if (loadedApp) {
    await navigateWithinLoadedApp(page, '/day', '按日统计');
  } else {
    await page.goto(DAY_URL, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForLoadState('networkidle', { timeout: 45000 }).catch(() => {});
    await page.waitForTimeout(1200);
  }
  const loginVisible = await page.locator('input[placeholder="账号"]').count().catch(() => 0);
  if (page.url().includes('/login') || loginVisible) {
    throw new Error('OpenSMS 登录已失效，无法读取平台服务器日期');
  }
  const candidates = await page.evaluate(() => {
    const rows = [];
    for (const element of document.querySelectorAll('body *')) {
      const rect = element.getBoundingClientRect();
      if (!rect.width || !rect.height || rect.top < -1 || rect.top > 180) continue;
      const style = getComputedStyle(element);
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
      const text = (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
      if (!text || text.length > 80 || !/20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}/.test(text)) continue;
      rows.push({ text, top: rect.top, left: rect.left, right: rect.right, width: rect.width });
    }
    return rows;
  });
  const normalized = candidates
    .map(row => ({ ...row, date: normalizeDateText(row.text) }))
    .filter(row => row.date)
    .sort((a, b) => (b.left - a.left) || (a.top - b.top) || (a.width - b.width));
  const dates = [...new Set(normalized.map(row => row.date))];
  if (dates.length !== 1) {
    throw new Error(`平台顶部服务器日期无法唯一解析: ${JSON.stringify(normalized)}`);
  }
  return { serverDate: dates[0], candidates: normalized };
}

async function openExportPage(page, { loadedApp = false } = {}) {
  if (loadedApp) {
    await navigateWithinLoadedApp(page, '/export4day', '近10日统计导出');
  } else {
    await page.goto(EXPORT_URL, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForLoadState('networkidle', { timeout: 45000 }).catch(() => {});
    await page.waitForTimeout(1500);
  }
  let bodyText = await page.locator('body').innerText({ timeout: 10000 }).catch(() => '');
  if (bodyText.includes('后台生成全部号码近10日统计')) return bodyText;

  const sidebarEntry = page.getByText('近10日统计导出', { exact: true }).first();
  if (await sidebarEntry.count()) {
    await sidebarEntry.click({ timeout: 30000 });
    await page.waitForLoadState('networkidle', { timeout: 45000 }).catch(() => {});
    await page.waitForTimeout(1500);
    bodyText = await page.locator('body').innerText({ timeout: 10000 }).catch(() => '');
  }
  return bodyText;
}

async function main() {
  await ensureArtifactDirs();
  const exportDirectorySelection = await resolveExportDirectory();
  BASE = exportDirectorySelection.directory;
  if (process.env.OPENSMS_EXPORT_DIRECTORY_CHECK_ONLY === '1') {
    console.log(JSON.stringify(exportDirectorySelection, null, 2));
    return;
  }
  const archivedBeforeDownload = await archiveCurrentExport();
  const before = await listExports();
  const beforeMax = before[0]?.mtimeMs ?? 0;
  const fallbackBefore = BASE === FALLBACK_EXPORT_DIR ? [] : await listExports(FALLBACK_EXPORT_DIR);
  const fallbackBeforeMax = fallbackBefore[0]?.mtimeMs ?? 0;
  const session = await createOpenSmsApiSession({ cdp: CDP, pathName: '/export4day' });
  const { browser, api, token, page: hostPage } = session;
  const silentFrame = await createSilentSameOriginFrame(hostPage, 'opensms-export');
  const page = silentFrame.frame;
  try {
  const loadedAppFallback = false;
  const serverDateReadback = await readPlatformServerDate(page);
  const text0Initial = await openExportPage(page, { loadedApp: false });
  let text0 = text0Initial;
  if (!text0.includes('后台生成') && text0.includes('正在加载系统资源')) {
    throw new Error('OpenSMS 静默后台页资源未加载；为避免切换用户当前可见页面，本轮停止导出');
  }
  if (text0.includes('后台生成选中的')) {
    const clearSelection = page.getByText('清空', { exact: true }).first();
    if (!await clearSelection.count()) {
      throw new Error('导出页存在号码筛选，但未找到清空按钮');
    }
    await clearSelection.evaluate(element => element.click());
    await page.waitForTimeout(500);
    text0 = await page.locator('body').innerText({ timeout: 10000 }).catch(() => '');
  }
  const existingGeneration = text0.includes('后台生成中...') && /状态：\s*生成中/.test(text0);
  const existingCompleted = text0.includes('生成完成，可以下载') && /状态：\s*已完成/.test(text0);
  if (!text0.includes('后台生成全部号码近10日统计') && !existingGeneration && !existingCompleted) {
    throw new Error(`export page not ready: ${page.url()} text=${text0.slice(0, 500)}`);
  }

  hostPage.on('dialog', async dialog => {
    await dialog.accept().catch(() => {});
  });

  // A completed task shown on entry is historical. Start a new generation so
  // every run has a fresh task ID and source workbook.
  if (!existingGeneration) {
    const generate = page.getByText('后台生成全部号码近10日统计', { exact: false }).first();
    await generate.evaluate(element => element.click());
    await page.waitForTimeout(1000);
    const confirm = page.getByText('确认生成', { exact: false }).first();
    if (await confirm.count() && await confirm.isVisible()) {
      await confirm.evaluate(element => element.click());
    }
  }

  await page.waitForTimeout(1000);
  const submittedText = await page.locator('body').innerText({ timeout: 10000 });
  const taskId = submittedText.match(/任务ID：\s*([a-f0-9]{32})/i)?.[1];
  if (!taskId) throw new Error(`未能读取本轮导出任务ID: ${submittedText.slice(-800)}`);
  const completionDeadline = Date.now() + 600000;
  let generationStatus = null;
  while (Date.now() < completionDeadline) {
    generationStatus = await api(
      'GET',
      '/adminExport/export4day/async/status',
      null,
      { taskId },
    );
    if (generationStatus?.status === 'SUCCESS') break;
    if (generationStatus?.status === 'FAILED') {
      throw new Error(`本轮导出任务失败: ${taskId} ${JSON.stringify(generationStatus)}`);
    }
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
  if (generationStatus?.status !== 'SUCCESS') {
    throw new Error(`等待本轮导出任务完成超时: ${taskId}`);
  }
  const saved = await downloadExportFromPage(page, token, taskId);
  if (BASE !== FALLBACK_EXPORT_DIR) await archiveFreshFallbackExtras(fallbackBeforeMax);

  const after = await listExports();
  const result = {
    startedAt: nowIso(),
    silentBrowserMode: SILENT_BROWSER_MODE,
    browserForegroundActivationAttempted: !SILENT_BROWSER_MODE,
    exportDirectory: BASE,
    exportDirectorySelection,
    loadedAppFallback,
    archivedBeforeDownload,
    taskId,
    taskStatus: generationStatus.status,
    phoneCount: generationStatus.phoneCount,
    elapsedMs: generationStatus.elapsedMs,
    downloadedAt: nowIso(),
    serverDate: serverDateReadback.serverDate,
    serverDateCandidates: serverDateReadback.candidates,
    url: page.url(),
    beforeLatest: before[0] || null,
    saved,
    afterLatest: after[0] || null,
    bodyText: (await page.locator('body').innerText().catch(() => '')).slice(0, 1000),
  };
  const receiptPath = path.join(SNAPSHOT_DIR, `opensms_export_once_${receiptStamp()}.json`);
  result.receiptPath = receiptPath;
  await fs.writeFile(receiptPath, `${JSON.stringify(result, null, 2)}\n`, { flag: 'wx' });
  console.log(JSON.stringify(result, null, 2));
  } finally {
    await silentFrame.cleanup();
    await browser.close().catch(() => {});
  }
}

main().catch(err => {
  console.error(err.stack || String(err));
  process.exit(1);
});
