#!/usr/bin/env node

// Full read-only Grab&Go Customer SIM Cards export from the exact BitBrowser
// 430 session. The site's unfiltered WebForms buffer becomes impractically
// large, so this implementation partitions the SIM-number search by every
// 88x/89x prefix, then reconciles overlapping substring matches by the maximum
// multiplicity of each complete row signature.

import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = (() => {
  try { return require('playwright'); }
  catch { return require('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'); }
})();

const PROFILE_ID = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
const PROFILE_DIR = path.join('/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache', PROFILE_ID);
const PORT_FILE = path.join(PROFILE_DIR, 'DevToolsActivePort');
const TARGET_URL = 'https://www.grabandgoplus.com/Retailer_CustomerSIMCards';
const SHOW_ALL_SELECTOR = '#GagPRetailerHolder_btnShowAllSimCards';
const SHOW_ALL_FIELD = 'ctl00$GagPRetailerHolder$btnShowAllSimCards';
const FILTER_SELECTOR = '#GagPRetailerHolder_btnFilter';
const FILTER_FIELD = 'ctl00$GagPRetailerHolder$btnFilter';
const SEARCH_SELECTOR = '#GagPRetailerHolder_txtSearchCriteria';
const REQUIRED_FIELDS = [
  'lblPurchaseDate',
  'lblActivationRequestDate',
  'lblActivateDate',
  'lblSimCardNumber',
  'lblSKU',
  'lblCost',
  'lnSimDetails',
];
const SEARCH_TERMS = [
  ...Array.from({ length: 10 }, (_, digit) => `89${digit}`),
  ...Array.from({ length: 10 }, (_, digit) => `88${digit}`),
];

function parseArgs(argv) {
  const args = { outputJson: '', progressJson: '', resume: false, timeoutMs: 300000 };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--output-json') args.outputJson = argv[++index] || '';
    else if (arg === '--progress-json') args.progressJson = argv[++index] || '';
    else if (arg === '--resume') args.resume = true;
    else if (arg === '--timeout-ms') args.timeoutMs = Number(argv[++index] || 0);
    else if (arg === '--self-test') args.selfTest = true;
    else throw new Error(`未知参数: ${arg}`);
  }
  if (!args.selfTest && !args.outputJson) throw new Error('必须指定 --output-json FILE');
  if (!Number.isFinite(args.timeoutMs) || args.timeoutMs < 30000) throw new Error('--timeout-ms 必须至少为30000');
  return args;
}

async function atomicWriteJson(filePath, payload) {
  if (!filePath) return;
  const resolved = path.resolve(filePath);
  const temporary = `${resolved}.${process.pid}.tmp`;
  await fs.mkdir(path.dirname(resolved), { recursive: true });
  await fs.writeFile(temporary, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  await fs.rename(temporary, resolved);
}

async function connect430() {
  const raw = await fs.readFile(PORT_FILE, 'utf8');
  const port = raw.split(/\r?\n/)[0]?.trim();
  if (!/^\d+$/.test(port || '')) throw new Error(`BitBrowser 430端口文件无效: ${PORT_FILE}`);
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`, { timeout: 120000 });
  const context = browser.contexts()[0];
  if (!context) throw new Error('BitBrowser 430没有可用上下文');
  return { browser, context, port };
}

async function createBackgroundPage(browser, context) {
  const before = new Set(context.pages());
  const pageEvent = context.waitForEvent('page', { timeout: 60000 }).catch(() => null);
  const cdp = await browser.newBrowserCDPSession();
  const created = await cdp.send('Target.createTarget', { url: TARGET_URL, background: true, newWindow: false });
  const poll = (async () => {
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      const page = context.pages().find(candidate => !before.has(candidate) && /Retailer_CustomerSIMCards/i.test(candidate.url()));
      if (page) return page;
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    return null;
  })();
  const page = await Promise.race([pageEvent, poll]);
  if (!page) throw new Error(`后台目标已创建但未关联Page: targetId=${created.targetId}`);
  await page.waitForLoadState('domcontentloaded', { timeout: 120000 }).catch(() => {});
  return page;
}

async function loginState(page) {
  return page.evaluate(() => {
    const showAll = document.querySelector('#GagPRetailerHolder_btnShowAllSimCards');
    const body = document.body?.innerText || '';
    return {
      url: location.href,
      title: document.title,
      showAllPresent: Boolean(showAll),
      showAllText: String(showAll?.value || showAll?.innerText || '').trim(),
      hasPasswordInput: Boolean(document.querySelector('input[type="password"]')),
      hasCustomerSimMarker: /Customer\s+SIM\s+Cards/i.test(body),
      greetingPresent: /\bHello\b/i.test(body),
    };
  });
}

function responseMatchesField(response, field) {
  if (!/grabandgoplus\.com\/Retailer_CustomerSIMCards/i.test(response.url())) return false;
  if (!['xhr', 'document'].includes(response.request().resourceType())) return false;
  const postData = String(response.request().postData() || '');
  return postData.includes(field) || postData.includes(encodeURIComponent(field));
}

async function counters(page) {
  return page.evaluate(() => ({
    total: Number.parseInt(document.getElementById('hfTotal')?.value || '0', 10) || 0,
    rendered: document.querySelectorAll('.scrollableDataList .sim-row').length,
    searchValue: document.querySelector('#GagPRetailerHolder_txtSearchCriteria')?.value || '',
    visible: typeof window.__simVisible === 'number' ? window.__simVisible : 0,
  }));
}

async function clickShowAll(page, timeoutMs) {
  const responsePromise = page.waitForResponse(
    response => responseMatchesField(response, SHOW_ALL_FIELD),
    { timeout: timeoutMs },
  );
  await page.locator(SHOW_ALL_SELECTOR).click({ noWaitAfter: true, timeout: 30000 });
  const response = await responsePromise;
  await response.finished().catch(() => {});
  await page.waitForFunction(
    () => Number.parseInt(document.getElementById('hfTotal')?.value || '0', 10) > 0,
    null,
    { timeout: 60000 },
  );
  return { status: response.status(), method: response.request().method(), resourceType: response.request().resourceType() };
}

async function queryTerm(page, term, timeoutMs) {
  // Assign directly so the textbox's legacy onchange postback cannot race the
  // explicit Go-button request.
  await page.evaluate(({ selector, value }) => {
    const input = document.querySelector(selector);
    if (!input) throw new Error(`搜索框不存在: ${selector}`);
    input.value = value;
  }, { selector: SEARCH_SELECTOR, value: term });
  const responsePromise = page.waitForResponse(
    response => responseMatchesField(response, FILTER_FIELD),
    { timeout: timeoutMs },
  );
  await page.locator(FILTER_SELECTOR).click({ noWaitAfter: true, timeout: 30000 });
  const response = await responsePromise;
  await response.finished().catch(() => {});
  await page.waitForFunction(
    expected => {
      const search = document.querySelector('#GagPRetailerHolder_txtSearchCriteria')?.value || '';
      const total = Number.parseInt(document.getElementById('hfTotal')?.value || '0', 10) || 0;
      const rendered = document.querySelectorAll('.scrollableDataList .sim-row').length;
      return search === expected && rendered === total;
    },
    term,
    { timeout: timeoutMs },
  );
  return { response, counters: await counters(page) };
}

async function extractRows(page) {
  return page.evaluate(() => {
    function cleanText(element) {
      if (!element) return '';
      if ('value' in element && ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName)) {
        return String(element.value || '').trim();
      }
      const clone = element.cloneNode(true);
      for (const translated of clone.querySelectorAll('[data-immersive-translate-translation-element-mark], .immersive-translate-target-wrapper')) translated.remove();
      return String(clone.textContent || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
    }
    const rows = [];
    const fieldNames = new Set();
    for (const [ordinal, rowNode] of [...document.querySelectorAll('.scrollableDataList .sim-row')].entries()) {
      const fields = {};
      let controlIndex = '';
      for (const element of rowNode.querySelectorAll('[id^="GagPRetailerHolder_dlCustomerSimCards_"]')) {
        const match = element.id.match(/^GagPRetailerHolder_dlCustomerSimCards_(.+)_(\d+)$/);
        if (!match) continue;
        const [, fieldName, index] = match;
        controlIndex ||= index;
        fieldNames.add(fieldName);
        fields[fieldName] = cleanText(element);
      }
      rows.push({ ordinal: ordinal + 1, controlIndex, fields });
    }
    return { rows, fieldNames: [...fieldNames] };
  });
}

function signature(fields, fieldNames) {
  return fieldNames.map(field => String(fields?.[field] || '')).join('\u001f');
}

function buildMultiplicity(rows, fieldNames) {
  const bySignature = new Map();
  for (const row of rows) {
    const key = signature(row.fields, fieldNames);
    const entry = bySignature.get(key) || { row, count: 0 };
    entry.count += 1;
    bySignature.set(key, entry);
  }
  return bySignature;
}

function mergeMaxMultiplicity(target, rows, fieldNames) {
  const subset = buildMultiplicity(rows, fieldNames);
  for (const [key, entry] of subset) {
    const previous = target.get(key);
    if (!previous || entry.count > previous.count) target.set(key, entry);
  }
}

function expandMultiplicity(map) {
  const rows = [];
  for (const { row, count } of map.values()) {
    for (let index = 0; index < count; index += 1) rows.push({ ...row, fields: { ...row.fields } });
  }
  rows.sort((left, right) => {
    const date = String(right.fields.lblPurchaseDate || '').localeCompare(String(left.fields.lblPurchaseDate || ''));
    if (date) return date;
    return String(right.fields.lblSimCardNumber || '').localeCompare(String(left.fields.lblSimCardNumber || ''));
  });
  rows.forEach((row, index) => { row.ordinal = index + 1; row.controlIndex = String(index); });
  return rows;
}

function summarizeCompleteness(rows, fieldNames) {
  const nonEmptyByField = {};
  const blankByField = {};
  for (const field of fieldNames) {
    const nonEmpty = rows.reduce((count, row) => count + (String(row.fields[field] || '').trim() ? 1 : 0), 0);
    nonEmptyByField[field] = nonEmpty;
    blankByField[field] = rows.length - nonEmpty;
  }
  const simValues = rows.map(row => String(row.fields.lblSimCardNumber || '').trim()).filter(Boolean);
  return {
    nonEmptyByField,
    blankByField,
    simNumberNonEmptyCount: simValues.length,
    simNumberUniqueCount: new Set(simValues).size,
    duplicateSimNumberCount: simValues.length - new Set(simValues).size,
  };
}

async function writeCheckpoint(args, base, completedTerms, termStats, multiplicity, fieldNames) {
  const rows = expandMultiplicity(multiplicity);
  const payload = {
    ...base,
    status: 'running',
    updatedAt: new Date().toISOString(),
    completedTerms,
    termStats,
    fieldNames,
    extractedRowCount: rows.length,
    rows,
  };
  await atomicWriteJson(args.outputJson, payload);
  await atomicWriteJson(args.progressJson, {
    status: 'running',
    event: 'partition_completed',
    updatedAt: payload.updatedAt,
    total: base.total,
    completedTermCount: completedTerms.length,
    completedTerms,
    extractedRowCount: rows.length,
    lastTerm: completedTerms.at(-1) || '',
  });
}

async function selfTest() {
  const fields = { lblPurchaseDate: '2026-01-01', lblSimCardNumber: '89123' };
  const names = ['lblPurchaseDate', 'lblSimCardNumber'];
  const map = new Map();
  mergeMaxMultiplicity(map, [{ fields }, { fields }], names);
  mergeMaxMultiplicity(map, [{ fields }], names);
  const passed = expandMultiplicity(map).length === 2 && SEARCH_TERMS.length === 20;
  console.log(JSON.stringify({ status: passed ? 'passed' : 'failed', liveBrowserUsed: false }));
  if (!passed) process.exitCode = 2;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.selfTest) return selfTest();
  const startedAt = new Date().toISOString();
  const { browser, context, port } = await connect430();
  let page;
  try {
    page = await createBackgroundPage(browser, context);
    const login = await loginState(page);
    if (!(login.showAllPresent && login.showAllText === 'Show All' && login.hasCustomerSimMarker && !login.hasPasswordInput)) {
      const payload = {
        status: 'login_required', startedAt, finishedAt: new Date().toISOString(), browser: 'BitBrowser 430',
        profileId: PROFILE_ID, port, loginState: login, sensitiveDataSubmittedCount: 0,
        instruction: '请在BitBrowser 430登录Grab&Go，确认Customer SIM Cards页面显示Show All后重试',
      };
      await atomicWriteJson(args.outputJson, payload);
      console.log(JSON.stringify({ ...payload, outputJson: path.resolve(args.outputJson) }));
      process.exitCode = 4;
      return;
    }

    const showAllResponse = await clickShowAll(page, args.timeoutMs);
    const showAllCounters = await counters(page);
    if (!(showAllCounters.total > 0 && showAllCounters.rendered > 0 && showAllCounters.rendered <= showAllCounters.total)) {
      throw new Error(`Show All计数无效: ${JSON.stringify(showAllCounters)}`);
    }

    const base = {
      startedAt,
      sourceUrl: TARGET_URL,
      browser: 'BitBrowser 430',
      profileId: PROFILE_ID,
      port,
      silentBrowserMode: true,
      browserForegroundActivationAttempted: false,
      pageCreatedInBackground: true,
      loginProof: {
        showAllPresent: login.showAllPresent,
        showAllText: login.showAllText,
        hasCustomerSimMarker: login.hasCustomerSimMarker,
        greetingPresent: login.greetingPresent,
      },
      interface: {
        type: 'ASP.NET WebForms POST (Show All total + SIM-number prefix partitions)',
        endpoint: TARGET_URL,
        method: 'POST',
        showAllField: SHOW_ALL_FIELD,
        filterField: FILTER_FIELD,
        searchTerms: SEARCH_TERMS,
        reconciliation: 'maximum multiplicity per complete row signature',
        showAllResponse,
      },
      total: showAllCounters.total,
    };

    let completedTerms = [];
    let termStats = [];
    let fieldNames = [...REQUIRED_FIELDS];
    const multiplicity = new Map();
    if (args.resume) {
      try {
        const checkpoint = JSON.parse(await fs.readFile(path.resolve(args.outputJson), 'utf8'));
        if (checkpoint.total === base.total && Array.isArray(checkpoint.completedTerms) && Array.isArray(checkpoint.rows)) {
          completedTerms = checkpoint.completedTerms.filter(term => SEARCH_TERMS.includes(term));
          termStats = Array.isArray(checkpoint.termStats) ? checkpoint.termStats.filter(stat => completedTerms.includes(stat.term)) : [];
          fieldNames = Array.isArray(checkpoint.fieldNames) && checkpoint.fieldNames.length ? checkpoint.fieldNames : fieldNames;
          mergeMaxMultiplicity(multiplicity, checkpoint.rows, fieldNames);
        }
      } catch {
        // No compatible checkpoint: start a fresh partition reconciliation.
      }
    }

    for (const term of SEARCH_TERMS) {
      if (completedTerms.includes(term)) continue;
      const queried = await queryTerm(page, term, args.timeoutMs);
      const extracted = await extractRows(page);
      const discovered = [
        ...REQUIRED_FIELDS.filter(field => extracted.fieldNames.includes(field)),
        ...extracted.fieldNames.filter(field => !REQUIRED_FIELDS.includes(field)).sort(),
      ];
      const missing = REQUIRED_FIELDS.filter(field => !discovered.includes(field));
      if (missing.length) throw new Error(`分区${term}缺少必需字段: ${missing.join(',')}`);
      if (extracted.rows.length !== queried.counters.total || queried.counters.rendered !== queried.counters.total) {
        throw new Error(`分区${term}行数不一致: rows=${extracted.rows.length}, ${JSON.stringify(queried.counters)}`);
      }
      const mismatched = extracted.rows.filter(row => !String(row.fields.lblSimCardNumber || '').includes(term)).length;
      if (mismatched) throw new Error(`分区${term}包含${mismatched}条不匹配SIM记录`);
      fieldNames = discovered;
      mergeMaxMultiplicity(multiplicity, extracted.rows, fieldNames);
      completedTerms.push(term);
      termStats.push({ term, status: queried.response.status(), total: queried.counters.total, rendered: queried.counters.rendered, mismatched });
      await writeCheckpoint(args, base, completedTerms, termStats, multiplicity, fieldNames);
      console.log(JSON.stringify({
        status: 'running', event: 'partition_completed', term,
        completedTermCount: completedTerms.length, partitionRows: extracted.rows.length,
        reconciledRows: expandMultiplicity(multiplicity).length, total: base.total,
      }));
    }

    const rows = expandMultiplicity(multiplicity);
    const missingRequiredFields = REQUIRED_FIELDS.filter(field => !fieldNames.includes(field));
    const rowCountMatchesTotal = rows.length === base.total;
    const completeness = summarizeCompleteness(rows, fieldNames);
    const passed = completedTerms.length === SEARCH_TERMS.length && rowCountMatchesTotal && missingRequiredFields.length === 0;
    const payload = {
      ...base,
      status: passed ? 'passed' : 'failed_validation',
      finishedAt: new Date().toISOString(),
      completedTerms,
      termStats,
      fieldNames,
      extractedRowCount: rows.length,
      rowCountMatchesTotal,
      missingRequiredFields,
      completeness,
      rows,
    };
    await atomicWriteJson(args.outputJson, payload);
    await atomicWriteJson(args.progressJson, {
      status: payload.status,
      event: 'completed',
      updatedAt: new Date().toISOString(),
      total: payload.total,
      completedTermCount: completedTerms.length,
      extractedRowCount: payload.extractedRowCount,
      rowCountMatchesTotal,
      missingRequiredFields,
      completeness,
    });
    console.log(JSON.stringify({
      status: payload.status,
      total: payload.total,
      extractedRowCount: payload.extractedRowCount,
      rowCountMatchesTotal,
      missingRequiredFields,
      completeness,
      outputJson: path.resolve(args.outputJson),
    }));
    if (!passed) process.exitCode = 2;
  } finally {
    if (page && !page.isClosed()) await page.close({ runBeforeUnload: false }).catch(() => {});
    await browser.close().catch(() => {});
  }
}

async function exitAfterFlush(code) {
  await Promise.all([
    new Promise(resolve => process.stdout.write('', resolve)),
    new Promise(resolve => process.stderr.write('', resolve)),
  ]);
  process.exit(code);
}

main().then(
  () => exitAfterFlush(Number(process.exitCode || 0)),
  async error => {
    console.error(error?.stack || String(error));
    await exitAfterFlush(1);
  },
);
