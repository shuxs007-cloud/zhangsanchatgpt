#!/usr/bin/env node
// Task F Grab&Go read-only Activation Date lookup through the existing
// BitBrowser 430 profile. Requests prefer the site's same-origin HTTP flow and
// fall back to JavaScript fetch in an already-open background page. ICCIDs are
// never sent through Firecrawl or an AI/browser connector.

import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { runSessionQueue } from './grabandgo_task_f_session_queue.mjs';
import { createHash } from 'node:crypto';
import { acquireSiteLock } from '/Users/zhangsan/Documents/数据分析/opensms_site_lock.mjs';

const require = createRequire(import.meta.url);
function loadPlaywright() {
  try {
    return require('playwright');
  } catch {}
  return require('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
}
let playwright;
function playwrightRuntime() {
  return playwright ||= loadPlaywright();
}

const ACCOUNT_URL = 'https://www.grabandgoplus.com/Retailer_Account';
const CUSTOMER_SIM_URL = 'https://www.grabandgoplus.com/Retailer_CustomerSIMCards';
const LOGIN_URL = 'https://www.grabandgoplus.com/Login';
const DEFAULT_CDP = process.env.BITBROWSER_CDP || 'http://127.0.0.1:52826';
const FILTER_NAME = 'ctl00$GagPRetailerHolder$btnFilter';
const SEARCH_NAME = 'ctl00$GagPRetailerHolder$txtSearchCriteria';
const LOGIN_USER_NAME = 'ctl00$GagPAllPagesHolder$txtUserName';
const LOGIN_PASSWORD_NAME = 'ctl00$GagPAllPagesHolder$txtPassWord';
const LOGIN_BUTTON_NAME = 'ctl00$GagPAllPagesHolder$btnLogIn';
const DEFAULT_WORKERS = 8;
const MAX_WORKERS = 8;
const DEFAULT_DELAY_MS = 1000;
const DEFAULT_PROGRESS_INTERVAL_MS = 300000;
const LIVE_PROGRESS_SNAPSHOT_INTERVAL_MS = 10000;
const CUSTOMER_SIM_LOGIN_MARKER = /\bCustomer\s+SIM\s+Cards\b/i;

function requireAuthorization() {
  if (process.env.OPENSMS_TASK_F_POLICY_CATALOG_SYNC === '1') return;
  const gates = ['OPENSMS_FORCE_TASK_F', 'OPENSMS_ENABLE_TASK_F', 'OPENSMS_EXECUTE_TASK_F'];
  const missing = gates.filter(name => process.env[name] !== '1');
  if (missing.length) throw new Error(`任务F授权门禁不足: ${missing.join(',')}`);
}

function text(value) {
  return String(value ?? '').trim();
}

function normalizeIccid(value) {
  const compact = text(value).replace(/\s+/g, '').replace(/[Ff]$/, '');
  return /^\d{18,22}$/.test(compact) ? compact : '';
}

function readKeychainCredential() {
  try {
    const envelope = JSON.parse(execFileSync('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3', [
      path.join(path.dirname(fileURLToPath(import.meta.url)),'grabandgo_auth_keychain.py'), 'get',
    ], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }));
    if(envelope.status!=='found')return null;
    const raw=Buffer.from(envelope.data,'base64').toString('utf8');
    const value = JSON.parse(raw);
    const username = text(value.username);
    const password = String(value.password || '');
    if (!username || !password) return null;
    return { username, password };
  } catch {
    return null;
  }
}

function decodeHtml(value) {
  return String(value || '')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&#(\d+);/g, (_match, value) => String.fromCharCode(Number(value)));
}

function attrsOf(tag) {
  const attrs = {};
  const regex = /([:\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
  let match;
  while ((match = regex.exec(tag))) {
    const name = String(match[1] || '').toLowerCase();
    if (!name || name === 'input' || name === 'form') continue;
    attrs[name] = decodeHtml(match[2] ?? match[3] ?? match[4] ?? '');
  }
  return attrs;
}

function formInfo(html) {
  const formMatch = String(html || '').match(/<form\b[^>]*>/i);
  const action = formMatch ? attrsOf(formMatch[0]).action || '' : '';
  const inputs = [];
  for (const match of String(html || '').matchAll(/<input\b[^>]*>/gi)) {
    const attrs = attrsOf(match[0]);
    if (!attrs.name) continue;
    inputs.push({
      name: attrs.name,
      id: attrs.id || '',
      value: attrs.value || '',
      type: String(attrs.type || 'text').toLowerCase(),
      disabled: Object.prototype.hasOwnProperty.call(attrs, 'disabled'),
      checked: Object.prototype.hasOwnProperty.call(attrs, 'checked'),
    });
  }
  return { action, inputs };
}

function paramsFromHtml(html, includeSubmitName = '') {
  const params = new URLSearchParams();
  for (const input of formInfo(html).inputs) {
    if (input.disabled) continue;
    if (['submit', 'button', 'image', 'reset'].includes(input.type) && input.name !== includeSubmitName) continue;
    if (['checkbox', 'radio'].includes(input.type) && !input.checked) continue;
    params.append(input.name, input.value);
  }
  return params;
}

function textFromHtml(html) {
  return decodeHtml(String(html || '')
    .replace(/<script[\s\S]*?<\/script>/gi, '\n')
    .replace(/<style[\s\S]*?<\/style>/gi, '\n')
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/(div|p|tr|td|th|li|span|label|h\d)>/gi, '\n')
    .replace(/<[^>]+>/g, ' '))
    .replace(/\u00a0/g, ' ')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n\s+/g, '\n')
    .trim();
}

function indexedSpanField(html, index, fieldPattern) {
  const regex = new RegExp(
    `<span\\b[^>]*id=(?:"([^"]*${fieldPattern}_${index})"|'([^']*${fieldPattern}_${index})'|([^\\s>]*${fieldPattern}_${index}))[^>]*>([\\s\\S]*?)<\\/span>`,
    'i',
  );
  const match = String(html || '').match(regex);
  if (!match) return { present: false, raw: '', source: '' };
  return {
    present: true,
    raw: textFromHtml(match[4]),
    source: match[1] || match[2] || match[3] || '',
  };
}

function detailRowsFromHtml(html) {
  const simByIndex = new Map();
  const simRegex = /<span\b[^>]*id=(?:"GagPRetailerHolder_dlCustomerSimCards_lblSimCardNumber_(\d+)"|'GagPRetailerHolder_dlCustomerSimCards_lblSimCardNumber_(\d+)'|GagPRetailerHolder_dlCustomerSimCards_lblSimCardNumber_(\d+))[^>]*>([\s\S]*?)<\/span>/gi;
  let simMatch;
  while ((simMatch = simRegex.exec(html))) {
    const index = simMatch[1] ?? simMatch[2] ?? simMatch[3];
    simByIndex.set(index, {
      iccid: normalizeIccid(textFromHtml(simMatch[4]).replace(/\D/g, '')),
      offset: simMatch.index,
    });
  }
  const rows = [];
  const linkRegex = /<a\b[^>]*href=(?:"([^"]*)"|'([^']*)'|([^\s>]+))[^>]*>[\s\S]*?<\/a>/gi;
  let linkMatch;
  while ((linkMatch = linkRegex.exec(html))) {
    const href = decodeHtml(linkMatch[1] ?? linkMatch[2] ?? linkMatch[3] ?? '');
    if (!/lnSimDetails/.test(href)) continue;
    const postback = href.match(/__doPostBack\('([^']*)','([^']*)'\)/);
    if (!postback) continue;
    const idMatch = linkMatch[0].match(/id=(?:"GagPRetailerHolder_dlCustomerSimCards_lnSimDetails_(\d+)"|'GagPRetailerHolder_dlCustomerSimCards_lnSimDetails_(\d+)'|GagPRetailerHolder_dlCustomerSimCards_lnSimDetails_(\d+))/i);
    const index = idMatch ? (idMatch[1] ?? idMatch[2] ?? idMatch[3]) : '';
    const sim = simByIndex.get(index) || { iccid: '', offset: -1 };
    const purchaseField = indexedSpanField(html, index, 'lblPurchaseDate');
    const requestField = indexedSpanField(html, index, 'lbl(?:Activation|Act)RequestDate');
    const activationField = indexedSpanField(html, index, 'lbl(?:Activation|Activate)Date');
    const simField = indexedSpanField(html, index, 'lblSimCardNumber');
    const skuField = indexedSpanField(html, index, 'lblSKU');
    let activation = { value: '', source: '', raw: '' };
    if (activationField.present) {
      const raw = activationField.raw;
      const value = parseDate(raw);
      if (value) activation = { value, source: activationField.source, raw };
    }
    if (!activation.value && sim.offset >= 0) {
      const rowStart = html.lastIndexOf('<tr', sim.offset);
      const rowEnd = html.indexOf('</tr>', sim.offset);
      if (rowStart >= 0 && rowEnd > rowStart) {
        const rowHtml = html.slice(rowStart, rowEnd + 5);
        const cellTexts = Array.from(rowHtml.matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/gi), match => textFromHtml(match[1]));
        const simCellIndex = cellTexts.findIndex(value => normalizeIccid(value.replace(/\D/g, '')) === sim.iccid);
        const dateCells = (simCellIndex >= 0 ? cellTexts.slice(0, simCellIndex) : cellTexts)
          .map(raw => ({ value: parseDate(raw), raw }))
          .filter(item => item.value);
        if (dateCells.length) {
          const item = dateCells.at(-1);
          activation = { value: item.value, source: 'customer_sim_result_row_before_sim', raw: item.raw };
        }
      }
    }
    rows.push({
      index,
      iccid: sim.iccid,
      target: postback[1],
      argument: postback[2],
      activationDate: activation.value,
      activationDateRaw: activation.raw,
      activationDateSource: activation.source,
      purchaseDate: parseDate(purchaseField.raw),
      purchaseDateRaw: purchaseField.raw,
      actRequestDate: requestField.raw,
      simNumber: sim.iccid,
      simNumberRaw: simField.raw,
      sku: skuField.raw,
      webFieldPresence: {
        purchaseDate: purchaseField.present,
        actRequestDate: requestField.present,
        activationDate: activationField.present,
        simNumber: simField.present,
        sku: skuField.present,
      },
    });
  }
  return rows;
}

function parseDate(value) {
  const raw = text(value).replace(/\./g, '/').replace(/\s+/g, ' ');
  if (!raw) return '';
  let match = raw.match(/\b((?:19|20)\d{2})[-/](\d{1,2})[-/](\d{1,2})\b/);
  let year;
  let month;
  let day;
  if (match) {
    [, year, month, day] = match;
  } else {
    match = raw.match(/\b(\d{1,2})[-/](\d{1,2})[-/]((?:19|20)?\d{2})\b/);
    if (match) {
      [, month, day, year] = match;
      if (year.length === 2) year = Number(year) < 70 ? `20${year}` : `19${year}`;
    } else {
      const monthNames = {
        jan: 1, january: 1, feb: 2, february: 2, mar: 3, march: 3,
        apr: 4, april: 4, may: 5, jun: 6, june: 6, jul: 7, july: 7,
        aug: 8, august: 8, sep: 9, sept: 9, september: 9, oct: 10,
        october: 10, nov: 11, november: 11, dec: 12, december: 12,
      };
      match = raw.match(/\b([A-Za-z]+)\s+(\d{1,2}),?\s+((?:19|20)\d{2})\b/);
      if (!match || !monthNames[match[1].toLowerCase()]) return '';
      month = monthNames[match[1].toLowerCase()];
      day = match[2];
      year = match[3];
    }
  }
  const iso = `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  const validation = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(validation.valueOf()) || validation.toISOString().slice(0, 10) !== iso) return '';
  return iso;
}

function extractActivationDate(html) {
  const info = formInfo(html);
  const exactInput = info.inputs.find(input => /activation.*date|date.*activation/i.test(`${input.id} ${input.name}`));
  if (exactInput?.value) {
    const parsed = parseDate(exactInput.value);
    if (parsed) return { value: parsed, source: exactInput.id || exactInput.name, raw: exactInput.value };
  }
  const rowMatches = String(html || '').match(/<tr\b[^>]*>[\s\S]*?<\/tr>/gi) || [];
  for (const row of rowMatches) {
    const rowText = textFromHtml(row);
    if (!/Activation\s*Date/i.test(rowText)) continue;
    const remainder = rowText.replace(/[\s\S]*?Activation\s*Date\s*:*/i, '').trim();
    const parsed = parseDate(remainder);
    if (parsed) return { value: parsed, source: 'details_table_row', raw: remainder };
  }
  const lines = textFromHtml(html).split(/\n+/).map(line => line.trim()).filter(Boolean);
  const index = lines.findIndex(line => /Activation\s*Date/i.test(line));
  if (index >= 0) {
    const candidates = [lines[index].replace(/[\s\S]*?Activation\s*Date\s*:*/i, '').trim(), ...lines.slice(index + 1, index + 5)];
    for (const candidate of candidates) {
      const parsed = parseDate(candidate);
      if (parsed) return { value: parsed, source: 'details_text', raw: candidate };
    }
  }
  return { value: '', source: '', raw: '' };
}

function loginState(url, html) {
  const finalUrl = text(url);
  const body = textFromHtml(html).slice(0, 12000);
  let sameOrigin = false;
  try { sameOrigin = new URL(finalUrl).origin === new URL(LOGIN_URL).origin; } catch {}
  const retailerUrl = sameOrigin && /Retailer_(?:Account|CustomerSIMCards|SimCardInfo)/i.test(finalUrl);
  const loginUrl = /login|signin|default\.aspx/i.test(finalUrl) && !retailerUrl;
  const passwordInput = /<input\b[^>]*(?:type=["']?password|name=["'][^"']*(?:password|login)[^"']*)/i.test(html);
  const exactCustomerSimMarker = CUSTOMER_SIM_LOGIN_MARKER.test(body);
  const authenticatedPageMarker = exactCustomerSimMarker
    || /Retailer\s+Account|txtSearchCriteria|btnFilter|dlCustomerSimCards|txtSimCardNumber|txtActivationDate/i.test(`${body}\n${html}`);
  const loginText = /\b(?:sign\s*in|log\s*in|password)\b/i.test(body) && !authenticatedPageMarker;
  const authenticated = !loginUrl && !passwordInput && !loginText && retailerUrl && authenticatedPageMarker;
  return { authenticated, finalUrl, retailerUrl, exactCustomerSimMarker, authenticatedPageMarker, loginUrl, passwordInput, loginText };
}

export function confirmedGrabAuthenticationFailure(response, state = loginState(response?.url, response?.text || '')) {
  let finalUrl;
  try { finalUrl = new URL(state.finalUrl); } catch { return false; }
  if (finalUrl.origin !== new URL(LOGIN_URL).origin || state.authenticated) return false;
  if (response.status === 401) return true;
  // HTTP/protocol failures are not evidence that the stored login expired.
  return response.status === 200 && /^\/Login\/?$/i.test(finalUrl.pathname)
    && state.loginUrl === true && state.passwordInput === true;
}

async function responseRecord(response) {
  return { status: response.status(), url: response.url(), text: await response.text() };
}

async function assertWorkerAuthenticated(api, workerId) {
  const customerSim = await responseRecord(await api.get(CUSTOMER_SIM_URL, { timeout: 60000, failOnStatusCode: false }));
  const state = loginState(customerSim.url, customerSim.text);
  if (customerSim.status >= 400 || !state.authenticated) {
    throw new Error(`Grab&Go未登录，请在BitBrowser 430中登录后重试；worker=${workerId}, HTTP=${customerSim.status}, url=${state.finalUrl}`);
  }
  return { workerId, httpStatus: customerSim.status, finalUrl: state.finalUrl, authenticated: true };
}

async function postHtml(api, baseHtml, params) {
  const action = formInfo(baseHtml).action || CUSTOMER_SIM_URL;
  const url = new URL(action, CUSTOMER_SIM_URL).href;
  return responseRecord(await api.post(url, {
    headers: {
      accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
      'content-type': 'application/x-www-form-urlencoded',
      origin: new URL(CUSTOMER_SIM_URL).origin,
      referer: CUSTOMER_SIM_URL,
    },
    data: params.toString(),
    timeout: 60000,
    failOnStatusCode: false,
  }));
}

export async function authenticateWorkerWithCredential(api, credential, workerId) {
  const loginPage = await responseRecord(await api.get(LOGIN_URL, {
    timeout: 60000,
    failOnStatusCode: false,
  }));
  const directState = loginState(loginPage.url, loginPage.text);
  if (loginPage.status < 400 && directState.authenticated) {
    return authenticatedSession(api, null);
  }
  if (!confirmedGrabAuthenticationFailure(loginPage, directState) || loginPage.status !== 200) {
    throw new Error(`worker ${workerId}登录页未确认，未提交凭据: HTTP=${loginPage.status}`);
  }
  const loginForm = formInfo(loginPage.text);
  const names = new Set(loginForm.inputs.map(input => input.name));
  for (const required of [LOGIN_USER_NAME, LOGIN_PASSWORD_NAME, LOGIN_BUTTON_NAME]) {
    if (!names.has(required)) throw new Error(`worker ${workerId}登录表单缺少字段: ${required}`);
  }
  const params = paramsFromHtml(loginPage.text, LOGIN_BUTTON_NAME);
  params.set(LOGIN_USER_NAME, credential.username);
  params.set(LOGIN_PASSWORD_NAME, credential.password);
  params.set(LOGIN_BUTTON_NAME, 'LOGIN');
  const action = new URL(loginForm.action || LOGIN_URL, LOGIN_URL).href;
  if(new URL(action).origin!==new URL(LOGIN_URL).origin)throw Error('GRAB_LOGIN_FORM_ORIGIN_CHANGED');
  const submitted = await responseRecord(await api.post(action, {
    headers: {
      accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
      'content-type': 'application/x-www-form-urlencoded',
      origin: new URL(LOGIN_URL).origin,
      referer: LOGIN_URL,
    },
    data: params.toString(),
    timeout: 60000,
    failOnStatusCode: false,
  }));
  if (submitted.status >= 400) throw new Error(`worker ${workerId}独立登录HTTP失败: ${submitted.status}`);
  const submittedState = loginState(submitted.url, submitted.text);
  // Playwright follows the login redirect. When that response already contains
  // the exact search page, reuse it as the initial proof. The whole-pool live
  // recheck still runs only after every login has completed.
  if (submittedState.authenticated && submittedState.exactCustomerSimMarker
      && /\/Retailer_CustomerSIMCards(?:[/?#]|$)/i.test(submittedState.finalUrl)) {
    return {
      authenticated: true,
      response: submitted,
      state: submittedState,
      checks: [{ requestedUrl: action, httpStatus: submitted.status,
        finalUrl: submittedState.finalUrl, authenticated: true,
        exactCustomerSimMarker: true, transport: 'same-origin-http-login-response' }],
    };
  }
  const session = await authenticatedSession(api, null);
  if (!session.authenticated || !session.state.exactCustomerSimMarker) {
    throw new Error(`worker ${workerId}独立登录后未取得Customer SIM Cards精确登录态`);
  }
  return session;
}

export async function checkIndependentLogins(outputPath, requestedCount, {
  readCredential=readKeychainCredential,
  newContext=options=>playwrightRuntime().request.newContext(options),
}={}) {
  requireAuthorization();
  if (!outputPath) throw new Error('usage: --check-independent-logins OUTPUT.json [workers<=8]');
  const count = Math.min(MAX_WORKERS, Math.max(1, Number(requestedCount || DEFAULT_WORKERS)));
  const credential = readCredential();
  if (!credential) throw new Error('任务F钥匙串凭据不可用');
  const contexts = [];
  let initialized = [];
  try {
    const settled = await Promise.allSettled(Array.from({ length: count }, async (_unused, index) => {
      const directApi = await newContext({
        storageState: { cookies: [], origins: [] },
        extraHTTPHeaders: { 'accept-language': 'en-US,en;q=0.9' },
        ignoreHTTPSErrors: false,
      });
      contexts.push(directApi);
      const telemetry = { sameOriginHttpRequests: 0, backgroundJavascriptFallbackRequests: 0 };
      const api = requestWithJavascriptFallback(directApi, null, telemetry);
      const session = await authenticateWorkerWithCredential(api, credential, index + 1);
      const storage = await directApi.storageState();
      const sessionCookie = (storage.cookies || []).find(cookie => /^ASP\.NET_SessionId$/i.test(cookie.name));
      return { directApi, api, sessionId: text(sessionCookie?.value), workerId: index + 1 };
    }));
    const failures = settled.filter(entry => entry.status === 'rejected');
    initialized = settled.filter(entry => entry.status === 'fulfilled').map(entry => entry.value);
    const rechecks = await Promise.allSettled(initialized.map(async entry => {
      const session = await authenticatedSession(entry.api, null);
      if (!session.authenticated || !session.state.exactCustomerSimMarker) {
        throw new Error(`worker ${entry.workerId}复核未登录`);
      }
      const storage = await entry.directApi.storageState();
      const sessionCookie = (storage.cookies || []).find(cookie => /^ASP\.NET_SessionId$/i.test(cookie.name));
      entry.sessionId = text(sessionCookie?.value);
      return true;
    }));
    const recheckFailures = rechecks.filter(entry => entry.status === 'rejected');
    const sessionIds = initialized.map(entry => entry.sessionId).filter(Boolean);
    const uniqueAspNetSessionCount = new Set(sessionIds).size;
    const independentPassed = failures.length === 0
      && recheckFailures.length === 0
      && initialized.length === count
      && sessionIds.length === count
      && uniqueAspNetSessionCount === count;
    let serialVerified=false;
    // Multi-login may invalidate earlier ASP.NET sessions. A failure to prove
    // eight sessions is not a failure to prove one; dispose the whole pool
    // before establishing and independently rechecking the serial fallback.
    if(!independentPassed && initialized.length>0){
      await Promise.all(contexts.map(context=>context.dispose().catch(()=>{})));
      contexts.length=0;
      try{
        const directApi=await newContext({storageState:{cookies:[],origins:[]},ignoreHTTPSErrors:false});
        contexts.push(directApi);
        const session=await authenticateWorkerWithCredential(directApi,credential,1);
        const recheck=await authenticatedSession(directApi,null);
        serialVerified=session.authenticated===true&&recheck.authenticated===true&&recheck.state.exactCustomerSimMarker===true;
      }catch{serialVerified=false;}
    }
    const passed=independentPassed||serialVerified;
    const payload = {
      status: passed ? 'passed' : 'failed',
      mode: 'task-f-independent-login-session-check-only',
      createdAt: new Date().toISOString(),
      browser: 'BitBrowser 430 OpenSMS inventory + isolated local HTTP login containers',
      cdpEndpoint: process.env.BITBROWSER_CDP || '',
      loginStatus: passed ? 'authenticated' : 'failed',
      silentBrowserMode: true,
      browserForegroundActivationAttempted: false,
      transportPriority: ['same-origin-http', 'background-page-javascript'],
      requestedWorkers: count,
      authenticatedWorkers: initialized.length - recheckFailures.length,
      effectiveWorkers: independentPassed ? count : serialVerified ? 1 : 0,
      uniqueAspNetSessionCount,
      independentAspNetSessions: independentPassed,
      serialFallbackVerified: serialVerified,
      independentFormLoginAttempted: true,
      independentFormLoginSucceeded: passed,
      credentialsReadFrom: 'macOS Keychain',
      credentialAvailable: true,
      credentialStore: 'macOS Keychain',
      credentialsWrittenToReceipt: false,
      sensitiveIccidSubmittedCount: 0,
      failures: [
        ...failures.map(entry => text(entry.reason?.message || entry.reason)),
        ...recheckFailures.map(entry => text(entry.reason?.message || entry.reason)),
      ],
    };
    await atomicWriteJson(outputPath, payload);
    console.log(JSON.stringify({ ...payload, path: path.resolve(outputPath) }));
    if (!passed) process.exitCode = 4;
  } finally {
    await Promise.all(contexts.map(context => context.dispose().catch(() => {})));
  }
}

export async function queryOne(api, item, workerId, reusableHtml = '') {
  const base = reusableHtml
    ? { status: 200, url: CUSTOMER_SIM_URL, text: reusableHtml }
    : await responseRecord(await api.get(CUSTOMER_SIM_URL, { timeout: 180000, failOnStatusCode: false }));
  const baseLogin = loginState(base.url, base.text);
  if (base.status >= 400 || !baseLogin.authenticated) {
    throw new Error(`Grab&Go会话失效，请重新登录；worker=${workerId}, HTTP=${base.status}, url=${base.url}`);
  }
  const filterParams = paramsFromHtml(base.text, FILTER_NAME);
  filterParams.set('__EVENTTARGET', '');
  filterParams.set('__EVENTARGUMENT', '');
  filterParams.set(SEARCH_NAME, item.queryIccid);
  filterParams.set(FILTER_NAME, 'Go');
  const filtered = await postHtml(api, base.text, filterParams);
  const filteredLogin = loginState(filtered.url, filtered.text);
  if (filtered.status >= 400 || !filteredLogin.authenticated) {
    throw new Error(`Grab&Go筛选会话失效；worker=${workerId}, HTTP=${filtered.status}, url=${filtered.url}`);
  }
  const exactRows = detailRowsFromHtml(filtered.text).filter(row => row.iccid === item.queryIccid);
  if (!exactRows.length) {
    return {
      row: {
        ...item,
        workerId,
        status: 'not_found',
        queriedAt: new Date().toISOString(),
        detailSimIccid: '',
        purchaseDate: '',
        purchaseDateRaw: '',
        actRequestDate: '',
        activationDate: '',
        activationDateRaw: '',
        activationDateSource: '',
        simNumber: '',
        simNumberRaw: '',
        sku: '',
        webRecordFound: false,
        webFieldPresence: {
          purchaseDate: false,
          actRequestDate: false,
          activationDate: false,
          simNumber: false,
          sku: false,
        },
        recordSchemaVersion: 2,
      },
      reusableHtml: filtered.text,
    };
  }
  if (exactRows.length !== 1) throw new Error(`Grab&Go精确ICCID结果数量不是1: ${exactRows.length}`);
  if (exactRows[0].simNumber !== item.queryIccid) {
    throw new Error(`Grab&Go列表SIM Number不一致: expected=${item.queryIccid}, actual=${exactRows[0].simNumber || 'blank'}`);
  }
  const missingWebFields = Object.entries(exactRows[0].webFieldPresence || {})
    .filter(([_name, present]) => !present)
    .map(([name]) => name);
  if (missingWebFields.length) {
    throw new Error(`Grab&Go结果行缺少字段节点: ${missingWebFields.join(',')}`);
  }
  const detailParams = paramsFromHtml(filtered.text);
  detailParams.set('__EVENTTARGET', exactRows[0].target);
  detailParams.set('__EVENTARGUMENT', exactRows[0].argument);
  const details = await postHtml(api, filtered.text, detailParams);
  const detailsLogin = loginState(details.url, details.text);
  if (details.status >= 400 || !detailsLogin.authenticated) {
    throw new Error(`Grab&Go详情会话失效；worker=${workerId}, HTTP=${details.status}, url=${details.url}`);
  }
  const simInput = formInfo(details.text).inputs.find(input => /txtSimCardNumber$/i.test(input.id) || /txtSimCardNumber$/i.test(input.name));
  const detailSimIccid = normalizeIccid(String(simInput?.value || '').replace(/\D/g, ''));
  if (!detailSimIccid || detailSimIccid !== item.queryIccid) {
    throw new Error(`Grab&Go详情ICCID不一致: expected=${item.queryIccid}, actual=${detailSimIccid || 'blank'}`);
  }
  const activation = exactRows[0].activationDate
    ? {
        value: exactRows[0].activationDate,
        raw: exactRows[0].activationDateRaw,
        source: exactRows[0].activationDateSource,
      }
    : extractActivationDate(details.text);
  return {
    row: {
      ...item,
      workerId,
      status: activation.value ? 'found' : 'activation_missing',
      queriedAt: new Date().toISOString(),
      detailSimIccid,
      activationDate: activation.value,
      activationDateRaw: activation.raw,
      activationDateSource: activation.source,
      purchaseDate: exactRows[0].purchaseDate,
      purchaseDateRaw: exactRows[0].purchaseDateRaw,
      actRequestDate: exactRows[0].actRequestDate,
      simNumber: exactRows[0].simNumber,
      simNumberRaw: exactRows[0].simNumberRaw,
      sku: exactRows[0].sku,
      webRecordFound: true,
      webFieldPresence: exactRows[0].webFieldPresence,
      recordSchemaVersion: 2,
    },
    // The details form posts back to Retailer_SimCardInfo and cannot be used
    // as the next CustomerSIMCards search form. Reuse the last search-page
    // state instead; otherwise every later item on this worker is falsely
    // classified as not_found.
    reusableHtml: filtered.text,
  };
}

export async function runLookupWorkers(items, workers, {
  delayMs = DEFAULT_DELAY_MS,
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  onResult = () => {},
  assertHealthy = () => {},
} = {}) {
  if (!Number.isFinite(delayMs) || delayMs < DEFAULT_DELAY_MS) {
    throw new Error('任务F每ICCID间隔不得低于1000毫秒');
  }
  return runSessionQueue(items, workers, async (entry, item, index, workerId, stop) => {
    assertHealthy();
    let result;
    try {
      const queried = await queryOne(entry.api, item, workerId, entry.reusableHtml || '');
      result = queried.row;
      entry.reusableHtml = queried.reusableHtml;
    } catch (firstError) {
      entry.reusableHtml = '';
      await sleep(delayMs);
      try {
        const queried = await queryOne(entry.api, item, workerId);
        result = { ...queried.row, retry: true };
        entry.reusableHtml = queried.reusableHtml;
      } catch (secondError) {
        result = { ...item, workerId, status: 'error', queriedAt: new Date().toISOString(),
          error: `${firstError.message}; retry=${secondError.message}` };
      }
    }
    try {
      await onResult(result, index);
    } catch (error) {
      // Stop siblings from taking another card during this worker's cooldown.
      stop(error);
      throw error;
    } finally {
      // Keep the full cooldown even for the last card and local receipt errors.
      await sleep(delayMs);
    }
    return result;
  });
}

async function runSelfTest() {
  const sample = `
    <form action="./Retailer_CustomerSIMCards">
      <input type="hidden" name="__VIEWSTATE" value="abc" />
      <input id="GagPRetailerHolder_txtActivationDate" name="ctl00$GagPRetailerHolder$txtActivationDate" value="08/03/2026" />
      <input id="GagPRetailerHolder_txtSimCardNumber" name="ctl00$GagPRetailerHolder$txtSimCardNumber" value="8912230200081836799" />
    </form>`;
  const activation = extractActivationDate(sample);
  const customerSimLogin = loginState(
    CUSTOMER_SIM_URL,
    '<html><body><h1>Customer SIM Cards</h1><form><input name="ctl00$GagPRetailerHolder$txtSearchCriteria"></form></body></html>',
  );
  const detailLogin = loginState(
    'https://www.grabandgoplus.com/Retailer_SimCardInfo',
    sample,
  );
  const listRows = detailRowsFromHtml(`
    <table><tr>
      <td><span id="GagPRetailerHolder_dlCustomerSimCards_lblPurchaseDate_0">2026-08-27</span></td>
      <td><span id="GagPRetailerHolder_dlCustomerSimCards_lblActRequestDate_0">2026-09-02 14:59</span></td>
      <td><span id="GagPRetailerHolder_dlCustomerSimCards_lblActivateDate_0">2026-09-02 15:03</span></td>
      <td><span id="GagPRetailerHolder_dlCustomerSimCards_lblSimCardNumber_0">8912230200251591141</span></td>
      <td><span id="GagPRetailerHolder_dlCustomerSimCards_lblSKU_0">AMKM254GP7</span></td>
      <td><a id="GagPRetailerHolder_dlCustomerSimCards_lnSimDetails_0" href="javascript:__doPostBack('ctl00$GagPRetailerHolder$dlCustomerSimCards$ctl00$lnSimDetails','')">Details</a></td>
    </tr></table>`);
  const passed = normalizeIccid('8912230200081836799F') === '8912230200081836799'
    && DEFAULT_WORKERS === 8
    && MAX_WORKERS === 8
    && activation.value === '2026-08-03'
    && parseDate('September 1, 2026') === '2026-09-01'
    && customerSimLogin.authenticated
    && customerSimLogin.exactCustomerSimMarker
    && detailLogin.authenticated
    && listRows.length === 1
    && listRows[0].iccid === '8912230200251591141'
    && listRows[0].activationDate === '2026-09-02'
    && listRows[0].purchaseDate === '2026-08-27'
    && listRows[0].actRequestDate === '2026-09-02 14:59'
    && listRows[0].simNumber === '8912230200251591141'
    && listRows[0].sku === 'AMKM254GP7'
    && Object.values(listRows[0].webFieldPresence).every(Boolean);
  console.log(JSON.stringify({
    status: passed ? 'passed' : 'failed',
    liveBrowserUsed: false,
    browser: 'BitBrowser 430 existing profile',
    transportPriority: ['same-origin-http', 'background-page-javascript'],
    defaultWorkers: DEFAULT_WORKERS,
    maxWorkers: MAX_WORKERS,
    activation,
    customerSimLogin,
    detailLogin,
    listRows,
  }));
  if (!passed) process.exitCode = 2;
}

export async function validateCurrent430Endpoint(endpoint, {
  read = () => fs.readFile('/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/a53ce74e6c9e4fa08aec35f81ce1e2a3/DevToolsActivePort','utf8'),
  fetchVersion = value => fetch(`${value}/json/version`,{signal:AbortSignal.timeout(10000)}),
} = {}) {
  const lines = String(await read()).trimEnd().split(/\r?\n/);
  const port = Number(lines[0]);
  const expected = `http://127.0.0.1:${port}`;
  if (!Number.isInteger(port) || port < 1 || port > 65535 || lines.length !== 2
      || !lines[1].startsWith('/devtools/browser/') || endpoint !== expected) {
    throw new Error('OPENSMS_TASK_F_430_ENDPOINT_MISMATCH');
  }
  const response = await fetchVersion(endpoint);
  if (!response.ok) throw new Error('OPENSMS_TASK_F_430_CDP_UNAVAILABLE');
  const version = await response.json();
  const ws = new URL(version.webSocketDebuggerUrl);
  if (ws.hostname !== '127.0.0.1' || ws.port !== String(port) || ws.pathname !== lines[1]) {
    throw new Error('OPENSMS_TASK_F_430_IDENTITY_CHANGED');
  }
}

async function connectBitBrowser430() {
  const endpoint = process.env.BITBROWSER_CDP || DEFAULT_CDP;
  if (process.env.OPENSMS_TASK_F_CURRENT_SESSION_ONLY === '1') await validateCurrent430Endpoint(endpoint);
  const browser = await playwrightRuntime().chromium.connectOverCDP(endpoint, { timeout: 120000 });
  const context = browser.contexts()[0];
  if (!context) throw new Error('BitBrowser 430没有可用浏览器上下文');
  return { browser, context, endpoint };
}

function isGrabAndGoPage(page) {
  try {
    return /(^|\.)grabandgoplus\.com$/i.test(new URL(page.url()).hostname);
  } catch {
    return false;
  }
}

export function automaticLoginAuthorized(policy) {
  return policy?.version === 2 && policy.enabled === true
    && policy.authorization === 'current-user-standing-grab-login-on-authentication-failure'
    && policy.credentialStore === 'local-non-synchronizable-macos-keychain'
    && policy.loginOrigin === new URL(LOGIN_URL).origin
    && policy.maxRecoveryLoginPreflight === 1
    && policy.allowMissingQueryPage === true
    && policy.recoveryTransport === 'existing-bitbrowser-430-context-request'
    && policy.continueCurrentTaskAfterVerifiedLogin === true
    && policy.cookieHandling === 'existing-context-no-cookie-export'
    && policy.queryBeforeVerifiedLogin === false && policy.replayFailedJobs === false;
}

async function readAutomaticLoginPolicy() {
  try {
    return JSON.parse(await fs.readFile(new URL('./grabandgo_login_policy.json', import.meta.url), 'utf8'));
  } catch { return null; }
}

export async function ensureBackgroundAccountPage(browser, context, {
  policy = null,
} = {}) {
  const grabAndGoPages = context.pages().filter(candidate => isGrabAndGoPage(candidate));
  let page = grabAndGoPages.find(candidate => /Retailer_CustomerSIMCards/i.test(candidate.url()))
    || grabAndGoPages.find(candidate => /Retailer_Account/i.test(candidate.url()));
  let created = false;
  if (!page) {
    if (process.env.OPENSMS_TASK_F_CURRENT_SESSION_ONLY === '1') {
      // The context's request client shares the existing 430 cookie jar. A
      // missing tab is not expired authentication and does not require a tab.
      if (automaticLoginAuthorized(policy || await readAutomaticLoginPolicy())) {
        return { page: null, created: false, queryPageMissing: true };
      }
      throw new Error('当前430没有已打开的Grab&Go账户/查卡页；人工会话模式不创建或切换页面');
    }
    const pagePromise = context.waitForEvent('page', { timeout: 60000 });
    const cdp = await browser.newBrowserCDPSession();
    await cdp.send('Target.createTarget', {
      url: ACCOUNT_URL,
      background: true,
      newWindow: false,
    });
    page = await pagePromise;
    created = true;
  }
  if (created) await page.waitForLoadState('domcontentloaded', { timeout: 60000 }).catch(() => {});
  return { page, created };
}

export async function authenticatedWithRecovery(browser, context, opened, telemetry, {
  policy = null,
  readCredential = readKeychainCredential,
} = {}) {
  let activePage = opened.page;
  let api = requestWithJavascriptFallback(context.request, activePage, telemetry);
  let session = await authenticatedSession(api, activePage);
  let recovery = {
    attempted: false,
    recovered: false,
    credentialAvailable: null,
    credentialStore: 'macOS Keychain',
    reason: '',
  };
  if (!session.authenticated && session.authenticationFailureConfirmed === true
      && automaticLoginAuthorized(policy || await readAutomaticLoginPolicy())) {
    const credential = readCredential();
    recovery.credentialAvailable = Boolean(credential);
    if (!credential) recovery.reason = 'keychain_credential_unavailable';
    else {
      recovery.attempted = true;
      recovery.attempts = 1;
      recovery.transport = 'existing-bitbrowser-430-context-request';
      try {
        // Login POST must never be replayed through the JavaScript fallback.
        // BrowserContext.request updates the same 430 cookie jar, not a copy.
        const loginApi = requestWithJavascriptFallback(context.request, null, telemetry);
        await authenticateWorkerWithCredential(loginApi, credential, 1);
        const verified = await authenticatedSession(loginApi, null);
        if (verified.authenticated && verified.response.status === 200
            && verified.state.exactCustomerSimMarker
            && /^https:\/\/www\.grabandgoplus\.com\/Retailer_CustomerSIMCards(?:[/?#]|$)/i.test(verified.state.finalUrl)) {
          session = verified;
          recovery.recovered = true;
          recovery.cookieUpdatedInCurrentContext = true;
          recovery.continueCurrentTask = true;
        } else recovery.reason = 'protected_query_page_not_verified';
      } catch {
        // Do not include HTTP bodies, request data or credential-bearing errors.
        recovery.reason = 'single_login_attempt_not_verified';
      } finally { credential.password = ''; }
    }
  }
  return { activePage, api, session, recovery };
}

async function authenticatedSession(api, page = null) {
  const checks = [];
  if (page && isGrabAndGoPage(page) && /Retailer_CustomerSIMCards/i.test(page.url())) {
    const html = await page.content().catch(() => '');
    const state = loginState(page.url(), html);
    const check = {
      requestedUrl: CUSTOMER_SIM_URL,
      httpStatus: 200,
      finalUrl: state.finalUrl,
      authenticated: state.authenticated,
      exactCustomerSimMarker: state.exactCustomerSimMarker,
      transport: 'background-page-dom',
    };
    checks.push(check);
    if (check.authenticated) {
      try {
        const liveResponse = await responseRecord(await pageJavascriptRequest(page, 'GET', CUSTOMER_SIM_URL));
        const liveState = loginState(liveResponse.url, liveResponse.text);
        const liveCheck = {
          requestedUrl: CUSTOMER_SIM_URL,
          httpStatus: liveResponse.status,
          finalUrl: liveState.finalUrl,
          authenticated: liveResponse.status < 400 && liveState.authenticated,
          exactCustomerSimMarker: liveState.exactCustomerSimMarker,
          authenticationFailureConfirmed: confirmedGrabAuthenticationFailure(liveResponse, liveState),
          transport: 'background-page-javascript-live-verification',
        };
        checks.push(liveCheck);
        if (liveCheck.authenticated) {
          return { authenticated: true, response: liveResponse, state: liveState, checks };
        }
      } catch (error) {
        checks.push({
          requestedUrl: CUSTOMER_SIM_URL,
          httpStatus: 0,
          finalUrl: '',
          authenticated: false,
          transport: 'background-page-javascript-live-verification',
          error: error.message,
        });
      }
    }
  }
  for (const url of [CUSTOMER_SIM_URL, ACCOUNT_URL]) {
    let response;
    try {
      response = await responseRecord(await api.get(url, { timeout: 180000, failOnStatusCode: false }));
    } catch (error) {
      checks.push({
        requestedUrl: url,
        httpStatus: 0,
        finalUrl: '',
        authenticated: false,
        transport: 'same-origin-http',
        error: error.message,
      });
      continue;
    }
    const state = loginState(response.url, response.text);
    const check = {
      requestedUrl: url,
      httpStatus: response.status,
      finalUrl: state.finalUrl,
      authenticated: response.status < 400 && state.authenticated,
      exactCustomerSimMarker: state.exactCustomerSimMarker,
      authenticationFailureConfirmed: confirmedGrabAuthenticationFailure(response, state),
      transport: 'same-origin-http',
    };
    checks.push(check);
    if (check.authenticated) return { authenticated: true, response, state, checks };
  }
  const fallback = checks.at(-1) || {};
  return {
    authenticated: false,
    authenticationFailureConfirmed: fallback.authenticationFailureConfirmed === true
      && checks.filter(check => check.transport !== 'background-page-dom')
        .every(check => check.authenticationFailureConfirmed === true),
    response: { status: fallback.httpStatus || 0, url: fallback.finalUrl || '', text: '' },
    state: { finalUrl: fallback.finalUrl || '' },
    checks,
  };
}

async function atomicWriteJson(filePath, payload, { exclusive = false } = {}) {
  if (!filePath) return;
  const resolved = path.resolve(filePath);
  const temporary = `${resolved}.${process.pid}.tmp`;
  await fs.mkdir(path.dirname(resolved), { recursive: true });
  try {
    await fs.writeFile(temporary, `${JSON.stringify(payload, null, 2)}\n`);
    if (exclusive) {
      // Publish a complete inode without replacing a previous business receipt.
      await fs.link(temporary, resolved);
    } else {
      await fs.rename(temporary, resolved);
    }
  } finally {
    await fs.unlink(temporary).catch(error => {
      if (error.code !== 'ENOENT') throw error;
    });
  }
}

function responseLike(payload) {
  return {
    status: () => Number(payload.status || 0),
    url: () => String(payload.url || ''),
    text: async () => String(payload.text || ''),
  };
}

async function pageJavascriptRequest(page, method, url, options = {}) {
  if (!page || !isGrabAndGoPage(page)) throw new Error('没有可用于后台JavaScript请求的Grab&Go页面');
  const result = await page.evaluate(async input => {
    const response = await fetch(input.url, {
      method: input.method,
      credentials: 'include',
      headers: input.headers || {},
      ...(input.data === undefined ? {} : { body: input.data }),
    });
    return { status: response.status, url: response.url, text: await response.text() };
  }, { method, url, headers: options.headers || {}, data: options.data });
  return responseLike(result);
}

function requestWithJavascriptFallback(primary, page, telemetry) {
  async function invoke(method, url, options = {}) {
    const response = method === 'GET'
      ? await primary.get(url, options)
      : await primary.post(url, options);
    telemetry.sameOriginHttpRequests += 1;
    const redirectedToLogin = /\/Login(?:[/?#]|$)/i.test(response.url());
    if ((response.status() !== 403 && !redirectedToLogin) || !page || !isGrabAndGoPage(page)) return response;
    telemetry.backgroundJavascriptFallbackRequests += 1;
    return pageJavascriptRequest(page, method, url, options);
  }
  return {
    get: (url, options) => invoke('GET', url, options),
    post: (url, options) => invoke('POST', url, options),
  };
}

async function runLoginHelper() {
  requireAuthorization();
  const { browser, context, endpoint } = await connectBitBrowser430();
  const opened = await ensureBackgroundAccountPage(browser, context);
  console.log(JSON.stringify({
    status: 'login_required',
    accountUrl: ACCOUNT_URL,
    finalUrl: opened.page?.url() || ACCOUNT_URL,
    browser: 'BitBrowser 430',
    cdpEndpoint: endpoint,
    backgroundTabCreated: opened.created,
    browserForegroundActivationAttempted: false,
    loginInstruction: '请在BitBrowser 430中打开已创建的Grab&Go标签并完成登录，然后重新强制执行任务F',
  }));
}

export async function checkLogin(outputPath, {
  connectBrowser = connectBitBrowser430,
  readCredential = readKeychainCredential,
  policy = null,
} = {}) {
  requireAuthorization();
  if (!outputPath) throw new Error('usage: node grabandgo_task_f_lookup.mjs --check-login OUTPUT.json');
  const { browser, context, endpoint } = await connectBrowser();
  const opened = await ensureBackgroundAccountPage(browser, context, { policy });
  const telemetry = { sameOriginHttpRequests: 0, backgroundJavascriptFallbackRequests: 0 };
  const authenticated = await authenticatedWithRecovery(browser, context, opened, telemetry, { policy, readCredential });
  const session = authenticated.session;
  const account = session.response;
  const state = session.state;
  const payload = {
    status: session.authenticated ? 'authenticated' : 'login_required',
    createdAt: new Date().toISOString(),
    accountUrl: ACCOUNT_URL,
    customerSimUrl: CUSTOMER_SIM_URL,
    loginChecks: session.checks,
    authenticationFailureConfirmed: session.authenticationFailureConfirmed === true,
    finalUrl: state.finalUrl,
    browser: 'BitBrowser 430',
    cdpEndpoint: endpoint,
    backgroundTabCreated: opened.created,
    queryPageMissing: opened.queryPageMissing === true,
    autoLoginAttempted: authenticated.recovery.attempted,
    autoLoginRecovered: authenticated.recovery.recovered,
    autoLoginAttempts: authenticated.recovery.attempts || 0,
    autoLoginTransport: authenticated.recovery.transport || '',
    credentialAvailable: authenticated.recovery.credentialAvailable,
    credentialStore: authenticated.recovery.credentialStore,
    autoLoginFailureReason: authenticated.recovery.reason || '',
    transportPriority: ['same-origin-http', 'background-page-javascript'],
    ...telemetry,
    silentBrowserMode: true,
    browserForegroundActivationAttempted: false,
    loginInstruction: session.authenticated ? null : 'Grab&Go登录未核验通过，需要人工协助；未自动重跑失败任务。',
    sensitiveIccidSubmittedCount: 0,
    credentialsWrittenToReceipt: false,
  };
  await fs.mkdir(path.dirname(path.resolve(outputPath)), { recursive: true });
  await fs.writeFile(outputPath, `${JSON.stringify(payload, null, 2)}\n`, { flag: 'wx', mode: 0o600 });
  console.log(JSON.stringify({ ...payload, path: path.resolve(outputPath) }));
  if (payload.status !== 'authenticated') process.exitCode = 4;
}

async function debugOne(queryValue, outputDirectory) {
  requireAuthorization();
  const queryIccid = normalizeIccid(queryValue);
  if (!queryIccid || !outputDirectory) {
    throw new Error('usage: node grabandgo_task_f_lookup.mjs --debug-one ICCID OUTPUT_DIR');
  }
  const { browser, context, endpoint } = await connectBitBrowser430();
  const pages = context.pages().filter(candidate => isGrabAndGoPage(candidate));
  const page = pages.find(candidate => /Retailer_CustomerSIMCards/i.test(candidate.url()))
    || pages.find(candidate => /Retailer_Account/i.test(candidate.url()));
  if (!page) throw new Error('BitBrowser 430没有Grab&Go后台页面');
  const telemetry = { sameOriginHttpRequests: 0, backgroundJavascriptFallbackRequests: 0 };
  const authenticated = await authenticatedWithRecovery(browser, context, { page, created: false }, telemetry);
  const api = authenticated.api;
  const session = authenticated.session;
  if (!session.authenticated) throw new Error('BitBrowser 430的Customer SIM Cards页面未登录');
  const filterParams = paramsFromHtml(session.response.text, FILTER_NAME);
  filterParams.set('__EVENTTARGET', '');
  filterParams.set('__EVENTARGUMENT', '');
  filterParams.set(SEARCH_NAME, queryIccid);
  filterParams.set(FILTER_NAME, 'Go');
  const filtered = await postHtml(api, session.response.text, filterParams);
  const filteredState = loginState(filtered.url, filtered.text);
  const outputDir = path.resolve(outputDirectory);
  await fs.mkdir(outputDir, { recursive: true });
  const htmlPath = path.join(outputDir, `filtered_${queryIccid}.html`);
  await fs.writeFile(htmlPath, filtered.text, { flag: 'wx' });
  const parsedRows = detailRowsFromHtml(filtered.text);
  const exactRows = parsedRows.filter(row => row.iccid === queryIccid);
  let details = null;
  let detailsPath = '';
  let detailSimIccid = '';
  let detailActivation = { value: '', source: '', raw: '' };
  if (exactRows.length === 1) {
    const detailParams = paramsFromHtml(filtered.text);
    detailParams.set('__EVENTTARGET', exactRows[0].target);
    detailParams.set('__EVENTARGUMENT', exactRows[0].argument);
    details = await postHtml(api, filtered.text, detailParams);
    detailsPath = path.join(outputDir, `details_${queryIccid}.html`);
    await fs.writeFile(detailsPath, details.text, { flag: 'wx' });
    const simInput = formInfo(details.text).inputs.find(input => /txtSimCardNumber$/i.test(input.id) || /txtSimCardNumber$/i.test(input.name));
    detailSimIccid = normalizeIccid(String(simInput?.value || '').replace(/\D/g, ''));
    detailActivation = extractActivationDate(details.text);
  }
  const occurrences = [];
  let offset = 0;
  while ((offset = filtered.text.indexOf(queryIccid, offset)) >= 0) {
    occurrences.push(offset);
    offset += queryIccid.length;
  }
  const receipt = {
    status: filtered.status < 400 && filteredState.authenticated ? 'passed' : 'session_expired',
    createdAt: new Date().toISOString(),
    queryIccid,
    endpoint,
    httpStatus: filtered.status,
    finalUrl: filtered.url,
    responseBytes: Buffer.byteLength(filtered.text),
    occurrenceCount: occurrences.length,
    occurrenceOffsets: occurrences.slice(0, 20),
    legacyParsedExactRows: exactRows.length,
    parsedExactRows: exactRows,
    listActivation: exactRows.length === 1 ? {
      value: exactRows[0].activationDate || '',
      raw: exactRows[0].activationDateRaw || '',
      source: exactRows[0].activationDateSource || '',
    } : null,
    detailsHttpStatus: details?.status || 0,
    detailsFinalUrl: details?.url || '',
    detailsPath,
    detailSimIccid,
    detailActivation,
    exactCustomerSimMarker: CUSTOMER_SIM_LOGIN_MARKER.test(textFromHtml(filtered.text)),
    authenticatedResponse: filteredState.authenticated,
    autoLoginAttempted: authenticated.recovery.attempted,
    autoLoginRecovered: authenticated.recovery.recovered,
    credentialAvailable: authenticated.recovery.credentialAvailable,
    credentialStore: authenticated.recovery.credentialStore,
    htmlPath,
    ...telemetry,
  };
  const receiptPath = path.join(outputDir, `filtered_${queryIccid}.json`);
  await atomicWriteJson(receiptPath, receipt);
  console.log(JSON.stringify({ ...receipt, receiptPath }));
  if (receipt.status !== 'passed') process.exitCode = 4;
}

export async function initializeConcurrentWorkers(
  seedCookies, authenticationSource, count, credential = null, requestFactory = null,
) {
  if (!Number.isInteger(count) || count < 1 || count > MAX_WORKERS) {
    throw new Error('任务F工作器数量必须为1至8的整数');
  }
  const createdApis = [];
  try {
    const settled = await Promise.allSettled(Array.from({ length: count }, async (_unused, index) => {
      const directApi = await (requestFactory || playwrightRuntime().request).newContext({
        storageState: { cookies: seedCookies, origins: [] },
        extraHTTPHeaders: { 'accept-language': 'en-US,en;q=0.9' },
        ignoreHTTPSErrors: false,
      });
      createdApis.push(directApi);
      const telemetry = { sameOriginHttpRequests: 0, backgroundJavascriptFallbackRequests: 0 };
      const api = requestWithJavascriptFallback(directApi, null, telemetry);
      const workerSession = credential
        ? await authenticateWorkerWithCredential(api, credential, index + 1)
        : await authenticatedSession(api, null);
      if (!workerSession.authenticated || !workerSession.state.exactCustomerSimMarker) {
        throw new Error(`worker ${index + 1}未取得Customer SIM Cards精确登录态`);
      }
      const workerStorage = await directApi.storageState();
      const sessionCookie = (workerStorage.cookies || []).find(cookie => /^ASP\.NET_SessionId$/i.test(cookie.name));
      return {
        worker: { api, directApi, telemetry, disposable: true, reusableHtml: workerSession.response.text },
        login: {
          workerId: index + 1, authenticated: true, finalUrl: workerSession.state.finalUrl,
          exactCustomerSimMarker: workerSession.state.exactCustomerSimMarker,
          authenticationSource, aspNetSessionAssigned: Boolean(sessionCookie?.value),
        },
        sessionId: text(sessionCookie?.value),
      };
    }));
    const failures = settled.filter(entry => entry.status === 'rejected');
    if (failures.length) {
      throw new Error(failures.map(entry => entry.reason?.message || String(entry.reason)).join('; '));
    }
    return settled.map(entry => entry.value);
  } catch (error) {
    // allSettled above drains login requests, including contexts created late.
    await Promise.all(createdApis.map(api => api.dispose().catch(() => {})));
    throw error;
  }
}

export async function requireUniqueWorkerSessions(candidate, expectedCount) {
  function assertUnique() {
    const sessionIds = candidate.map(entry => entry.sessionId).filter(Boolean);
    const uniqueCount = new Set(sessionIds).size;
    if (candidate.length !== expectedCount || (expectedCount > 1
        && (sessionIds.length !== expectedCount || uniqueCount !== expectedCount))) {
      throw new Error(`独立ASP.NET会话不足: expected=${expectedCount}, actual=${uniqueCount}`);
    }
  }
  try {
    assertUnique();
    // Keep this barrier after ALL logins: a later login may expire an earlier
    // session. No ICCID request is dispatched until every live recheck passes.
    const rechecks = await Promise.allSettled(candidate.map(async (entry, index) => {
      const current = await authenticatedSession(entry.worker.api, null);
      if (!current.authenticated || !current.state.exactCustomerSimMarker) {
        throw new Error(`worker ${index + 1}独立会话复核未登录`);
      }
      entry.worker.reusableHtml = current.response.text;
      const storage = await entry.worker.directApi.storageState();
      const cookie = (storage.cookies || []).find(value => /^ASP\.NET_SessionId$/i.test(value.name));
      entry.sessionId = text(cookie?.value);
      entry.login.aspNetSessionAssigned = Boolean(entry.sessionId);
    }));
    const failures = rechecks.filter(entry => entry.status === 'rejected');
    if (failures.length) {
      throw new Error(failures.map(entry => entry.reason?.message || String(entry.reason)).join('; '));
    }
    // Authentication may rotate cookies; validate the IDs that will actually
    // perform lookups instead of trusting the pre-recheck snapshot.
    assertUnique();
    return candidate;
  } catch (error) {
    await Promise.all(candidate.map(entry => entry.worker.directApi.dispose().catch(() => {})));
    throw error;
  }
}

export async function main({
  readCredential = readKeychainCredential,
  requestFactory = null,
  connectBrowser = connectBitBrowser430,
} = {}) {
  if (process.argv.includes('--self-test')) return runSelfTest();
  if (process.argv.includes('--open-login')) return runLoginHelper();
  const independentLoginCheckIndex = process.argv.indexOf('--check-independent-logins');
  if (independentLoginCheckIndex >= 0) {
    return checkIndependentLogins(
      process.argv[independentLoginCheckIndex + 1],
      process.argv[independentLoginCheckIndex + 2],
      {readCredential,...(requestFactory?{newContext:options=>requestFactory.newContext(options)}:{})},
    );
  }
  const debugOneIndex = process.argv.indexOf('--debug-one');
  if (debugOneIndex >= 0) return debugOne(process.argv[debugOneIndex + 1], process.argv[debugOneIndex + 2]);
  const loginCheckIndex = process.argv.indexOf('--check-login');
  if (loginCheckIndex >= 0) return checkLogin(process.argv[loginCheckIndex + 1]);
  requireAuthorization();
  const inputPath = process.argv[2];
  const outputPath = process.argv[3];
  const requestedWorkers = Number(process.argv[4] || DEFAULT_WORKERS);
  if (!Number.isInteger(requestedWorkers) || requestedWorkers < 1) throw new Error('任务F工作器数量必须为正整数');
  const workerCount = Math.min(MAX_WORKERS, requestedWorkers);
  const requestedDelayMs = Number(process.env.OPENSMS_TASK_F_DELAY_MS || DEFAULT_DELAY_MS);
  const delayMs = Number.isFinite(requestedDelayMs) ? Math.max(1000, requestedDelayMs) : DEFAULT_DELAY_MS;
  if (!inputPath || !outputPath) {
    throw new Error('usage: node grabandgo_task_f_lookup.mjs PENDING.json OUTPUT.json [workers<=8]');
  }
  const pendingBytes = await fs.readFile(inputPath);
  const pendingPayload = JSON.parse(pendingBytes.toString('utf8'));
  const runBinding = {
    runId: pendingPayload.documentRunId || pendingPayload.runId || process.env.OPENSMS_RUN_ID || null,
    documentRunId: pendingPayload.documentRunId || null,
    pendingPath: path.resolve(inputPath),
    pendingSha256: createHash('sha256').update(pendingBytes).digest('hex'),
  };
  if (pendingPayload.documentRunId && process.env.OPENSMS_RUN_ID
      && pendingPayload.documentRunId !== process.env.OPENSMS_RUN_ID) {
    throw new Error('任务F文档与当前运行编号不一致');
  }
  const items = (pendingPayload.rows || []).map(row => ({ ...row, queryIccid: normalizeIccid(row.queryIccid || row.sourceIccid) }));
  if (items.some(item => !item.queryIccid)) throw new Error('任务F待查清单含无效ICCID');
  const progressPath = process.argv[5] ? path.resolve(process.argv[5]) : '';
  // Keep the user-facing five-minute contract independent of inherited env.
  const progressIntervalMs = DEFAULT_PROGRESS_INTERVAL_MS;

  // Enforce the empty-diff rule at this entry point too, before reading the
  // keychain, loading Playwright, connecting CDP, or contacting Grab&Go.
  if (!items.length) {
    const payload = {
      status: 'passed', createdAt: new Date().toISOString(),
      mode: 'task-f-grabandgo-empty-pending-no-network',
      ...runBinding,
      loginStatus: 'not_required', inputCount: 0, statusCounts: {}, results: [],
      requestedWorkers: workerCount, effectiveWorkers: 0, uniqueAspNetSessionCount: 0,
      independentAspNetSessions: false, workerSessionMode: 'no-pending-no-session',
      adaptiveConcurrency: true, sharedSessionForcesSerial: true, requestedWorkersSatisfied: true,
      independentFormLoginAttempted: false, independentFormLoginSucceeded: false,
      sameOriginHttpRequests: 0, backgroundJavascriptFallbackRequests: 0,
      sensitiveIccidSubmittedCount: 0, workerLogins: [], noNetworkRequired: true,
      backgroundTabCreated: false, autoLoginAttempted: false, autoLoginRecovered: false,
      silentBrowserMode: true, browserForegroundActivationAttempted: false,
      cloudUploadAttempted: false, credentialsWrittenToReceipt: false,
      perWorkerDelayMs: delayMs, progressIntervalSeconds: Math.round(progressIntervalMs / 1000),
      databaseMissingOnly: pendingPayload.reportBackfillMode !== true,
      databaseActivationMissingOnly: pendingPayload.reportBackfillMode === true,
      reportBackfillMode: pendingPayload.reportBackfillMode === true,
      strictIdentityGuard: 'queryIccid_equals_listSimNumber_equals_detailSimIccid',
      identityMismatchStoredAsTerminal: false, strictDetailSimEqualityRequired: true,
      recordSchemaVersion: 2, fullSourceDataIncluded: true,
      recordedWebFields: ['Purchase Date', 'Act. Request Date', 'Activation Date', 'SIM Number', 'SKU'],
    };
    await atomicWriteJson(outputPath, payload, { exclusive: true });
    await atomicWriteJson(progressPath, {
      ...payload, results: undefined, event: 'task_f_lookup_completed',
      updatedAt: payload.createdAt, elapsedSeconds: 0,
      totalInputCount: 0, completedCount: 0, remainingCount: 0, progressPath,
    });
    console.log(JSON.stringify({ ...payload, results: undefined, path: path.resolve(outputPath) }));
    return;
  }

  const independentLoginOnly = process.env.OPENSMS_TASK_F_INDEPENDENT_LOGIN_ONLY === '1';
  const currentSessionOnly = process.env.OPENSMS_TASK_F_CURRENT_SESSION_ONLY === '1';
  const keychainCredential = currentSessionOnly ? null : readCredential();
  let browser = null;
  let context = null;
  let endpoint = process.env.BITBROWSER_CDP || DEFAULT_CDP;
  let opened = { page: null, created: false };
  const baseTelemetry = { sameOriginHttpRequests: 0, backgroundJavascriptFallbackRequests: 0 };
  let authenticated = {
    api: null,
    session: {
      authenticated: true,
      response: { status: 200, url: CUSTOMER_SIM_URL, text: '' },
      state: { finalUrl: CUSTOMER_SIM_URL },
      checks: [],
    },
    recovery: {
      attempted: false,
      recovered: false,
      credentialAvailable: Boolean(keychainCredential),
      credentialStore: 'macOS Keychain',
      reason: '',
    },
  };
  if (!independentLoginOnly) {
    ({ browser, context, endpoint } = await connectBrowser());
    opened = await ensureBackgroundAccountPage(browser, context);
    authenticated = await authenticatedWithRecovery(browser, context, opened, baseTelemetry, {readCredential});
  }
  const baseApi = authenticated.api;
  const session = authenticated.session;
  const account = session.response;
  const accountState = session.state;
  if (!session.authenticated) {
    const payload = {
      status: 'login_required',
      createdAt: new Date().toISOString(),
      accountUrl: ACCOUNT_URL,
      customerSimUrl: CUSTOMER_SIM_URL,
      loginChecks: session.checks,
      finalUrl: accountState.finalUrl,
      browser: 'BitBrowser 430',
      cdpEndpoint: endpoint,
      backgroundTabCreated: opened.created,
      autoLoginAttempted: authenticated.recovery.attempted,
      autoLoginRecovered: authenticated.recovery.recovered,
      autoLoginAttempts: authenticated.recovery.attempts || 0,
      continueCurrentTaskAfterLogin: false,
      credentialAvailable: authenticated.recovery.credentialAvailable,
      credentialStore: authenticated.recovery.credentialStore,
      autoLoginFailureReason: authenticated.recovery.reason || '',
      transportPriority: ['same-origin-http', 'background-page-javascript'],
      ...baseTelemetry,
      silentBrowserMode: true,
      browserForegroundActivationAttempted: false,
      loginInstruction: '请在BitBrowser 430中登录Grab&Go，然后重新强制执行任务F',
      sensitiveIccidSubmittedCount: 0,
    };
    await atomicWriteJson(outputPath, payload, { exclusive: true });
    console.log(JSON.stringify({ ...payload, path: path.resolve(outputPath) }));
    process.exitCode = 4;
    return;
  }

  const storage = context ? await context.storageState() : { cookies: [] };
  const authCookies = (storage.cookies || []).filter(cookie => /grabandgoplus\.com$/i.test(cookie.domain.replace(/^\./, '')));
  const sharedAspNetSessionCookie = authCookies.some(cookie => /^ASP\.NET_SessionId$/i.test(cookie.name));
  const persistentAuthCookies = authCookies.filter(cookie => !/^ASP\.NET_SessionId$/i.test(cookie.name));

  const workers = [];
  const workerLogins = [];
  const targetWorkers = Math.min(workerCount, items.length);
  let authenticatedClientContextsAvailable = false;
  let effectiveWorkers = 0;
  let concurrencyLimitedReason = null;
  let workerSessionMode = '';
  let uniqueAspNetSessionCount = 0;
  let independentAspNetSessions = false;
  let independentFormLoginAttempted = false;
  let independentFormLoginSucceeded = false;

  if (!authCookies.length && !independentLoginOnly) {
    throw new Error('任务F无法建立自适应工作器：BitBrowser 430中没有可复制的Grab&Go登录Cookie');
  }
  let initialized = null;
  const initializationErrors = [];
  if (persistentAuthCookies.length) {
    try {
      initialized = await requireUniqueWorkerSessions(await initializeConcurrentWorkers(
        persistentAuthCookies,
        'validated-background-page-plus-persistent-auth-cookies-new-session',
        targetWorkers,
        null,
        requestFactory,
      ), targetWorkers);
      workerSessionMode = targetWorkers > 1
        ? 'fresh-aspnet-session-per-http-worker'
        : 'single-worker-session';
    } catch (error) {
      initializationErrors.push(`fresh-session:${error.message}`);
    }
  }
  if (!initialized && !currentSessionOnly && (targetWorkers > 1 || independentLoginOnly)) {
    independentFormLoginAttempted = true;
    const credential = keychainCredential;
    if (credential) {
      try {
        initialized = await requireUniqueWorkerSessions(await initializeConcurrentWorkers(
          [],
          'macos-keychain-independent-form-login',
          targetWorkers,
          credential,
          requestFactory,
        ), targetWorkers);
        independentFormLoginSucceeded = true;
        workerSessionMode = targetWorkers > 1
          ? 'independent-form-login-unique-aspnet-session-per-worker'
          : 'independent-form-login-single-worker';
      } catch (error) {
        initializationErrors.push(`independent-form-login:${error.message}`);
        if (independentLoginOnly && targetWorkers > 1) {
          try {
            initialized = await requireUniqueWorkerSessions(await initializeConcurrentWorkers(
              [], 'macos-keychain-independent-form-login-serial-fallback', 1, credential, requestFactory,
            ), 1);
            independentFormLoginSucceeded = true;
            workerSessionMode = 'independent-form-login-single-worker';
            concurrencyLimitedReason = 'independent_sessions_unavailable_forces_serial_execution';
          } catch (fallbackError) {
            initializationErrors.push(`independent-serial-fallback:${fallbackError.message}`);
          }
        }
      }
    } else {
      initializationErrors.push('independent-form-login:keychain_credential_unavailable');
    }
  }
  if (!initialized && authCookies.length) {
    try {
      initialized = await initializeConcurrentWorkers(
        authCookies,
        'validated-background-page-plus-copied-browser-cookies',
        1,
        null,
        requestFactory,
      );
      workerSessionMode = 'shared-aspnet-session-serial-worker';
      concurrencyLimitedReason = 'shared_server_session_forces_serial_execution';
    } catch (error) {
      initializationErrors.push(`shared-session:${error.message}`);
    }
  }
  if (!initialized || !initialized.length) {
    throw new Error(`任务F无法建立自适应工作器，已在查询前停止: ${initializationErrors.join('; ')}`);
  }
  const assignedSessionIds = initialized.map(entry => entry.sessionId).filter(Boolean);
  uniqueAspNetSessionCount = new Set(assignedSessionIds).size;
  independentAspNetSessions = initialized.length > 1
    && assignedSessionIds.length === initialized.length
    && uniqueAspNetSessionCount === initialized.length;
  authenticatedClientContextsAvailable = true;
  effectiveWorkers = initialized.length;
  workers.push(...initialized.map(entry => entry.worker));
  workerLogins.push(...initialized.map(entry => ({
    ...entry.login,
    independentAspNetSession: independentAspNetSessions,
  })));

  if (process.env.OPENSMS_TASK_F_SESSION_CHECK_ONLY === '1') {
    const payload = {
      status: 'passed',
      mode: 'task-f-worker-session-check-only',
      createdAt: new Date().toISOString(),
      browser: 'BitBrowser 430',
      cdpEndpoint: endpoint,
      loginStatus: 'authenticated',
      requestedWorkers: workerCount,
      effectiveWorkers,
      independentAspNetSessions,
      uniqueAspNetSessionCount,
      workerSessionMode,
      adaptiveConcurrency: true,
      sharedSessionForcesSerial: true,
      requestedWorkersSatisfied: effectiveWorkers === targetWorkers,
      independentFormLoginAttempted,
      independentFormLoginSucceeded,
      concurrencyLimitedReason,
      workerLogins,
      sensitiveIccidSubmittedCount: 0,
      credentialsWrittenToReceipt: false,
      browserForegroundActivationAttempted: false,
    };
    await Promise.all(workers.filter(entry => entry.disposable).map(entry => entry.directApi.dispose().catch(() => {})));
    await atomicWriteJson(outputPath, payload);
    console.log(JSON.stringify({ ...payload, workerLogins: undefined, path: path.resolve(outputPath) }));
    return;
  }

  const results = new Array(items.length);
  const lookupStartedAt = Date.now();
  let progressWrite = Promise.resolve();
  let progressError = null;

  function progressPayload(event, status = 'running') {
    const completed = results.filter(Boolean);
    const progressStatusCounts = {};
    const samplesByStatus = {};
    for (const row of completed) {
      progressStatusCounts[row.status] = (progressStatusCounts[row.status] || 0) + 1;
      const samples = samplesByStatus[row.status] || (samplesByStatus[row.status] = []);
      if (samples.length < 10) {
        samples.push({
          phone: row.phone || '',
          queryIccid: row.queryIccid || '',
          purchaseDate: row.purchaseDateRaw || row.purchaseDate || '',
          actRequestDate: row.actRequestDate || '',
          activationDate: row.activationDate || '',
          simNumber: row.simNumber || '',
          sku: row.sku || '',
          workerId: row.workerId || 0,
          error: row.error || '',
        });
      }
    }
    return {
      event,
      status,
      ...runBinding,
      updatedAt: new Date().toISOString(),
      elapsedSeconds: Math.round((Date.now() - lookupStartedAt) / 1000),
      totalInputCount: items.length,
      completedCount: completed.length,
      remainingCount: items.length - completed.length,
      statusCounts: progressStatusCounts,
      samplesByStatus,
      requestedWorkers: workerCount,
      effectiveWorkers,
      independentAspNetSessions,
      uniqueAspNetSessionCount,
      workerSessionMode,
      perWorkerDelayMs: delayMs,
      progressIntervalSeconds: Math.round(progressIntervalMs / 1000),
      databaseMissingOnly: pendingPayload.reportBackfillMode !== true,
      databaseActivationMissingOnly: pendingPayload.reportBackfillMode === true,
      reportBackfillMode: pendingPayload.reportBackfillMode === true,
      cloudUploadAttempted: false,
      adaptiveConcurrency: true,
      sharedSessionForcesSerial: true,
      independentFormLoginAttempted,
      independentFormLoginSucceeded,
      progressPath,
    };
  }

  function queueProgress(event, status = 'running', publish = true) {
    const payload = progressPayload(event, status);
    progressWrite = progressWrite.then(async () => {
      await atomicWriteJson(progressPath, payload);
      if (publish) console.log(JSON.stringify(payload));
    });
    return progressWrite;
  }

  let progressTimer;
  let snapshotTimer;

  try {
    await queueProgress('task_f_lookup_started');
    progressTimer = setInterval(() => {
      void queueProgress('task_f_lookup_progress').catch(error => { progressError ||= error; });
    }, progressIntervalMs);
    // Local snapshots do not change the fixed five-minute publication clock.
    snapshotTimer = setInterval(() => {
      void queueProgress('task_f_lookup_snapshot', 'running', false).catch(error => { progressError ||= error; });
    }, LIVE_PROGRESS_SNAPSHOT_INTERVAL_MS);
    await runLookupWorkers(items, workers, {
      delayMs,
      onResult: (result, index) => { results[index] = result; },
      assertHealthy: () => { if (progressError) throw progressError; },
    });
  } finally {
    clearInterval(progressTimer);
    clearInterval(snapshotTimer);
    await Promise.all(workers.filter(entry => entry.disposable).map(entry => entry.directApi.dispose().catch(() => {})));
    await progressWrite;
  }

  let sharedSessionSerialConfirmationCount = 0;
  let sharedSessionSerialConfirmationFailureCount = 0;
  if (!independentAspNetSessions && effectiveWorkers > 1) {
    let confirmationHtml = session.response.text;
    for (let index = 0; index < results.length; index += 1) {
      if (results[index]?.status !== 'not_found') continue;
      const originalWorkerId = results[index].workerId;
      try {
        const confirmed = await queryOne(baseApi, items[index], 0, confirmationHtml);
        confirmationHtml = confirmed.reusableHtml;
        results[index] = {
          ...confirmed.row,
          workerId: originalWorkerId,
          sharedSessionSerialConfirmation: true,
          serialConfirmationWorkerId: 0,
        };
        sharedSessionSerialConfirmationCount += 1;
      } catch (error) {
        results[index] = {
          ...items[index],
          workerId: originalWorkerId,
          status: 'error',
          queriedAt: new Date().toISOString(),
          sharedSessionSerialConfirmation: false,
          error: `shared-session-not-found-confirmation:${error.message}`,
        };
        sharedSessionSerialConfirmationFailureCount += 1;
      }
      await new Promise(resolve => setTimeout(resolve, delayMs));
    }
  }

  const statusCounts = {};
  for (const row of results) statusCounts[row.status] = (statusCounts[row.status] || 0) + 1;
  const payload = {
    status: statusCounts.error ? 'passed_with_transient_errors' : 'passed',
    ...runBinding,
    createdAt: new Date().toISOString(),
    mode: independentLoginOnly
      ? 'task-f-grabandgo-read-only-webforms-independent-http-sessions'
      : 'task-f-grabandgo-read-only-webforms-bitbrowser-430',
    browser: independentLoginOnly
      ? 'BitBrowser 430 OpenSMS inventory + isolated local HTTP login containers'
      : 'BitBrowser 430',
    cdpEndpoint: endpoint,
    accountUrl: ACCOUNT_URL,
    customerSimUrl: CUSTOMER_SIM_URL,
    loginStatus: 'authenticated',
    accountFinalUrl: accountState.finalUrl,
    silentBrowserMode: true,
    browserForegroundActivationAttempted: false,
    backgroundTabCreated: opened.created,
    autoLoginAttempted: authenticated.recovery.attempted,
    autoLoginRecovered: authenticated.recovery.recovered,
    autoLoginAttempts: authenticated.recovery.attempts || 0,
    continueCurrentTaskAfterLogin: authenticated.recovery.continueCurrentTask === true,
    credentialsWrittenToReceipt: false,
    credentialAvailable: authenticated.recovery.credentialAvailable,
    credentialStore: authenticated.recovery.credentialStore,
    transportPriority: ['same-origin-http', 'background-page-javascript'],
    sameOriginHttpRequests: baseTelemetry.sameOriginHttpRequests
      + workers.reduce((sum, entry) => sum + entry.telemetry.sameOriginHttpRequests, 0),
    backgroundJavascriptFallbackRequests: baseTelemetry.backgroundJavascriptFallbackRequests
      + workers.reduce((sum, entry) => sum + entry.telemetry.backgroundJavascriptFallbackRequests, 0),
    requestedWorkers: workerCount,
    effectiveWorkers,
    independentAspNetSessions,
    browserSeedHadAspNetSessionCookie: sharedAspNetSessionCookie,
    uniqueAspNetSessionCount,
    workerSessionMode,
    sharedAspNetSessionConcurrentWorkers: !independentAspNetSessions && effectiveWorkers > 1,
    copiedBrowserCookiesToWorkerContexts: authenticatedClientContextsAvailable,
    concurrencyLimitedReason,
    adaptiveConcurrency: true,
    sharedSessionForcesSerial: true,
    requestedWorkersSatisfied: effectiveWorkers === targetWorkers,
    independentFormLoginAttempted,
    independentFormLoginSucceeded,
    strictIdentityGuard: 'queryIccid_equals_listSimNumber_equals_detailSimIccid',
    identityMismatchStoredAsTerminal: false,
    sharedSessionNotFoundRequiresSerialConfirmation: true,
    sharedSessionSerialConfirmationCount,
    sharedSessionSerialConfirmationFailureCount,
    perWorkerDelayMs: delayMs,
    strictDetailSimEqualityRequired: true,
    recordSchemaVersion: 2,
    recordedWebFields: ['Purchase Date', 'Act. Request Date', 'Activation Date', 'SIM Number', 'SKU'],
    fullSourceDataIncluded: true,
    inputCount: items.length,
    statusCounts,
    workerLogins,
    results,
  };
  await atomicWriteJson(outputPath, payload, { exclusive: true });
  await queueProgress('task_f_lookup_completed', payload.status);
  console.log(JSON.stringify({ ...payload, results: undefined, path: path.resolve(outputPath) }));
}

async function exitAfterFlushing(code) {
  // connectOverCDP keeps its websocket referenced even after all business work
  // has finished.  Do not call browser.close() here: that would close the
  // user's live BitBrowser 430 profile.  Flush both output streams, then end
  // only this short-lived helper process so the parent patrol runner can read
  // the receipt and continue (or fail cleanly on login_required).
  await Promise.all([
    new Promise(resolve => process.stdout.write('', resolve)),
    new Promise(resolve => process.stderr.write('', resolve)),
  ]);
  process.exit(code);
}

async function runCliWithSiteLock() {
  if (process.argv.includes('--self-test')) return main();
  const loginOnly = ['--open-login', '--check-login', '--check-independent-logins', '--debug-one']
    .some(option => process.argv.includes(option));
  if (!loginOnly && process.argv[2]) {
    const pending = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
    // Preserve empty-diff no-auth/no-browser/no-network semantics, even when
    // another job currently owns the browser lock.
    if (!(pending.rows || []).length) return main();
  }
  const lease = await acquireSiteLock({browserId: 'a53ce74e6c9e4fa08aec35f81ce1e2a3'});
  try { return await main(); } finally { lease.release(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) runCliWithSiteLock().then(
  () => exitAfterFlushing(Number(process.exitCode || 0)),
  async error => {
    console.error(error?.stack || String(error));
    await exitAfterFlushing(1);
  },
);
