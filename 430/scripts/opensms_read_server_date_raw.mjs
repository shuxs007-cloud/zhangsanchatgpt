import fs from 'node:fs/promises';
import path from 'node:path';

import { connectRawOpenSmsPage } from '../opensms_protocol_client.mjs';
import { SNAPSHOT_DIR, ensureArtifactDirs } from '../opensms_artifact_paths.mjs';

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
  const requested = String(process.argv[2] || '/day').trim();
  const pagePath = requested.startsWith('/') ? requested : `/${requested}`;
  if (!['/day', '/phoneConfig'].includes(pagePath)) throw new Error(`不支持的服务器日期页面: ${pagePath}`);

  const raw = await connectRawOpenSmsPage({ cdp: process.env.BITBROWSER_CDP, pathName: pagePath });
  try {
    const state = await raw.client.evaluate(`(() => ({
      href: location.href,
      body: String(document.body?.innerText || '').slice(0, 500),
      candidates: Array.from(document.querySelectorAll('body *')).flatMap(element => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        const text = String(element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim();
        if (!rect.width || !rect.height || rect.top < -1 || rect.top > 180) return [];
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return [];
        if (!text || text.length > 80 || !/20\\d{2}[-/.\u5e74]\\d{1,2}[-/.\u6708]\\d{1,2}/.test(text)) return [];
        return [{ text, top: rect.top, left: rect.left, width: rect.width }];
      }),
    }))()`);
    if (/\/login(?:$|[?#])/.test(state.href)) throw new Error('OpenSMS登录已失效');
    const normalized = state.candidates
      .map(row => ({ ...row, date: normalizeDateText(row.text) }))
      .filter(row => row.date)
      .sort((left, right) => (right.left - left.left) || (left.top - right.top));
    const dates = [...new Set(normalized.map(row => row.date))];
    if (dates.length !== 1) throw new Error(`平台顶部服务器日期无法唯一解析: ${JSON.stringify(normalized)}`);
    const receipt = {
      checkedAt: new Date().toISOString(),
      mode: 'current-430-session-raw-target-read-only',
      requestedPath: pagePath,
      page: state.href,
      targetId: raw.target.id,
      cdpEndpoint: raw.endpoint,
      serverDate: dates[0],
      candidates: normalized,
    };
    const receiptPath = path.join(SNAPSHOT_DIR, `opensms_server_date_${pagePath.slice(1)}_${stamp()}.json`);
    await fs.writeFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, { flag: 'wx' });
    console.log(JSON.stringify({ path: receiptPath, ...receipt }, null, 2));
  } finally {
    raw.client.close();
  }
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
