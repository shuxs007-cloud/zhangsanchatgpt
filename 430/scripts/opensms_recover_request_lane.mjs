// Explicitly authorized maintenance only. Never retries or submits business.
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { acquireSiteLock } from '/Users/zhangsan/Documents/数据分析/opensms_site_lock.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const requestLock = path.join(root, 'opensms_artifacts/state/request_pacing/request.lock');
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const check = (condition, reason) => { if (!condition) throw new Error(reason); };
function processAlive(pid) {
  check(Number.isSafeInteger(pid) && pid > 0, 'Invalid process identity');
  try { process.kill(pid, 0); return true; }
  catch (error) { if (error.code === 'ESRCH') return false; throw error; }
}
async function exclusiveCopy(file, bytes) {
  const handle = await fs.open(file, 'wx', 0o600);
  try { await handle.writeFile(bytes); await handle.sync(); }
  finally { await handle.close(); }
  check(sha256(await fs.readFile(file)) === sha256(bytes), 'Evidence copy verification failed');
}
async function closeOwnedPage({ endpoint, runId, receiptPath, environment }) {
  const result = await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(root, 'scripts/opensms_api_target_guard_cli.mjs'),
      'close', '--run-id', runId, '--receipt', receiptPath], {
      cwd: root, env: { ...process.env, ...environment, BITBROWSER_CDP: endpoint },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let out = '';
    const timer = setTimeout(() => { child.kill('SIGTERM'); reject(new Error('Owned-page cleanup timeout; lane retained')); }, 40000);
    child.stdout.on('data', data => { out += data; });
    child.stderr.resume(); // Never expose arbitrary browser output or credentials.
    child.once('error', error => { clearTimeout(timer); reject(error); });
    child.once('close', code => {
      clearTimeout(timer);
      if (code !== 0) { reject(new Error('Owned-page cleanup failed; lane retained')); return; }
      try { resolve(JSON.parse(out)); } catch { reject(new Error('Invalid cleanup confirmation; lane retained')); }
    });
  });
  check(result.success === true && result.closed === true, 'Owned page not confirmed closed');
  return { success: true, closed: true, alreadyClosed: result.alreadyClosed === true, targetId: result.targetId };
}

export async function recoverRequestLane(options, dependencies = {}) {
  check(options.authorizationSource === 'current-user', 'Explicit current-user maintenance authorization required');
  check(/^[0-9]{8}T[0-9]{6}\+0800$/.test(options.runId || ''), 'Exact original runId required');
  for (const key of ['stateSha256', 'receiptSha256', 'ownerSha256'])
    check(/^[a-f0-9]{64}$/.test(options[key] || ''), `Exact ${key} required`);
  for (const key of ['statePath', 'receiptPath']) check(path.isAbsolute(options[key] || ''), `Absolute ${key} required`);
  const lane = dependencies.requestLock || requestLock;
  const evidenceBase = dependencies.evidenceBase || path.join(root, 'opensms_artifacts/maintenance/request_lane');
  const alive = dependencies.processAlive || processAlive;
  const acquire = dependencies.acquire || (() => acquireSiteLock({ browserId: 'a53ce74e6c9e4fa08aec35f81ce1e2a3', timeoutMs: 0 }));
  const close = dependencies.closeOwnedPage || closeOwnedPage;
  const list = dependencies.listTargets || (async endpoint => {
    const response = await fetch(`${endpoint}/json/list`, { signal: AbortSignal.timeout(5000) });
    check(response.ok, 'Local browser metadata unavailable; lane retained');
    return response.json();
  });
  const lease = await acquire();
  let evidenceDirectory;
  try {
    check(!lease.inherited, 'Maintenance must acquire its own website lease');
    const laneStat = await fs.lstat(lane);
    check(laneStat.isDirectory() && !laneStat.isSymbolicLink(), 'Unexpected request lock type');
    const names = await fs.readdir(lane);
    check(names.includes('owner') && names.every(name => ['owner', 'blocked.json', 'next.json'].includes(name)), 'Unexpected lane contents');
    const bytes = {};
    for (const name of names) {
      const stat = await fs.lstat(path.join(lane, name));
      check(stat.isFile() && !stat.isSymbolicLink(), 'Unexpected lane evidence type');
      bytes[name] = await fs.readFile(path.join(lane, name));
    }
    check(sha256(bytes.owner) === options.ownerSha256, 'Request owner changed; lane retained');
    const match = bytes.owner.toString('utf8').match(/^([1-9][0-9]*)-[a-f0-9-]+$/);
    check(match, 'Invalid request owner');
    const requestPid = Number(match[1]);
    const stateBytes = await fs.readFile(options.statePath);
    const receiptBytes = await fs.readFile(options.receiptPath);
    check(sha256(stateBytes) === options.stateSha256, 'Original state changed; lane retained');
    check(sha256(receiptBytes) === options.receiptSha256, 'Original target receipt changed; lane retained');
    const state = JSON.parse(stateBytes), receipt = JSON.parse(receiptBytes);
    check(state.runId === options.runId && state.apiTarget?.runId === options.runId
      && state.apiTarget.receipt === options.receiptPath && state.apiTarget.targetId === receipt.targetId
      && state.apiTarget.endpoint === receipt.endpoint, 'Original run/page binding mismatch');
    check(receipt.ownedBy === 'opensms-patrol-api-target' && receipt.runId === options.runId
      && receipt.hidden === true && receipt.ownerPid === state.runnerPid
      && String(receipt.marker || '').startsWith(`opensms-patrol-api:${options.runId}:`)
      && /^http:\/\/127\.0\.0\.1:\d+$/.test(receipt.endpoint || '')
      && /^[A-Fa-f0-9]+$/.test(receipt.targetId || ''), 'Invalid owned-page evidence');
    const pids = [...new Set([requestPid, state.runnerPid, receipt.keeperPid])];
    check(pids.every(pid => !alive(pid)), 'Original process still alive; lane retained');
    await fs.mkdir(evidenceBase, { recursive: true, mode: 0o700 });
    evidenceDirectory = path.join(evidenceBase, `${options.runId}-${Date.now()}-${randomUUID()}`);
    await fs.mkdir(evidenceDirectory, { mode: 0o700 });
    await exclusiveCopy(path.join(evidenceDirectory, 'original-state.json'), stateBytes);
    await exclusiveCopy(path.join(evidenceDirectory, 'original-target-receipt.json'), receiptBytes);
    for (const name of names) await exclusiveCopy(path.join(evidenceDirectory, `original-${name}`), bytes[name]);
    await exclusiveCopy(path.join(evidenceDirectory, 'authorization.json'), Buffer.from(JSON.stringify({
      runId: options.runId, authorizationSource: options.authorizationSource,
      purpose: 'preserve-evidence-verify-owned-page-and-archive-abandoned-request-lane',
      sourceState: options.statePath, sourceStateSha256: options.stateSha256,
      sourceReceipt: options.receiptPath, sourceReceiptSha256: options.receiptSha256,
      ownerSha256: options.ownerSha256, checkedDeadPids: pids, recordedAt: new Date().toISOString(),
      businessRequests: 0, businessReplay: false,
    }, null, 2)));
    const cleanupPath = path.join(evidenceDirectory, 'cleanup-target-receipt.json');
    await exclusiveCopy(cleanupPath, receiptBytes);
    const cleanup = await close({ endpoint: receipt.endpoint, runId: options.runId,
      receiptPath: cleanupPath, environment: {
        OPENSMS_SITE_LOCK_TOKEN: lease.token || JSON.parse(await fs.readFile(lease.file)).token,
        OPENSMS_SITE_LOCK_PID: String(process.pid),
      } });
    check(cleanup.success === true && cleanup.closed === true && cleanup.targetId === receipt.targetId, 'Cleanup target identity mismatch');
    const targets = await list(receipt.endpoint);
    check(Array.isArray(targets) && targets.every(target => typeof target?.id === 'string'), 'Invalid local target confirmation');
    check(!targets.some(target => target.id === receipt.targetId), 'Original target still present; lane retained');
    check(pids.every(pid => !alive(pid)), 'Process identity changed; lane retained');
    const currentNames = await fs.readdir(lane);
    check(currentNames.length === names.length && currentNames.every(name => names.includes(name)), 'Request lane contents changed; retained');
    for (const name of names)
      check(sha256(await fs.readFile(path.join(lane, name))) === sha256(bytes[name]), 'Request evidence changed; lane retained');
    check(sha256(await fs.readFile(options.statePath)) === options.stateSha256
      && sha256(await fs.readFile(options.receiptPath)) === options.receiptSha256, 'Original run evidence changed; lane retained');
    const currentStat = await fs.lstat(lane);
    check(currentStat.dev === laneStat.dev && currentStat.ino === laneStat.ino, 'Request lane replaced; retained');
    const archivedLock = path.join(evidenceDirectory, 'preserved-request.lock');
    // Recoverable move, not a recursive delete. Global cooldown remains unchanged.
    await fs.rename(lane, archivedLock);
    const result = { status: 'recovered', runId: options.runId, evidenceDirectory, archivedLock,
      cleanup, browserRequestTargetAbsent: true, businessRequests: 0, businessReplay: false,
      originalStateUnchanged: sha256(await fs.readFile(options.statePath)) === options.stateSha256,
      originalReceiptUnchanged: sha256(await fs.readFile(options.receiptPath)) === options.receiptSha256,
      cooldownUnchanged: true };
    await exclusiveCopy(path.join(evidenceDirectory, 'recovery.json'), Buffer.from(JSON.stringify(result, null, 2)));
    return result;
  } catch (error) {
    if (evidenceDirectory) await exclusiveCopy(path.join(evidenceDirectory, 'failure.json'),
      Buffer.from(JSON.stringify({ status: 'failed', error: error.message, businessReplay: false }))).catch(() => {});
    throw error;
  } finally { lease.release(); }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  const argv = process.argv.slice(2);
  const allowed = ['--run-id', '--state', '--state-sha256', '--receipt', '--receipt-sha256', '--owner-sha256', '--authorization-source'];
  const value = key => argv[argv.indexOf(key) + 1];
  try {
    check(argv.length === allowed.length * 2 && allowed.every(key => argv.filter(arg => arg === key).length === 1)
      && argv.every((arg, index) => index % 2 === 1 || allowed.includes(arg)), 'Exact maintenance arguments required');
    console.log(JSON.stringify(await recoverRequestLane({
      runId: value('--run-id'), statePath: value('--state'), stateSha256: value('--state-sha256'),
      receiptPath: value('--receipt'), receiptSha256: value('--receipt-sha256'), ownerSha256: value('--owner-sha256'),
      authorizationSource: value('--authorization-source'),
    })));
  } catch (error) { console.log(JSON.stringify({ status: 'failed', error: error.message })); process.exitCode = 2; }
}
