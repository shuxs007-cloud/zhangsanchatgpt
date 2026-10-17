#!/usr/bin/env node
// Standalone investigation: exact current swap-zone population, no task execution.
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRawOpenSmsApiSession, assertOk } from '../opensms_protocol_client.mjs';
import { collectCarrierConfigPagesReadOnly } from './opensms_carriers_readonly.mjs';
import { createReadOnlyApiLimiter, mapReadOnlyBounded } from './opensms_readonly_concurrency.mjs';
const str = value => String(value ?? '').trim();
const normPhone = value => {
  const s = str(value).replace(/[\s()+-]/g, '');
  return /^1\d{10}$/.test(s) ? s.slice(1) : /^\d{10}$/.test(s) ? s : '';
};
const names = ['换号区C', '换号区T'];
const identif = row => str(row.carrireIdentif || row.carrierIdentif || row.identif);
const carrierName = row => str(row.name || row.carrierName || row.carrireName || row.carrier);
const output = process.argv[2];
if (!output || process.argv.length !== 3) throw new Error('usage: opensms_swap_population_readonly.mjs OUTPUT.json');
const portFile = '/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/a53ce74e6c9e4fa08aec35f81ce1e2a3/DevToolsActivePort';
const port = (await fs.readFile(portFile, 'utf8')).split(/\r?\n/)[0].trim();
if (!/^\d+$/.test(port)) throw new Error('430 CDP port invalid');
process.env.OPENSMS_EXACT_CDP_ONLY = '1';
// Never inherit another task's pinned page or any force gate.
delete process.env.OPENSMS_RAW_API_TARGET_ID;
const session = await createRawOpenSmsApiSession({ cdp: `http://127.0.0.1:${port}`, pathName: '/phoneConfig' });
const limiter = createReadOnlyApiLimiter(session.api, { concurrency: 4,
  allowedRequests: ['GET /carrier/config/listNotDeleted', 'POST /phone/config/list'] });
const startedAt = new Date().toISOString();
try {
  const configs = await collectCarrierConfigPagesReadOnly(limiter.api, { concurrency: 4 });
  const groups = names.map(name => {
    const matches = configs.rows.filter(row => carrierName(row) === name);
    if (matches.length !== 1 || !identif(matches[0])) throw new Error(`Exact carrier resolution failed: ${name}`);
    return { carrierName: name, carrierIdentif: identif(matches[0]), pages: [] };
  });
  async function readPage(group, pageNum, recheck = false) {
    const pageSize = 500;
    const body = { pageNum, pageSize, carrireIdentif: group.carrierIdentif };
    const response = await limiter.api('POST', '/phone/config/list', body, { pageNum, pageSize });
    assertOk('Read current swap population', response);
    const total = Number(response.total);
    if (!Number.isSafeInteger(total) || total < 0 || total > 500000 || !Array.isArray(response.rows)) throw new Error('Invalid pagination response');
    if (group.total === undefined) group.total = total;
    if (total !== group.total) throw new Error(`Population changed during read: ${group.carrierName}`);
    if (response.rows.length !== Math.min(500, Math.max(0, total - (pageNum - 1) * 500))) throw new Error('Incomplete page');
    const rows = response.rows.map(row => {
      const phone10 = normPhone(row.cardNo ?? row.phone ?? row.cardPhone ?? row.tel);
      if (!phone10 || !str(row.id) || identif(row) !== group.carrierIdentif) throw new Error('Phone/carrier identity mismatch');
      return { phone10, carrierName: group.carrierName, backendId: str(row.id), raw: row };
    });
    if (recheck) {
      if (JSON.stringify(rows.map(r => r.backendId)) !== JSON.stringify(group.pages[0].rows.map(r => r.backendId))) throw new Error('First page changed during read');
      group.finalTotalRecheck = { total, firstPageIdsUnchanged: true, checkedAt: new Date().toISOString() };
    } else group.pages[pageNum - 1] = { pageNum, reportedTotal: total, rowCount: rows.length, rows };
  }
  await mapReadOnlyBounded(groups, group => readPage(group, 1), { concurrency: 4 });
  const rest = groups.flatMap(group => Array.from({ length: Math.max(0, Math.ceil(group.total / 500) - 1) }, (_, i) => ({ group, pageNum: i + 2 })));
  await mapReadOnlyBounded(rest, ({ group, pageNum }) => readPage(group, pageNum), { concurrency: 4 });
  await mapReadOnlyBounded(groups, group => readPage(group, 1, true), { concurrency: 4 });
  const rows = groups.flatMap(g => g.pages.flatMap(p => p.rows));
  if (new Set(rows.map(r => r.backendId)).size !== rows.length || new Set(rows.map(r => r.phone10)).size !== rows.length) throw new Error('Duplicate phone/backend identity');
  const payload = { status: 'passed', readOnly: true, browserProfile: '430', browserForegroundActivationAttempted: false,
    startedAt, completedAt: new Date().toISOString(), total: rows.length,
    groups: groups.map(({ pages, ...g }) => ({ ...g, pageProofs: pages.map(({ rows, ...p }) => p) })),
    readOnlyConcurrency: limiter.stats, rows };
  await fs.mkdir(path.dirname(path.resolve(output)), { recursive: true });
  await fs.writeFile(output, JSON.stringify(payload, null, 2) + '\n', { flag: 'wx' });
  console.log(JSON.stringify({ ...payload, rows: undefined, path: path.resolve(output), availableRowFields: Object.keys(rows[0]?.raw || {}) }));
} finally {
  await limiter.drain();
  await session.browser.close();
}
