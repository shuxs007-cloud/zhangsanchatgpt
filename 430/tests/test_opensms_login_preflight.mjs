import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
import { probeExistingSession, isPreflightPageReady } from '../scripts/opensms_login_recovery_cli.mjs';

test('preflight recognizes spaced login text and loaded login forms without treating them as authenticated', () => {
  const base = {origin: 'https://adminsms.smsoug.com', readyState: 'complete'};
  for (const bodyText of ['登 录', '登\n录', '登录', '管理员', '号码管理']) {
    assert.equal(isPreflightPageReady({...base, bodyText}), true);
  }
  assert.equal(isPreflightPageReady({...base, hasPassword: true}), true);
  assert.equal(isPreflightPageReady({...base, bodyText: ''}), false);
  assert.equal(isPreflightPageReady({...base, bodyText: '502 Bad Gateway'}), false);
  assert.equal(isPreflightPageReady({...base, bodyText: '登录', readyState: 'loading'}), false);
  assert.equal(isPreflightPageReady({...base, bodyText: '登录', origin: 'http://other'}), false);
  assert.equal(isPreflightPageReady(null), false);
});

test('valid API session wins over stale login tab without navigation', async () => {
  let closed = 0;
  assert.equal(await probeExistingSession(async () => ({api: async (method, path, body, query) => {
    assert.equal(method, 'GET');
    assert.equal(path, '/carrier/config/listNotDeleted');
    assert.equal(query.pageSize, 1);
    return {code: 200, rows: [], total: 0};
  }, browser: {close: async () => {closed++;}}})), true);
  assert.equal(closed, 1);
});

test('only explicit 401 triggers login; malformed responses and network failures stay diagnostic', async () => {
  let closed = 0;
  for (const response of [{code: 401, rows: [], total: 0}, {}, {code: 200, rows: null, total: 1}]) {
    const invoke=()=>probeExistingSession(async () => ({api: async () => response,
      browser: {close: async () => {closed++;}}}));
    if (response.code===401) assert.equal(await invoke(),false);
    else await assert.rejects(invoke(),{code:'OPENSMS_LOGIN_PROBE_INVALID_RESPONSE'});
  }
  assert.equal(closed, 3);
  await assert.rejects(probeExistingSession(async () => {throw new Error('offline');}),/offline/);
});

test('429, 403 and transport failures never fall through to login, and API clients close',async()=>{
  for (const status of [429,403,503]) {
    let closed=0;
    await assert.rejects(probeExistingSession(async()=>({api:async()=>{throw Object.assign(new Error('upstream failure'),{status});},
      browser:{close:async()=>{closed++;}}})),e=>e.status===status);
    assert.equal(closed,1);
  }
});

test('verified receipt uses the actual route and sanitized public protocol metadata', async () => {
  let evidence;
  const metadata = {source: 'current-page-app-bundle'};
  assert.equal(await probeExistingSession(async () => ({
    api: async () => ({code: 200, rows: [], total: 0}),
    target: {url: 'https://adminsms.smsoug.com/day?private=redacted#redacted'},
    token: 'must-not-be-copied', payloadProtocol: metadata,
    browser: {close: async () => {}},
  }), details => { evidence = details; }), true);
  assert.deepEqual(evidence, {finalUrl: 'https://adminsms.smsoug.com/day', payloadProtocol: metadata});
  assert.doesNotMatch(JSON.stringify(evidence), /redacted|must-not-be-copied/);
});

test('background login redirect can be accepted only when explicitly enabled for API verification', async () => {
  const source = await fs.readFile(new URL('../scripts/opensms_login_recovery.mjs', import.meta.url), 'utf8');
  const context = vm.createContext({setTimeout: callback => {callback(); return 0;}, clearTimeout,
    Buffer, URL, process: {env: {}}, Date, console});
  vm.runInContext(source.replace(/export /g, '') + '\nthis.recover = recoverOpenSmsLogin;', context);
  const states = [
    {isLogin: true, loginExpired: false, href: 'https://adminsms.smsoug.com/login', bodyText: '登录'},
    {isLogin: false, loginExpired: false, href: 'https://adminsms.smsoug.com/index', bodyText: '首页 管理员'},
  ];
  const adapter = {eval: async () => states.shift(), send: async () => ({})};
  const result = await context.recover(adapter, {allowAuthenticatedRedirect: true});
  assert.equal(result.existingSessionRestored, true);
  assert.equal(result.attempts, 0);
  const standardStates = [
    {isLogin: true, href: 'https://adminsms.smsoug.com/login'},
    {isLogin: false, href: 'https://adminsms.smsoug.com/index'},
  ];
  await assert.rejects(context.recover({eval: async () => standardStates.shift(), send: async () => ({})}), /未进入登录页/);
});
