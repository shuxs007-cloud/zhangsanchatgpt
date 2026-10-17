// Only these observed read-only endpoints may be automatically retried.
// In particular, POST carrier changes and expiry notifications never enter it.
export const RETRYABLE_READ_PATHS = new Set([
  '/carrier/config/listNotDeleted',
  '/admin/statisticsList',
  '/adminExport/export4day/async/status',
  '/adminExport/export4day/async/download',
]);

export function retryableRead(method, apiPath) {
  return String(method).toUpperCase() === 'GET' && RETRYABLE_READ_PATHS.has(apiPath);
}

export function readRetryAfterMs(response, now = Date.now()) {
  const value = response?.retryAfter;
  if (value != null && String(value).trim() !== '') {
    const seconds = Number(value);
    if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
    const date = Date.parse(String(value));
    if (Number.isFinite(date)) return Math.max(0, date - now);
  }
  try {
    const body = JSON.parse(response.text);
    if (Number.isFinite(body.retry_after) && body.retry_after >= 0) return body.retry_after * 1000;
  } catch {}
  return null;
}

function transientKind(error) {
  // Current human authorization permits one cooled-down retry of a known
  // read-only 403. This is not authentication expiry or a transport timeout.
  if (Number(error?.status) === 403) {
    // An HTTP-200 business permission code is not the HTTP block described by
    // the human; download validation preserves both fields independently.
    if (error.downloadDiagnostic && error.downloadDiagnostic.httpStatus !== 403) return null;
    return 'http_403';
  }
  // Explicit HTTP authentication/validation failure must never be classified
  // by a misleading network/timeout string in its body.
  if (Number(error?.status) >= 400 && Number(error?.status) < 500
      && ![408, 429].includes(Number(error?.status))) return null;
  if ([408, 429, 500, 502, 503, 504].includes(Number(error?.status))) return `http_${error.status}`;
  if (error?.code === 'CDP_COMMAND_TIMEOUT'
      || /CDP command timeout: Runtime.evaluate/i.test(String(error?.message || ''))) return 'browser_runtime_timeout';
  if (['AbortError', 'TimeoutError'].includes(error?.name)) return 'read_timeout';
  if (error?.name === 'NetworkError') return 'network_fetch_failed';
  const message = String(error?.message || '');
  if (/Failed to fetch|fetch failed|NetworkError|net::ERR_|ECONNRESET/i.test(message)) return 'network_fetch_failed';
  if (/AbortError|TimeoutError|aborted|CDP command timeout: Runtime.evaluate/i.test(message)) return 'read_timeout';
  if (/Execution context was destroyed|Cannot find context with specified id|Inspected target navigated/i.test(message)) return 'page_context_changed';
  return null;
}

export async function withReadRetry(operation, {
  method, apiPath, searchParams = null, targetId = null,
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  clock = Date.now, onAttempt = () => {},
} = {}) {
  const allowed = retryableRead(method, apiPath)
    && (!['/adminExport/export4day/async/status', '/adminExport/export4day/async/download'].includes(apiPath)
      || /^[a-f0-9]{32}$/i.test(String(searchParams?.taskId || '')));
  const limit = allowed ? 3 : 1;
  let retryLimit = limit;
  const attemptReceipts = [];
  for (let attempt = 1; attempt <= retryLimit; attempt += 1) {
    const started = clock();
    try {
      const result = await operation();
      const success = { method, apiPath, targetId, attempt, success: true, durationMs: clock() - started };
      attemptReceipts.push(success);
      await onAttempt(success);
      return result;
    } catch (error) {
      const kind = transientKind(error);
      // The attempt following a 403 is final, even if its error is different.
      // Mixed failures cannot turn this permission into repeated 403 retries.
      if (kind === 'http_403') retryLimit = Math.min(retryLimit, attempt + 1);
      const backoffMs = Number(error?.status) === 403 ? 30000
        : Number(error?.status) === 429 ? 30000 * 2 ** (attempt - 1)
        : attempt === 1 ? 10000 : 30000;
      const waitMs = Math.max(backoffMs, Number(error?.retryAfterMs) || 0);
      const retry = allowed && !!kind && attempt < retryLimit && waitMs <= 120000
        && error.requestCompletionUnconfirmed !== true
        && (kind !== 'browser_runtime_timeout' || error.requestCompletionConfirmed === true)
        && (apiPath !== '/adminExport/export4day/async/download' || [403, 429].includes(Number(error?.status)));
      // Do not persist request data, authorization, cookies, or response bodies.
      const diagnostic = {
        method, apiPath, targetId, attempt, success: false, retry,
        pageNum: Number.isFinite(Number(searchParams?.pageNum)) ? Number(searchParams.pageNum) : null,
        pageSize: Number.isFinite(Number(searchParams?.pageSize)) ? Number(searchParams.pageSize) : null,
        category: kind || (Number.isSafeInteger(error?.status) && error.status >= 400 && error.status <= 599
          ? `http_${error.status}` : error?.code === 'OPENSMS_RESPONSE_PARSE_FAILED'
            ? 'response_parse_failed' : error?.code === 'OPENSMS_RESPONSE_INVALID'
              ? 'response_invalid' : 'non_retryable'), durationMs: clock() - started,
        waitMs: retry ? waitMs : 0,
        ...(error.requestCompletionConfirmed !== undefined
          ? {completionConfirmed: error.requestCompletionConfirmed} : {}),
      };
      attemptReceipts.push(diagnostic);
      await onAttempt(diagnostic);
      if (!retry) {
        error.readDiagnostic = diagnostic;
        error.readAttempts = attemptReceipts;
        if (allowed) error.message = `${error.message} [read=${apiPath}, page=${diagnostic.pageNum ?? '-'}, target=${targetId ?? '-'}, attempts=${attempt}]`;
        throw error;
      }
      await sleep(waitMs);
    }
  }
}
