#!/usr/bin/env node
// Read-only comparison for coverage failures. Never patches an inventory.
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRawOpenSmsApiSession, shortCardNo } from '../opensms_protocol_client.mjs';

const [coveragePath, output] = process.argv.slice(2);
const coverage = JSON.parse(await fs.readFile(coveragePath, 'utf8'));
const phones = [...new Set((coverage.missing || []).map(row => row.phoneKey))];
if (!phones.length || phones.length > 500 || phones.some(p => !/^\d{10}$/.test(p))) {
  throw new Error('Require 1–500 exact coverage-missing phones');
}
const handle = await fs.open(output, 'wx');
const port = (await fs.readFile('/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/a53ce74e6c9e4fa08aec35f81ce1e2a3/DevToolsActivePort', 'utf8')).split(/\r?\n/)[0];
if (!/^\d+$/.test(port)) throw new Error('Invalid exact 430 CDP port');
delete process.env.OPENSMS_RAW_API_TARGET_ID;
process.env.OPENSMS_EXACT_CDP_ONLY = '1';
const session = await createRawOpenSmsApiSession({ cdp: `http://127.0.0.1:${port}` });
try {
  const comparisons = [];
  for (const showAble of [1, null, 0]) {
    const body = { cardNo: phones.join('\n'), opr: 'koo', showAble, pageNum: 1, pageSize: 500 };
    const result = await session.api('POST', '/phone/config/list', body, { pageNum: 1, pageSize: 500 });
    if (result.code !== 200 || !Array.isArray(result.rows) || Number(result.total) !== result.rows.length
        || result.rows.some(r => !phones.includes(shortCardNo(r.cardNo)))
        || new Set(result.rows.map(r => String(r.id))).size !== result.rows.length) {
      throw new Error('Read-only comparison response failed completeness/identity validation');
    }
    comparisons.push({ showAble, total: Number(result.total), rows: result.rows });
  }
  const receipt = { status: 'passed', readOnly: true, checkedAt: new Date().toISOString(),
    coverage: path.resolve(coveragePath), requested: phones.length,
    silentBrowserMode: true, browserForegroundActivationAttempted: false, comparisons };
  await handle.writeFile(JSON.stringify(receipt, null, 2) + '\n');
  await handle.sync();
  console.log(JSON.stringify({ ...receipt, comparisons: comparisons.map(({ showAble, total }) => ({ showAble, total })), output: path.resolve(output) }));
} finally {
  await handle.close();
  await session.browser.close();
}
