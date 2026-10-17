#!/usr/bin/env node

// Read-only full Customer SIM Cards export from the exact BitBrowser 430
// profile. The site is ASP.NET WebForms: Show All resets the result set, then
// an UpdatePanel async postback extends the rendered buffer as the list grows.

import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = (() => {
  try {
    return require('playwright');
  } catch {
    return require('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
  }
})();

const PROFILE_ID = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
const PROFILE_DIR = path.join(
  '/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache',
  PROFILE_ID,
);
const PORT_FILE = path.join(PROFILE_DIR, 'DevToolsActivePort');
const TARGET_URL = 'https://www.grabandgoplus.com/Retailer_CustomerSIMCards';
const SHOW_ALL_SELECTOR = '#GagPRetailerHolder_btnShowAllSimCards';
const SHOW_ALL_FIELD = 'ctl00$GagPRetailerHolder$btnShowAllSimCards';
const EXTEND_EVENT_TARGET = 'ctl00$GagPRetailerHolder$btnExtendBuffer';
const REQUIRED_FIELDS = [
  'lblPurchaseDate',
  'lblActivationRequestDate',
  'lblActivateDate',
  'lblSimCardNumber',
  'lblSKU',
  'lblCost',
  'lnSimDetails',
];

function parseArgs(argv) {
  const args = {
    outputJson: '',
    progressJson: '',
    resumeCurrentShowAll: false,
    showAllTimeoutMs: 600000,
    extendTimeoutMs: 300000,
    snapshotCurrentPartial: false,
    captureFirstExtend: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--output-json') args.outputJson = argv[++index] || '';
    else if (arg === '--progress-json') args.progressJson = argv[++index] || '';
    else if (arg === '--resume-current-show-all') args.resumeCurrentShowAll = true;
    else if (arg === '--snapshot-current-partial') args.snapshotCurrentPartial = true;
    else if (arg === '--capture-first-extend') args.captureFirstExtend = true;
    else if (arg === '--show-all-timeout-ms') args.showAllTimeoutMs = Number(argv[++index] || 0);
    else if (arg === '--extend-timeout-ms') args.extendTimeoutMs = Number(argv[++index] || 0);
    else if (arg === '--self-test') args.selfTest = true;
    else throw new Error(`未知参数: ${arg}`);
  }
  if (!args.selfTest && !args.outputJson) {
    throw new Error('用法: grabandgo_export_all_simcards.mjs --output-json FILE [--progress-json FILE] [--resume-current-show-all] [--snapshot-current-partial]');
  }
  if (!Number.isFinite(args.showAllTimeoutMs) || args.showAllTimeoutMs < 30000) {
    throw new Error('--show-all-timeout-ms 必须至少为30000');
  }
  if (!Number.isFinite(args.extendTimeoutMs) || args.extendTimeoutMs < 30000) {
    throw new Error('--extend-timeout-ms 必须至少为30000');
  }
  return args;
}

function redactPostData(raw) {
  if (!raw) return [];
  return [...new Set(Array.from(new URLSearchParams(raw).keys()))];
}

async function atomicWriteJson(filePath, payload) {
  if (!filePath) return;
  const resolved = path.resolve(filePath);
  const temporary = `${resolved}.${process.pid}.tmp`;
  await fs.mkdir(path.dirname(resolved), { recursive: true });
  await fs.writeFile(temporary, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  await fs.rename(temporary, resolved);
}

async function readPort() {
  const raw = await fs.readFile(PORT_FILE, 'utf8');
  const [port] = raw.split(/\r?\n/).map(value => value.trim());
  if (!/^\d+$/.test(port || '')) throw new Error(`BitBrowser 430端口文件无效: ${PORT_FILE}`);
  return port;
}

async function connect430() {
  const port = await readPort();
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`, { timeout: 120000 });
  const context = browser.contexts()[0];
  if (!context) throw new Error('BitBrowser 430没有可用上下文');
  return { browser, context, port };
}

function isTargetPage(page) {
  try {
    const url = new URL(page.url());
    return /(^|\.)grabandgoplus\.com$/i.test(url.hostname)
      && /Retailer_CustomerSIMCards/i.test(url.pathname);
  } catch {
    return false;
  }
}

async function targetPageInBackground(browser, context, allowExisting = false) {
  if (allowExisting) {
    // A shared BitBrowser profile can contain another Customer SIM Cards tab
    // whose search box is in use. Resume only the unfiltered Show All tab with
    // the largest already-rendered buffer so a checkpoint can never attach to
    // the user's searched tab.
    const candidates = context.pages().filter(isTargetPage);
    const inspected = [];
    for (const page of candidates) {
      const counters = await pageCounters(page).catch(() => null);
      if (counters && counters.total > 0 && !counters.searchValue) inspected.push({ page, counters });
    }
    inspected.sort((left, right) => right.counters.rendered - left.counters.rendered);
    if (inspected[0]) return { page: inspected[0].page, created: false };
  }
  const existingPages = new Set(context.pages());
  const cdp = await browser.newBrowserCDPSession();
  await cdp.send('Target.createTarget', { url: TARGET_URL, background: true, newWindow: false });
  const deadline = Date.now() + 60000;
  let page = null;
  while (Date.now() < deadline && !page) {
    page = context.pages().find(candidate => !existingPages.has(candidate) && isTargetPage(candidate)) || null;
    if (!page) await new Promise(resolve => setTimeout(resolve, 250));
  }
  if (!page) throw new Error('创建后台Customer SIM Cards页面后未找到对应页面');
  await page.waitForLoadState('domcontentloaded', { timeout: 120000 }).catch(() => {});
  return { page, created: true };
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

async function pageCounters(page) {
  return page.evaluate(() => ({
    total: Number.parseInt(document.getElementById('hfTotal')?.value || '0', 10) || 0,
    rendered: document.querySelectorAll('.scrollableDataList .sim-row').length,
    serverRendered: Number.parseInt(document.getElementById('hfRendered')?.value || '0', 10) || 0,
    visible: typeof window.__simVisible === 'number' ? window.__simVisible : 0,
    shownLabel: document.getElementById('lblShownCount')?.innerText || '',
    searchValue: document.querySelector('#GagPRetailerHolder_txtSearchCriteria')?.value || '',
  }));
}

function responseMatchesField(response, field) {
  if (!/grabandgoplus\.com\/Retailer_CustomerSIMCards/i.test(response.url())) return false;
  if (!['xhr', 'document'].includes(response.request().resourceType())) return false;
  return String(response.request().postData() || '').includes(encodeURIComponent(field))
    || String(response.request().postData() || '').includes(field);
}

async function clickShowAll(page, timeoutMs) {
  const responsePromise = page.waitForResponse(
    response => responseMatchesField(response, SHOW_ALL_FIELD),
    { timeout: timeoutMs },
  );
  await page.locator(SHOW_ALL_SELECTOR).click({ timeout: 30000, noWaitAfter: true });
  const response = await responsePromise;
  await response.finished().catch(() => {});
  await page.waitForFunction(
    () => Number.parseInt(document.getElementById('hfTotal')?.value || '0', 10) > 0,
    null,
    { timeout: 60000 },
  );
  return {
    method: response.request().method(),
    resourceType: response.request().resourceType(),
    url: response.url(),
    status: response.status(),
    postFieldNames: redactPostData(response.request().postData() || ''),
  };
}

async function extendOneBuffer(page, previousRendered, timeoutMs) {
  const before = await pageCounters(page);
  if (before.rendered >= before.total) return { response: null, counters: before };
  const responsePromise = page.waitForResponse(
    response => responseMatchesField(response, EXTEND_EVENT_TARGET),
    { timeout: timeoutMs },
  );
  // Use the site's visible Load More control. Reset only the client-side
  // in-flight guard because a cancelled navigation can otherwise leave it true.
  await page.evaluate(() => { window.__simExtending = false; });
  await page.locator('#btnLoadMoreSims').click({ timeout: 30000, noWaitAfter: true });
  const response = await responsePromise;
  await response.finished().catch(() => {});
  await page.waitForLoadState('domcontentloaded', { timeout: 120000 }).catch(() => {});
  await page.waitForFunction(
    previous => document.querySelectorAll('.scrollableDataList .sim-row').length > previous,
    previousRendered,
    { timeout: timeoutMs },
  );
  return {
    response: {
      method: response.request().method(),
      resourceType: response.request().resourceType(),
      url: response.url(),
      status: response.status(),
      postFieldNames: redactPostData(response.request().postData() || ''),
      postControlValues: (() => {
        const form = new URLSearchParams(response.request().postData() || '');
        const selected = {};
        for (const [key, value] of form.entries()) {
          if (
            key === '__EVENTTARGET'
            || key === '__EVENTARGUMENT'
            || key === 'ctl00$ctl09'
            || key.endsWith('$hfTotal')
            || key.endsWith('$hfRendered')
            || key.endsWith('$hfResetView')
            || key.endsWith('$btnExtendBuffer')
          ) selected[key] = value;
        }
        return selected;
      })(),
    },
    counters: await pageCounters(page),
  };
}

async function extractRows(page) {
  return page.evaluate(() => {
    function cleanText(element) {
      if (!element) return '';
      if ('value' in element && ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName)) {
        return String(element.value || '').trim();
      }
      const clone = element.cloneNode(true);
      for (const translated of clone.querySelectorAll('[data-immersive-translate-translation-element-mark], .immersive-translate-target-wrapper')) {
        translated.remove();
      }
      return String(clone.textContent || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
    }

    const result = [];
    const fieldNames = new Set();
    const rowNodes = [...document.querySelectorAll('.scrollableDataList .sim-row')];
    for (const [ordinal, rowNode] of rowNodes.entries()) {
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
      result.push({ ordinal: ordinal + 1, controlIndex, fields });
    }
    return { rows: result, fieldNames: [...fieldNames] };
  });
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

async function runSelfTest() {
  const passed = responseMatchesField({
    url: () => TARGET_URL,
    request: () => ({ resourceType: () => 'xhr', postData: () => `__EVENTTARGET=${encodeURIComponent(EXTEND_EVENT_TARGET)}` }),
  }, EXTEND_EVENT_TARGET);
  console.log(JSON.stringify({ status: passed ? 'passed' : 'failed', liveBrowserUsed: false }));
  if (!passed) process.exitCode = 2;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.selfTest) return runSelfTest();
  const startedAt = new Date().toISOString();
  const { browser, context, port } = await connect430();
  const opened = await targetPageInBackground(browser, context, args.resumeCurrentShowAll);
  const state = await loginState(opened.page);
  if (!(state.showAllPresent && state.showAllText === 'Show All' && state.hasCustomerSimMarker && !state.hasPasswordInput)) {
    const payload = {
      status: 'login_required',
      startedAt,
      finishedAt: new Date().toISOString(),
      browser: 'BitBrowser 430',
      profileId: PROFILE_ID,
      port,
      pageCreatedInBackground: opened.created,
      loginState: state,
      sensitiveDataSubmittedCount: 0,
      instruction: '请在BitBrowser 430登录Grab&Go，确认Customer SIM Cards页面显示Show All后重试',
    };
    await atomicWriteJson(args.outputJson, payload);
    console.log(JSON.stringify({ ...payload, outputJson: path.resolve(args.outputJson) }));
    process.exitCode = 4;
    return;
  }

  const initialCounters = await pageCounters(opened.page);
  const network = [];
  let showAllSkippedForResume = false;
  let priorExtensionCount = 0;
  if (args.resumeCurrentShowAll && initialCounters.total > 0 && !initialCounters.searchValue) {
    showAllSkippedForResume = true;
    if (args.progressJson) {
      try {
        const priorProgress = JSON.parse(await fs.readFile(path.resolve(args.progressJson), 'utf8'));
        if (priorProgress.total === initialCounters.total && priorProgress.rendered === initialCounters.rendered) {
          priorExtensionCount = Number(priorProgress.extensionCount || 0);
        }
      } catch {
        // A missing or invalid progress file does not invalidate the live page;
        // the final row-count checks still gate delivery.
      }
    }
  } else {
    network.push(await clickShowAll(opened.page, args.showAllTimeoutMs));
  }

  let counters = await pageCounters(opened.page);
  if (!(counters.total > 0 && counters.rendered > 0 && counters.rendered <= counters.total)) {
    throw new Error(`Show All后计数无效: ${JSON.stringify(counters)}`);
  }
  const finalTotal = counters.total;
  if (args.snapshotCurrentPartial) {
    const extracted = await extractRows(opened.page);
    const fieldNames = [
      ...REQUIRED_FIELDS.filter(field => extracted.fieldNames.includes(field)),
      ...extracted.fieldNames.filter(field => !REQUIRED_FIELDS.includes(field)).sort(),
    ];
    const missingRequiredFields = REQUIRED_FIELDS.filter(field => !fieldNames.includes(field));
    const completeness = summarizeCompleteness(extracted.rows, fieldNames);
    const controlIndexesUnique = new Set(extracted.rows.map(row => row.controlIndex)).size === extracted.rows.length;
    const payload = {
      status: missingRequiredFields.length === 0 && controlIndexesUnique ? 'partial_checkpoint' : 'failed_validation',
      startedAt,
      finishedAt: new Date().toISOString(),
      sourceUrl: TARGET_URL,
      browser: 'BitBrowser 430',
      profileId: PROFILE_ID,
      silentBrowserMode: true,
      loginProof: {
        showAllPresent: state.showAllPresent,
        showAllText: state.showAllText,
        hasCustomerSimMarker: state.hasCustomerSimMarker,
      },
      total: counters.total,
      rendered: counters.rendered,
      extractedRowCount: extracted.rows.length,
      rowCountMatchesRendered: extracted.rows.length === counters.rendered,
      controlIndexesUnique,
      missingRequiredFields,
      fieldNames,
      completeness,
      rows: extracted.rows,
    };
    await atomicWriteJson(args.outputJson, payload);
    await atomicWriteJson(args.progressJson, {
      status: payload.status,
      event: 'partial_checkpoint_saved',
      updatedAt: new Date().toISOString(),
      total: payload.total,
      rendered: payload.rendered,
      extractedRowCount: payload.extractedRowCount,
      controlIndexesUnique,
      missingRequiredFields,
    });
    console.log(JSON.stringify({
      status: payload.status,
      total: payload.total,
      rendered: payload.rendered,
      extractedRowCount: payload.extractedRowCount,
      controlIndexesUnique,
      missingRequiredFields,
      outputJson: path.resolve(args.outputJson),
    }));
    if (payload.status === 'failed_validation') process.exitCode = 2;
    return;
  }
  let extensionCount = priorExtensionCount;
  let lastRendered = counters.rendered;
  await atomicWriteJson(args.progressJson, {
    status: 'running', event: 'show_all_ready', updatedAt: new Date().toISOString(), ...counters,
  });
  console.log(JSON.stringify({ event: 'show_all_ready', total: counters.total, rendered: counters.rendered }));

  while (counters.rendered < counters.total) {
    if (counters.searchValue) {
      throw new Error('专用后台标签的搜索条件被外部修改，停止以防混入筛选结果');
    }
    const extended = await extendOneBuffer(opened.page, counters.rendered, args.extendTimeoutMs);
    if (!extended.response) break;
    network.push(extended.response);
    counters = extended.counters;
    extensionCount += 1;
    if (counters.searchValue || counters.total !== finalTotal) {
      throw new Error(`列表范围在抓取中发生变化: search=${counters.searchValue}, total=${counters.total}, expected=${finalTotal}`);
    }
    if (counters.rendered <= lastRendered) {
      throw new Error(`缓冲扩展未增加行数: before=${lastRendered}, after=${counters.rendered}`);
    }
    lastRendered = counters.rendered;
    const progress = {
      status: 'running',
      event: 'buffer_extended',
      updatedAt: new Date().toISOString(),
      extensionCount,
      ...counters,
    };
    await atomicWriteJson(args.progressJson, progress);
    console.log(JSON.stringify(progress));
    if (args.captureFirstExtend) {
      const payload = {
        status: 'diagnostic_capture',
        beforeRendered: lastRendered,
        after: counters,
        response: extended.response,
      };
      await atomicWriteJson(args.outputJson, payload);
      console.log(JSON.stringify(payload));
      return;
    }
  }

  const extracted = await extractRows(opened.page);
  const fieldNames = [
    ...REQUIRED_FIELDS.filter(field => extracted.fieldNames.includes(field)),
    ...extracted.fieldNames.filter(field => !REQUIRED_FIELDS.includes(field)).sort(),
  ];
  const missingRequiredFields = REQUIRED_FIELDS.filter(field => !fieldNames.includes(field));
  const completeness = summarizeCompleteness(extracted.rows, fieldNames);
  const finalCounters = await pageCounters(opened.page);
  const rowCountMatchesTotal = extracted.rows.length === finalCounters.total;
  const controlIndexCount = new Set(extracted.rows.map(row => row.controlIndex)).size;
  const controlIndexesUnique = controlIndexCount === extracted.rows.length;
  const passed = rowCountMatchesTotal && controlIndexesUnique && missingRequiredFields.length === 0;
  const payload = {
    status: passed ? 'passed' : 'failed_validation',
    startedAt,
    finishedAt: new Date().toISOString(),
    sourceUrl: TARGET_URL,
    browser: 'BitBrowser 430',
    profileId: PROFILE_ID,
    port,
    silentBrowserMode: true,
    browserForegroundActivationAttempted: false,
    pageCreatedInBackground: opened.created,
    loginProof: {
      showAllPresent: state.showAllPresent,
      showAllText: state.showAllText,
      hasCustomerSimMarker: state.hasCustomerSimMarker,
      greetingPresent: state.greetingPresent,
    },
    interface: {
      type: 'ASP.NET WebForms POST (Show All UpdatePanel + buffered document postback)',
      endpoint: TARGET_URL,
      method: 'POST',
      showAllField: SHOW_ALL_FIELD,
      extendEventTarget: EXTEND_EVENT_TARGET,
      showAllSkippedForResume,
      initialRenderedCount: initialCounters.rendered,
      extensionCount,
      requestFieldNames: [...new Set(network.flatMap(item => item.postFieldNames || []))],
    },
    total: finalCounters.total,
    rendered: finalCounters.rendered,
    extractedRowCount: extracted.rows.length,
    rowCountMatchesTotal,
    controlIndexesUnique,
    missingRequiredFields,
    fieldNames,
    completeness,
    rows: extracted.rows,
  };
  await atomicWriteJson(args.outputJson, payload);
  await atomicWriteJson(args.progressJson, {
    status: payload.status,
    event: 'completed',
    updatedAt: new Date().toISOString(),
    total: payload.total,
    extractedRowCount: payload.extractedRowCount,
    fieldNames,
    completeness,
  });
  console.log(JSON.stringify({
    status: payload.status,
    total: payload.total,
    extractedRowCount: payload.extractedRowCount,
    fieldNames,
    completeness,
    outputJson: path.resolve(args.outputJson),
  }));
  if (!passed) process.exitCode = 2;
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
