import fs from 'node:fs/promises';
import path from 'node:path';

import {
  createOpenSmsApiSession,
  createSilentSameOriginFrame,
} from '../opensms_protocol_client.mjs';
import { SNAPSHOT_DIR, ensureArtifactDirs } from '../opensms_artifact_paths.mjs';

const SILENT_BROWSER_MODE = true;

function normalizeDateText(value) {
  const match = String(value || '')
    .replace(/[\u5e74/.]/g, '-')
    .replace(/\u6708/g, '-')
    .replace(/\u65e5/g, '')
    .match(/(20\d{2})-(\d{1,2})-(\d{1,2})/);
  if (!match) return '';
  return `${match[1]}-${match[2].padStart(2, '0')}-${match[3].padStart(2, '0')}`;
}

function stamp() {
  return new Date().toISOString().replace(/[-:.]/g, '');
}

async function main() {
  await ensureArtifactDirs();
  const requestedPath = String(process.argv[2] || '/day').trim();
  const pagePath = requestedPath.startsWith('/') ? requestedPath : `/${requestedPath}`;
  if (!['/day', '/phoneConfig'].includes(pagePath)) {
    throw new Error(`不支持的服务器日期读取页面: ${pagePath}`);
  }
  const session = await createOpenSmsApiSession({ pathName: pagePath });
  const silentFrame = await createSilentSameOriginFrame(session.page, `opensms-date-${pagePath.slice(1)}`);
  const page = silentFrame.frame;
  try {
    const { adminBase } = session;
    await page.goto(`${adminBase}${pagePath}`, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(1200);

    const bodyText = await page.locator('body').innerText().catch(() => '');
    if (bodyText.includes('正在加载系统资源')) {
      throw new Error('OpenSMS 静默后台页资源未加载；为避免切换用户当前可见页面，本轮停止读取服务器日期');
    }

    const loginVisible = await page.locator('input[placeholder="\u8d26\u53f7"]').count().catch(() => 0);
    if (page.url().includes('/login') || loginVisible) {
      throw new Error('OpenSMS \u767b\u5f55\u5df2\u5931\u6548\uff0c\u65e0\u6cd5\u8bfb\u53d6\u5e73\u53f0\u670d\u52a1\u5668\u65e5\u671f');
    }

    const candidates = await page.evaluate(() => {
      const rows = [];
      for (const element of document.querySelectorAll('body *')) {
        const rect = element.getBoundingClientRect();
        if (!rect.width || !rect.height || rect.top < -1 || rect.top > 180) continue;
        const style = getComputedStyle(element);
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
        const text = (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
        if (!text || text.length > 80 || !/20\d{2}[-/.\u5e74]\d{1,2}[-/.\u6708]\d{1,2}/.test(text)) continue;
        rows.push({ text, top: rect.top, left: rect.left, width: rect.width });
      }
      return rows;
    });
    const normalized = candidates
      .map(row => ({ ...row, date: normalizeDateText(row.text) }))
      .filter(row => row.date)
      .sort((left, right) => (right.left - left.left) || (left.top - right.top) || (left.width - right.width));
    const dates = [...new Set(normalized.map(row => row.date))];
    if (dates.length !== 1) {
      throw new Error(`\u5e73\u53f0\u9876\u90e8\u670d\u52a1\u5668\u65e5\u671f\u65e0\u6cd5\u552f\u4e00\u89e3\u6790: ${JSON.stringify(normalized)}`);
    }

    const receipt = {
      checkedAt: new Date().toISOString(),
      mode: 'current-430-session-read-only',
      silentBrowserMode: SILENT_BROWSER_MODE,
      browserForegroundActivationAttempted: false,
      requestedPath: pagePath,
      page: page.url(),
      serverDate: dates[0],
      loadedAppFallback: false,
      candidates: normalized,
    };
    const pageLabel = pagePath.slice(1);
    const receiptPath = path.join(SNAPSHOT_DIR, `opensms_server_date_${pageLabel}_${stamp()}.json`);
    await fs.writeFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`);
    console.log(JSON.stringify({ path: receiptPath, ...receipt }, null, 2));
  } finally {
    await silentFrame.cleanup();
    await session.browser.close().catch(() => {});
  }
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
