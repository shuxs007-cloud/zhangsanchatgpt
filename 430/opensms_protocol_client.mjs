import fssync from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import { createRequire } from 'node:module';
import { ensureBackgroundPageNavigation } from './opensms_navigation_guard.mjs';
import { retryableRead, withReadRetry, readRetryAfterMs } from './opensms_read_retry.mjs';
import { withOpenSmsRequestPacing, requestTimeouts } from './opensms_request_pacing.mjs';
import { loadPayloadCodec, loadPagePayloadCodec } from './opensms_payload_codec.mjs';
import { loadRawPayloadCodec } from './opensms_payload_build_recovery.mjs';
import { withBackgroundStartupRecovery } from './opensms_background_startup_recovery.mjs';
import { fetchExportStatusBounded } from './opensms_bounded_status_fetch.mjs';
import { validateOpenSmsApiResponse } from './opensms_api_response.mjs';
import { ensureOpenSmsSiteOwnership } from './opensms_site_execution.mjs';
import { configureOwnedHiddenViewport } from './opensms_hidden_viewport.mjs';

const require = createRequire(import.meta.url);

function loadPlaywright() {
  try {
    return require('playwright');
  } catch {}
  return require('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
}

const { chromium } = loadPlaywright();

export const API_BASE = 'https://adminsms.smsoug.com/api';
export const DEFAULT_CDP = process.env.BITBROWSER_CDP || 'http://127.0.0.1:52826';

const BROWSER_CACHE = '/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache';
const BITBROWSER_430_PROFILE_ID = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';

export class RawCdpClient {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.pending = new Map();
  }

  async connect() {
    await new Promise((resolve, reject) => {
      this.socket = new WebSocket(this.url);
      const timer = setTimeout(() => reject(new Error('CDP WebSocket connection timeout')), 10000);
      this.socket.addEventListener('open', () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
      this.socket.addEventListener('error', event => {
        clearTimeout(timer);
        reject(new Error(`CDP WebSocket error: ${event.message || 'unknown'}`));
      }, { once: true });
      this.socket.addEventListener('message', event => {
        const message = JSON.parse(event.data);
        if (!message.id) return;
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        clearTimeout(pending.timer);
        if (message.error) pending.reject(new Error(`CDP ${pending.method}: ${JSON.stringify(message.error)}`));
        else pending.resolve(message.result);
      });
      this.socket.addEventListener('close', () => {
        for (const pending of this.pending.values()) {
          clearTimeout(pending.timer);
          pending.reject(Object.assign(new Error(`CDP connection closed during ${pending.method}`), {
            code: 'CDP_CONNECTION_CLOSED', cdpMethod: pending.method,
          }));
        }
        this.pending.clear();
      });
    });
  }

  send(method, params = {}, timeoutMs = 90000) {
    if (['Page.bringToFront', 'Target.activateTarget', 'Browser.setWindowBounds'].includes(method)) {
      throw new Error(`静默模式禁止焦点或窗口操作: ${method}`);
    }
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(Object.assign(new Error(`CDP command timeout: ${method}`), {
          code: 'CDP_COMMAND_TIMEOUT', cdpMethod: method,
        }));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer, method });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression, timeoutMs = 90000) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: false,
    }, timeoutMs);
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    }
    return result.result?.value;
  }

  close() {
    this.socket?.close();
  }
}

async function readBrowserWebSocket(endpoint) {
  const response = await fetch(`${endpoint}/json/version`, { signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw new Error(`CDP browser version HTTP ${response.status}`);
  const version = await response.json();
  if (!version.webSocketDebuggerUrl) throw new Error('CDP browser websocket缺失');
  if (process.env.OPENSMS_BROWSER_KIND === 'cloakbrowser') {
    const expected = process.env.OPENSMS_EXPECTED_BROWSER_WS_PATH;
    const ws = new URL(version.webSocketDebuggerUrl), local = new URL(endpoint);
    if (!expected || ws.pathname !== expected || ws.hostname !== local.hostname || ws.port !== local.port) {
      throw new Error('OPENSMS_NEW_BROWSER_CDP_IDENTITY_CHANGED');
    }
  }
  return version.webSocketDebuggerUrl;
}

export function backgroundContextOptions(kind, info, existingId) {
  if (kind !== 'cloakbrowser') return {};
  // Playwright newPage() uses an isolated context. Hidden report targets must
  // use that SAME context; never copy cookies into a different browser/profile.
  if (info?.targetId !== existingId || typeof info?.browserContextId !== 'string' || !info.browserContextId) {
    throw new Error('OPENSMS_NEW_BROWSER_CONTEXT_UNCONFIRMED');
  }
  return {browserContextId:info.browserContextId};
}

export async function createRawBackgroundPage({ cdp = DEFAULT_CDP, url, authenticatedTargetId } = {}) {
  await ensureOpenSmsSiteOwnership();
  return withBackgroundStartupRecovery(() => createRawBackgroundPageAttempt({cdp, url, authenticatedTargetId}));
}

async function createRawBackgroundPageAttempt({ cdp, url, authenticatedTargetId }) {
  const endpoint = String(cdp || '').replace(/\/$/, '');
  if (!endpoint) throw new Error('缺少BitBrowser 430 CDP端点');
  const requestedUrl = new URL(String(url || ''), 'https://adminsms.smsoug.com').toString();
  if (!/^https:\/\/adminsms\.smsoug\.com(?:\/|$)/.test(requestedUrl)) {
    throw new Error(`后台页面不是OpenSMS同源地址: ${requestedUrl}`);
  }
  const browserClient = new RawCdpClient(await readBrowserWebSocket(endpoint));
  await browserClient.connect();
  let targetId = null;
  let pageClient = null;
  try {
    const protocolResponse = await fetch(`${endpoint}/json/protocol`, { signal: AbortSignal.timeout(5000) });
    if (!protocolResponse.ok) throw new Error('无法确认隐藏页面支持，拒绝创建普通标签页');
    const protocol = await protocolResponse.json();
    const create = protocol.domains?.find(d => d.domain === 'Target')?.commands?.find(c => c.name === 'createTarget');
    if (!create?.parameters?.some(p => p.name === 'hidden')) throw new Error('浏览器不支持隐藏页面，静默操作已停止');
    const existing = (await readRawTargets(endpoint)).find(t => t.type === 'page'
      && (!authenticatedTargetId || t.id === authenticatedTargetId)
      && /^https:\/\/adminsms\.smsoug\.com(?:\/|$)/.test(t.url));
    if (!existing) throw new Error('缺少现有OpenSMS页面，禁止创建新窗口');
    const contextOptions = process.env.OPENSMS_BROWSER_KIND === 'cloakbrowser'
      ? backgroundContextOptions('cloakbrowser', (await browserClient.send('Target.getTargetInfo', {targetId:existing.id}, 10000)).targetInfo, existing.id)
      : {};
    const created = await browserClient.send('Target.createTarget', {
      ...contextOptions,
      url: requestedUrl,
      background: true,
      hidden: true,
      // This BitBrowser kernel misinterprets explicit newWindow:false and
      // reports a context ID it cannot resolve. Use the exact profile endpoint
      // and protocol defaults; hidden:true itself forbids creating a window.
      forTab: false,
    }, 15000);
    targetId = created.targetId;
    if (!targetId) throw new Error('Target.createTarget未返回targetId');
    const deadline = Date.now() + 15000;
    let target = null;
    while (Date.now() < deadline) {
      const targets = await readRawTargets(endpoint);
      target = targets.find(item => item.id === targetId && item.webSocketDebuggerUrl) || null;
      if (target) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (!target) throw new Error(`无法取得后台页面target: ${targetId}`);
    const client = new RawCdpClient(target.webSocketDebuggerUrl);
    pageClient = client;
    await client.connect();
    const viewportReceipt = await configureOwnedHiddenViewport(client);
    // No event subscriptions are consumed by this raw client. Runtime/Page
    // enable unnecessarily wait on renderer startup; bounded evaluate below
    // verifies the real context instead. Never treat connection as readiness.
    let navigationReceipt;
    try {
      navigationReceipt = await ensureBackgroundPageNavigation(client, requestedUrl, { targetId });
    } catch (error) {
      client.close();
      throw error;
    }
    return {
      client,
      browserClient,
      endpoint,
      target,
      targetId,
      requestedUrl,
      navigationReceipt,
      viewportReceipt,
      background: true,
      hidden: true,
      close: async () => {
        client.close();
        try {
          const result = await browserClient.send('Target.closeTarget', { targetId }, 15000);
          if (!result?.success) throw new Error(`后台页面关闭未确认: ${targetId}`);
        } finally { browserClient.close(); }
      },
    };
  } catch (error) {
    pageClient?.close();
    if (targetId) {
      let closed = false;
      try { closed = (await browserClient.send('Target.closeTarget', { targetId }, 15000))?.success === true; } catch {}
      error.backgroundCleanup = { targetId, closed, status: closed ? 'confirmed_closed' : 'cleanup_unconfirmed' };
    }
    browserClient.close();
    throw error;
  }
}

export function encryptPayload(payload, codec) {
  if (!codec) throw new Error('缺少当前会话AES参数；禁止回退旧参数');
  return codec.encrypt(payload);
}

export function decryptPayload(payload, codec) {
  if (!codec) throw new Error('缺少当前会话AES参数；禁止回退旧参数');
  return codec.decrypt(payload);
}

export function parseResponseText(text, codec) {
  if (!codec) throw new Error('缺少当前会话AES参数；禁止回退旧参数');
  return codec.parse(text);
}

export function assertOk(label, result) {
  if (!result || result.code !== 200) throw new Error(`${label} failed: ${JSON.stringify(result)}`);
}

export function norm(value) {
  return String(value || '').replace(/\s+/g, '').toLowerCase();
}

export function carrierNorm(value) {
  return norm(value).replace(/-/g, '');
}

export function shortCardNo(phone) {
  const s = String(phone || '').replace(/\D/g, '');
  return s.length > 10 ? s.slice(-10) : s;
}

export function targetPhonesFromCandidate(candidate) {
  const rows = candidate.rows || [];
  return rows.map(r => String(r['电话'] || r.phone || '').trim()).filter(Boolean);
}

function readDevtoolsPorts() {
  const ports = [];
  try {
    for (const item of fssync.readdirSync(BROWSER_CACHE, { withFileTypes: true })) {
      if (!item.isDirectory()) continue;
      const portFile = path.join(BROWSER_CACHE, item.name, 'DevToolsActivePort');
      try {
        const port = fssync.readFileSync(portFile, 'utf8').split(/\r?\n/)[0].trim();
        if (port) ports.push({
          endpoint: `http://127.0.0.1:${port}`,
          port,
          portFile,
          profileId: item.name,
        });
      } catch {}
    }
  } catch {}
  return ports;
}

async function readRawTargets(endpoint) {
  const response = await fetch(`${endpoint}/json/list`, { signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw new Error(`CDP target list HTTP ${response.status}`);
  return response.json();
}

export function selectPinnedOpenSmsTarget(targets, pinnedId) {
  // Chromium exposes hidden pages as "other". Only the exact run-owned ID
  // may use that type; never broaden general discovery or fall back by URL.
  const pinned = targets.find(target => {
    if (target.id !== pinnedId || !['page', 'other'].includes(target.type) || !target.webSocketDebuggerUrl) return false;
    try { return new URL(target.url).origin === 'https://adminsms.smsoug.com'; } catch { return false; }
  });
  if (!pinned) throw new Error(`本轮专属API页面缺失或不再同源: ${pinnedId}`);
  return pinned;
}

export function selectExistingOpenSmsTarget(targets, pathName) {
  const pages = targets.filter(target => {
    if (target.type !== 'page' || !target.webSocketDebuggerUrl) return false;
    try { return new URL(target.url).origin === 'https://adminsms.smsoug.com'; }
    catch { return false; }
  });
  return pages.find(t => new URL(t.url).pathname === pathName)
    || pages.find(t => new URL(t.url).pathname === '/message/chatMessage')
    || pages.find(t => new URL(t.url).pathname === '/phoneConfig')
    || pages[0] || null;
}

async function findRawTarget(cdp, pathName) {
  if (process.env.OPENSMS_BROWSER_KIND === 'cloakbrowser') {
    if (!cdp || process.env.OPENSMS_EXACT_CDP_ONLY !== '1') throw new Error('OPENSMS_NEW_BROWSER_EXACT_ENDPOINT_REQUIRED');
    await readBrowserWebSocket(cdp);
  }
  const discovered = readDevtoolsPorts();
  const exact430 = discovered.find(item => item.profileId === BITBROWSER_430_PROFILE_ID)?.endpoint;
  const endpoints = process.env.OPENSMS_EXACT_CDP_ONLY === '1' || process.env.OPENSMS_RAW_API_TARGET_ID
    ? [cdp || exact430]
    : [cdp, exact430, ...discovered.map(item => item.endpoint)];
  const seen = new Set();
  const errors = [];
  for (const endpoint of endpoints) {
    if (!endpoint || seen.has(endpoint)) continue;
    seen.add(endpoint);
    try {
      const targets = await readRawTargets(endpoint);
      const pinnedId = process.env.OPENSMS_RAW_API_TARGET_ID;
      if (pinnedId) return {endpoint, target: selectPinnedOpenSmsTarget(targets, pinnedId)};
      const target = selectExistingOpenSmsTarget(targets, pathName);
      if (target) return { endpoint, target };
      errors.push({ endpoint, category: 'no_same_origin_page',
        pageCount: targets.filter(t => t.type === 'page').length });
    } catch (error) {
      errors.push({ endpoint, error: error.message });
    }
  }
  throw Object.assign(new Error(`无法定位430当前OpenSMS页面（不代表登录失效，请在430保留后台页面）: ${JSON.stringify(errors)}`),
    {code: 'OPENSMS_PAGE_TARGET_MISSING'});
}

export async function connectRawOpenSmsPage({ cdp = DEFAULT_CDP, pathName = '/phoneConfig' } = {}) {
  await ensureOpenSmsSiteOwnership();
  const { endpoint, target } = await findRawTarget(cdp, pathName);
  const client = new RawCdpClient(target.webSocketDebuggerUrl);
  await client.connect();
  // Callers perform bounded real reads; subscribing to execution-context events
  // adds a renderer-dependent handshake but no useful proof of readiness.
  return { client, endpoint, target };
}

export async function createRawOpenSmsApiSession({ cdp = DEFAULT_CDP, pathName = '/phoneConfig' } = {}) {
  const raw = await connectRawOpenSmsPage({ cdp, pathName });
  const { client, target } = raw;
  const adminBase = new URL(target.url).origin;
  let token = await client.evaluate(`(() => {
    const raw = document.cookie.split('; ').find(part => part.startsWith('Admin-Token='));
    return raw ? decodeURIComponent(raw.slice('Admin-Token='.length)) : '';
  })()`);
  if (!token) {
    const cookies = await client.send('Network.getCookies',{urls:[`${adminBase}/`,target.url]}).catch(() => ({ cookies: [] }));
    token = cookies.cookies?.find(cookie => cookie.name === 'Admin-Token' && cookie.domain.includes('adminsms.smsoug.com'))?.value || '';
  }
  if (!token) {
    client.close();
    throw new Error('未从430当前页面目标获取到 Admin-Token');
  }
  let payloadCodec, payloadBuildRecovery;
  try {
    const loaded = await loadRawPayloadCodec(raw, {cdp: raw.endpoint,
      createBackgroundPage: createRawBackgroundPage, withReadPacing: withOpenSmsRequestPacing});
    payloadCodec = loaded.codec;
    payloadBuildRecovery = loaded.recovery;
  }
  catch (error) { client.close(); throw error; }

  async function fetchFromPage(method, apiPath, data, searchParams, options) {
    const url = new URL(`${adminBase}/api${apiPath}`);
    for (const [key, value] of Object.entries(searchParams || {})) {
      if (value === undefined || value === null) continue;
      url.searchParams.set(key, String(value));
    }
    const body = data === undefined || data === null
      ? null
      : encryptPayload(JSON.stringify(data), payloadCodec);
    const authorization = token.startsWith('Bearer ') ? token : `Bearer ${token}`;
    const input = {
      requestUrl: url.toString(),
      requestMethod: method,
      requestBody: body,
      authorization,
      extraHeaders: options.headers || {},
      readTimeoutMs: requestTimeouts(method, apiPath, options).readTimeoutMs,
    };
    const snapshot = method === 'GET' && apiPath === '/adminExport/export4day/async/status'
      ? await fetchExportStatusBounded(client, input)
      : await client.evaluate(`(async () => {
      const input = ${JSON.stringify(input)};
      try {
      const response = await fetch(input.requestUrl, {
        method: input.requestMethod,
        credentials: 'include',
        ...(input.readTimeoutMs ? { signal: AbortSignal.timeout(input.readTimeoutMs) } : {}),
        headers: {
          Accept: 'application/json, text/plain, */*',
          Authorization: input.authorization,
          ...(input.requestBody === null ? {} : { 'Content-Type': 'text/plain' }),
          ...input.extraHeaders,
        },
        ...(input.requestBody === null ? {} : { body: input.requestBody }),
      });
      return { status: response.status, ok: response.ok, url: response.url,
        retryAfter: response.headers.get('Retry-After'), text: await response.text() };
      } catch (error) {
        // CDP often serializes a rejected DOMException as just "DOMException".
        // Keep its actual name inside this context, so only known read errors
        // can enter the existing allowlisted retry path. Never expose inputs.
        const name = String(error?.name || 'Error');
        const category = ['AbortError', 'TimeoutError', 'NetworkError'].includes(name)
          ? name : name === 'TypeError' && /Failed to fetch|fetch failed/i.test(String(error?.message))
            ? 'NetworkError' : 'PageFetchError';
        return {transportError: {name: category}};
      }
    })()`, requestTimeouts(method, apiPath, options).evaluationTimeoutMs);
    if (snapshot?.transportError) {
      const name = snapshot.transportError.name;
      throw Object.assign(new Error(`Page fetch ${name}`), {name});
    }
    return snapshot;
  }

  async function api(method, apiPath, data = null, searchParams = null, options = {}) {
    const result = await withReadRetry(() => withOpenSmsRequestPacing(async () => {
      const response = await fetchFromPage(method, apiPath, data, searchParams, options);
      return validateOpenSmsApiResponse(response, body => parseResponseText(body, payloadCodec), {method, apiPath});
    }), {
      method, apiPath, searchParams, targetId: target.id,
      onAttempt: event => { if (!event.success || event.attempt > 1) console.error(JSON.stringify({ readRequest: event })); },
    });
    return result;
  }

  return {
    ...raw,
    adminBase,
    apiBase: `${adminBase}/api`,
    token,
    api,
    apiTransport: 'raw-page-target-cdp',
    payloadProtocol: payloadCodec.metadata,
    payloadBuildRecovery,
    browser: { close: async () => client.close() },
  };
}

/**
 * Load one OpenSMS route inside an off-screen same-origin iframe and evaluate
 * a caller-supplied function inside that document.  This keeps the user's
 * visible tab untouched while avoiding Playwright's expensive browser-wide
 * connect handshake on BitBrowser profiles with many extension targets.
 */
export async function evaluateInRawHiddenSameOriginFrame(
  client,
  {
    pathName,
    label = 'opensms-raw',
    evaluator,
    timeoutMs = 60000,
  } = {},
) {
  const route = String(pathName || '').trim();
  if (!route.startsWith('/') || route.startsWith('//')) {
    throw new Error(`隐藏同源页面路径无效: ${route}`);
  }
  if (typeof evaluator !== 'function') {
    throw new Error('隐藏同源页面缺少求值函数');
  }
  const safeLabel = String(label).replace(/[^a-z0-9_-]/gi, '-').slice(0, 40) || 'opensms-raw';
  const frameId = `${safeLabel}-silent-${Date.now()}-${crypto.randomBytes(4).toString('hex')}`;
  const expression = `(async () => {
    const frameId = ${JSON.stringify(frameId)};
    const route = ${JSON.stringify(route)};
    const frame = document.createElement('iframe');
    frame.id = frameId;
    frame.name = frameId;
    frame.setAttribute('aria-hidden', 'true');
    frame.setAttribute('tabindex', '-1');
    frame.style.cssText = [
      'position:fixed!important',
      'left:-20000px!important',
      'top:0!important',
      'width:1920px!important',
      'height:1080px!important',
      'opacity:0!important',
      'pointer-events:none!important',
      'border:0!important',
      'z-index:-2147483648!important'
    ].join(';');
    const loaded = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('隐藏同源页面加载超时: ' + route)), 45000);
      frame.addEventListener('load', () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
    });
    document.documentElement.appendChild(frame);
    frame.src = new URL(route, location.origin).toString();
    try {
      await loaded;
      const frameWindow = frame.contentWindow;
      const frameDocument = frame.contentDocument;
      if (!frameWindow || !frameDocument) throw new Error('隐藏同源页面不可访问: ' + route);
      return await (${evaluator.toString()})(frameWindow, frameDocument);
    } finally {
      frame.remove();
    }
  })()`;
  return client.evaluate(expression, timeoutMs);
}

async function tryConnect(endpoint, pathName = '/phoneConfig') {
  // A dead/stalled browser endpoint otherwise consumes the full Playwright
  // 120-second timeout.  The lightweight HTTP probe rejects it in seconds.
  await readRawTargets(endpoint);
  const browser = await chromium.connectOverCDP(endpoint, { timeout: 120000 });
  const pages = browser.contexts().flatMap(context => context.pages());
  const adminPages = pages.filter(candidate => {
    try {
      return new URL(candidate.url()).hostname === 'adminsms.smsoug.com';
    } catch {
      return false;
    }
  });
  // A BitBrowser profile may retain an old /login tab beside the authenticated
  // app.  Never let enumeration order choose that stale tab for a hidden-frame
  // export/date operation.  Prefer the requested route, then any loaded app
  // route, and only keep /login as a last-resort diagnostic target.
  const route = candidate => new URL(candidate.url()).pathname;
  const page = adminPages.find(candidate => route(candidate) === pathName && route(candidate) !== '/login')
    || adminPages.find(candidate => route(candidate) === '/phoneConfig')
    || adminPages.find(candidate => route(candidate) === '/index')
    || adminPages.find(candidate => route(candidate) !== '/login' && route(candidate) !== '/')
    || adminPages.find(candidate => route(candidate) !== '/login')
    || adminPages[0];
  if (!page) {
    await browser.close().catch(() => {});
    return null;
  }
  return { browser, context: page.context(), page, endpoint };
}

export async function connectOpenSmsBrowser(cdp = DEFAULT_CDP, pathName = '/phoneConfig') {
  await ensureOpenSmsSiteOwnership();
  const discovered = readDevtoolsPorts();
  const exact430 = discovered.find(item => item.profileId === BITBROWSER_430_PROFILE_ID)?.endpoint;
  const endpoints = process.env.OPENSMS_EXACT_CDP_ONLY === '1'
    ? [cdp || exact430]
    : [cdp, exact430, ...discovered.map(item => item.endpoint)];
  const seen = new Set();
  const errors = [];
  for (const endpoint of endpoints) {
    if (!endpoint || seen.has(endpoint)) continue;
    seen.add(endpoint);
    try {
      const hit = await tryConnect(endpoint, pathName);
      if (hit) return hit;
    } catch (error) {
      errors.push({ endpoint, error: error.message });
    }
  }
  throw new Error(`无法连接已登录 OpenSMS BitBrowser 会话: ${JSON.stringify(errors)}`);
}

export async function createSilentSameOriginFrame(hostPage, label = 'opensms') {
  const safeLabel = String(label).replace(/[^a-z0-9_-]/gi, '-').slice(0, 40) || 'opensms';
  const frameId = `${safeLabel}-silent-${Date.now()}-${crypto.randomBytes(4).toString('hex')}`;
  const handle = await hostPage.evaluateHandle(id => {
    const frame = document.createElement('iframe');
    frame.id = id;
    frame.name = id;
    frame.setAttribute('aria-hidden', 'true');
    frame.setAttribute('tabindex', '-1');
    frame.style.cssText = [
      'position:fixed!important',
      'left:-20000px!important',
      'top:0!important',
      'width:1920px!important',
      'height:1080px!important',
      'opacity:0!important',
      'pointer-events:none!important',
      'border:0!important',
      'z-index:-2147483648!important',
    ].join(';');
    document.documentElement.appendChild(frame);
    return frame;
  }, frameId);
  const element = handle.asElement();
  const frame = await element?.contentFrame();
  await handle.dispose().catch(() => {});
  if (!frame) {
    await hostPage.evaluate(id => document.getElementById(id)?.remove(), frameId).catch(() => {});
    throw new Error('无法建立OpenSMS隐藏同源页面；静默模式已停止，未创建浏览器标签');
  }
  return {
    frame,
    frameId,
    cleanup: async () => {
      await hostPage.evaluate(id => document.getElementById(id)?.remove(), frameId).catch(() => {});
    },
  };
}

export async function createOpenSmsApiSession({ cdp = DEFAULT_CDP, pathName = '/phoneConfig' } = {}) {
  if (process.env.OPENSMS_FORCE_RAW_CDP === '1') {
    return createRawOpenSmsApiSession({ cdp, pathName });
  }
  const session = await connectOpenSmsBrowser(cdp, pathName);
  const { browser, context, page } = session;
  const current = page.url();
  const adminBase = /adminsms\.smsoug\.com/.test(current)
    ? new URL(current).origin
    : 'https://adminsms.smsoug.com';
  if (!page.url().startsWith(adminBase)) throw new Error('当前430会话没有可静默复用的OpenSMS同源页面');

  async function readAdminToken() {
    const pageToken = await page.evaluate(() => {
      const raw = document.cookie.split('; ').find(part => part.startsWith('Admin-Token='));
      return raw ? decodeURIComponent(raw.slice('Admin-Token='.length)) : '';
    }).catch(() => '');
    const cookies = await context.cookies([adminBase, `${adminBase}/`, 'https://adminsms.smsoug.com']);
    const tokenCookie = cookies.find(cookie => cookie.name === 'Admin-Token' && cookie.domain.includes('adminsms.smsoug.com'))
      || cookies.find(cookie => cookie.name === 'Admin-Token');
    return pageToken || tokenCookie?.value || '';
  }

  const token = await readAdminToken();
  if (!token) throw new Error('未获取到 Admin-Token');
  let payloadCodec;
  try { payloadCodec = await loadPagePayloadCodec(page); }
  catch (error) { await browser.close().catch(() => {}); throw error; }

  /*
   * Use the admin page's same-origin API proxy from the real browser page.
   * Browser-context request clients can be challenged independently by
   * Cloudflare even while the visible 430 session is valid.
   */
  async function fetchFromAdminPage(method, apiPath, data, searchParams, options) {
    const url = new URL(`${adminBase}/api${apiPath}`);
    for (const [key, value] of Object.entries(searchParams || {})) {
      if (value === undefined || value === null) continue;
      url.searchParams.set(key, String(value));
    }
    const body = data === undefined || data === null
      ? null
      : encryptPayload(JSON.stringify(data), payloadCodec);
    const authorization = token.startsWith('Bearer ') ? token : `Bearer ${token}`;
    return page.evaluate(async ({ requestUrl, requestMethod, requestBody, authorization, extraHeaders }) => {
      const response = await fetch(requestUrl, {
        method: requestMethod,
        credentials: 'include',
        headers: {
          Accept: 'application/json, text/plain, */*',
          Authorization: authorization,
          ...(requestBody === null ? {} : { 'Content-Type': 'text/plain' }),
          ...extraHeaders,
        },
        ...(requestBody === null ? {} : { body: requestBody }),
      });
      return {
        status: response.status,
        ok: response.ok,
        url: response.url,
        retryAfter: response.headers.get('Retry-After'),
        text: await response.text(),
      };
    }, {
      requestUrl: url.toString(),
      requestMethod: method,
      requestBody: body,
      authorization,
      extraHeaders: options.headers || {},
    });
  }

  async function api(method, apiPath, data = null, searchParams = null, options = {}) {
    return withReadRetry(() => withOpenSmsRequestPacing(async () => {
      const response = await fetchFromAdminPage(method, apiPath, data, searchParams, options);
      return validateOpenSmsApiResponse(response, body => parseResponseText(body, payloadCodec), {method, apiPath});
    }), {
      method, apiPath, searchParams,
      onAttempt: event => { if (!event.success || event.attempt > 1) console.error(JSON.stringify({readRequest:event})); },
    });
  }

  return { ...session, adminBase, apiBase: `${adminBase}/api`, token, api, apiTransport: 'page-origin-proxy', payloadProtocol: payloadCodec.metadata };
}

export function findCarrier(carriers, query) {
  const q = carrierNorm(query);
  return carriers.find(row => carrierNorm(row.name || row.carrierName) === q)
    || carriers.find(row => carrierNorm(row.name || row.carrierName).startsWith(q))
    || carriers.find(row => carrierNorm(row.name || row.carrierName).includes(q));
}

export async function getCarrierConfigs(api) {
  const rows = [];
  const pageSize = 20;
  for (let pageNum = 1; pageNum <= 100; pageNum += 1) {
    const resp = await api('GET', '/carrier/config/listNotDeleted', null, { pageNum, pageSize });
    assertOk('读取运营商配置', resp);
    const pageRows = resp.rows || [];
    rows.push(...pageRows);
    const total = Number(resp.total || 0);
    if (!pageRows.length || rows.length >= total || pageRows.length < pageSize) break;
  }
  return rows;
}

export async function listPhonesByNumbers(api, phones, pageSize = 500) {
  const cardNos = phones.map(shortCardNo).filter(Boolean);
  const queryParams = {
    cardNo: cardNos.join('\n'),
    pageNum: 1,
    pageSize: Math.max(pageSize, cardNos.length || 1),
  };
  const resp = await api('POST', '/phone/config/list', queryParams, {
    pageNum: 1,
    pageSize: Math.max(pageSize, cardNos.length || 1),
  });
  assertOk('查询号码配置', resp);
  return {
    total: Number(resp.total || 0),
    rows: resp.rows || [],
    cardNos,
  };
}

export async function updatePhoneCarrier(api, ids, carrireIdentif) {
  const resp = await api('PUT', '/phone/config/updateCarrireIdentif', {
    ids,
    carrireIdentif,
  });
  assertOk('批量修改运营商', resp);
  return resp;
}

export function compactPhoneRow(row) {
  return {
    id: row.id,
    cardNo: row.cardNo,
    phone: row.phone || row.cardPhone || row.tel,
    carrierName: row.carrierName || row.carrireName || row.carrier,
    carrireIdentif: row.carrireIdentif,
    slot: row.slot || row.cardSlot || row.slotIdentif,
    opr: row.opr,
    userAble: row.userAble,
    status: row.status,
  };
}
