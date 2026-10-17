import fs from 'node:fs/promises';
import path from 'node:path';

import {
  createRawBackgroundPage,
} from '../opensms_protocol_client.mjs';
import { SNAPSHOT_DIR, ensureArtifactDirs } from '../opensms_artifact_paths.mjs';

function stamp() {
  return new Date().toISOString().replace(/[-:.]/g, '');
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

async function readRoute(pagePath) {
  const started = Date.now();
  const backgroundPage = await createRawBackgroundPage({
    cdp: process.env.BITBROWSER_CDP,
    url: `https://adminsms.smsoug.com${pagePath}`,
  });
  const setupMs = Date.now() - started;
  try {
    // Target.createTarget starts this navigation; do not restart the route load.
    const dateReadStartedMs = Date.now();
    const deadline = Date.now() + 60000;
    let state = null;
    let lastError = null;
    while (Date.now() < deadline) {
      try {
        state = await backgroundPage.client.evaluate(`(() => ({
          href: location.href,
          bodyText: String(document.body?.innerText || '').slice(0, 5000),
          loginVisible: !!document.querySelector('input[placeholder="账号"]'),
        }))()`, 15000);
        if (/\/login(?:$|[?#])/.test(state.href) || state.loginVisible) {
          throw new Error('OpenSMS 登录已失效，无法读取平台服务器日期');
        }
        if (hasExpectedPath(state.href, pagePath)
          && /20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}/.test(state.bodyText)) break;
      } catch (error) {
        lastError = error;
        if (/登录已失效/.test(error.message)) throw error;
      }
      await sleep(250);
    }
    if (!state || !hasExpectedPath(state.href, pagePath)
      || !/20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}/.test(state.bodyText)) {
      throw new Error(`读取平台服务器日期超时: ${lastError?.message || JSON.stringify(state)}`);
    }
    const candidates = await backgroundPage.client.evaluate(`(() => {
      const rows = [];
      for (const element of document.querySelectorAll('body *')) {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        const text = String(element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim();
        if (!rect.width || !rect.height || rect.top < -1 || rect.top > 180) continue;
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
        if (!text || text.length > 80 || !/20\\d{2}[-/.年]\\d{1,2}[-/.月]\\d{1,2}/.test(text)) continue;
        rows.push({ text, top: rect.top, left: rect.left, width: rect.width });
      }
      return rows;
    })()`);
    const normalize = value => {
      const match = String(value || '')
        .replace(/[年/.]/g, '-')
        .replace(/月/g, '-')
        .replace(/日/g, '')
        .match(/(20\d{2})-(\d{1,2})-(\d{1,2})/);
      return match ? `${match[1]}-${match[2].padStart(2, '0')}-${match[3].padStart(2, '0')}` : '';
    };
    const normalized = candidates
      .map(row => ({ ...row, date: normalize(row.text) }))
      .filter(row => row.date)
      .sort((left, right) => (right.left - left.left) || (left.top - right.top));
    const dates = [...new Set(normalized.map(row => row.date))];
    if (dates.length !== 1) throw new Error(`平台顶部服务器日期无法唯一解析: ${JSON.stringify(normalized)}`);
    const receipt = {
      checkedAt: new Date().toISOString(),
      mode: 'exact-430-raw-cdp-background-target-read-only',
      silentBrowserMode: true,
      browserForegroundActivationAttempted: false,
      playwrightBrowserConnectionUsed: false,
      requestedPath: pagePath,
      page: state.href,
      targetId: backgroundPage.targetId,
      navigationReceipt: backgroundPage.navigationReceipt ?? null,
      cdpEndpoint: backgroundPage.endpoint,
      serverDate: dates[0],
      candidates: normalized,
      durationMs: Date.now() - started,
      timingsMs: { setupMs, dateReadMs: Date.now() - dateReadStartedMs },
    };
    const receiptPath = path.join(
      SNAPSHOT_DIR,
      `opensms_server_date_${pagePath.slice(1)}_${stamp()}.json`,
    );
    await fs.writeFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, { flag: 'wx' });
    return { path: receiptPath, ...receipt };
  } finally {
    await backgroundPage.close().catch(() => {});
  }
}

async function main() {
  const started = Date.now();
  await ensureArtifactDirs();
  const [day, phoneConfig] = await Promise.all([
    readRoute('/day'),
    readRoute('/phoneConfig'),
  ]);
  console.log(JSON.stringify({
    success: true,
    mode: 'combined-parallel-exact-430-raw-cdp-background-targets',
    silentBrowserMode: true,
    browserForegroundActivationAttempted: false,
    playwrightBrowserConnectionUsed: false,
    elapsedMs: Date.now() - started,
    day,
    phoneConfig,
  }, null, 2));
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
