// One already-submitted export status GET, in the existing exact page. The
// host owns the deadline: no long-lived CDP awaitPromise or page timer needed.
import { randomUUID } from 'node:crypto';

export function fetchExportStatusBounded(client, input, options = {}) {
  return fetchExportReadBounded(client, input, options, 'status');
}

export function fetchExportDownloadBounded(client, input, options = {}) {
  return fetchExportReadBounded(client, input, options, 'download');
}

async function fetchExportReadBounded(client, input, {
  clock = Date.now, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
} = {}, kind) {
  const isDownload = kind === 'download';
  const url = new URL(input.requestUrl);
  if (input.requestMethod !== 'GET' || input.requestBody !== null
      || url.origin !== 'https://adminsms.smsoug.com' || url.username || url.password
      || url.pathname !== `/api/adminExport/export4day/async/${kind}`
      || url.hash || [...url.searchParams.keys()].join(',') !== 'taskId'
      || !/^[a-f0-9]{32}$/i.test(url.searchParams.get('taskId') || '')
      || input.readTimeoutMs !== (isDownload ? 120000 : 45000)) throw new Error(`Invalid bounded export ${kind} request`);
  const key = `__opensms_${kind}_${randomUUID().replaceAll('-', '')}`;
  const keyJson = JSON.stringify(key);
  const guard = `if (location.origin !== 'https://adminsms.smsoug.com') throw new Error('Status page origin changed');`;
  const startedAt = clock();
  const deadline = startedAt + input.readTimeoutMs;
  const read = async (abort = false, timeoutMs = 5000) => client.evaluate(`(() => {
    ${guard}
    const key = ${keyJson};
    const job = globalThis[key];
    if (!job) {
      // Fence a delayed dispatch, but absence is not proof that an old-context
      // request has finished. Host will fail closed rather than retry it.
      if (${abort}) globalThis[key] = { cancelled: true, done: true };
      return { missing: true };
    }
    if (${abort} && !job.done) job.controller.abort();
    if (!job.done) return { done: false };
    const result = { done: true, response: job.response };
    delete globalThis[key];
    return result;
  })()`, timeoutMs);
  try {
    const dispatched = await client.evaluate(`(() => {
      ${guard}
      const key = ${keyJson};
      if (Object.prototype.hasOwnProperty.call(globalThis, key)) throw new Error('Status dispatch fenced');
      const input = ${JSON.stringify(input)};
      const job = { controller: new AbortController(), done: false };
      globalThis[key] = job;
      (async () => {
        try {
          const response = await fetch(input.requestUrl, {
            method: 'GET', credentials: 'include', signal: job.controller.signal,
            headers: { Accept: 'application/json, text/plain, */*',
              Authorization: input.authorization, ...input.extraHeaders },
          });
          const result = { status: response.status, ok: response.ok, url: response.url,
            retryAfter: response.headers.get('Retry-After') };
          if (${isDownload} && response.ok) {
            const bytes = new Uint8Array(await response.arrayBuffer());
            let binary = '';
            for (let offset = 0; offset < bytes.length; offset += 0x8000) {
              binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
            }
            result.base64 = btoa(binary);
            result.contentDisposition = response.headers.get('content-disposition');
          } else {
            result.text = await response.text();
          }
          if (${isDownload}) result.contentType = response.headers.get('content-type');
          job.response = result;
        } catch (error) {
          const name = String(error?.name || 'Error');
          const category = ['AbortError', 'TimeoutError', 'NetworkError'].includes(name)
            ? name : name === 'TypeError' && /Failed to fetch|fetch failed/i.test(String(error?.message))
              ? 'NetworkError' : 'PageFetchError';
          job.response = { transportError: { name: category } };
        } finally { job.done = true; }
      })();
      return { started: true };
    })()`, 5000);
    if (dispatched?.started !== true) throw new Error('Status dispatch not confirmed');
    while (clock() < deadline) {
      const snapshot = await read(false, Math.max(1, Math.min(5000, deadline - clock())));
      if (snapshot?.done === true && snapshot.response) return snapshot.response;
      if (snapshot?.missing) throw new Error('Status request context lost');
      await sleep(Math.min(1000, Math.max(0, deadline - clock())));
    }
    throw Object.assign(new Error(`Export ${kind} host deadline exceeded`), {name: 'TimeoutError'});
  } catch (error) {
    let completionConfirmed = false;
    const cleanupDeadline = clock() + 10000;
    try {
      while (clock() < cleanupDeadline) {
        const snapshot = await read(true, Math.max(1, Math.min(5000, cleanupDeadline - clock())));
        if (snapshot?.done === true && snapshot.response) {
          completionConfirmed = true;
          // A CDP reply may be lost although this exact GET already finished.
          // Consume its complete response (including 401/429 and Retry-After),
          // never launch another GET or turn that evidence into a generic retry.
          const response = snapshot.response;
          if (Number.isInteger(response.status) && typeof response.ok === 'boolean'
              && (typeof response.text === 'string'
                || (isDownload && typeof response.base64 === 'string'))) return response;
          break;
        }
        if (snapshot?.missing) break;
        await sleep(Math.min(250, Math.max(0, cleanupDeadline - clock())));
      }
    } catch { /* No credentials or arbitrary page error text in diagnostics. */ }
    error.requestCompletionConfirmed = completionConfirmed;
    error.requestCompletionUnconfirmed = !completionConfirmed;
    error[isDownload ? 'downloadFetchDiagnostic' : 'statusFetchDiagnostic'] = {
      mode: 'host-polled-same-page', timeoutMs: input.readTimeoutMs,
      elapsedMs: clock() - startedAt, completionConfirmed,
      category: error.code === 'CDP_COMMAND_TIMEOUT' ? 'browser_runtime_timeout'
        : error.name === 'TimeoutError' ? 'read_timeout' : `${kind}_transport_error`,
    };
    throw error;
  }
}
