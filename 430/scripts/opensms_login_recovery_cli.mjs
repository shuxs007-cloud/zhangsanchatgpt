import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { createRawOpenSmsApiSession } from '../opensms_protocol_client.mjs';
import { SNAPSHOT_DIR, ensureArtifactDirs } from '../opensms_artifact_paths.mjs';

function stamp() {
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Hong_Kong',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(new Date()).replace(/[-: ]/g, '');
}

// Page readiness is not authentication: the API probe below remains mandatory.
export function isPreflightPageReady(state) {
  return state?.origin === 'https://adminsms.smsoug.com'
    && ['interactive', 'complete'].includes(state.readyState)
    && (state.hasPassword === true || /登录|管理员|号码管理/.test(String(state.bodyText || '').replace(/\s/g, '')));
}

export async function probeExistingSession(sessionFactory = createRawOpenSmsApiSession, onVerified = () => {}) {
  let session;
  try {
    session = await sessionFactory({ cdp: process.env.BITBROWSER_CDP, pathName: '/day' });
    const response = await session.api('GET', '/carrier/config/listNotDeleted', null, { pageNum: 1, pageSize: 1 });
    const valid = Number(response?.code) === 200 && Array.isArray(response?.rows)
      && Number.isInteger(Number(response?.total)) && Number(response.total) >= 0;
    if (!valid && Number(response?.code) !== 401) {
      throw Object.assign(new Error(`登录只读预检响应不完整或非认证错误（code=${response?.code ?? 'missing'}，rowsArray=${Array.isArray(response?.rows)}）`),
        {code:'OPENSMS_LOGIN_PROBE_INVALID_RESPONSE'});
    }
    if (valid) {
      const targetUrl = session.target?.url ? new URL(session.target.url) : null;
      onVerified({ finalUrl: targetUrl ? `${targetUrl.origin}${targetUrl.pathname}` : null,
        ...(session.target?.id ? {targetId:session.target.id} : {}),
        payloadProtocol: session.payloadProtocol || null,
        ...(session.payloadBuildRecovery ? {payloadBuildRecovery: session.payloadBuildRecovery} : {}) });
    }
    return valid;
  } catch (error) {
    // Invalid ciphertext is a protocol regression, not expired authentication.
    // Do not reload/login or suppress the real reason when the codec fails.
    // Only explicit authentication evidence may trigger login. Rate limits,
    // transport failures and malformed payloads are not expired credentials.
    if (error?.status === 401 || /未从430当前页面目标获取到 Admin-Token/.test(String(error?.message))) return false;
    throw error;
  } finally {
    if (session) await session.browser.close().catch(() => {});
  }
}

async function main() {
  await ensureArtifactDirs();
  const startedMs = Date.now();
  let verified = {};
  const verifySession = () => probeExistingSession(createRawOpenSmsApiSession,
    details => { verified = details; });
  let result;
  if (await verifySession()) {
    result = { recovered: false, loginRequired: false, verificationMode: 'authenticated-readonly-api-probe' };
    if (process.env.OPENSMS_BROWSER_KIND === 'cloakbrowser') {
      const {cacheVerifiedOpenSmsSession} = await import('./opensms_new_browser.mjs');
      const status = JSON.parse(await fs.readFile(new URL('../opensms_artifacts/state/new_browser/status.json', import.meta.url), 'utf8'));
      if (String(status.pid) !== process.env.OPENSMS_BROWSER_SERVICE_PID
          || status.sessionId !== process.env.OPENSMS_BROWSER_SESSION_ID) {
        throw Object.assign(new Error('OpenSMS登录缓存归属改变，未启动业务'), {code:'OPENSMS_NEW_BROWSER_SESSION_CHANGED'});
      }
      result.authReuse = await cacheVerifiedOpenSmsSession({...status,authProbeTargetId:verified.targetId});
    }
  } else {
    const cause = Object.assign(new Error('OpenSMS只读接口确认登录失效，业务已停止，等待人工登录'),
      {code:'OPENSMS_MANUAL_LOGIN_REQUIRED'});
    if (process.env.OPENSMS_BROWSER_KIND === 'cloakbrowser') {
      const {OpenSmsAuthStore} = await import('./opensms_browser_auth_store.mjs');
      await new OpenSmsAuthStore().invalidate();
      const {showLoginForConfirmedExpiry} = await import('./opensms_new_browser.mjs');
      const saved = JSON.parse(await fs.readFile(new URL('../opensms_artifacts/state/new_browser/status.json', import.meta.url),'utf8'));
      if (String(saved.pid) !== process.env.OPENSMS_BROWSER_SERVICE_PID || saved.sessionId !== process.env.OPENSMS_BROWSER_SESSION_ID) {
        throw Object.assign(new Error('登录窗口归属改变'),{code:'OPENSMS_NEW_BROWSER_SESSION_CHANGED'});
      }
      cause.loginWindow = await showLoginForConfirmedExpiry(saved,cause);
    }
    throw cause;
  }
    const receipt = {
      createdAt: new Date().toISOString(),
      mode: 'raw-cdp-silent-login-preflight',
      silentBrowserMode: true,
      browserForegroundActivationAttempted: false,
      loginRequired: result.loginRequired === true,
      recovered: result.recovered === true,
      attempts: Number(result.attempts || 0),
      captchaService: result.captchaService || null,
      finalUrl: verified.finalUrl || result.finalUrl || null,
      payloadProtocol: verified.payloadProtocol || null,
      payloadBuildRecovery: verified.payloadBuildRecovery || null,
      verificationMode: result.verificationMode,
      authReuse: result.authReuse || null,
      durationMs: Date.now() - startedMs,
      success: result.loginRequired !== true || result.recovered === true,
    };
    const receiptPath = path.join(
      SNAPSHOT_DIR,
      `opensms_login_recovery_${stamp()}+0800.json`,
    );
    await fs.writeFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, { flag: 'wx' });
    console.log(JSON.stringify({ path: receiptPath, ...receipt }, null, 2));
    if (!receipt.success) process.exitCode = 2;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(async error => {
    const failure = {success:false,createdAt:new Date().toISOString(),
      failureCategory: error.status ? `upstream_http_${error.status}` : error.code || 'preflight_failed',
      httpStatus:error.status || null, readDiagnostic:error.readDiagnostic || null,
      startupRecovery:error.startupRecovery || null,
      payloadBuildRecovery:error.payloadBuildRecovery || null,
      loginFailureConfirmed:error.status===401 || error.code==='OPENSMS_MANUAL_LOGIN_REQUIRED',
      loginWindow:error.loginWindow || null, silentBrowserMode:error.loginWindow?.shown !== true,
      browserForegroundActivationAttempted:error.loginWindow?.shown === true};
    const failurePath=path.join(SNAPSHOT_DIR,`opensms_login_preflight_failure_${stamp()}+0800.json`);
    try {
      await fs.writeFile(failurePath,JSON.stringify(failure,null,2)+'\n',{flag:'wx'});
      console.log(JSON.stringify({...failure,path:failurePath}));
    } catch { console.error('登录预检失败回执保存失败；原始错误如下'); }
    console.error(error.stack || String(error));
    process.exitCode = 1;
  });
}
