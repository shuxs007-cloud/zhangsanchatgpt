#!/usr/bin/env node
// Explicit read-only diagnosis of an exact failed run; never supplies business candidates.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { createRawOpenSmsApiSession, shortCardNo } from '../opensms_protocol_client.mjs';
import { validateExact430Endpoint } from './opensms_diagnose_task_d_mapping.mjs';
import { collectFilteredRetention } from './opensms_task_d_filtered_retention.mjs';
import { createReadOnlyApiLimiter } from './opensms_readonly_concurrency.mjs';

const [source, runId, output] = process.argv.slice(2);
if (!source || !runId || !output || process.argv.length !== 5) throw new Error('usage: source-retention-report.json runId new-output.json');
const bytes = await fs.readFile(source);
const receipt = JSON.parse(bytes);
if (receipt.runId !== runId || receipt.status !== 'passed' || receipt.independentSourceReadbackPassed !== true
    || !Array.isArray(receipt.retention?.cardNos)) throw new Error('诊断来源未经本轮独立核验');
process.env.OPENSMS_EXACT_CDP_ONLY = '1';
delete process.env.OPENSMS_RAW_API_TARGET_ID;
const portFile = '/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/a53ce74e6c9e4fa08aec35f81ce1e2a3/DevToolsActivePort';
const endpoint = validateExact430Endpoint(await fs.readFile(portFile, 'utf8'));
await fs.mkdir(path.dirname(path.resolve(output)), { recursive: true });
const handle = await fs.open(output, 'wx');
const report = {runId, source: path.resolve(source), sourceSha256: crypto.createHash('sha256').update(bytes).digest('hex'),
  readOnly: true, businessWrites: 0, diagnosticOnly: true, sourceCards: receipt.retention.cardNos,
  startedAt: new Date().toISOString(), status: 'running'};
let session, limiter;
try {
  session = await createRawOpenSmsApiSession({cdp: endpoint, pathName: '/phoneConfig'});
  limiter = createReadOnlyApiLimiter(session.api, {allowedRequests: ['POST /phone/config/list']});
  await collectFilteredRetention(limiter.api, report.sourceCards, {onProof: proof => { report.proof = proof; }});
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = error.message;
  process.exitCode = 1;
} finally {
  await limiter?.drain();
  session?.client.close();
  report.finishedAt = new Date().toISOString();
  report.identityIssues = [];
  for (const query of report.proof?.queries || []) {
    const requested = new Set(query.cardNos);
    for (const page of query.pages) for (const row of page.rows) {
      const number = shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
      const id = typeof row.id === 'string' ? row.id.trim() : Number.isSafeInteger(row.id) ? String(row.id) : '';
      const carrier = row.carrireIdentif || row.carrierIdentif || row.identif;
      if (!requested.has(number) || !id || !carrier) report.identityIssues.push({
        filter: query.filter, page: page.pageNum, phone: number, row,
        outsideRequested: !requested.has(number), missingId: !id, missingCarrier: !carrier,
      });
    }
  }
  report.summary = {queryCount: report.proof?.queries.length ?? null, identityIssueRows: report.identityIssues.length,
    uniqueIssuePhones: new Set(report.identityIssues.map(row => row.phone)).size,
    outsideRequested: report.identityIssues.filter(row => row.outsideRequested).length,
    missingId: report.identityIssues.filter(row => row.missingId).length,
    missingCarrier: report.identityIssues.filter(row => row.missingCarrier).length,
    states: [...new Set(report.identityIssues.map(item => item.row.status))]};
  try { await handle.writeFile(JSON.stringify(report, null, 2)); await handle.sync(); }
  finally { await handle.close(); }
}
console.log(JSON.stringify({status: report.status, error: report.error, ...report.summary, output: path.resolve(output)}));
