import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { launchBrowser, authFailureConfirmed, shouldReuseHeadless, shouldReuseBrowser,
  adoptExistingSession, showLoginForConfirmedExpiry } from '../scripts/opensms_new_browser.mjs';
import { backgroundContextOptions } from '../opensms_protocol_client.mjs';

test('new browser launcher stays local, secrets go only to Keychain, no foreground pages', async () => {
  const source = await fs.readFile(new URL('../scripts/opensms_new_browser.mjs', import.meta.url), 'utf8');
  for (const forbidden of ['cookies(', 'addCookies(', 'activateTarget', 'security find-generic-password'])
    assert.ok(!source.includes(forbidden), forbidden);
  assert.match(source, /authStore.save\(await context.storageState\(\)\)/);
  assert.match(source, /headless = true/);
  assert.match(source, /automaticCheckDisabled = true/);
  assert.match(source, /if \(!authFailureConfirmed\(cause\)\) throw/);
  assert.match(source, /if \(runningStates.has\(current.status\)\)/);
  assert.match(source, /adoptExistingSession\(current\)/);
  assert.doesNotMatch(source, /publish\(owned,true,storageState\)/);
  assert.doesNotMatch(source, /24\*60\*60\*1000/);
  assert.match(source, /OPENSMS_SITE_JOB_LOCK_TOKEN = reportOwner.token/);
  assert.doesNotMatch(source, /atomic\([^\n]*(storageState|cookie|Admin-Token)/);
  assert.match(source, /if \(fsSync.existsSync\(requestLock\)\) throw/);
  assert.match(source, /acquireSiteLock\(\{browserId, timeoutMs:0\}\)/);
  assert.match(source, /browserSessionId:sessionId/);
  assert.match(source, /authentication:'unknown-until-original-runner-api-preflight'/);
});

test('a verified live headed or headless browser keeps its exact PID/session/port; no launcher or close', async () => {
  for (const headless of [false,true,undefined]) {
    const saved={status:'awaiting-manual-login',headless,pid:42,sessionId:'same',port:12345,
      webSocketPath:'/devtools/browser/same'};
    const calls=[];
    const value=await adoptExistingSession(saved, {verify:async input=>{assert.equal(input,saved);calls.push('verify');return {targetId:'authenticated-page'};},
      cache:async input=>{assert.equal(input.authProbeTargetId,'authenticated-page');calls.push('cache');},persist:async()=>calls.push('persist')});
    assert.deepEqual(calls,['verify','cache','persist']);
    for (const key of ['pid','sessionId','port','webSocketPath','headless']) assert.equal(value[key],saved[key]);
    assert.equal(shouldReuseBrowser(value),true);
    assert.equal(value.browserReused,true);
    assert.equal(value.lastVerifiedTargetId,'authenticated-page');
  }
});

test('technical failure cannot persist readiness, invalidate or open a login window', async () => {
  for (const error of [{status:403},{status:429},new Error('offline'),new Error('protocol')]) {
    await assert.rejects(adoptExistingSession({}, {verify:async()=>{throw error;},
      cache:async()=>assert.fail('cache touched'),persist:async()=>assert.fail('state written')}));
    await assert.rejects(showLoginForConfirmedExpiry({},error, {
      connect:async()=>assert.fail('browser connected'),persist:async()=>assert.fail('state written')}),/UNCONFIRMED/);
  }
  await assert.rejects(adoptExistingSession({}, {verify:async()=>true,
    cache:async()=>{throw new Error('OPENSMS_MANUAL_LOGIN_REQUIRED');},
    persist:async()=>assert.fail('state written')}),/AUTH_CACHE_CONTEXT_UNCONFIRMED/);
});

test('confirmed expiry displays the existing headed login page once, without creating another page/browser', async () => {
  let url='https://adminsms.smsoug.com/day', navigations=0, foreground=0, disconnects=0, persisted;
  const page={url:()=>url,goto:async value=>{url=value;navigations++;return {status:()=>200};},
    bringToFront:async()=>foreground++};
  const cdp={send:async method => method==='Target.getTargets'
    ? {targetInfos:[{type:'page',url,targetId:'same-target'}]} : {windowId:1},detach:async()=>{}};
  const browser={contexts:()=>[{pages:()=>[page]}],newBrowserCDPSession:async()=>cdp,close:async()=>disconnects++};
  const deps={connect:async()=>browser,persist:async value=>{persisted=value;}};
  const saved={pid:42,sessionId:'same',headless:false};
  assert.equal((await showLoginForConfirmedExpiry(saved,{status:401},deps)).shown,true);
  assert.equal(persisted.pid,saved.pid); assert.equal(persisted.sessionId,saved.sessionId);
  await showLoginForConfirmedExpiry(persisted,{status:401},deps);
  assert.equal(navigations,1);assert.equal(foreground,1);assert.equal(disconnects,2);
});

test('expired headless browser requests one safe visible transition, not an immediate launch during report ownership', async () => {
  let pending;
  const result=await showLoginForConfirmedExpiry({headless:true,pid:42,sessionId:'same'},
    {code:'OPENSMS_MANUAL_LOGIN_REQUIRED'}, {connect:async()=>assert.fail('live report interrupted'),
      persist:async value=>{pending=value;}});
  assert.equal(result.shown,false);assert.equal(result.requiresVisibleService,true);
  assert.equal(pending.manualLoginRequested,true);assert.equal(pending.authentication,'expired');
});

test('only explicit authentication failure expires the cache; 403/network cannot open login', () => {
  assert.equal(authFailureConfirmed({status:401}),true);
  assert.equal(authFailureConfirmed({code:'OPENSMS_MANUAL_LOGIN_REQUIRED'}),true);
  assert.equal(authFailureConfirmed(new Error('OPENSMS_MANUAL_LOGIN_REQUIRED')),true);
  for (const error of [{status:403},{status:429},{code:'ECONNRESET'},new Error('ciphertext invalid')]) {
    assert.equal(authFailureConfirmed(error),false);
  }
  assert.equal(shouldReuseHeadless({status:'ready',headless:true}),true);
  for (const saved of [null,{}, {status:'ready',headless:false}, {status:'awaiting-manual-login',headless:true}]) {
    assert.equal(shouldReuseHeadless(saved),false);
  }
});

test('CDP metadata must match the allocated loopback port', async () => {
  const original = globalThis.fetch;
  let closed = false, options;
  globalThis.fetch = async url => ({ok:true,json:async()=>({webSocketDebuggerUrl:
    String(url).replace('http:', 'ws:').replace('/json/version','/devtools/browser/test')})});
  try {
    const result = await launchBrowser({reservePort:async()=>12345, launch:async value => {
      options = value; return {version:()=> 'test',close:async()=>{closed=true;}};
    }});
    assert.ok(result.port > 0);
    assert.equal(result.webSocketPath, '/devtools/browser/test');
    assert.equal(options.headless, true);
    assert.ok(options.args.includes('--remote-debugging-address=127.0.0.1'));
    assert.equal(closed,false);
    globalThis.fetch = async () => ({ok:true,json:async()=>({webSocketDebuggerUrl:'ws://example.com:1234/devtools/browser/other'})});
    await assert.rejects(launchBrowser({headless:true,reservePort:async()=>12345,launch:async()=>({version:()=> 'test',close:async()=>{closed=true;}})}), /IDENTITY_INVALID/);
    assert.equal(closed,true);
  } finally {globalThis.fetch = original;}
});

test('hidden report page shares the exact manual-login context, not another incognito/default context', () => {
  assert.deepEqual(backgroundContextOptions('cloakbrowser', {targetId:'exact',browserContextId:'context'}, 'exact'),
    {browserContextId:'context'});
  assert.deepEqual(backgroundContextOptions('bitbrowser-430', {}, 'exact'), {});
  for (const value of [{}, {targetId:'other',browserContextId:'context'}, {targetId:'exact'}]) {
    assert.throws(() => backgroundContextOptions('cloakbrowser', value, 'exact'), /CONTEXT_UNCONFIRMED/);
  }
});
