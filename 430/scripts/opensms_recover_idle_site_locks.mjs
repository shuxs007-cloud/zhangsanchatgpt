// Explicit maintenance for an idle recorded browser, never automatic lock stealing.
import fs from 'node:fs';
import path from 'node:path';
import {createHash, randomUUID} from 'node:crypto';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {acquireSiteLock} from '/Users/zhangsan/Documents/数据分析/opensms_site_lock.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const browserId = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const check = (ok, reason) => {if (!ok) throw new Error(reason);};
function alive(pid) {
  check(Number.isSafeInteger(pid) && pid > 0, 'Invalid PID');
  try {process.kill(pid, 0); return true;}
  catch (error) {if (error.code === 'ESRCH') return false; throw error;}
}
function snapshot(file) {
  const stat = fs.lstatSync(file);
  check(stat.isFile() && !stat.isSymbolicLink(), 'Unexpected lock file type');
  const bytes = fs.readFileSync(file);
  return {file, stat, bytes, sha256:digest(bytes), owner:JSON.parse(bytes)};
}
function unchanged(item) {
  const next = snapshot(item.file);
  check(next.stat.ino === item.stat.ino && next.stat.dev === item.stat.dev
    && next.sha256 === item.sha256, 'Lock changed; refusing recovery');
}
function save(file, bytes) {
  const fd = fs.openSync(file, 'wx', 0o600);
  try {fs.writeFileSync(fd, bytes); fs.fsyncSync(fd);} finally {fs.closeSync(fd);}
  check(digest(fs.readFileSync(file)) === digest(bytes), 'Evidence copy failed');
}
async function localJson(endpoint, suffix) {
  const response = await fetch(endpoint + suffix, {signal:AbortSignal.timeout(5000)});
  check(response.ok, 'Local browser metadata unavailable');
  return response.json();
}

export async function recoverIdleSiteLocks(options, deps = {}) {
  check(options.authorizationSource === 'current-user', 'Explicit current-user authorization required');
  for (const key of ['siteSha256', 'reportSha256'])
    check(/^[a-f0-9]{64}$/.test(options[key] || ''), 'Exact original lock hashes required');
  const directory = deps.lockDirectory || '/Users/zhangsan/.opensms/locks';
  const request = deps.requestLock || path.join(root, 'opensms_artifacts/state/request_pacing/request.lock');
  const stateDirectory = deps.stateDirectory || path.join(root, 'opensms_artifacts/state/new_browser');
  const evidenceBase = deps.evidenceBase || path.join(root, 'opensms_artifacts/maintenance/idle_site_locks');
  const isAlive = deps.alive || alive, query = deps.localJson || localJson;
  const acquire = deps.acquire || (key => acquireSiteLock({browserId:key, directory, timeoutMs:0}));
  const site = snapshot(path.join(directory, `${browserId}.lock`));
  const report = snapshot(path.join(directory, 'opensms-new-browser-report.lock'));
  check(site.sha256 === options.siteSha256 && report.sha256 === options.reportSha256, 'Original hash mismatch');
  check(site.owner.pid === report.owner.pid && site.owner.token && report.owner.token,
    'Original lock ownership differs');
  const idle = () => {
    check(!isAlive(site.owner.pid), 'Original owner still alive');
    check(!fs.existsSync(request), 'Request lane present; separate recovery required');
  };
  idle();
  const statusFile = path.join(stateDirectory, 'status.json');
  const identityFile = path.join(stateDirectory, 'identity.json');
  const statusBytes = fs.readFileSync(statusFile), identityBytes = fs.readFileSync(identityFile);
  const status = JSON.parse(statusBytes), identity = JSON.parse(identityBytes);
  check(status.status === 'ready' && status.headless === true && isAlive(status.pid)
    && status.authentication === 'verified-readonly-api' && identity.pid === status.pid
    && identity.browserSessionId === status.sessionId && identity.token,
    'Recorded headless browser identity unconfirmed');
  check(Number.isSafeInteger(status.port) && status.port > 0 && status.port < 65536,
    'Invalid local browser port');
  const endpoint = `http://127.0.0.1:${status.port}`;
  const verifyBrowser = async () => {
    const version = await query(endpoint, '/json/version');
    const ws = new URL(version.webSocketDebuggerUrl);
    check(ws.hostname === '127.0.0.1' && ws.port === String(status.port)
      && ws.pathname === status.webSocketPath, 'Browser endpoint changed');
    const targets = await query(endpoint, '/json/list');
    check(targets.length === 1 && targets[0].type === 'page'
      && targets[0].id === status.lastVerifiedTargetId
      && targets[0].url === 'https://adminsms.smsoug.com/index',
      'Browser is not idle: unconfirmed target remains');
    check(fs.readFileSync(statusFile).equals(statusBytes)
      && fs.readFileSync(identityFile).equals(identityBytes), 'Browser state changed');
    idle();
    return {targetId:targets[0].id, targetCount:targets.length, idle:true};
  };
  const browser = await verifyBrowser();
  const evidence = path.join(evidenceBase, `${new Date().toISOString().replace(/[:.]/g, '-')}-${randomUUID()}`);
  fs.mkdirSync(evidence, {recursive:true, mode:0o700});
  for (const item of [site, report]) save(path.join(evidence, path.basename(item.file) + '.evidence'), item.bytes);
  const result = {status:'prepared', authorizationSource:'current-user', originalPid:site.owner.pid,
    originalSiteSha256:site.sha256, originalReportSha256:report.sha256,
    browser, businessRequests:0, businessReplay:false, browserRestarted:false, evidenceDirectory:evidence};
  save(path.join(evidence, 'preflight.json'), JSON.stringify(result, null, 2));
  let siteLease, reportLease;
  try {
    // All asynchronous checks precede this synchronous compare/archive pair.
    // The old path is moved, never unlinked. A competing owner is never removed.
    idle(); unchanged(site); unchanged(report);
    fs.renameSync(site.file, path.join(evidence, 'preserved-site.lock'));
    siteLease = await acquire(browserId);
    check(!siteLease.inherited, 'Maintenance cannot inherit a lease');
    idle(); unchanged(report);
    fs.renameSync(report.file, path.join(evidence, 'preserved-report.lock'));
    reportLease = await acquire('opensms-new-browser-report');
    check(!reportLease.inherited, 'Maintenance cannot inherit a report lease');
    result.browser = await verifyBrowser();
    result.status = 'recovered';
    save(path.join(evidence, 'result.json'), JSON.stringify(result, null, 2));
    return result;
  } catch (error) {
    save(path.join(evidence, 'failure.json'), JSON.stringify({...result, status:'failed', error:error.message}, null, 2));
    throw error;
  } finally {
    try {reportLease?.release();} finally {siteLease?.release();}
  }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  const args = process.argv.slice(2);
  const flag = name => args[args.indexOf(name) + 1];
  recoverIdleSiteLocks({authorizationSource:flag('--authorization-source'),
    siteSha256:flag('--site-sha256'), reportSha256:flag('--report-sha256')})
    .then(result => console.log(JSON.stringify(result)))
    .catch(error => {console.log(JSON.stringify({status:'failed', error:error.message})); process.exitCode=2;});
}
