import {PAYLOAD_ORIGIN} from './opensms_payload_codec.mjs';

// Navigation-ready is not application-ready. This probe reads no token, cookie,
// response body or key, and never navigates, activates or focuses a target.
export function readPayloadPageState() {
  const origin = 'https://adminsms.smsoug.com';
  const scripts = [...new Set([...document.scripts].map(s => s.src).filter(Boolean))]
    .map(src => new URL(src)).filter(url => url.origin === origin
      && /^\/static\/js\/app\.[\w-]+\.js$/.test(url.pathname)
      && !url.username && !url.password && !url.search && !url.hash);
  return {origin: location.origin, readyState: document.readyState,
    appScriptCount: scripts.length, scriptUrl: scripts.length === 1 ? scripts[0].href : null};
}

export async function waitForPayloadPageReady(client, {
  timeoutMs = 30000, pollMs = 200, now = () => performance.now(),
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
} = {}) {
  if (!(timeoutMs > 0 && timeoutMs <= 60000 && pollMs > 0 && pollMs <= timeoutMs)) {
    throw new Error('应用就绪等待参数无效');
  }
  const started = now();
  let polls = 0, last = null;
  const receipt = status => ({status, elapsedMs: Math.round(now() - started), polls,
    readyState: last?.readyState || null, appScriptCount: last?.appScriptCount ?? null,
    scriptUrl: last?.scriptUrl || null, silentBrowserMode: true});
  const fail = (code, message) => {
    throw Object.assign(new Error(message), {code, applicationReadiness: receipt(code)});
  };
  for (;;) {
    const remaining = timeoutMs - (now() - started);
    if (remaining <= 0) fail('OPENSMS_APP_READY_TIMEOUT', '隐藏页面应用加载超时；尚未开始接口采集');
    try {
      polls++;
      last = await client.evaluate(`(${readPayloadPageState.toString()})()`, Math.max(1, Math.min(5000, remaining)));
    } catch (error) {
      // A context replacement during initial navigation is transient; all other
      // CDP failures stop safely rather than guessing that the page is usable.
      if (!/Execution context was destroyed|Cannot find context with specified id/i.test(String(error?.message))) {
        fail('OPENSMS_APP_PROBE_FAILED', '隐藏页面应用就绪检查失败；尚未开始接口采集');
      }
      last = null;
    }
    if (last) {
      if (last.origin !== PAYLOAD_ORIGIN) fail('OPENSMS_APP_ORIGIN_MISMATCH', '隐藏页面不在目标站点；拒绝接口采集');
      if (last.appScriptCount > 1) fail('OPENSMS_APP_BUNDLE_AMBIGUOUS', '隐藏页面应用脚本不唯一；拒绝接口采集');
      if (['interactive', 'complete'].includes(last.readyState) && last.appScriptCount === 1) return receipt('ready');
      if (last.readyState === 'complete' && last.appScriptCount === 0) {
        fail('OPENSMS_APP_BUNDLE_MISSING', '隐藏页面加载完成但没有应用脚本；可能是验证页或错误页');
      }
    }
    await sleep(Math.min(pollMs, Math.max(0, timeoutMs - (now() - started))));
  }
}
