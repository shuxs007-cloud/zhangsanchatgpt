#!/usr/bin/env node
// Exact read-only diagnosis of report-selected phones; no business candidates/writes.
import fs from 'node:fs/promises';
import { createRawOpenSmsApiSession, listPhonesByNumbers } from '../opensms_protocol_client.mjs';
import { collectCarrierConfigPagesReadOnly } from './opensms_carriers_readonly.mjs';
import { createReadOnlyApiLimiter, mapReadOnlyBounded } from './opensms_readonly_concurrency.mjs';
const [inputPath, outputPath] = process.argv.slice(2);
const input = JSON.parse(await fs.readFile(inputPath, 'utf8'));
const numbers = [...new Set(input.rows.map(r => r.phone10))];
if (numbers.some(n => !/^\d{10}$/.test(n))) throw new Error('Invalid diagnostic phone');
const port = (await fs.readFile('/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/a53ce74e6c9e4fa08aec35f81ce1e2a3/DevToolsActivePort', 'utf8')).split(/\r?\n/)[0];
if (!/^\d+$/.test(port)) throw new Error('Invalid 430 port');
process.env.OPENSMS_EXACT_CDP_ONLY = '1';
delete process.env.OPENSMS_RAW_API_TARGET_ID;
const session = await createRawOpenSmsApiSession({ cdp: `http://127.0.0.1:${port}` });
const limiter = createReadOnlyApiLimiter(session.api, { concurrency: 4, allowedRequests: ['GET /carrier/config/listNotDeleted', 'POST /phone/config/list'] });
try {
  const config = await collectCarrierConfigPagesReadOnly(limiter.api);
  const carriers = new Map(config.rows.map(r => [String(r.carrireIdentif || r.carrierIdentif || r.identif), r.name || r.carrierName]));
  const batches = Array.from({ length: Math.ceil(numbers.length / 500) }, (_, i) => numbers.slice(i * 500, i * 500 + 500));
  const responses = await mapReadOnlyBounded(batches, async ns => {
    const response = await listPhonesByNumbers(limiter.api, ns, 500);
    if (response.total !== response.rows.length) throw new Error('Diagnostic page incomplete or duplicate identities');
    return response.rows.map(raw => {
      const card = String(raw.cardNo);
      const phone10 = /^1\d{10}$/.test(card) ? card.slice(1) : card;
      const carrierName = carriers.get(String(raw.carrireIdentif));
      if (!ns.includes(phone10) || !raw.id || !carrierName) throw new Error('Diagnostic identity/carrier unresolved');
      return { phone10, carrierName, raw };
    });
  }, { concurrency: 4 });
  const rows = responses.flat();
  if (new Set(rows.map(r => r.phone10)).size !== rows.length) throw new Error('Duplicate phone');
  const found = new Set(rows.map(r => r.phone10));
  const result = { status: 'passed', readOnly: true, checkedAt: new Date().toISOString(),
    requested: numbers.length, matched: rows.length, missing: numbers.filter(n => !found.has(n)),
    silentBrowserMode: true, browserForegroundActivationAttempted: false, rows };
  await fs.writeFile(outputPath, JSON.stringify(result, null, 2), { flag: 'wx' });
  console.log(JSON.stringify({ ...result, rows: undefined, outputPath }));
} finally {
  await limiter.drain();
  await session.browser.close();
}
