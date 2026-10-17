// One recorded browser/context for A, binding and device maintenance. No launch,
// credential import, page creation, foregrounding or business request here.
import fs from 'node:fs/promises';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {acquireSiteLock} from '/Users/zhangsan/Documents/数据分析/opensms_site_lock.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const SHARED_BROWSER_NAME = 'OpenSMS 共用浏览器';
export const SHARED_BROWSER_ID = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
const stateDirectory = path.join(root, 'opensms_artifacts/state/new_browser');
const requestLock = path.join(root, 'opensms_artifacts/state/request_pacing/request.lock');
const origin = 'https://adminsms.smsoug.com';

export function validateSharedSession(saved, owner) {
  if (saved?.browser !== 'cloakbrowser' || saved.status !== 'ready'
      || saved.authentication !== 'verified-readonly-api'
      || !Number.isInteger(saved.pid) || saved.pid <= 0
      || !Number.isInteger(saved.port) || saved.port < 1 || saved.port > 65535
      || !/^[0-9a-f-]{36}$/.test(saved.sessionId || '')
      || !/^\/devtools\/browser\/[0-9a-f-]+$/.test(saved.webSocketPath || '')
      || !saved.lastVerifiedTargetId || owner?.pid !== saved.pid
      || owner.browserSessionId !== saved.sessionId || !owner.token) {
    throw new Error('OPENSMS_SHARED_BROWSER_UNCONFIRMED');
  }
  return {...saved, displayName:SHARED_BROWSER_NAME,
    cdp:`http://127.0.0.1:${saved.port}`};
}

export function sharedSessionEnvironment(session) {
  return {OPENSMS_BROWSER_KIND:'cloakbrowser', BITBROWSER_CDP:session.cdp,
    OPENSMS_CDP_PORT:String(session.port), OPENSMS_EXACT_CDP_ONLY:'1',
    OPENSMS_FORCE_RAW_CDP:'1', OPENSMS_SILENT_BROWSER:'1',
    OPENSMS_BROWSER_SESSION_ID:session.sessionId,
    OPENSMS_BROWSER_SERVICE_PID:String(session.pid),
    OPENSMS_EXPECTED_BROWSER_WS_PATH:session.webSocketPath,
    OPENSMS_RAW_API_TARGET_ID:session.lastVerifiedTargetId,
    OPENSMS_BINDING_TARGET_ID:session.lastVerifiedTargetId};
}

export async function acquireSharedOpenSmsSession({timeoutMs=7200000,
  acquire=acquireSiteLock, readFile=fs.readFile, fetcher=fetch,
  now=Date.now,
  processCheck=pid=>process.kill(pid,0), commandForPid=pid=>execFileSync(
    '/bin/ps',['-p',String(pid),'-o','args='],{encoding:'utf8'}).trim(),
  requestCheck=async()=>{try {await fs.access(requestLock); return true;}
    catch(error) {if(error.code==='ENOENT') return false; throw error;}},
}={}) {
  const environmentKeys=[...Object.keys(sharedSessionEnvironment({})),
    'OPENSMS_SITE_LOCK_TOKEN','OPENSMS_SITE_LOCK_PID',
    'OPENSMS_SITE_JOB_LOCK_TOKEN','OPENSMS_SITE_JOB_LOCK_PID'];
  const originalEnvironment=Object.fromEntries(environmentKeys.map(key=>[key,process.env[key]]));
  const restoreEnvironment=()=>{for(const [key,value] of Object.entries(originalEnvironment)) {
    if(value===undefined) delete process.env[key];else process.env[key]=value;
  }};
  const waitStarted = now();
  const lease = await acquire({browserId:SHARED_BROWSER_ID,timeoutMs});
  let report;
  try {
    if (await requestCheck()) throw new Error('OPENSMS_PACING_REQUEST_UNCONFIRMED');
    const siteOwner=JSON.parse(await readFile(lease.file,'utf8'));
    if(!siteOwner.token || !Number.isInteger(siteOwner.pid)) throw new Error('OPENSMS_SHARED_SITE_OWNER_UNCONFIRMED');
    // The report lease uses its own inherited credentials. It must not replace
    // the website token or deadlock a binding child against its parent.
    process.env.OPENSMS_SITE_LOCK_TOKEN=originalEnvironment.OPENSMS_SITE_JOB_LOCK_TOKEN||'';
    process.env.OPENSMS_SITE_LOCK_PID=originalEnvironment.OPENSMS_SITE_JOB_LOCK_PID||'';
    const remainingMs = timeoutMs === 0 ? 0 : timeoutMs - (now() - waitStarted);
    if (timeoutMs !== 0 && remainingMs <= 0) throw new Error('OPENSMS_SHARED_SESSION_WAIT_TIMEOUT');
    try {report=await acquire({browserId:'opensms-new-browser-report',timeoutMs:remainingMs});}
    finally {
      process.env.OPENSMS_SITE_LOCK_TOKEN=siteOwner.token;
      process.env.OPENSMS_SITE_LOCK_PID=String(siteOwner.pid);
    }
    const reportOwner=JSON.parse(await readFile(report.file,'utf8'));
    if(!reportOwner.token || !Number.isInteger(reportOwner.pid)) throw new Error('OPENSMS_SHARED_REPORT_OWNER_UNCONFIRMED');
    process.env.OPENSMS_SITE_JOB_LOCK_TOKEN=reportOwner.token;
    process.env.OPENSMS_SITE_JOB_LOCK_PID=String(reportOwner.pid);
    const saved = JSON.parse(await readFile(path.join(stateDirectory,'status.json'),'utf8'));
    const owner = JSON.parse(await readFile(path.join(stateDirectory,'identity.json'),'utf8'));
    const session = validateSharedSession(saved,owner);
    processCheck(session.pid);
    const command = commandForPid(session.pid);
    if (!command.includes(path.join(root,'scripts/opensms_new_browser.mjs'))
        || !command.includes('--serve')) throw new Error('OPENSMS_SHARED_BROWSER_SERVICE_CHANGED');
    const versionResponse = await fetcher(`${session.cdp}/json/version`,{signal:AbortSignal.timeout(5000)});
    if (!versionResponse.ok) throw new Error('OPENSMS_SHARED_BROWSER_CDP_UNAVAILABLE');
    const version = await versionResponse.json();
    const ws = new URL(version.webSocketDebuggerUrl);
    if (ws.hostname!=='127.0.0.1' || ws.port!==String(session.port)
        || ws.pathname!==session.webSocketPath) throw new Error('OPENSMS_SHARED_BROWSER_IDENTITY_CHANGED');
    const targetsResponse = await fetcher(`${session.cdp}/json/list`,{signal:AbortSignal.timeout(5000)});
    if (!targetsResponse.ok) throw new Error('OPENSMS_SHARED_BROWSER_TARGET_UNAVAILABLE');
    const targets = await targetsResponse.json();
    const target = targets.find(t=>t.id===session.lastVerifiedTargetId && t.type==='page');
    if (!target || new URL(target.url).origin!==origin || new URL(target.url).pathname==='/login') {
      throw new Error('OPENSMS_SHARED_BROWSER_TARGET_CHANGED');
    }
    const targetWs = new URL(target.webSocketDebuggerUrl);
    if (targetWs.hostname!=='127.0.0.1' || targetWs.port!==String(session.port)
        || targetWs.pathname!==`/devtools/page/${target.id}`) throw new Error('OPENSMS_SHARED_BROWSER_TARGET_IDENTITY_CHANGED');
    for (const [key,value] of Object.entries(sharedSessionEnvironment(session))) {
      process.env[key]=value;
    }
    let released=false;
    return {session,target,release(){
      if(released) return; released=true;
      try {report.release();} finally {lease.release();restoreEnvironment();}
    }};
  } catch(error) {
    try {report?.release();} finally {lease.release();restoreEnvironment();}
    throw error;
  }
}
