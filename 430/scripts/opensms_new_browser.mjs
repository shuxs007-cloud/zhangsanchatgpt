// OpenSMS-only login reuse. Keychain-backed state, headless by default; no business writes.
import fs from 'node:fs/promises';
import fsSync from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { acquireSiteLock } from '/Users/zhangsan/Documents/数据分析/opensms_site_lock.mjs';
import { ensureOpenSmsSiteOwnership } from '../opensms_site_execution.mjs';
import { OpenSmsAuthStore, scopedAuthState, authStateFingerprint, authStateFromTargetCookies, AUTH_ORIGIN } from './opensms_browser_auth_store.mjs';
import {SHARED_BROWSER_NAME} from './opensms_shared_browser.mjs';

const script = fileURLToPath(import.meta.url);
const root = path.resolve(path.dirname(script), '..');
const directory = path.join(root, 'opensms_artifacts/state/new_browser');
const statusPath = path.join(directory, 'status.json');
const requestLock = path.join(root, 'opensms_artifacts/state/request_pacing/request.lock');
const reportDirectory = '/Users/zhangsan/.opensms/locks';
const reportLock = path.join(reportDirectory, 'opensms-new-browser-report.lock');
const startupFailurePath = path.join(directory, 'startup_failure.json');
const startupReadyPath = path.join(directory, 'startup_ready.json');
const browserId = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
const modulePath = '/Users/zhangsan/Documents/数据分析/cloakbrowser_free_test/node_modules/cloakbrowser/dist/index.js';
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const authStore = new OpenSmsAuthStore();
const runningStates = new Set(['awaiting-manual-login', 'ready', 'auth-check-failed', 'login-required']);

async function atomic(file, data) {
  const temp = `${file}.${randomUUID()}.tmp`;
  const handle = await fs.open(temp, 'wx', 0o600);
  try { await handle.writeFile(JSON.stringify(data)); await handle.sync(); }
  finally { await handle.close(); }
  await fs.rename(temp, file);
}
async function status() {
  let saved;
  try { saved = JSON.parse(await fs.readFile(statusPath, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return {status:'not-running'}; throw error; }
  if (runningStates.has(saved.status)) {
    try { process.kill(saved.pid, 0); }
    catch { return {...saved, status:'closed', authentication:'unknown'}; }
  }
  return {...saved,displayName:SHARED_BROWSER_NAME};
}
export async function launchBrowser({headless = true, launch, reservePort} = {}) {
  const port = reservePort ? await reservePort() : await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => { const value = server.address().port; server.close(() => resolve(value)); });
  });
  if (!launch) {
    // Use the same installed, free browser as the referenced local integration.
    // Keep the same browser/fingerprint; a 403 is never a reason to rotate it.
    delete process.env.CLOAKBROWSER_LICENSE_KEY;
    delete process.env.CLOAKBROWSER_RELEASE_CHANNEL;
    ({launch} = await import(pathToFileURL(modulePath).href));
  }
  const browser = await launch({headless, args:['--fingerprint=4302026',
    `--remote-debugging-port=${port}`, '--remote-debugging-address=127.0.0.1',
    '--disable-background-networking', '--start-minimized']});
  try {
    const response = await fetch(`http://127.0.0.1:${port}/json/version`, {signal:AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error('OPENSMS_NEW_BROWSER_CDP_UNAVAILABLE');
    const version = await response.json();
    const ws = new URL(version.webSocketDebuggerUrl);
    if (!['127.0.0.1','localhost'].includes(ws.hostname) || ws.port !== String(port)
        || !ws.pathname.startsWith('/devtools/browser/')) throw new Error('OPENSMS_NEW_BROWSER_CDP_IDENTITY_INVALID');
    return {browser, port, webSocketPath:ws.pathname, version:browser.version()};
  } catch (error) { await browser.close(); throw error; }
}

export function authFailureConfirmed(error) {
  return error?.code === 'OPENSMS_MANUAL_LOGIN_REQUIRED' || error?.message === 'OPENSMS_MANUAL_LOGIN_REQUIRED'
    || error?.status === 401;
}

export function shouldReuseHeadless(saved) {
  return saved?.status === 'ready' && saved.headless === true;
}

export function shouldReuseBrowser(saved) {
  return saved?.status === 'ready' && saved.authentication === 'verified-readonly-api';
}

// A live manual-login browser is adopted in place, not replaced to change its
// display mode. In particular, a missing disk cache is not expired live login.
export async function adoptExistingSession(saved, {
  verify = verifySession, cache = cacheVerifiedOpenSmsSession, persist = value => atomic(statusPath,value),
} = {}) {
  const verified = await verify(saved);
  try {await cache({...saved,authProbeTargetId:verified?.targetId});}
  catch (error) {
    // An authenticated API followed by a failed cache/context capture is not
    // proof of expiry. Never turn this local failure into a login popup.
    if (authFailureConfirmed(error)) throw new Error('OPENSMS_AUTH_CACHE_CONTEXT_UNCONFIRMED');
    throw error;
  }
  const adopted = {...saved, displayName:SHARED_BROWSER_NAME, status:'ready', authentication:'verified-readonly-api',
    authStore:'local-macOS-Keychain', lastVerifiedAt:new Date().toISOString(),
    browserReused:true, lastVerifiedTargetId:verified?.targetId || saved.lastVerifiedTargetId,
    manualLoginRequested:false, loginWindowShown:false};
  await persist(adopted);
  return adopted;
}

// The only foreground exception: the human explicitly requested a manual login
// window on confirmed expiry. No 403/transport/protocol error reaches this path.
export async function showLoginForConfirmedExpiry(saved, cause, {
  connect = async value => {await ensureOpenSmsSiteOwnership(); return exactBrowser(value);},
  persist = value => atomic(statusPath,value),
} = {}) {
  if (!authFailureConfirmed(cause)) throw new Error('OPENSMS_LOGIN_EXPIRY_UNCONFIRMED');
  const pending = {...saved,status:'login-required',authentication:'expired',manualLoginRequested:true};
  if (saved.headless === true) {
    await persist(pending);
    return {shown:false,requiresVisibleService:true,browserReused:true};
  }
  const browser = await connect(saved);
  try {
    const pages = browser.contexts().flatMap(context => context.pages()).filter(page => {
      try {return new URL(page.url()).origin === AUTH_ORIGIN;} catch {return false;}
    });
    const page = pages.find(value => new URL(value.url()).pathname === '/login') || pages[0];
    if (!page) throw new Error('OPENSMS_NEW_BROWSER_CONTEXT_UNCONFIRMED');
    if (!saved.loginWindowShown) {
      if (new URL(page.url()).pathname !== '/login') {
        const response = await page.goto(`${AUTH_ORIGIN}/login?redirect=%2Findex`,
          {waitUntil:'domcontentloaded',timeout:45000});
        if (!response || response.status() >= 400) throw Object.assign(new Error('OPENSMS_LOGIN_WINDOW_HTTP_FAILED'),{status:response?.status()});
      }
      const cdp = await browser.newBrowserCDPSession();
      try {
        const targets = await cdp.send('Target.getTargets');
        const candidates = targets.targetInfos.filter(target => target.type === 'page' && target.url === page.url());
        if (candidates.length !== 1) throw new Error('OPENSMS_LOGIN_WINDOW_TARGET_UNCONFIRMED');
        const window = await cdp.send('Browser.getWindowForTarget',{targetId:candidates[0].targetId});
        await cdp.send('Browser.setWindowBounds',{windowId:window.windowId,bounds:{windowState:'normal'}});
        await page.bringToFront();
      } finally {await cdp.detach();}
    }
    await persist({...pending,loginWindowShown:true});
    return {shown:true,requiresVisibleService:false,browserReused:true};
  } finally {await browser.close();}
}

async function authLease() {
  if (fsSync.existsSync(requestLock)) throw new Error('OPENSMS_PACING_REQUEST_UNCONFIRMED: 原请求锁未核验，不启动新浏览器联网');
  const lease = await acquireSiteLock({browserId, timeoutMs:0});
  if (lease.inherited) throw new Error('新浏览器服务必须取得自己的排他锁');
  let report;
  try {
    const owner = JSON.parse(await fs.readFile(lease.file, 'utf8'));
    report = await acquireSiteLock({browserId:'opensms-new-browser-report', timeoutMs:0, directory:reportDirectory});
    if (report.inherited) throw new Error('新浏览器服务不能继承导出锁');
    const reportOwner = JSON.parse(await fs.readFile(report.file, 'utf8'));
    // The second lease has its own token. Restore the website token explicitly.
    process.env.OPENSMS_SITE_LOCK_TOKEN = owner.token;
    process.env.OPENSMS_SITE_LOCK_PID = String(owner.pid);
    process.env.OPENSMS_SITE_JOB_LOCK_TOKEN = reportOwner.token;
    process.env.OPENSMS_SITE_JOB_LOCK_PID = String(reportOwner.pid);
    if (fsSync.existsSync(requestLock)) throw new Error('OPENSMS_PACING_REQUEST_UNCONFIRMED');
    return {owner, release() {
      report.release(); lease.release();
      delete process.env.OPENSMS_SITE_JOB_LOCK_TOKEN;
      delete process.env.OPENSMS_SITE_JOB_LOCK_PID;
    }};
  } catch (error) {report?.release(); lease.release(); throw error;}
}

function sessionEnvironment(saved) {
  return {OPENSMS_BROWSER_KIND:'cloakbrowser', BITBROWSER_CDP:`http://127.0.0.1:${saved.port}`,
    OPENSMS_EXACT_CDP_ONLY:'1', OPENSMS_FORCE_RAW_CDP:'1',
    OPENSMS_BROWSER_SESSION_ID:saved.sessionId, OPENSMS_BROWSER_SERVICE_PID:String(saved.pid),
    OPENSMS_EXPECTED_BROWSER_WS_PATH:saved.webSocketPath};
}

async function verifySession(saved) {
  Object.assign(process.env, sessionEnvironment(saved));
  const {probeExistingSession} = await import('./opensms_login_recovery_cli.mjs');
  let verified;
  if (!await probeExistingSession(undefined,value=>{verified=value;})) throw Object.assign(new Error('OPENSMS_MANUAL_LOGIN_REQUIRED'),
    {code:'OPENSMS_MANUAL_LOGIN_REQUIRED'});
  return verified;
}

async function exactBrowser(saved) {
  const identity = JSON.parse(await fs.readFile(path.join(directory, 'identity.json'), 'utf8'));
  if (identity.pid !== saved.pid || identity.browserSessionId !== saved.sessionId || !identity.token) {
    throw new Error('OPENSMS_NEW_BROWSER_SESSION_CHANGED');
  }
  process.kill(saved.pid, 0);
  const response = await fetch(`http://127.0.0.1:${saved.port}/json/version`, {signal:AbortSignal.timeout(5000)});
  if (!response.ok) throw new Error('OPENSMS_NEW_BROWSER_CDP_UNAVAILABLE');
  const version = await response.json();
  const ws = new URL(version.webSocketDebuggerUrl);
  if (ws.hostname !== '127.0.0.1' || ws.port !== String(saved.port) || ws.pathname !== saved.webSocketPath) {
    throw new Error('OPENSMS_NEW_BROWSER_CDP_IDENTITY_CHANGED');
  }
  const {createRequire} = await import('node:module');
  const require = createRequire(import.meta.url);
  const {chromium} = require('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
  return chromium.connectOverCDP(`http://127.0.0.1:${saved.port}`, {timeout:10000});
}

export async function cacheVerifiedOpenSmsSession(saved) {
  // connectOverCDP's exposed default context may list an incognito page while
  // returning an empty cookie jar. Read the exact authenticated target instead.
  const {connectRawOpenSmsPage} = await import('../opensms_protocol_client.mjs');
  const priorPin = process.env.OPENSMS_RAW_API_TARGET_ID;
  if (saved.authProbeTargetId) process.env.OPENSMS_RAW_API_TARGET_ID = saved.authProbeTargetId;
  let raw;
  try {
    raw = await connectRawOpenSmsPage({cdp:`http://127.0.0.1:${saved.port}`,pathName:'/day'});
    const info = (await raw.client.send('Target.getTargetInfo')).targetInfo;
    if (info.targetId !== raw.target.id || new URL(info.url).origin !== AUTH_ORIGIN
        || !info.browserContextId) throw new Error('OPENSMS_NEW_BROWSER_CONTEXT_UNCONFIRMED');
    const result = await raw.client.send('Network.getCookies',{urls:[AUTH_ORIGIN+'/',raw.target.url]});
    return await authStore.save(authStateFromTargetCookies(result.cookies));
  } finally {
    raw?.client.close();
    if (priorPin === undefined) delete process.env.OPENSMS_RAW_API_TARGET_ID;
    else process.env.OPENSMS_RAW_API_TARGET_ID = priorPin;
  }
}

async function closeIdleService(saved) {
  // All website/report/request guards are held or checked by the caller.
  // A PID alone is insufficient: check the exact service command too.
  const {execFileSync} = await import('node:child_process');
  const command = execFileSync('/bin/ps', ['-p',String(saved.pid),'-o','args='], {encoding:'utf8'}).trim();
  if (!command.includes(script) || !command.includes('--serve')) throw new Error('OPENSMS_NEW_BROWSER_SERVICE_IDENTITY_INVALID');
  process.kill(saved.pid, 'SIGTERM');
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    try {process.kill(saved.pid, 0);} catch (error) {if (error.code === 'ESRCH') return; throw error;}
    await delay(100);
  }
  throw new Error('OPENSMS_NEW_BROWSER_CLOSE_UNCONFIRMED');
}

async function serve({loginRequested = false} = {}) {
  let launched, context, current, tick;
  let stopping = false, transition = false;
  const started = Date.now();
  let remembered = '';
  let lastTickAt = Date.now(), automaticCheckDisabled = false;
  const publish = async (lease, headless, storageState) => {
    launched = await launchBrowser({headless});
    context = await launched.browser.newContext({storageState:storageState || {cookies:[],origins:[]}});
    const page = await context.newPage();
    const sessionId = randomUUID();
    current = {status:'awaiting-manual-login',browser:'cloakbrowser',displayName:SHARED_BROWSER_NAME,headless,
      pid:process.pid,sessionId,port:launched.port,webSocketPath:launched.webSocketPath,
      browserVersion:launched.version,startedAt:new Date().toISOString(),
      authentication:'unknown-until-original-runner-api-preflight',writes:0};
    await atomic(path.join(directory, 'identity.json'), {...lease.owner,browserSessionId:sessionId});
    await atomic(statusPath,current);
    const response = await page.goto(`${AUTH_ORIGIN}${headless ? '/index' : '/login?redirect=%2Findex'}`,
      {waitUntil:'domcontentloaded',timeout:45000});
    if (!response || response.status() >= 400) throw Object.assign(new Error(`OPENSMS_BROWSER_HTTP_${response?.status() ?? 'UNKNOWN'}`),
      {status:response?.status()});
    if (headless) {
      await verifySession(current);
      await authStore.save(await context.storageState());
      remembered = authStateFingerprint(await context.storageState());
      current = {...current,status:'ready',authentication:'verified-readonly-api',authStore:'local-macOS-Keychain'};
      await atomic(statusPath,current);
    } else {
      await showLoginForConfirmedExpiry(current,{code:'OPENSMS_MANUAL_LOGIN_REQUIRED'});
      current = {...current,status:'login-required',authentication:'expired',
        manualLoginRequested:true,loginWindowShown:true};
    }
    launched.browser.once('disconnected', () => {if (!transition) stopping = true;});
  };

  let lease = await authLease();
  try {
    const prior = await status();
    let restored;
    if (runningStates.has(prior.status)) {
      // Capture only an independently authenticated, exact owned session.
      try {
        await adoptExistingSession(prior);
        await atomic(startupReadyPath,{pid:process.pid,servicePid:prior.pid,sessionId:prior.sessionId,verified:true});
        return;
      } catch (error) {
        if (!authFailureConfirmed(error)) throw error;
        await authStore.invalidate(); restored = null;
        const window = await showLoginForConfirmedExpiry(prior,error);
        if (!window.requiresVisibleService) {
          await atomic(startupReadyPath,{pid:process.pid,servicePid:prior.pid,sessionId:prior.sessionId,manualLogin:true});
          return;
        }
        if (!loginRequested) throw error;
      }
      // Only a confirmed expired *headless* service needs a visible replacement.
      // Valid live browsers, including headed ones, never reach this close.
      await closeIdleService(prior);
    }
    restored = await authStore.load();
    try {await publish(lease, Boolean(restored), restored);}
    catch (error) {
      if (!authFailureConfirmed(error)) throw error;
      await authStore.invalidate();
      if (!loginRequested) {
        current = {...current,status:'login-required',authentication:'expired'};
        throw error;
      }
      transition = true; await launched?.browser.close(); transition = false;
      await publish(lease, false, null);
    }
    lease.release(); lease = null;
    process.once('SIGTERM', () => {stopping = true;});
    process.once('SIGINT', () => {stopping = true;});
    while (!stopping) {
      await delay(1000);
      if (stopping) break;
      if (automaticCheckDisabled || Date.now() - lastTickAt < 30000) continue;
      lastTickAt = Date.now();
      tick = (async () => {
        let storageState;
        try {storageState = scopedAuthState(await context.storageState());} catch {return;}
        if (authStateFingerprint(storageState) === remembered) return;
        let owned;
        try {owned = await authLease();} catch {return;} // Busy jobs are never interrupted.
        try {
          await verifySession(current);
          await authStore.save(storageState);
          remembered = authStateFingerprint(storageState);
          current = {...current,status:'ready',authentication:'verified-readonly-api',
            authStore:'local-macOS-Keychain',lastVerifiedAt:new Date().toISOString(),
            manualLoginRequested:false,loginWindowShown:false};
          await atomic(statusPath,current);
        } catch (error) {
          if (authFailureConfirmed(error)) {
            await authStore.invalidate();
            await showLoginForConfirmedExpiry(current,error);
            // Keep the owned process alive; a safe start/login action performs
            // the one necessary visible transition after all report locks free.
            automaticCheckDisabled = true;
          } else {
            // No automatic probe retry after a 403, protocol or transport error.
            // Keep a manual window intact; explicit action is required to retry.
            automaticCheckDisabled = true;
            current = {...current,status:'auth-check-failed',authentication:'unconfirmed-technical-error'};
            await atomic(statusPath,current);
          }
          // Technical errors never invalidate the cookie or open a browser.
        } finally {owned.release();}
      })();
      await tick; tick = null;
    }
  } finally {
    await tick?.catch(() => {});
    if (launched) {
      await launched.browser.close().catch(() => {});
      await atomic(statusPath, {status:current?.status === 'login-required' ? 'login-required' : 'closed',
        browser:'cloakbrowser',headless:current?.headless,pid:process.pid,sessionId:current?.sessionId,
        authentication:current?.authentication,endedAt:new Date().toISOString()});
    }
    lease?.release();
  }
}
async function main() {
  const action = process.argv[2];
  if (action === 'status') { console.log(JSON.stringify(await status())); return; }
  if (action === '--serve') { await serve({loginRequested:process.argv[3] === 'login'}); return; }
  if (!['start','background','login'].includes(action)) throw new Error('仅接受 start、background、login 或 status');
  const current = await status();
  if (fsSync.existsSync(requestLock)) throw new Error('OPENSMS_PACING_REQUEST_UNCONFIRMED: 先核验旧请求，不自动清锁');
  await fs.mkdir(directory, {recursive:true,mode:0o700});
  if (runningStates.has(current.status)) {
    const lease = await authLease();
    try {
      try {
        const adopted = await adoptExistingSession(current);
        if (action === 'background' && adopted.headless === false) {
          // A verified manual-login session is cached before the sole shared
          // service is closed; the replacement below starts headless from it.
          await closeIdleService(adopted);
        } else {
          console.log(JSON.stringify(adopted)); return;
        }
      }
      catch (error) {
        if (!authFailureConfirmed(error)) throw error;
        await authStore.invalidate();
        const window = await showLoginForConfirmedExpiry(current,error);
        if (!window.requiresVisibleService) {
          console.log(JSON.stringify({...await status(),loginWindow:window})); return;
        }
      }
    } finally {lease.release();}
  }
  const log = fsSync.openSync(path.join(directory, 'browser.log'), 'a', 0o600);
  const env = {...process.env};
  delete env.OPENSMS_SITE_LOCK_TOKEN; delete env.OPENSMS_SITE_LOCK_PID;
  delete env.OPENSMS_SITE_JOB_LOCK_TOKEN; delete env.OPENSMS_SITE_JOB_LOCK_PID;
  for (const key of Object.keys(env)) if (/^OPENSMS_FORCE_TASK_|^OPENSMS_ENABLE_TASK_|^OPENSMS_EXECUTE_TASK_/.test(key)) delete env[key];
  const child = spawn(process.execPath, [script, '--serve','login'], {cwd:root,env,detached:true,stdio:['ignore',log,log]});
  fsSync.closeSync(log); child.unref();
  for (let i=0; i<180; i++) {
    await delay(500);
    const value = await status();
    if (value.pid === child.pid && (shouldReuseBrowser(value) || (['awaiting-manual-login','login-required'].includes(value.status) && value.headless === false))) {
      console.log(JSON.stringify(value)); return;
    }
    try { process.kill(child.pid, 0); } catch {
      const failure = JSON.parse(await fs.readFile(startupFailurePath,'utf8').catch(()=>'{}'));
      if (failure.pid === child.pid) throw new Error(failure.error);
      const ready = JSON.parse(await fs.readFile(startupReadyPath,'utf8').catch(()=>'{}'));
      if (ready.pid === child.pid && ((ready.verified === true && shouldReuseBrowser(value)) || ready.manualLogin === true)
          && value.pid === ready.servicePid && value.sessionId === ready.sessionId) {
        console.log(JSON.stringify(value)); return;
      }
      throw new Error('OPENSMS_NEW_BROWSER_START_FAILED');
    }
  }
  throw new Error('新浏览器仍在启动；不自动重启');
}
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  main().catch(async error => {
    // Safe categories only. Neither the cookie nor the Keychain pipe is logged.
    const code = /^OPENSMS_[A-Z0-9_]+$/.test(error.message) ? error.message
      : /Timed out waiting for site lock/.test(error.message) ? 'OPENSMS_BROWSER_SITE_BUSY' : 'OPENSMS_NEW_BROWSER_START_FAILED';
    if (process.argv[2] === '--serve') await atomic(startupFailurePath,{pid:process.pid,error:code,createdAt:new Date().toISOString()}).catch(()=>{});
    console.log(JSON.stringify({status:'failed',error:code})); process.exitCode=2;
  });
}
