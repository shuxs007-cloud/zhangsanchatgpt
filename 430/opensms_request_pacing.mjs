// One cross-process lane for OpenSMS requests. No retries or business decisions.
import fs from 'node:fs/promises';
import { watch as watchDirectory } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { readRetryAfterMs } from './opensms_read_retry.mjs';
import { ensureOpenSmsSiteOwnership } from './opensms_site_execution.mjs';

export const REQUEST_PACING = Object.freeze({
  concurrency: 1, completionGapMs: 1000, failureCooldownMs: 30000,
  readTimeoutMs: 45000, queueTimeoutMs: 900000,
});
export function requestTimeouts(method, apiPath, options = {}) {
  const read = (method === 'GET' && [
    '/carrier/config/listNotDeleted', '/admin/statisticsList',
    '/phone/cardRetain/list', '/adminExport/export4day/async/status',
  ].includes(apiPath)) || (method === 'POST' && apiPath === '/phone/config/list');
  return { readTimeoutMs: read ? REQUEST_PACING.readTimeoutMs : null,
    evaluationTimeoutMs: Math.max(Number(options.timeoutMs) || 0, read ? 60000 : 180000) };
}

export function createRequestPacer({
  directory = path.join(path.dirname(fileURLToPath(import.meta.url)), 'opensms_artifacts/state/request_pacing'),
  gapMs = REQUEST_PACING.completionGapMs, cooldownMs = REQUEST_PACING.failureCooldownMs,
  queueTimeoutMs = REQUEST_PACING.queueTimeoutMs,
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)), clock = Date.now,
  onReceipt = () => {},
} = {}) {
  if (![gapMs, cooldownMs, queueTimeoutMs].every(n => Number.isFinite(n) && n >= 0)) throw new Error('Invalid request pacing configuration');
  const lock = path.join(directory, 'request.lock');
  const stateFile = path.join(directory, 'next.json');
  const waiters = path.join(directory, 'waiters');
  let queue = Promise.resolve();
  function queueWake() {
    let signal;
    const changed = new Promise(resolve => { signal = resolve; });
    const watchers = [];
    try {
      for (const folder of [directory, waiters]) {
        const watcher = watchDirectory(folder, () => signal());
        // A watcher error is not permission to bypass the queue. The original
        // timed recheck still verifies every ticket and the same HTTP lock.
        watcher.on('error', () => signal());
        watchers.push(watcher);
      }
    } catch (error) { for (const watcher of watchers) watcher.close(); throw error; }
    return {changed, close: () => { for (const watcher of watchers) watcher.close(); }};
  }
  async function firstWaiter() {
    const names = await fs.readdir(waiters);
    // A registration has not committed yet. Do not race past it; crashed or
    // malformed queue evidence is never deleted/stolen to make progress.
    if (names.some(name => name.endsWith('.pending'))) return null;
    const rows = [];
    for (const name of names) {
      if (!/^[1-9]\d*-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.json$/.test(name)) throw new Error('OPENSMS_PACING_QUEUE_INVALID');
      let row;
      try { row = JSON.parse(await fs.readFile(path.join(waiters, name), 'utf8')); }
      catch (error) { if (error.code === 'ENOENT') continue; throw new Error('OPENSMS_PACING_QUEUE_INVALID'); }
      if (row.version !== 1 || name !== `${row.owner}.json`
          || !Number.isSafeInteger(row.queuedAt) || row.queuedAt < 0) {
        throw new Error('OPENSMS_PACING_QUEUE_INVALID');
      }
      rows.push(row);
    }
    rows.sort((a, b) => a.queuedAt - b.queuedAt || a.owner.localeCompare(b.owner));
    return rows[0]?.owner ?? null;
  }
  async function execute(operation) {
    const queuedAt = clock(), owner = `${process.pid}-${randomUUID()}`;
    if (!Number.isSafeInteger(queuedAt) || queuedAt < 0) throw new Error('OPENSMS_PACING_CLOCK_INVALID');
    await fs.mkdir(directory, {recursive: true, mode: 0o700});
    await fs.mkdir(waiters, {recursive: true, mode: 0o700});
    const waiter = path.join(waiters, `${owner}.json`);
    const registration = path.join(waiters, `${owner}.pending`);
    const ticket = JSON.stringify({version: 1, owner, queuedAt});
    // Waiting tickets are not submitted-request evidence. Atomic publication
    // is sufficient here; the original request lock still protects in-flight
    // or unknown requests and the completed-request cooldown is still fsynced.
    await fs.writeFile(registration, ticket, {flag:'wx', mode:0o600});
    await fs.rename(registration, waiter);
    async function removeOwnWaiter() {
      if (await fs.readFile(waiter, 'utf8') !== ticket) throw new Error('OPENSMS_PACING_WAITER_CHANGED');
      await fs.unlink(waiter);
    }
    // Only the oldest published ticket may contend for the original HTTP lock.
    // No priority bypass, new lane, request overlap or shorter cooldown.
    let acquired = false;
    try {
      while (true) {
        // Subscribe before inspecting eligibility to avoid a lost lock-release
        // wakeup. Local filesystem events do not send HTTP or shorten its gap.
        const wake = queueWake();
        try {
          try {
            await fs.access(path.join(lock, 'blocked.json'));
            throw new Error('OPENSMS_PACING_REQUEST_UNCONFIRMED: 上次浏览器请求结束未确认，禁止发送新请求');
          } catch (blockedError) { if (blockedError.code !== 'ENOENT') throw blockedError; }
          if (await firstWaiter() === owner) {
            try { await fs.mkdir(lock, {mode: 0o700}); acquired = true; break; }
            catch (error) { if (error.code !== 'EEXIST') throw error; }
          }
          if (clock() - queuedAt >= queueTimeoutMs) throw new Error('OPENSMS_PACING_QUEUE_TIMEOUT: 请求限速锁仍占用，未发送请求；禁止自动抢锁');
          await Promise.race([wake.changed, sleep(100)]);
        } finally { wake.close(); }
      }
    } finally {
      // Cancellation before acquiring the lane is safe: no HTTP was sent.
      // Once acquired, preserve both ownership records on every unsafe exit.
      if (!acquired) await removeOwnWaiter();
    }
    // Crashed owners are never stolen: the browser request may still be live.
    await fs.writeFile(path.join(lock, 'owner'), owner, {flag:'wx', mode:0o600});
    let nextAllowedAt = 0;
    try {
      const saved = JSON.parse(await fs.readFile(stateFile, 'utf8'));
      if (!Number.isSafeInteger(saved.nextAllowedAt) || saved.nextAllowedAt < 0) throw new Error('OPENSMS_PACING_STATE_INVALID');
      nextAllowedAt = saved.nextAllowedAt;
    } catch (error) {
      if (error.code !== 'ENOENT') {
        // Fail closed; keep ownership evidence, do not send on corrupt state.
        throw error;
      }
    }
    if (nextAllowedAt - clock() > queueTimeoutMs) throw new Error('OPENSMS_PACING_COOLDOWN_EXCESSIVE: 未提前发送请求');
    while (clock() < nextAllowedAt) await sleep(Math.min(nextAllowedAt - clock(), 1000));
    const startedAt = clock();
    let result, failure;
    try { result = await operation(); } catch (error) { failure = error; }
    if (failure?.requestCompletionUnconfirmed === true) {
      // Do not release a lane while its browser fetch may still be in flight.
      // Keep exact ownership evidence; cleanup is never an automatic retry.
      await fs.writeFile(path.join(lock, 'blocked.json'), JSON.stringify({
        reason: 'browser_request_completion_unconfirmed', blockedAt: clock(),
      }), {flag: 'wx', mode: 0o600});
      throw failure;
    }
    const status = Number(failure?.status ?? result?.status);
    const httpFailed = Number.isSafeInteger(status) && status >= 400 && status <= 599;
    const temporaryFailure = failure || httpFailed || result?.ok === false;
    const serverWait = Math.max(Number(failure?.retryAfterMs) || 0, readRetryAfterMs(result, clock()) || 0);
    const pauseMs = Math.max(gapMs, temporaryFailure ? cooldownMs : 0, serverWait);
    const completedAt = clock();
    const receipt = {queuedMs: startedAt - queuedAt, requestMs: completedAt - startedAt,
      gapMs: pauseMs, concurrency: 1, queueDiscipline: 'fifo', success: !temporaryFailure,
      ...(Number.isSafeInteger(status) && status >= 100 && status <= 599 ? {httpStatus:status} : {})};
    // Durable timing is shared by all subprocesses and subsequent runs.
    const temp = path.join(lock, 'next.json');
    const handle = await fs.open(temp, 'wx', 0o600);
    try { await handle.writeFile(JSON.stringify({nextAllowedAt: completedAt + pauseMs})); await handle.sync(); }
    finally { await handle.close(); }
    await fs.rename(temp, stateFile);
    if (await fs.readFile(path.join(lock, 'owner'), 'utf8') !== owner) throw new Error('OPENSMS_PACING_OWNER_CHANGED');
    await fs.unlink(path.join(lock, 'owner'));
    await removeOwnWaiter();
    await fs.rmdir(lock);
    try { await onReceipt(receipt); } catch { /* Logging cannot replay a completed request. */ }
    if (failure) throw failure;
    return result;
  }
  return operation => {
    const pending = queue.then(() => execute(operation));
    queue = pending.catch(() => {});
    return pending;
  };
}

const productionPacer = createRequestPacer({
  onReceipt: receipt => console.error(JSON.stringify({requestPacing: receipt})),
});
export const withOpenSmsRequestPacing = async operation => {
  // Never wait for the job lease while holding the HTTP lane: its owner may
  // need that lane to finish the current job.
  await ensureOpenSmsSiteOwnership();
  return productionPacer(async () => {
    // Recheck inside the request lane: queued children cannot outlive the parent.
    await ensureOpenSmsSiteOwnership();
    return operation();
  });
};
