#!/usr/bin/env node

// Lightweight read-only exporter for the exact BitBrowser 430 profile. It
// talks directly to Chromium CDP so it never synchronizes or activates the
// user's other tabs. Grab&Go has no JSON paging API; the authenticated page is
// ASP.NET WebForms. We obtain the fresh Show All total, then query by terminal
// alphanumeric character and keep only true suffix matches; those groups are
// disjoint and collectively cover numeric/alphanumeric SIM identifiers.

import fs from 'node:fs/promises';
import path from 'node:path';

const PROFILE_ID = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
const PORT_FILE = path.join(
  '/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache',
  PROFILE_ID,
  'DevToolsActivePort',
);
const TARGET_URL = 'https://www.grabandgoplus.com/Retailer_CustomerSIMCards';
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
  ...Array.from({ length: 10 }, (_, digit) => String(digit)),
  ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
];

function parseArgs(argv) {
  const args = {
    outputJson: '', progressJson: '', stateJson: '', partsDir: '',
    resume: false, bootstrapHttpState: false, fetchAll: false, captureExtendRequest: false, captureFetchExtend: false, timeoutMs: 300000,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--output-json') args.outputJson = argv[++index] || '';
    else if (arg === '--progress-json') args.progressJson = argv[++index] || '';
    else if (arg === '--state-json') args.stateJson = argv[++index] || '';
    else if (arg === '--parts-dir') args.partsDir = argv[++index] || '';
    else if (arg === '--resume') args.resume = true;
    else if (arg === '--bootstrap-http-state') args.bootstrapHttpState = true;
    else if (arg === '--fetch-all') args.fetchAll = true;
    else if (arg === '--capture-extend-request') args.captureExtendRequest = true;
    else if (arg === '--capture-fetch-extend') args.captureFetchExtend = true;
    else if (arg === '--timeout-ms') args.timeoutMs = Number(argv[++index] || 0);
    else if (arg === '--self-test') args.selfTest = true;
    else throw new Error(`未知参数: ${arg}`);
  }
  if (!args.selfTest && !args.outputJson) throw new Error('必须指定 --output-json FILE');
  if (!args.selfTest && args.bootstrapHttpState && (!args.stateJson || !args.partsDir || !args.progressJson)) {
    throw new Error('--bootstrap-http-state 必须同时指定 --state-json、--parts-dir、--progress-json');
  }
  if (!args.selfTest && args.fetchAll && (!args.stateJson || !args.partsDir || !args.progressJson)) {
    throw new Error('--fetch-all 必须同时指定 --state-json、--parts-dir、--progress-json');
  }
  if (!Number.isFinite(args.timeoutMs) || args.timeoutMs < 30000) throw new Error('--timeout-ms 必须至少为30000');
  return args;
}

async function atomicWriteJson(filePath, payload, privateFile = false) {
  if (!filePath) return;
  const resolved = path.resolve(filePath);
  const temporary = `${resolved}.${process.pid}.tmp`;
  await fs.mkdir(path.dirname(resolved), { recursive: true });
  await fs.writeFile(temporary, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  if (privateFile) await fs.chmod(temporary, 0o600);
  await fs.rename(temporary, resolved);
}

class CdpClient {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 0;
    this.pending = new Map();
    this.listeners = new Map();
    socket.addEventListener('message', event => {
      const message = JSON.parse(String(event.data));
      if (!message.id) {
        for (const listener of this.listeners.get(message.method) || []) listener(message.params, message.sessionId);
        return;
      }
      if (!this.pending.has(message.id)) return;
      const pending = this.pending.get(message.id);
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(`${pending.method}: ${JSON.stringify(message.error)}`));
      else pending.resolve(message.result);
    });
    socket.addEventListener('close', () => {
      for (const pending of this.pending.values()) pending.reject(new Error('CDP WebSocket已关闭'));
      this.pending.clear();
    });
  }

  static async connect(url, timeoutMs = 30000) {
    const socket = new WebSocket(url);
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('连接CDP WebSocket超时')), timeoutMs);
      socket.addEventListener('open', () => { clearTimeout(timeout); resolve(); }, { once: true });
      socket.addEventListener('error', event => { clearTimeout(timeout); reject(new Error(`CDP WebSocket错误: ${event.message || 'unknown'}`)); }, { once: true });
    });
    return new CdpClient(socket);
  }

  send(method, params = {}, sessionId = undefined) {
    const id = ++this.nextId;
    const message = { id, method, params };
    if (sessionId) message.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, method });
      this.socket.send(JSON.stringify(message));
    });
  }

  on(method, listener) {
    if (!this.listeners.has(method)) this.listeners.set(method, new Set());
    this.listeners.get(method).add(listener);
    return () => this.listeners.get(method)?.delete(listener);
  }

  close() { this.socket.close(); }
}

async function connect430() {
  const raw = await fs.readFile(PORT_FILE, 'utf8');
  const port = raw.split(/\r?\n/)[0]?.trim();
  if (!/^\d+$/.test(port || '')) throw new Error(`BitBrowser 430端口文件无效: ${PORT_FILE}`);
  const metadata = await (await fetch(`http://127.0.0.1:${port}/json/version`, { signal: AbortSignal.timeout(10000) })).json();
  if (!metadata.webSocketDebuggerUrl) throw new Error('BitBrowser 430未返回DevTools WebSocket地址');
  return { client: await CdpClient.connect(metadata.webSocketDebuggerUrl), port };
}

async function evaluate(client, sessionId, expression, awaitPromise = false) {
  const response = await client.send('Runtime.evaluate', {
    expression,
    awaitPromise,
    returnByValue: true,
    userGesture: false,
  }, sessionId);
  if (response.exceptionDetails) {
    const description = response.result?.description || response.exceptionDetails.text || '页面执行失败';
    throw new Error(description);
  }
  return response.result?.value;
}

async function waitForPageReady(client, sessionId, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const ready = await evaluate(client, sessionId, `JSON.stringify({
      readyState: document.readyState,
      hasBody: Boolean(document.body),
      href: location.href
    })`).catch(() => '');
    if (ready) {
      const parsed = JSON.parse(ready);
      if (
        parsed.hasBody
        && ['interactive', 'complete'].includes(parsed.readyState)
        && /grabandgoplus\.com\/Retailer_CustomerSIMCards/i.test(parsed.href || '')
      ) return;
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error('后台页面加载超时');
}

function pageCountersExpression() {
  return `JSON.stringify({
    total: Number.parseInt(document.getElementById('hfTotal')?.value || '0', 10) || 0,
    rendered: document.querySelectorAll('.scrollableDataList .sim-row').length,
    searchValue: document.querySelector('#GagPRetailerHolder_txtSearchCriteria')?.value || '',
    visible: typeof window.__simVisible === 'number' ? window.__simVisible : 0
  })`;
}

async function pageCounters(client, sessionId) {
  return JSON.parse(await evaluate(client, sessionId, pageCountersExpression()));
}

function asyncPostbackExpression({ buttonSelector, searchValue, timeoutMs }) {
  const assignment = searchValue === undefined ? '' : `
    const input = document.querySelector('#GagPRetailerHolder_txtSearchCriteria');
    if (!input) throw new Error('搜索框不存在');
    input.value = ${JSON.stringify(searchValue)};
  `;
  return `(async () => {
    ${assignment}
    const button = document.querySelector(${JSON.stringify(buttonSelector)});
    if (!button) throw new Error('按钮不存在: ' + ${JSON.stringify(buttonSelector)});
    const manager = window.Sys?.WebForms?.PageRequestManager?.getInstance();
    if (!manager) throw new Error('ASP.NET PageRequestManager不存在');
    return await new Promise((resolve, reject) => {
      let settled = false;
      const cleanup = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        manager.remove_endRequest(onEnd);
      };
      const onEnd = () => {
        cleanup();
        resolve(${pageCountersExpression()});
      };
      const timer = setTimeout(() => {
        cleanup();
        reject(new Error('页面异步回传超时'));
      }, ${Math.max(30000, timeoutMs)});
      manager.add_endRequest(onEnd);
      button.click();
    });
  })()`;
}

async function clickShowAll(client, sessionId, timeoutMs) {
  const result = await evaluate(client, sessionId, asyncPostbackExpression({
    buttonSelector: '#GagPRetailerHolder_btnShowAllSimCards',
    timeoutMs,
  }), true);
  return JSON.parse(result);
}

async function queryTerm(client, sessionId, term, timeoutMs) {
  const result = await evaluate(client, sessionId, asyncPostbackExpression({
    buttonSelector: '#GagPRetailerHolder_btnFilter',
    searchValue: term,
    timeoutMs,
  }), true);
  return JSON.parse(result);
}

async function extractRows(client, sessionId) {
  const expression = `JSON.stringify((() => {
    function cleanText(element) {
      if (!element) return '';
      if ('value' in element && ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName)) return String(element.value || '').trim();
      const clone = element.cloneNode(true);
      for (const translated of clone.querySelectorAll('[data-immersive-translate-translation-element-mark], .immersive-translate-target-wrapper')) translated.remove();
      return String(clone.textContent || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
    }
    const rows = [];
    const fieldNames = new Set();
    for (const [ordinal, rowNode] of [...document.querySelectorAll('.scrollableDataList .sim-row')].entries()) {
      const fields = {};
      let controlIndex = '';
      for (const element of rowNode.querySelectorAll('[id^="GagPRetailerHolder_dlCustomerSimCards_"]')) {
        const match = element.id.match(/^GagPRetailerHolder_dlCustomerSimCards_(.+)_(\\d+)$/);
        if (!match) continue;
        const [, fieldName, index] = match;
        controlIndex ||= index;
        fieldNames.add(fieldName);
        fields[fieldName] = cleanText(element);
      }
      rows.push({ ordinal: ordinal + 1, controlIndex, fields });
    }
    return { rows, fieldNames: [...fieldNames] };
  })())`;
  return JSON.parse(await evaluate(client, sessionId, expression));
}

async function fetchExtendBatch(client, sessionId, previousRendered, timeoutMs) {
  const expression = `(async () => {
    function cleanText(element) {
      if (!element) return '';
      if ('value' in element && ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName)) return String(element.value || '').trim();
      const clone = element.cloneNode(true);
      for (const translated of clone.querySelectorAll('[data-immersive-translate-translation-element-mark], .immersive-translate-target-wrapper')) translated.remove();
      return String(clone.textContent || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
    }
    const previous = ${Number(previousRendered)};
    if (!window.__codexGrabFormFields) {
      window.__codexGrabFormFields = Object.fromEntries(new FormData(document.querySelector('form')).entries());
    }
    const payload = new URLSearchParams(window.__codexGrabFormFields);
    payload.set('__EVENTTARGET', 'ctl00$GagPRetailerHolder$btnExtendBuffer');
    payload.set('__EVENTARGUMENT', '');
    const started = performance.now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ${Math.max(30000, timeoutMs)});
    let response;
    try {
      response = await fetch(location.href, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: payload.toString(),
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
    const html = await response.text();
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    const form = parsed.querySelector('form');
    if (!form) throw new Error('续载回包缺少form');
    window.__codexGrabFormFields = Object.fromEntries(new FormData(form).entries());
    const rowNodes = [...parsed.querySelectorAll('.scrollableDataList .sim-row')];
    const rows = [];
    const fieldNames = new Set();
    for (let ordinal = previous; ordinal < rowNodes.length; ordinal += 1) {
      const rowNode = rowNodes[ordinal];
      const fields = {};
      let controlIndex = '';
      for (const element of rowNode.querySelectorAll('[id^="GagPRetailerHolder_dlCustomerSimCards_"]')) {
        const match = element.id.match(/^GagPRetailerHolder_dlCustomerSimCards_(.+)_(\\d+)$/);
        if (!match) continue;
        const [, fieldName, index] = match;
        controlIndex ||= index;
        fieldNames.add(fieldName);
        fields[fieldName] = cleanText(element);
      }
      rows.push({ ordinal: ordinal + 1, controlIndex, fields });
    }
    return JSON.stringify({
      status: response.status,
      elapsedMs: Math.round(performance.now() - started),
      responseBytes: new Blob([html]).size,
      total: Number.parseInt(parsed.getElementById('hfTotal')?.value || '0', 10) || 0,
      rendered: Number.parseInt(parsed.getElementById('hfRendered')?.value || '0', 10) || 0,
      rowCount: rowNodes.length,
      showAllPresent: Boolean(parsed.getElementById('GagPRetailerHolder_btnShowAllSimCards')),
      fieldNames: [...fieldNames],
      rows,
    });
  })()`;
  return JSON.parse(await evaluate(client, sessionId, expression, true));
}

async function readJsonParts(partsDir, maximumRendered) {
  const resolved = path.resolve(partsDir);
  const names = (await fs.readdir(resolved)).filter(name => /^part_\d{6}_\d{6}\.json$/.test(name)).sort();
  const rows = [];
  const fieldNames = [];
  for (const name of names) {
    const end = Number(name.match(/^part_\d{6}_(\d{6})\.json$/)?.[1] || 0);
    if (end > maximumRendered) continue;
    const payload = JSON.parse(await fs.readFile(path.join(resolved, name), 'utf8'));
    rows.push(...(payload.rows || []));
    for (const field of payload.fieldNames || []) if (!fieldNames.includes(field)) fieldNames.push(field);
  }
  rows.sort((left, right) => Number(left.ordinal) - Number(right.ordinal));
  return { rows, fieldNames };
}

async function runFetchAll(client, sessionId, args, context) {
  const initial = await extractRows(client, sessionId);
  const fields = [
    ...REQUIRED_FIELDS.filter(field => initial.fieldNames.includes(field)),
    ...initial.fieldNames.filter(field => !REQUIRED_FIELDS.includes(field)).sort(),
  ];
  if (initial.rows.length !== context.showAllCounters.rendered) {
    throw new Error(`首批行数不一致: rows=${initial.rows.length}, rendered=${context.showAllCounters.rendered}`);
  }
  await fs.mkdir(path.resolve(args.partsDir), { recursive: true });
  const existingParts = (await fs.readdir(path.resolve(args.partsDir))).filter(name => /^part_\d{6}_\d{6}\.json$/.test(name));
  if (existingParts.length) throw new Error(`分片目录必须为空: ${path.resolve(args.partsDir)}`);
  const initialPart = path.join(path.resolve(args.partsDir), `part_000001_${String(initial.rows.length).padStart(6, '0')}.json`);
  await atomicWriteJson(initialPart, {
    startOrdinal: 1,
    endOrdinal: initial.rows.length,
    fieldNames: fields,
    rows: initial.rows,
    receipt: { event: 'show_all', via: 'browser_cdp_updatepanel' },
  });
  let rendered = initial.rows.length;
  let network = [{ event: 'show_all', via: 'browser_cdp_updatepanel' }];
  await atomicWriteJson(args.progressJson, {
    status: 'running', event: 'show_all_ready', updatedAt: new Date().toISOString(),
    total: context.showAllCounters.total, rendered,
  });
  console.log(JSON.stringify({ status: 'running', event: 'show_all_ready', total: context.showAllCounters.total, rendered }));
  while (rendered < context.showAllCounters.total) {
    const previous = rendered;
    const batch = await fetchExtendBatch(client, sessionId, previous, args.timeoutMs);
    const expected = Math.min(previous + 200, context.showAllCounters.total);
    if (!(
      batch.status === 200
      && batch.showAllPresent
      && batch.total === context.showAllCounters.total
      && batch.rendered === expected
      && batch.rowCount === expected
      && batch.rows.length === expected - previous
    )) throw new Error(`续载批次校验失败: previous=${previous}, expected=${expected}, actual=${JSON.stringify({ status: batch.status, total: batch.total, rendered: batch.rendered, rowCount: batch.rowCount, newRows: batch.rows.length, showAllPresent: batch.showAllPresent })}`);
    const actualOrdinals = batch.rows.map(row => Number(row.ordinal));
    if (actualOrdinals[0] !== previous + 1 || actualOrdinals.at(-1) !== expected || new Set(actualOrdinals).size !== actualOrdinals.length) {
      throw new Error(`续载批次序号不连续: previous=${previous}, expected=${expected}`);
    }
    for (const field of batch.fieldNames) if (!fields.includes(field)) fields.push(field);
    const receipt = {
      event: 'buffer_extended', from: previous, to: expected,
      status: batch.status, elapsedMs: batch.elapsedMs, responseBytes: batch.responseBytes,
      via: 'browser_same_origin_fetch',
    };
    const partPath = path.join(path.resolve(args.partsDir), `part_${String(previous + 1).padStart(6, '0')}_${String(expected).padStart(6, '0')}.json`);
    await atomicWriteJson(partPath, {
      startOrdinal: previous + 1, endOrdinal: expected, fieldNames: batch.fieldNames,
      rows: batch.rows, receipt,
    });
    rendered = expected;
    network.push(receipt);
    const state = {
      status: 'running', updatedAt: new Date().toISOString(), total: context.showAllCounters.total,
      rendered, fieldNames: fields, partCount: network.length, network,
    };
    await atomicWriteJson(args.stateJson, state, true);
    await atomicWriteJson(args.progressJson, {
      status: 'running', event: 'buffer_extended', updatedAt: state.updatedAt,
      total: state.total, rendered, lastReceipt: receipt,
    });
    console.log(JSON.stringify({ status: 'running', total: state.total, rendered, ...receipt }));
  }
  const assembled = await readJsonParts(args.partsDir, rendered);
  const fieldNames = [
    ...REQUIRED_FIELDS.filter(field => assembled.fieldNames.includes(field)),
    ...assembled.fieldNames.filter(field => !REQUIRED_FIELDS.includes(field)).sort(),
  ];
  const missingRequiredFields = REQUIRED_FIELDS.filter(field => !fieldNames.includes(field));
  const rowCountMatchesTotal = assembled.rows.length === context.showAllCounters.total;
  const controlIndexesUnique = new Set(assembled.rows.map(row => row.controlIndex)).size === assembled.rows.length;
  const completeness = summarizeCompleteness(assembled.rows, fieldNames);
  const passed = rowCountMatchesTotal && controlIndexesUnique && missingRequiredFields.length === 0;
  const payload = {
    status: passed ? 'passed' : 'failed_validation',
    startedAt: context.startedAt,
    finishedAt: new Date().toISOString(),
    sourceUrl: TARGET_URL,
    browser: 'BitBrowser 430',
    profileId: PROFILE_ID,
    port: context.port,
    silentBrowserMode: true,
    browserForegroundActivationAttempted: false,
    pageCreatedInBackground: true,
    loginProof: {
      showAllPresent: context.login.showAllPresent,
      showAllText: context.login.showAllText,
      hasCustomerSimMarker: context.login.hasCustomerSimMarker,
      greetingPresent: context.login.greetingPresent,
    },
    interface: {
      type: 'ASP.NET WebForms Show All UpdatePanel + browser same-origin serial buffered POST',
      endpoint: TARGET_URL,
      method: 'POST',
      batchRows: 200,
      cookiePersistence: 'browser_session_only',
    },
    total: context.showAllCounters.total,
    rendered,
    extractedRowCount: assembled.rows.length,
    rowCountMatchesTotal,
    controlIndexesUnique,
    missingRequiredFields,
    fieldNames,
    completeness,
    networkSummary: {
      requestCount: network.length,
      elapsedMsTotal: network.reduce((sum, item) => sum + Number(item.elapsedMs || 0), 0),
      responseBytesTotal: network.reduce((sum, item) => sum + Number(item.responseBytes || 0), 0),
    },
    rows: assembled.rows,
  };
  await atomicWriteJson(args.outputJson, payload);
  await atomicWriteJson(args.progressJson, {
    status: payload.status, event: 'completed', updatedAt: new Date().toISOString(),
    total: payload.total, extractedRowCount: payload.extractedRowCount,
    rowCountMatchesTotal, controlIndexesUnique, missingRequiredFields, completeness,
  });
  await atomicWriteJson(args.stateJson, {
    status: payload.status, updatedAt: new Date().toISOString(), total: payload.total,
    rendered, fieldNames, partCount: network.length, network,
  }, true);
  console.log(JSON.stringify({
    status: payload.status, total: payload.total, extractedRowCount: payload.extractedRowCount,
    rowCountMatchesTotal, controlIndexesUnique, missingRequiredFields, completeness,
    outputJson: path.resolve(args.outputJson),
  }));
  if (!passed) process.exitCode = 2;
}

function sortAndRenumber(rows) {
  rows.sort((left, right) => {
    const byDate = String(right.fields.lblPurchaseDate || '').localeCompare(String(left.fields.lblPurchaseDate || ''));
    if (byDate) return byDate;
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
  const sims = rows.map(row => String(row.fields.lblSimCardNumber || '').trim()).filter(Boolean);
  return {
    nonEmptyByField,
    blankByField,
    simNumberNonEmptyCount: sims.length,
    simNumberUniqueCount: new Set(sims).size,
    duplicateSimNumberCount: sims.length - new Set(sims).size,
  };
}

async function writeCheckpoint(args, base, completedTerms, termStats, fieldNames, rows) {
  const checkpoint = {
    ...base,
    status: 'running',
    updatedAt: new Date().toISOString(),
    completedTerms,
    termStats,
    fieldNames,
    extractedRowCount: rows.length,
    rows: sortAndRenumber([...rows]),
  };
  await atomicWriteJson(args.outputJson, checkpoint);
  await atomicWriteJson(args.progressJson, {
    status: 'running',
    event: 'prefix_partition_completed',
    updatedAt: checkpoint.updatedAt,
    total: base.total,
    completedTermCount: completedTerms.length,
    completedTerms,
    extractedRowCount: rows.length,
    lastTerm: completedTerms.at(-1) || '',
  });
}

async function selfTest() {
  const sample = [
    { fields: { lblSimCardNumber: '891111' } },
    { fields: { lblSimCardNumber: '881111' } },
  ];
  const captured = SEARCH_TERMS.flatMap(term => sample.filter(row => row.fields.lblSimCardNumber.toUpperCase().endsWith(term)));
  const passed = SEARCH_TERMS.length === 36 && captured.length === sample.length;
  console.log(JSON.stringify({ status: passed ? 'passed' : 'failed', liveBrowserUsed: false }));
  if (!passed) process.exitCode = 2;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.selfTest) return selfTest();
  const startedAt = new Date().toISOString();
  const { client, port } = await connect430();
  let targetId = '';
  try {
    ({ targetId } = await client.send('Target.createTarget', { url: TARGET_URL, background: true, newWindow: false }));
    const attached = await client.send('Target.attachToTarget', { targetId, flatten: true });
    const sessionId = attached.sessionId;
    await client.send('Page.enable', {}, sessionId);
    await client.send('Runtime.enable', {}, sessionId);
    await waitForPageReady(client, sessionId);

    const login = JSON.parse(await evaluate(client, sessionId, `JSON.stringify((() => {
      const showAll = document.querySelector('#GagPRetailerHolder_btnShowAllSimCards');
      const body = document.body?.innerText || '';
      return {
        url: location.href,
        title: document.title,
        showAllPresent: Boolean(showAll),
        showAllText: String(showAll?.value || showAll?.innerText || '').trim(),
        hasPasswordInput: Boolean(document.querySelector('input[type="password"]')),
        hasCustomerSimMarker: /Customer\\s+SIM\\s+Cards/i.test(body),
        greetingPresent: /\\bHello\\b/i.test(body)
      };
    })())`));
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

    const showAllCounters = await clickShowAll(client, sessionId, args.timeoutMs);
    if (!(showAllCounters.total > 0 && showAllCounters.rendered > 0 && showAllCounters.rendered <= showAllCounters.total)) {
      throw new Error(`Show All计数无效: ${JSON.stringify(showAllCounters)}`);
    }
    if (args.fetchAll) {
      await runFetchAll(client, sessionId, args, { startedAt, port, login, showAllCounters });
      return;
    }
    if (args.captureFetchExtend) {
      const result = JSON.parse(await evaluate(client, sessionId, `(async () => {
        const form = document.querySelector('form');
        const payload = new URLSearchParams(new FormData(form));
        payload.set('__EVENTTARGET', 'ctl00$GagPRetailerHolder$btnExtendBuffer');
        payload.set('__EVENTARGUMENT', '');
        const response = await fetch(location.href, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: payload.toString(),
        });
        const html = await response.text();
        const parsed = new DOMParser().parseFromString(html, 'text/html');
        const rows = parsed.querySelectorAll('.scrollableDataList .sim-row').length;
        return JSON.stringify({
          status: response.status,
          responseBytes: new Blob([html]).size,
          total: Number.parseInt(parsed.getElementById('hfTotal')?.value || '0', 10) || 0,
          rendered: Number.parseInt(parsed.getElementById('hfRendered')?.value || '0', 10) || 0,
          rowCount: rows,
          showAllPresent: Boolean(parsed.getElementById('GagPRetailerHolder_btnShowAllSimCards')),
          formKeys: [...new Set([...new FormData(parsed.querySelector('form')).keys()])],
        });
      })()`, true));
      const payload = { status: 'fetch_diagnostic', before: showAllCounters, after: result };
      await atomicWriteJson(args.outputJson, payload);
      console.log(JSON.stringify(payload));
      return;
    }
    if (args.captureExtendRequest) {
      await client.send('Network.enable', {}, sessionId);
      let removeListener = () => {};
      const requestPromise = new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          removeListener();
          reject(new Error('等待原生加载更多请求超时'));
        }, 30000);
        removeListener = client.on('Network.requestWillBeSent', (params, eventSessionId) => {
          if (eventSessionId !== sessionId) return;
          const request = params?.request || {};
          const postData = String(request.postData || '');
          if (!/Retailer_CustomerSIMCards/i.test(request.url || '') || !postData.includes('btnExtendBuffer')) return;
          clearTimeout(timer);
          removeListener();
          resolve(request);
        });
      });
      const buttonRect = JSON.parse(await evaluate(client, sessionId, `JSON.stringify((() => {
        window.__simExtending = false;
        const button = document.getElementById('btnLoadMoreSims');
        if (!button) throw new Error('Load More按钮不存在');
        button.scrollIntoView({ block: 'center', inline: 'center' });
        const rect = button.getBoundingClientRect();
        return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, width: rect.width, height: rect.height };
      })())`));
      if (!(buttonRect.width > 0 && buttonRect.height > 0)) throw new Error(`Load More按钮不可点击: ${JSON.stringify(buttonRect)}`);
      await client.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: buttonRect.x, y: buttonRect.y }, sessionId);
      await client.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: buttonRect.x, y: buttonRect.y, button: 'left', clickCount: 1 }, sessionId);
      await client.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: buttonRect.x, y: buttonRect.y, button: 'left', clickCount: 1 }, sessionId);
      const request = await requestPromise;
      const form = new URLSearchParams(request.postData || '');
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
      const deadline = Date.now() + args.timeoutMs;
      let after = null;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        after = await pageCounters(client, sessionId).catch(() => null);
        if (after?.rendered > showAllCounters.rendered) break;
      }
      const payload = {
        status: after?.rendered > showAllCounters.rendered ? 'diagnostic_capture' : 'diagnostic_timeout',
        before: showAllCounters,
        after,
        request: {
          method: request.method,
          url: request.url,
          postFieldNames: [...new Set(form.keys())],
          postControlValues: selected,
        },
      };
      await atomicWriteJson(args.outputJson, payload);
      console.log(JSON.stringify(payload));
      return;
    }
    if (args.bootstrapHttpState) {
      const extracted = await extractRows(client, sessionId);
      const formFields = JSON.parse(await evaluate(client, sessionId, `JSON.stringify(Object.fromEntries(
        new FormData(document.querySelector('form')).entries()
      ))`));
      if (extracted.rows.length !== showAllCounters.rendered) {
        throw new Error(`Show All首批行数不一致: rows=${extracted.rows.length}, rendered=${showAllCounters.rendered}`);
      }
      const fields = [
        ...REQUIRED_FIELDS.filter(field => extracted.fieldNames.includes(field)),
        ...extracted.fieldNames.filter(field => !REQUIRED_FIELDS.includes(field)).sort(),
      ];
      const partPath = path.join(path.resolve(args.partsDir), `part_${String(1).padStart(6, '0')}_${String(showAllCounters.rendered).padStart(6, '0')}.json`);
      await atomicWriteJson(partPath, {
        startOrdinal: 1,
        endOrdinal: showAllCounters.rendered,
        fieldNames: fields,
        rows: extracted.rows,
        receipt: { event: 'show_all', via: 'browser_cdp_updatepanel' },
      });
      const state = {
        status: 'running',
        updatedAt: new Date().toISOString(),
        total: showAllCounters.total,
        rendered: showAllCounters.rendered,
        fieldNames: fields,
        formFields,
        loginProof: {
          showAllPresent: login.showAllPresent,
          showAllText: login.showAllText,
          hasCustomerSimMarker: login.hasCustomerSimMarker,
          hasPasswordInput: login.hasPasswordInput,
          greetingPresent: login.greetingPresent,
        },
        network: [{ event: 'show_all', via: 'browser_cdp_updatepanel' }],
      };
      await atomicWriteJson(args.stateJson, state, true);
      await atomicWriteJson(args.progressJson, {
        status: 'running',
        event: 'show_all_ready',
        updatedAt: state.updatedAt,
        total: state.total,
        rendered: state.rendered,
        bootstrap: 'browser_cdp_updatepanel',
      });
      console.log(JSON.stringify({
        status: 'running', event: 'show_all_ready', total: state.total,
        rendered: state.rendered, stateJson: path.resolve(args.stateJson), partPath,
      }));
      return;
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
        type: 'ASP.NET WebForms UpdatePanel POST (Show All total + SIM-number prefix partitions)',
        endpoint: TARGET_URL,
        method: 'POST',
        showAllField: 'ctl00$GagPRetailerHolder$btnShowAllSimCards',
        filterField: 'ctl00$GagPRetailerHolder$btnFilter',
        extendEventTarget: 'ctl00$GagPRetailerHolder$btnExtendBuffer',
        searchTerms: SEARCH_TERMS,
        reconciliation: 'retain rows whose SIM number truly ends with the queried alphanumeric character; suffix groups are disjoint',
      },
      total: showAllCounters.total,
      showAllInitialRendered: showAllCounters.rendered,
    };

    let completedTerms = [];
    let termStats = [];
    let fieldNames = [...REQUIRED_FIELDS];
    let collectedRows = [];
    if (args.resume) {
      try {
        const checkpoint = JSON.parse(await fs.readFile(path.resolve(args.outputJson), 'utf8'));
        if (checkpoint.total === base.total && Array.isArray(checkpoint.completedTerms) && Array.isArray(checkpoint.rows)) {
          completedTerms = checkpoint.completedTerms.filter(term => SEARCH_TERMS.includes(term));
          termStats = Array.isArray(checkpoint.termStats) ? checkpoint.termStats.filter(stat => completedTerms.includes(stat.term)) : [];
          fieldNames = Array.isArray(checkpoint.fieldNames) && checkpoint.fieldNames.length ? checkpoint.fieldNames : fieldNames;
          collectedRows = checkpoint.rows;
        }
      } catch {
        // No compatible checkpoint: start from the first prefix.
      }
    }

    for (const term of SEARCH_TERMS) {
      if (completedTerms.includes(term)) continue;
      const queried = await queryTerm(client, sessionId, term, args.timeoutMs);
      const extracted = await extractRows(client, sessionId);
      const discoveredFields = extracted.rows.length
        ? [
          ...REQUIRED_FIELDS.filter(field => extracted.fieldNames.includes(field)),
          ...extracted.fieldNames.filter(field => !REQUIRED_FIELDS.includes(field)).sort(),
        ]
        : fieldNames;
      const missing = REQUIRED_FIELDS.filter(field => !discoveredFields.includes(field));
      if (extracted.rows.length && missing.length) throw new Error(`分区${term}缺少必需字段: ${missing.join(',')}`);
      if (extracted.rows.length !== queried.total || queried.rendered !== queried.total || queried.searchValue !== term) {
        throw new Error(`分区${term}页面计数不一致: rows=${extracted.rows.length}, ${JSON.stringify(queried)}`);
      }
      const suffixRows = extracted.rows.filter(row => String(row.fields.lblSimCardNumber || '').toUpperCase().endsWith(term));
      const substringOnlyCount = extracted.rows.length - suffixRows.length;
      fieldNames = discoveredFields;
      collectedRows.push(...suffixRows);
      completedTerms.push(term);
      termStats.push({
        term,
        searchMatchedRows: extracted.rows.length,
        suffixMatchedRows: suffixRows.length,
        substringOnlyCount,
      });
      await writeCheckpoint(args, base, completedTerms, termStats, fieldNames, collectedRows);
      console.log(JSON.stringify({
        status: 'running', event: 'prefix_partition_completed', term,
        completedTermCount: completedTerms.length,
        searchMatchedRows: extracted.rows.length,
        suffixMatchedRows: suffixRows.length,
        extractedRowCount: collectedRows.length,
        total: base.total,
      }));
      if (collectedRows.length === base.total) break;
      if (collectedRows.length > base.total) throw new Error(`分区累计行数超过Show All总数: ${collectedRows.length} > ${base.total}`);
    }

    const rows = sortAndRenumber(collectedRows);
    const missingRequiredFields = REQUIRED_FIELDS.filter(field => !fieldNames.includes(field));
    const rowCountMatchesTotal = rows.length === base.total;
    const completeness = summarizeCompleteness(rows, fieldNames);
    const passed = rowCountMatchesTotal && missingRequiredFields.length === 0;
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
    if (targetId) await client.send('Target.closeTarget', { targetId }).catch(() => {});
    client.close();
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
