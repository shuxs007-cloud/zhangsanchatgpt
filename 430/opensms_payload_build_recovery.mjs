import {PAYLOAD_ORIGIN, loadPayloadCodec} from './opensms_payload_codec.mjs';
import {waitForPayloadPageReady} from './opensms_payload_readiness.mjs';

// Recover only the proven HTTP-200 old-build -> new SPA HTML condition, before
// any business request. The original document, target and login stay untouched.
export async function loadRawPayloadCodec(raw, {
  cdp, createBackgroundPage, loadCodec = loadPayloadCodec,
  waitUntilReady = waitForPayloadPageReady, fetcher = fetch,
  withReadPacing = operation => operation(),
  browserKind = process.env.OPENSMS_BROWSER_KIND,
  anchorTargetId = process.env.OPENSMS_BINDING_TARGET_ID,
} = {}) {
  let stale;
  try {
    return {codec: await loadCodec(expression => raw.client.evaluate(expression, 12000)), recovery: null};
  } catch (error) {
    if (error?.code !== 'OPENSMS_PROTOCOL_ERROR' || error.payloadFailureKind !== 'stale-app-bundle') throw error;
    stale = error;
  }
  const validAsset = value => {
    try {
      const url = new URL(value);
      return url.origin === PAYLOAD_ORIGIN && !url.username && !url.password
        && !url.search && !url.hash && /^\/static\/js\/app\.[\w-]+\.js$/.test(url.pathname);
    } catch { return false; }
  };
  const diagnostic = stale.payloadDiagnostic;
  if (browserKind !== 'cloakbrowser' || !/^http:\/\/127\.0\.0\.1:\d+$/.test(cdp || '')
      || typeof createBackgroundPage !== 'function'
      || diagnostic?.origin !== PAYLOAD_ORIGIN || !validAsset(diagnostic.scriptUrl)
      || !validAsset(diagnostic.replacementScriptUrl)
      || diagnostic.scriptUrl === diagnostic.replacementScriptUrl) throw stale;

  const recovery = {status: 'checking', reason: 'stale-app-bundle', attempts: 1,
    previousScriptUrl: diagnostic.scriptUrl, expectedScriptUrl: diagnostic.replacementScriptUrl,
    originalTargetId: raw.target.id, ownedTargetId: null, sameBrowserContext: false,
    ownedTargetClosed: false, originalPageNavigated: false, browserRestarted: false,
    silentBrowserMode: true, foregroundActivationAttempted: false, businessRequestsDuringRecovery: 0};
  let owned, codec, failure;
  const reject = (code, message) => Object.assign(new Error(message), {code});
  try {
    const info = (await raw.client.send('Target.getTargetInfo', {targetId: raw.target.id}, 5000)).targetInfo;
    if (info?.targetId !== raw.target.id || new URL(info.url).origin !== PAYLOAD_ORIGIN
        || !info.browserContextId) throw reject('OPENSMS_APP_BUILD_CONTEXT_UNCONFIRMED', '网页版本恢复的原页面归属未确认');
    const anchor = raw.target.type === 'page' ? raw.target.id : anchorTargetId;
    if (!anchor) throw reject('OPENSMS_APP_BUILD_CONTEXT_UNCONFIRMED', '网页版本恢复缺少精确原登录页面');
    if (anchor !== raw.target.id) {
      const anchorInfo = (await raw.client.send('Target.getTargetInfo', {targetId: anchor}, 5000)).targetInfo;
      if (anchorInfo?.targetId !== anchor || anchorInfo.type !== 'page'
          || new URL(anchorInfo.url).origin !== PAYLOAD_ORIGIN
          || anchorInfo.browserContextId !== info.browserContextId) {
        throw reject('OPENSMS_APP_BUILD_CONTEXT_UNCONFIRMED', '网页版本恢复的登录页面上下文不一致');
      }
    }
    owned = await createBackgroundPage({cdp, url: `${PAYLOAD_ORIGIN}/index`, authenticatedTargetId: anchor});
    if (!owned?.targetId || owned.targetId === raw.target.id || owned.targetId === anchor || owned.hidden !== true) {
      throw reject('OPENSMS_APP_BUILD_TARGET_UNCONFIRMED', '新版配置页面不是本次独立隐藏页面');
    }
    recovery.ownedTargetId = owned.targetId;
    const fresh = (await owned.browserClient.send('Target.getTargetInfo', {targetId: owned.targetId}, 5000)).targetInfo;
    if (fresh?.targetId !== owned.targetId || new URL(fresh.url).origin !== PAYLOAD_ORIGIN
        || fresh.browserContextId !== info.browserContextId) {
      throw reject('OPENSMS_APP_BUILD_CONTEXT_UNCONFIRMED', '新版配置页面不在原登录上下文');
    }
    recovery.sameBrowserContext = true;
    recovery.applicationReadiness = await waitUntilReady(owned.client);
    codec = await withReadPacing(() => loadCodec(expression => owned.client.evaluate(expression, 12000)));
    if (codec.metadata.scriptUrl !== diagnostic.replacementScriptUrl) {
      throw reject('OPENSMS_APP_BUILD_CHANGED_AGAIN', '网页版本再次变化；未使用不一致的新配置');
    }
    recovery.verifiedScriptUrl = codec.metadata.scriptUrl;
  } catch (error) {
    // Do not expose arbitrary CDP exceptions, page contents or key material.
    const code = /^OPENSMS_[A-Z_]+$/.test(error?.code || '') ? error.code : 'OPENSMS_APP_BUILD_RECOVERY_FAILED';
    failure = reject(code, '同会话新版网页配置校验未通过；未发送业务请求');
    if (Number.isInteger(error?.status) && error.status >= 400 && error.status <= 599) failure.status = error.status;
    if (!owned && error?.backgroundCleanup) recovery.creationCleanup = {
      targetId: error.backgroundCleanup.targetId || null, closed: error.backgroundCleanup.closed === true,
    };
  } finally {
    // Never close an unconfirmed/borrowed page or the original login target.
    if (owned && recovery.ownedTargetId === owned.targetId) {
      try {
        await owned.close();
        const response = await fetcher(`${cdp}/json/list`, {signal: AbortSignal.timeout(5000)});
        if (!response.ok) throw new Error('target-list-unavailable');
        const targets = await response.json();
        if (!Array.isArray(targets) || targets.some(target => target.id === owned.targetId)) throw new Error('target-still-present');
        recovery.ownedTargetClosed = true;
      } catch {
        failure = reject('OPENSMS_APP_BUILD_CLEANUP_UNCONFIRMED', '新版配置隐藏页面关闭未独立确认；已停止');
      }
    }
  }
  if (failure) {
    recovery.status = 'failed';
    failure.payloadBuildRecovery = recovery;
    throw failure;
  }
  recovery.status = 'recovered';
  return {codec, recovery};
}
