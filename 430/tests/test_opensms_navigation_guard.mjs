import assert from 'node:assert/strict';
import test from 'node:test';
import { ensureBackgroundPageNavigation } from '../opensms_navigation_guard.mjs';

const requestedUrl = 'https://adminsms.smsoug.com/day';

function fixture({ href = () => requestedUrl, navigate = async () => ({ frameId: 'offline-frame' }) } = {}) {
  let now = 0;
  let reads = 0;
  const sends = [];
  return {
    sends,
    get now() { return now; },
    client: {
      evaluate: async (expression, timeoutMs) => {
        assert.equal(expression, 'String(location.href)');
        assert.ok(timeoutMs > 0 && timeoutMs <= 1500);
        return href({ now, reads: reads++, sends });
      },
      send: async (method, params, timeoutMs) => {
        assert.equal(method, 'Page.navigate');
        assert.ok(timeoutMs > 0 && timeoutMs <= 1500);
        sends.push({ method, params, now });
        return navigate();
      },
    },
    options: {
      blankGraceMs: 300, timeoutMs: 1500, pollMs: 100, targetId: 'offline-target',
      clock: () => now, sleep: async ms => { now += ms; },
    },
  };
}

test('an immediately ready same-origin page never restarts its navigation', async () => {
  const f = fixture();
  const receipt = await ensureBackgroundPageNavigation(f.client, requestedUrl, f.options);
  assert.equal(receipt.status, 'ready');
  assert.equal(receipt.navigationCount, 0);
  assert.equal(receipt.elapsedMs, 0);
  assert.equal(receipt.lastUrl, requestedUrl);
  assert.equal(receipt.targetId, 'offline-target');
  assert.equal(f.sends.length, 0);
});

test('same-origin login and other routes return for caller validation without navigation', async () => {
  for (const route of ['/login?token=secret#password', '/phoneConfig']) {
    const f = fixture({ href: () => `https://adminsms.smsoug.com${route}` });
    const receipt = await ensureBackgroundPageNavigation(f.client, `${requestedUrl}?token=secret`, f.options);
    assert.equal(receipt.navigationCount, 0);
    assert.equal(receipt.requestedUrl, requestedUrl);
    assert.equal(receipt.lastUrl, `https://adminsms.smsoug.com${route.split('?')[0]}`);
    assert.equal(JSON.stringify(receipt).includes('secret'), false);
    assert.equal(f.sends.length, 0);
  }
});

test('normal delayed initial navigation finishes inside the grace without duplicate navigation', async () => {
  const f = fixture({ href: ({ now }) => now < 200 ? 'about:blank' : requestedUrl });
  const receipt = await ensureBackgroundPageNavigation(f.client, requestedUrl, f.options);
  assert.equal(receipt.status, 'ready');
  assert.equal(receipt.elapsedMs, 200);
  assert.equal(f.sends.length, 0);
});

test('a stuck initial blank target is recovered once after the grace', async () => {
  const f = fixture({ href: ({ sends }) => sends.length ? requestedUrl : 'about:blank' });
  const receipt = await ensureBackgroundPageNavigation(f.client, requestedUrl, f.options);
  assert.equal(receipt.status, 'recovered');
  assert.equal(receipt.navigationCount, 1);
  assert.equal(receipt.elapsedMs, 300);
  assert.equal(receipt.navigationResult.accepted, true);
  assert.deepEqual(f.sends, [{ method: 'Page.navigate', params: { url: requestedUrl }, now: 300 }]);
});

test('a navigation errorText fails immediately and strips sensitive error content', async () => {
  const f = fixture({ href: () => 'about:blank', navigate: async () => ({ errorText: 'net::ERR_NAME_NOT_RESOLVED ?token=secret' }) });
  await assert.rejects(ensureBackgroundPageNavigation(f.client, requestedUrl, f.options), error => {
    assert.equal(error.code, 'BACKGROUND_NAVIGATION_FAILED');
    assert.equal(error.navigationReceipt.navigationResult.errorText, 'net::ERR_NAME_NOT_RESOLVED');
    assert.equal(error.navigationReceipt.navigationCount, 1);
    assert.equal(JSON.stringify(error).includes('secret'), false);
    return true;
  });
  assert.equal(f.now, 300);
  assert.equal(f.sends.length, 1);
});

test('download navigation and CDP rejection each fail immediately without a retry', async () => {
  for (const [navigate, code] of [
    [async () => ({ isDownload: true }), 'BACKGROUND_NAVIGATION_IS_DOWNLOAD'],
    [async () => { throw new Error('protocol failure secret'); }, 'BACKGROUND_NAVIGATION_COMMAND_FAILED'],
  ]) {
    const f = fixture({ href: () => 'about:blank', navigate });
    await assert.rejects(ensureBackgroundPageNavigation(f.client, requestedUrl, f.options), error => {
      assert.equal(error.code, code);
      assert.equal(error.navigationReceipt.navigationCount, 1);
      assert.equal(error.message.includes('secret'), false);
      return true;
    });
    assert.equal(f.now, 300);
    assert.equal(f.sends.length, 1);
  }
});

test('a permanently blank target times out and never navigates a second time', async () => {
  const f = fixture({ href: () => 'about:blank' });
  await assert.rejects(ensureBackgroundPageNavigation(f.client, requestedUrl, f.options), error => {
    assert.equal(error.code, 'BACKGROUND_NAVIGATION_TIMEOUT');
    assert.equal(error.navigationReceipt.navigationCount, 1);
    assert.equal(error.navigationReceipt.lastUrl, 'about:blank');
    assert.equal(error.navigationReceipt.elapsedMs, 1500);
    return true;
  });
  assert.equal(f.sends.length, 1);
});

test('initial or redirected unexpected origins stop without any recovery navigation', async () => {
  for (const unexpected of ['https://outside.example/?token=secret', 'chrome-error://chromewebdata/', 'data:text/html,secret']) {
    const f = fixture({ href: () => unexpected });
    await assert.rejects(ensureBackgroundPageNavigation(f.client, requestedUrl, f.options), error => {
      assert.equal(error.code, 'UNEXPECTED_BACKGROUND_ORIGIN');
      assert.equal(error.navigationReceipt.navigationCount, 0);
      assert.equal(JSON.stringify(error).includes('secret'), false);
      return true;
    });
    assert.equal(f.sends.length, 0);
  }
  const f = fixture({ href: ({ sends }) => sends.length ? 'https://outside.example/' : 'about:blank' });
  await assert.rejects(ensureBackgroundPageNavigation(f.client, requestedUrl, f.options), { code: 'UNEXPECTED_BACKGROUND_ORIGIN' });
  assert.equal(f.sends.length, 1);
});

test('temporary execution-context destruction is retried without unnecessary navigation', async () => {
  const f = fixture({ href: ({ reads }) => {
    if (reads === 0) return 'about:blank';
    if (reads === 1) throw new Error('Execution context was destroyed.');
    if (reads === 2) throw new Error('Cannot find context with specified id');
    return requestedUrl;
  } });
  const receipt = await ensureBackgroundPageNavigation(f.client, requestedUrl, f.options);
  assert.equal(receipt.transientContextErrors, 2);
  assert.equal(receipt.status, 'ready');
  assert.equal(f.sends.length, 0);
});

test('context replacement after the recovery command does not allow another navigation', async () => {
  const f = fixture({ href: ({ sends }) => {
    if (sends.length) throw new Error('Cannot find default execution context');
    return 'about:blank';
  } });
  await assert.rejects(ensureBackgroundPageNavigation(f.client, requestedUrl, f.options), { code: 'BACKGROUND_NAVIGATION_TIMEOUT' });
  assert.equal(f.sends.length, 1);
});

test('non-context read errors fail fast and invalid requested origins never touch CDP', async () => {
  const f = fixture({ href: () => { throw new Error('WebSocket closed with secret'); } });
  await assert.rejects(ensureBackgroundPageNavigation(f.client, requestedUrl, f.options), { code: 'BACKGROUND_PAGE_READ_FAILED' });
  assert.equal(f.now, 0);
  assert.equal(f.sends.length, 0);
  await assert.rejects(ensureBackgroundPageNavigation({}, 'https://outside.example/', f.options), { code: 'INVALID_REQUESTED_URL' });
});
