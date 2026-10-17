const OPENSMS_ORIGIN = 'https://adminsms.smsoug.com';
const defaultSleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// Receipts deliberately exclude credentials, query strings, fragments, page
// contents and raw CDP errors. An unexpected origin is never navigated away.
function receiptUrl(value) {
  if (value === 'about:blank') return value;
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol)) return `${url.protocol}[redacted]`;
    return url.origin === OPENSMS_ORIGIN
      ? `${url.origin}${url.pathname}`
      : `${url.origin}/[redacted]`;
  } catch {
    return value == null ? null : '[invalid-url]';
  }
}

function isTransientContextError(error) {
  return /execution context (?:was )?destroyed|cannot find (?:default )?(?:execution )?context|execution context is not available/i
    .test(String(error?.message || ''));
}

/**
 * Verify the first navigation of a newly created background target only.
 * Target.createTarget normally owns navigation. A continuously blank target
 * receives at most one Page.navigate after its grace period. Existing pages,
 * login handling and business operations belong to the caller.
 */
export async function ensureBackgroundPageNavigation(client, requestedUrl, {
  blankGraceMs = 1200,
  timeoutMs = 15000,
  pollMs = 100,
  clock = Date.now,
  sleep = defaultSleep,
  targetId,
} = {}) {
  const started = clock();
  let lastUrl = null;
  let navigationCount = 0;
  let transientContextErrors = 0;
  let blankSince = null;
  let navigationResult = null;
  const receipt = status => ({
    status,
    targetId: targetId ?? null,
    requestedUrl: receiptUrl(requestedUrl),
    lastUrl: receiptUrl(lastUrl),
    navigationCount,
    recoveryAttempted: navigationCount > 0,
    elapsedMs: Math.max(0, clock() - started),
    transientContextErrors,
    navigationResult,
  });
  const fail = (code, message) => {
    const error = new Error(message);
    error.code = code;
    error.navigationReceipt = { ...receipt('failed'), failureCode: code };
    return error;
  };
  let destination;
  try {
    destination = new URL(requestedUrl);
  } catch {
    throw fail('INVALID_REQUESTED_URL', 'OpenSMS 后台页导航地址无效');
  }
  if (destination.origin !== OPENSMS_ORIGIN || destination.username || destination.password) {
    throw fail('INVALID_REQUESTED_URL', 'OpenSMS 后台页导航地址必须是无凭据的同源地址');
  }
  if (!Number.isFinite(blankGraceMs) || blankGraceMs < 0
    || !Number.isFinite(timeoutMs) || timeoutMs <= 0
    || !Number.isFinite(pollMs) || pollMs <= 0) {
    throw fail('INVALID_NAVIGATION_OPTIONS', 'OpenSMS 后台页导航等待参数无效');
  }
  const deadline = started + timeoutMs;
  while (clock() < deadline) {
    let href;
    try {
      href = await client.evaluate('String(location.href)', Math.max(1, deadline - clock()));
    } catch (error) {
      if (!isTransientContextError(error)) {
        throw Object.assign(fail('BACKGROUND_PAGE_READ_FAILED', 'OpenSMS 后台页导航状态读取失败'), {cause: error});
      }
      transientContextErrors += 1;
      // Context replacement can signal an in-flight initial navigation. Give
      // the next observed blank context its own grace, within the same timeout.
      blankSince = null;
      await sleep(Math.max(0, Math.min(pollMs, deadline - clock())));
      continue;
    }
    lastUrl = href;
    if (href !== 'about:blank') {
      let observed;
      try {
        observed = new URL(href);
      } catch {
        throw fail('UNEXPECTED_BACKGROUND_URL', 'OpenSMS 后台页返回了无效地址');
      }
      if (observed.origin !== destination.origin || observed.username || observed.password) {
        throw fail('UNEXPECTED_BACKGROUND_ORIGIN', 'OpenSMS 后台页进入意外来源，已停止导航恢复');
      }
      // Same-origin login pages are returned so the caller can report its
      // existing login-expired error without refreshing or attempting login.
      return receipt(navigationCount ? 'recovered' : 'ready');
    }
    blankSince ??= clock();
    if (navigationCount === 0 && clock() - blankSince >= blankGraceMs && clock() < deadline) {
      navigationCount += 1;
      let result;
      try {
        result = await client.send('Page.navigate', { url: destination.href }, Math.max(1, deadline - clock()));
      } catch {
        throw fail('BACKGROUND_NAVIGATION_COMMAND_FAILED', 'OpenSMS 后台页恢复导航命令失败');
      }
      navigationResult = {
        accepted: !result?.errorText && result?.isDownload !== true,
        isDownload: result?.isDownload === true,
        errorText: result?.errorText
          ? String(result.errorText).match(/net::ERR_[A-Z0-9_]+/)?.[0] || '[redacted navigation error]'
          : null,
      };
      if (result?.errorText) {
        throw fail('BACKGROUND_NAVIGATION_FAILED', `OpenSMS 后台页恢复导航失败: ${navigationResult.errorText}`);
      }
      if (result?.isDownload === true) {
        throw fail('BACKGROUND_NAVIGATION_IS_DOWNLOAD', 'OpenSMS 后台页恢复导航意外触发下载');
      }
      continue;
    }
    await sleep(Math.max(0, Math.min(pollMs, deadline - clock())));
  }
  throw fail('BACKGROUND_NAVIGATION_TIMEOUT', 'OpenSMS 后台页导航超时，未取得同源页面');
}
