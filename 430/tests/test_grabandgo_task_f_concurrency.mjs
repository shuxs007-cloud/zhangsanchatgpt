import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { performance } from 'node:perf_hooks';
import { setTimeout as delay } from 'node:timers/promises';
import test from 'node:test';
import {
  authenticateWorkerWithCredential, checkIndependentLogins, initializeConcurrentWorkers, main,
  confirmedGrabAuthenticationFailure,
  automaticLoginAuthorized, authenticatedWithRecovery, ensureBackgroundAccountPage,
  requireUniqueWorkerSessions, runLookupWorkers, validateCurrent430Endpoint,
} from '../scripts/grabandgo_task_f_lookup.mjs';
import { runSessionQueue } from '../scripts/grabandgo_task_f_session_queue.mjs';

const ORIGIN = 'https://www.grabandgoplus.com';
const CUSTOMER = `${ORIGIN}/Retailer_CustomerSIMCards`;
const LOGIN = `${ORIGIN}/Login`;
const USER = 'ctl00$GagPAllPagesHolder$txtUserName';
const PASSWORD = 'ctl00$GagPAllPagesHolder$txtPassWord';
const BUTTON = 'ctl00$GagPAllPagesHolder$btnLogIn';
const SEARCH = 'ctl00$GagPRetailerHolder$txtSearchCriteria';
const CREDENTIAL = { username: 'offline-test', password: 'offline-only' };
const AUTO_LOGIN_POLICY = JSON.parse(await fs.readFile(new URL('../scripts/grabandgo_login_policy.json', import.meta.url), 'utf8'));
const iccid = index => String(8912230200000000000n + BigInt(index));
const loginHtml = `<form action="/Login"><input name="${USER}"><input type="password" name="${PASSWORD}">
  <input type="submit" name="${BUTTON}" value="LOGIN"></form>`;
const formHtml = session => `<h1>Customer SIM Cards</h1><form action="/Retailer_CustomerSIMCards">
  <input name="__VIEWSTATE" value="${session}"><input name="${SEARCH}">
  <input name="ctl00$GagPRetailerHolder$btnFilter" value="Go"></form>`;
const resultHtml = (session, number) => formHtml(session) + `<table><tr>
  <td><span id="GagPRetailerHolder_dlCustomerSimCards_lblPurchaseDate_0">2026-08-01</span></td>
  <td><span id="GagPRetailerHolder_dlCustomerSimCards_lblActivationRequestDate_0">2026-08-03 09:00</span></td>
  <td><span id="GagPRetailerHolder_dlCustomerSimCards_lblActivateDate_0">2026-08-03</span></td>
  <td><span id="GagPRetailerHolder_dlCustomerSimCards_lblSimCardNumber_0">${number}</span></td>
  <td><span id="GagPRetailerHolder_dlCustomerSimCards_lblSKU_0">OFFLINE-SKU</span></td>
  <td><a id="GagPRetailerHolder_dlCustomerSimCards_lnSimDetails_0"
  href="javascript:__doPostBack('ctl00$GagPRetailerHolder$dlCustomerSimCards$ctl00$lnSimDetails','')">Details</a></td>
  </tr></table>`;
function response(url, html, status = 200) {
  return { status: () => status, url: () => url, text: async () => html };
}

test('only a confirmed same-origin login page or 401 allows authentication recovery',()=>{
  assert.equal(confirmedGrabAuthenticationFailure({url:LOGIN,text:loginHtml,status:200}),true);
  assert.equal(confirmedGrabAuthenticationFailure({url:CUSTOMER,text:'Unauthorized',status:401}),true);
  for(const status of [0,403,429,500]) {
    assert.equal(confirmedGrabAuthenticationFailure({url:LOGIN,text:loginHtml,status}),false);
  }
  assert.equal(confirmedGrabAuthenticationFailure({url:CUSTOMER,text:'protocol error',status:200}),false);
  assert.equal(confirmedGrabAuthenticationFailure({url:'https://elsewhere.invalid/Login',text:loginHtml,status:200}),false);
});

test('a 403 response containing login fields never receives credentials',async()=>{
  let posts=0;
  await assert.rejects(authenticateWorkerWithCredential({
    get:async()=>response(LOGIN,loginHtml,403),post:async()=>{posts++;throw Error('must not post');},
  },CREDENTIAL,1),/登录页未确认/);
  assert.equal(posts,0);
});

test('cross-origin protected-page marker never counts as authenticated or receives a credential', async () => {
  const request={get:async()=>response('https://elsewhere.invalid/Retailer_CustomerSIMCards',formHtml('foreign')),
    post:async()=>assert.fail('foreign origin')};
  const result=await authenticatedWithRecovery({}, {request}, {page:null},
    {sameOriginHttpRequests:0,backgroundJavascriptFallbackRequests:0},
    {policy:AUTO_LOGIN_POLICY,readCredential:()=>assert.fail('foreign origin must not read keychain')});
  assert.equal(result.session.authenticated,false);
  assert.equal(result.recovery.attempted,false);
});

test('automatic login requires the exact standing one-attempt policy', () => {
  assert.equal(automaticLoginAuthorized(AUTO_LOGIN_POLICY), true);
  for (const change of [{enabled:false},{version:1},{maxRecoveryLoginPreflight:2},
    {allowMissingQueryPage:false},{queryBeforeVerifiedLogin:true},{replayFailedJobs:true},
    {loginOrigin:'https://elsewhere.invalid'},{credentialStore:'plaintext'},
    {authorization:'historical'},{recoveryTransport:'new-browser'},
    {continueCurrentTaskAfterVerifiedLogin:false},{cookieHandling:'plaintext-cookie-file'}]) {
    assert.equal(automaticLoginAuthorized({...AUTO_LOGIN_POLICY, ...change}), false);
  }
});

test('missing current-430 tab uses existing HTTP context; creates and navigates no page', async () => {
  const previous = process.env.OPENSMS_TASK_F_CURRENT_SESSION_ONLY;
  process.env.OPENSMS_TASK_F_CURRENT_SESSION_ONLY = '1';
  try {
    const opened = await ensureBackgroundAccountPage({}, {pages:()=>[]}, {policy:AUTO_LOGIN_POLICY});
    assert.deepEqual(opened, {page:null, created:false, queryPageMissing:true});
    await assert.rejects(ensureBackgroundAccountPage({}, {pages:()=>[]},
      {policy:{...AUTO_LOGIN_POLICY, enabled:false}}), /没有已打开/);
  } finally {
    if (previous === undefined) delete process.env.OPENSMS_TASK_F_CURRENT_SESSION_ONLY;
    else process.env.OPENSMS_TASK_F_CURRENT_SESSION_ONLY = previous;
  }
});

test('healthy missing-tab session reads no credential and submits nothing', async () => {
  const result = await authenticatedWithRecovery({}, {request:{
    get:async()=>response(CUSTOMER, formHtml('healthy')),
    post:async()=>assert.fail('healthy session must not login'),
  }}, {page:null}, {sameOriginHttpRequests:0,backgroundJavascriptFallbackRequests:0},
  {policy:AUTO_LOGIN_POLICY, readCredential:()=>assert.fail('must not read keychain')});
  assert.equal(result.session.authenticated,true);
  assert.equal(result.recovery.attempted,false);
});

test('confirmed login page gets one context login and an independent protected query-page read', async () => {
  let posts=0, protectedReads=0, authenticated=false;
  const credential={...CREDENTIAL};
  const request={get:async url=>{
    if(url===CUSTOMER) protectedReads++;
    return authenticated && url===CUSTOMER ? response(CUSTOMER,formHtml('verified')) : response(LOGIN,loginHtml);
  },post:async(url,options)=>{
    posts++;
    assert.equal(url,LOGIN);
    const data=new URLSearchParams(options.data);
    assert.equal(data.get(USER),CREDENTIAL.username);
    assert.equal(data.get(PASSWORD),CREDENTIAL.password);
    authenticated=true;
    return response(CUSTOMER,formHtml('verified'));
  }};
  const result=await authenticatedWithRecovery({}, {request}, {page:null},
    {sameOriginHttpRequests:0,backgroundJavascriptFallbackRequests:0},
    {policy:AUTO_LOGIN_POLICY,readCredential:()=>credential});
  assert.equal(result.recovery.recovered,true);
  assert.equal(result.recovery.attempts,1);
  assert.equal(result.recovery.transport,'existing-bitbrowser-430-context-request');
  assert.equal(result.recovery.cookieUpdatedInCurrentContext,true);
  assert.equal(result.recovery.continueCurrentTask,true);
  assert.equal(posts,1); assert.equal(protectedReads,2);
  assert.equal(credential.password,'');
  assert.equal(JSON.stringify(result.recovery).includes(CREDENTIAL.password),false);
});

test('ambiguous network/403/protocol response never triggers a password attempt', async () => {
  for(const first of [response(CUSTOMER,'Forbidden',403),response(CUSTOMER,'protocol error'),null]) {
    let reads=0;
    const request={get:async()=>{
      if(++reads>1)return response(LOGIN,loginHtml);
      if(!first)throw Error('timeout');
      return first;
    },post:async()=>assert.fail('ambiguous failure must not login')};
    const result=await authenticatedWithRecovery({}, {request}, {page:null},
      {sameOriginHttpRequests:0,backgroundJavascriptFallbackRequests:0},
      {policy:AUTO_LOGIN_POLICY,readCredential:()=>assert.fail('must not read keychain')});
    assert.equal(result.recovery.attempted,false);
  }
});

test('failed login is not retried and credential-bearing error is never exposed', async () => {
  let posts=0;
  const request={get:async()=>response(LOGIN,loginHtml),post:async()=>{
    posts++;throw Error(`request failed: ${CREDENTIAL.password}`);
  }};
  const result=await authenticatedWithRecovery({}, {request}, {page:null},
    {sameOriginHttpRequests:0,backgroundJavascriptFallbackRequests:0},
    {policy:AUTO_LOGIN_POLICY,readCredential:()=>({...CREDENTIAL})});
  assert.equal(posts,1);assert.equal(result.recovery.recovered,false);
  assert.equal(result.recovery.reason,'single_login_attempt_not_verified');
  assert.notEqual(result.recovery.continueCurrentTask,true);
  assert.equal(JSON.stringify(result).includes(CREDENTIAL.password),false);
});

test('stale authenticated DOM never overrides a confirmed expired live session', async () => {
  let authenticated=false, posts=0;
  const page={url:()=>CUSTOMER,content:async()=>formHtml('stale'),
    evaluate:async()=>({status:200,url:LOGIN,text:loginHtml})};
  const request={get:async url=>authenticated && url===CUSTOMER
    ? response(CUSTOMER,formHtml('new')) : response(LOGIN,loginHtml),
    post:async()=>{posts++;authenticated=true;return response(CUSTOMER,formHtml('new'));}};
  const result=await authenticatedWithRecovery({}, {request}, {page},
    {sameOriginHttpRequests:0,backgroundJavascriptFallbackRequests:0},
    {policy:AUTO_LOGIN_POLICY,readCredential:()=>({...CREDENTIAL})});
  assert.equal(result.recovery.recovered,true);
  assert.equal(posts,1);
});

test('missing credential stops before submission', async () => {
  const result=await authenticatedWithRecovery({}, {request:{
    get:async()=>response(LOGIN,loginHtml),post:async()=>assert.fail('no credential'),
  }}, {page:null}, {sameOriginHttpRequests:0,backgroundJavascriptFallbackRequests:0},
  {policy:AUTO_LOGIN_POLICY,readCredential:()=>null});
  assert.equal(result.recovery.attempted,false);
  assert.equal(result.recovery.reason,'keychain_credential_unavailable');
});

// Entirely in-memory: no HTTP listener, socket, browser, keychain or database.
function mockSite({ latency = 1, duplicateIds = false, rotateIdsOnRecheck = false,
  failLoginId = 0, failRecheckId = 0, mismatchDetail = false, loginReturnsAccount = false,
  varyingLatency = false, allowAuthCookies = false } = {}) {
  const site = { contexts: [], events: [], active: 0, maximum: 0, requestCount: 0 };
  site.factory = { async newContext(options) {
    if (!allowAuthCookies) assert.deepEqual(options.storageState, { cookies: [], origins: [] });
    else {
      assert.deepEqual(options.storageState.origins,[]);
      assert.ok(options.storageState.cookies.some(cookie=>cookie.name==='persistent-auth'));
    }
    const id = site.contexts.length + 1;
    const ctx = { id, active: 0, maximum: 0, disposed: false, loggedIn: false,
      sessionId: duplicateIds ? 'shared-session' : `session-${id}`, current: '', customerGets: 0 };
    site.contexts.push(ctx);
    async function invoke(method, url, options = {}) {
      assert.equal(ctx.disposed, false, 'request after context disposal');
      assert.equal(ctx.active, 0, 'same session state machine overlapped');
      ctx.active += 1;
      ctx.maximum = Math.max(ctx.maximum, ctx.active);
      site.maximum = Math.max(site.maximum, ++site.active);
      site.requestCount += 1;
      site.events.push({ id, method, url, event: 'start' });
      try {
        await delay(latency + (varyingLatency ? 9 - id % 9 : 0));
        if (url === LOGIN && method === 'GET') return response(LOGIN, loginHtml);
        if (url === LOGIN && method === 'POST') {
          const form = new URLSearchParams(options.data);
          assert.equal(form.get(USER), CREDENTIAL.username);
          assert.equal(form.get(PASSWORD), CREDENTIAL.password);
          if (id === failLoginId) throw new Error('mock login failure');
          ctx.loggedIn = true;
          site.events.push({ id, event: 'login-completed' });
          return loginReturnsAccount
            ? response(`${ORIGIN}/Retailer_Account`, '<h1>Retailer Account</h1>')
            : response(CUSTOMER, formHtml(ctx.sessionId));
        }
        if (url === CUSTOMER && method === 'GET') {
          ctx.customerGets += 1;
          site.events.push({ id, event: 'customer-get', allLoggedIn: site.contexts.every(value => value.loggedIn) });
          if (id === failRecheckId) return response(LOGIN, loginHtml);
          if (rotateIdsOnRecheck) ctx.sessionId = 'rotated-shared-session';
          return response(CUSTOMER, formHtml(ctx.sessionId));
        }
        if (url === `${ORIGIN}/Retailer_Account`) return response(LOGIN, loginHtml);
        assert.equal(url, CUSTOMER);
        assert.equal(method, 'POST');
        const form = new URLSearchParams(options.data);
        assert.equal(form.get('__VIEWSTATE'), ctx.sessionId, 'cross-session ViewState');
        if (form.get('__EVENTTARGET')) {
          const sim = mismatchDetail ? iccid(999) : ctx.current;
          return response(`${ORIGIN}/Retailer_SimCardInfo`, `<form>
            <input id="txtSimCardNumber" name="txtSimCardNumber" value="${sim}">
            <input id="txtActivationDate" name="txtActivationDate" value="2026-08-03"></form>`);
        }
        ctx.current = form.get(SEARCH);
        site.events.push({ id, event: 'filter', iccid: ctx.current });
        return response(CUSTOMER, resultHtml(ctx.sessionId, ctx.current));
      } finally {
        ctx.active -= 1;
        site.active -= 1;
        site.events.push({ id, method, url, event: 'end' });
      }
    }
    ctx.get = (url, options) => invoke('GET', url, options);
    ctx.post = (url, options) => invoke('POST', url, options);
    ctx.storageState = async () => ({ cookies: [{ name: 'ASP.NET_SessionId', value: ctx.sessionId }] });
    ctx.dispose = async () => {
      assert.equal(site.active, 0, 'disposed a context while sibling requests remained in flight');
      assert.equal(ctx.disposed, false, 'context disposed twice');
      ctx.disposed = true;
      site.events.push({ id, event: 'disposed' });
    };
    return ctx;
  } };
  return site;
}

const initialize = (site, count) => initializeConcurrentWorkers([], 'offline-mock', count, CREDENTIAL, site.factory);
const close = entries => Promise.all(entries.map(entry => entry.worker.directApi.dispose()));

test('8 isolated workers overlap within the bound; all logins precede all live rechecks', async () => {
  const site = mockSite({ varyingLatency: true });
  const entries = await requireUniqueWorkerSessions(await initialize(site, 8), 8);
  assert.equal(site.maximum, 8);
  assert.ok(site.contexts.every(ctx => ctx.maximum === 1));
  assert.deepEqual(entries.map(entry => entry.sessionId), Array.from({ length: 8 }, (_, i) => `session-${i + 1}`));
  assert.equal(site.events.filter(event => event.event === 'customer-get').length, 8);
  assert.ok(site.events.filter(event => event.event === 'customer-get').every(event => event.allLoggedIn));
  assert.equal(site.requestCount, 24, 'login GET + login POST + independent live recheck per session');
  await close(entries);
});

test('only an exact authenticated Customer SIM login response skips the redundant GET', async () => {
  const site = mockSite({ loginReturnsAccount: true });
  const entries = await requireUniqueWorkerSessions(await initialize(site, 2), 2);
  assert.equal(site.requestCount, 8, 'non-search redirect still needs the initial Customer SIM GET');
  await close(entries);
  const api = { get: async () => response(LOGIN, loginHtml),
    post: async () => response(LOGIN, '<h1>Customer SIM Cards</h1><input type="password">') };
  await assert.rejects(authenticateWorkerWithCredential(api, CREDENTIAL, 1), /未取得Customer SIM Cards/);
});
test('login never sends credentials to a cross-origin form action',async()=>{
  let posts=0;
  const api={get:async()=>response(LOGIN,loginHtml.replace('/Login','https://invalid.example/Login')),
    post:async()=>{posts++;throw Error('must not post');}};
  await assert.rejects(authenticateWorkerWithCredential(api,CREDENTIAL,1),/ORIGIN_CHANGED/);
  assert.equal(posts,0);
});
test('failed multi-session independence requires a truly verified serial fallback',async t=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'grab-preflight-'));
  t.after(()=>fs.rm(directory,{recursive:true,force:true}));
  const saved=process.env.OPENSMS_TASK_F_POLICY_CATALOG_SYNC,exitCode=process.exitCode;
  process.env.OPENSMS_TASK_F_POLICY_CATALOG_SYNC='1';
  try{
    const site=mockSite({duplicateIds:true});
    const output=path.join(directory,'login.json');
    await checkIndependentLogins(output,8,{readCredential:()=>CREDENTIAL,newContext:options=>site.factory.newContext(options)});
    const result=JSON.parse(await fs.readFile(output,'utf8'));
    assert.equal(result.status,'passed');assert.equal(result.independentAspNetSessions,false);
    assert.equal(result.effectiveWorkers,1);assert.equal(result.serialFallbackVerified,true);
    assert.equal(site.contexts.length,9);assert.ok(site.contexts.every(c=>c.disposed));
  }finally{if(saved===undefined)delete process.env.OPENSMS_TASK_F_POLICY_CATALOG_SYNC;else process.env.OPENSMS_TASK_F_POLICY_CATALOG_SYNC=saved;process.exitCode=exitCode;}
});

test('initialization failure drains all started logins before disposing every context', async () => {
  const site = mockSite({ failLoginId: 1, varyingLatency: true });
  await assert.rejects(initialize(site, 8), /mock login failure/);
  assert.equal(site.active, 0);
  assert.equal(site.contexts.length, 8);
  assert.ok(site.contexts.every(ctx => ctx.disposed));
  assert.equal(site.events.filter(event => event.event === 'customer-get').length, 0);
});

test('duplicate IDs before or after recheck and expired sessions all reject and drain', async () => {
  for (const options of [{ duplicateIds: true }, { rotateIdsOnRecheck: true }, { failRecheckId: 2 }]) {
    const site = mockSite({ ...options, varyingLatency: true });
    const entries = await initialize(site, 8);
    await assert.rejects(requireUniqueWorkerSessions(entries, 8), /独立ASP.NET会话不足|复核未登录/);
    assert.equal(site.active, 0);
    assert.ok(site.contexts.every(ctx => ctx.disposed));
    if (options.duplicateIds) assert.equal(site.events.filter(event => event.event === 'customer-get').length, 0);
  }
});

test('query workers preserve identity, source fields, input order, isolation and each 1-second cooldown', async () => {
  const site = mockSite({ varyingLatency: true });
  const entries = await requireUniqueWorkerSessions(await initialize(site, 8), 8);
  const items = Array.from({ length: 19 }, (_, i) => ({ queryIccid: iccid(i),
    sourceIccid: `${iccid(i)}F`, sourceData: { slot: `slot-${i}`, retained: `source-${i}` } }));
  const waits = [];
  const results = await runLookupWorkers(items, entries.map(entry => entry.worker), {
    sleep: async ms => { waits.push(ms); await delay(1); },
  });
  assert.deepEqual(results.map(row => row.queryIccid), items.map(item => item.queryIccid));
  assert.ok(results.every(row => row.status === 'found' && row.detailSimIccid === row.queryIccid
    && row.simNumber === row.queryIccid && row.activationDate === '2026-08-03'
    && row.purchaseDate === '2026-08-01' && row.actRequestDate === '2026-08-03 09:00'
    && row.sku === 'OFFLINE-SKU'));
  assert.deepEqual(results.map(row => row.sourceData), items.map(row => row.sourceData));
  assert.deepEqual(waits, Array(items.length).fill(1000));
  assert.equal(site.maximum, 8);
  assert.ok(site.contexts.every(ctx => ctx.maximum === 1));
  for (const ctx of site.contexts) assert.match(entries[ctx.id - 1].worker.reusableHtml, /Customer SIM Cards/);
  await close(entries);
});

test('ICCID mismatch remains transient after retry and never becomes a successful date', async () => {
  const site = mockSite({ mismatchDetail: true });
  const entries = await requireUniqueWorkerSessions(await initialize(site, 1), 1);
  const waits = [];
  const results = await runLookupWorkers([{ queryIccid: iccid(1), sourceData: { retained: true } }],
    entries.map(entry => entry.worker), { sleep: async ms => { waits.push(ms); } });
  assert.equal(results[0].status, 'error');
  assert.match(results[0].error, /详情ICCID不一致/);
  assert.equal(results[0].activationDate, undefined);
  assert.deepEqual(results[0].sourceData, { retained: true });
  assert.deepEqual(waits, [1000, 1000]);
  await close(entries);
});

test('fatal local result failure stops queued cards and waits for active sessions and cooldowns', async () => {
  const site = mockSite({ varyingLatency: true });
  const entries = await requireUniqueWorkerSessions(await initialize(site, 8), 8);
  let cooldowns = 0;
  let completed = 0;
  await assert.rejects(runLookupWorkers(Array.from({ length: 40 }, (_, i) => ({ queryIccid: iccid(i) })),
    entries.map(entry => entry.worker), {
      onResult: () => { if (++completed === 1) throw new Error('mock local receipt failure'); },
      sleep: async () => { const first = ++cooldowns === 1; await delay(first ? 30 : 1); },
    }), /mock local receipt failure/);
  assert.equal(site.active, 0);
  assert.equal(cooldowns, 8);
  assert.equal(site.events.filter(event => event.event === 'filter').length, 8);
  await close(entries);
});

test('queue bounds, ordering and empty input do not require a session', async () => {
  assert.deepEqual(await runSessionQueue([], [], () => assert.fail('empty work started')), []);
  await assert.rejects(runSessionQueue([1], Array(9).fill({}), () => {}), /1至8/);
  await assert.rejects(runLookupWorkers([1], [{}], { delayMs: 999 }), /不得低于/);
  const starts = [];
  let drained = false;
  await assert.rejects(runSessionQueue([0, 1, 2, 3], [{}, {}], async (_entry, value) => {
    starts.push(value);
    if (!value) throw new Error('local failure');
    await delay(5);
    drained = true;
  }), /local failure/);
  assert.deepEqual(starts, [0, 1]);
  assert.equal(drained, true);
});

async function withMainFixture(t, rows, env, options, check) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'task-f-offline-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const input = path.join(directory, 'pending.json');
  const output = path.join(directory, 'lookup.json');
  const progress = path.join(directory, 'progress.json');
  await fs.writeFile(input, JSON.stringify({ reportBackfillMode: true, rows }));
  const originalArgv = process.argv;
  const originalEnv = { ...process.env };
  process.argv = [process.execPath, '/offline/lookup.mjs', input, output, '8', progress];
  Object.assign(process.env, { OPENSMS_TASK_F_POLICY_CATALOG_SYNC: '1', ...env });
  t.mock.method(console, 'log', () => {});
  try {
    await main(options);
    return await check({ output, progress, directory, runAgain: () => main(options) });
  } finally {
    process.argv = originalArgv;
    for (const key of Object.keys(process.env)) if (!(key in originalEnv)) delete process.env[key];
    Object.assign(process.env, originalEnv);
  }
}

test('0 pending calls neither keychain, CDP nor HTTP, atomically publishes receipts, and refuses overwrite', async t => {
  await withMainFixture(t, [], { OPENSMS_TASK_F_INDEPENDENT_LOGIN_ONLY: '0' }, {
    readCredential: () => assert.fail('keychain read for empty pending'),
    connectBrowser: () => assert.fail('CDP connection for empty pending'),
    requestFactory: { newContext: () => assert.fail('HTTP session for empty pending') },
  }, async ({ output, progress, directory, runAgain }) => {
    const bytes = await fs.readFile(output);
    const receipt = JSON.parse(bytes);
    assert.equal(receipt.effectiveWorkers, 0);
    assert.equal(receipt.sameOriginHttpRequests, 0);
    assert.equal(receipt.progressIntervalSeconds, 300);
    assert.deepEqual(receipt.results, []);
    assert.equal(JSON.parse(await fs.readFile(progress)).event, 'task_f_lookup_completed');
    await assert.rejects(runAgain(), { code: 'EEXIST' });
    assert.deepEqual(await fs.readFile(output), bytes);
    process.argv[3] = path.join(directory, 'without-progress.json');
    process.argv.length = 5;
    await runAgain();
    assert.equal(JSON.parse(await fs.readFile(process.argv[3])).inputCount, 0);
    assert.ok((await fs.readdir(directory)).every(file => !file.endsWith('.tmp')));
  });
});

test('current 430 profile and full browser path must match; parent CloakBrowser endpoint is refused', async () => {
  const deps={read:async()=> '49343\n/devtools/browser/exact\n',
    fetchVersion:async()=>({ok:true,json:async()=>({webSocketDebuggerUrl:'ws://127.0.0.1:49343/devtools/browser/exact'})})};
  await validateCurrent430Endpoint('http://127.0.0.1:49343',deps);
  await assert.rejects(validateCurrent430Endpoint('http://127.0.0.1:60904',
    {...deps,fetchVersion:async()=>assert.fail('wrong profile contacted')}),/ENDPOINT_MISMATCH/);
  await assert.rejects(validateCurrent430Endpoint('http://127.0.0.1:49343', {...deps,
    fetchVersion:async()=>({ok:true,json:async()=>({webSocketDebuggerUrl:'ws://127.0.0.1:49343/devtools/browser/recycled'})})}),/IDENTITY_CHANGED/);
});

test('expired 430 login refreshes its cookies then completes the same immutable pending task', async t => {
  const site=mockSite({allowAuthCookies:true});
  let authenticated=false, posts=0, protectedReads=0, credentialReads=0;
  const cookieValue='offline-new-cookie-not-for-receipts';
  const request={get:async url=>{
    if(url===CUSTOMER)protectedReads++;
    return authenticated && url===CUSTOMER
      ? response(CUSTOMER,formHtml('fresh-430')) : response(LOGIN,loginHtml);
  },post:async(url,options)=>{
    assert.equal(url,LOGIN);
    assert.equal(new URLSearchParams(options.data).get(PASSWORD),CREDENTIAL.password);
    posts++;authenticated=true;
    return response(CUSTOMER,formHtml('fresh-430'));
  }};
  const context={pages:()=>[],request,storageState:async()=>{
    assert.equal(authenticated,true);
    assert.equal(protectedReads,2,'new cookie must be independently verified before task continuation');
    return {cookies:[{name:'persistent-auth',value:cookieValue,domain:'www.grabandgoplus.com'}]};
  }};
  const requestFactory={newContext:async options=>{
    assert.equal(authenticated,true);
    assert.equal(options.storageState.cookies[0].value,cookieValue);
    return site.factory.newContext(options);
  }};
  const rows=[{queryIccid:iccid(1),sourceData:{originalRow:1}},
    {queryIccid:iccid(2),sourceData:{originalRow:2}}];
  await withMainFixture(t,rows,{
    OPENSMS_TASK_F_INDEPENDENT_LOGIN_ONLY:'0',OPENSMS_TASK_F_CURRENT_SESSION_ONLY:'1',
    OPENSMS_TASK_F_SESSION_CHECK_ONLY:'0',
  },{readCredential:()=>{credentialReads++;return {...CREDENTIAL};},requestFactory,
    connectBrowser:async()=>({browser:{},context,endpoint:'http://127.0.0.1:49343'})},
  async({output})=>{
    const bytes=await fs.readFile(output,'utf8'),receipt=JSON.parse(bytes);
    assert.equal(posts,1);assert.equal(credentialReads,1);
    assert.equal(receipt.status,'passed');
    assert.equal(receipt.autoLoginRecovered,true);
    assert.equal(receipt.continueCurrentTaskAfterLogin,true);
    assert.equal(receipt.inputCount,2);
    assert.deepEqual(receipt.results.map(row=>row.queryIccid),rows.map(row=>row.queryIccid));
    assert.deepEqual(receipt.results.map(row=>row.sourceData),rows.map(row=>row.sourceData));
    assert.ok(receipt.results.every(row=>row.status==='found'));
    assert.equal(bytes.includes(cookieValue),false);
    assert.equal(bytes.includes(CREDENTIAL.password),false);
  });
});

test('current logged-in 430 mode uses no Keychain, login form, navigation or foreground action', async t => {
  const site=mockSite({allowAuthCookies:true});
  const html=formHtml('existing-browser-session');
  const page={url:()=>CUSTOMER,content:async()=>html,
    evaluate:async()=>({status:200,url:CUSTOMER,text:html})};
  const context={pages:()=>[page],storageState:async()=>({cookies:[
    {name:'persistent-auth',value:'offline-only',domain:'www.grabandgoplus.com'},
    {name:'ASP.NET_SessionId',value:'existing-browser-session',domain:'www.grabandgoplus.com'}]})};
  await withMainFixture(t,Array.from({length:8},(_,i)=>({queryIccid:iccid(i)})),{
    OPENSMS_TASK_F_INDEPENDENT_LOGIN_ONLY:'0',OPENSMS_TASK_F_CURRENT_SESSION_ONLY:'1',
    OPENSMS_TASK_F_SESSION_CHECK_ONLY:'1',
  },{readCredential:()=>assert.fail('current session must not require Keychain'),requestFactory:site.factory,
    connectBrowser:async()=>({browser:{},context,endpoint:'http://127.0.0.1:49343'})},async({output})=>{
      const receipt=JSON.parse(await fs.readFile(output));
      assert.equal(receipt.status,'passed');assert.equal(receipt.effectiveWorkers,8);
      assert.equal(receipt.independentFormLoginAttempted,false);
      assert.equal(receipt.browserForegroundActivationAttempted,false);
      assert.equal(site.events.filter(value=>value.event==='login-completed'||value.event==='filter').length,0);
  });
});

test('main falls back to exactly one verified HTTP session after duplicate pool IDs and cleanup', async t => {
  const site = mockSite({ duplicateIds: true });
  let credentialReads = 0;
  await withMainFixture(t, Array.from({ length: 10 }, (_, i) => ({ queryIccid: iccid(i) })), {
    OPENSMS_TASK_F_INDEPENDENT_LOGIN_ONLY: '1', OPENSMS_TASK_F_SESSION_CHECK_ONLY: '1',
  }, {
    readCredential: () => { credentialReads += 1; return CREDENTIAL; }, requestFactory: site.factory,
    connectBrowser: () => assert.fail('independent HTTP fallback must stay browser-free'),
  }, async ({ output }) => {
    const receipt = JSON.parse(await fs.readFile(output));
    assert.equal(receipt.effectiveWorkers, 1);
    assert.equal(receipt.uniqueAspNetSessionCount, 1);
    assert.equal(receipt.independentAspNetSessions, false);
    assert.equal(receipt.requestedWorkersSatisfied, false);
    assert.equal(receipt.concurrencyLimitedReason, 'independent_sessions_unavailable_forces_serial_execution');
    assert.equal(credentialReads, 1);
    assert.equal(site.contexts.length, 9);
    assert.ok(site.contexts.every(ctx => ctx.disposed));
    const fallbackStart = site.events.findIndex(event => event.id === 9);
    assert.ok(site.events.slice(0, fallbackStart).filter(event => event.event === 'disposed').length === 8);
    assert.equal(site.events.filter(event => event.event === 'filter').length, 0);
    assert.equal(JSON.stringify(receipt).includes(CREDENTIAL.password), false);
  });
});

test('fixed-latency offline benchmark distinguishes existing parallelism from the saved request', async t => {
  const latency = 12;
  const measurements = {};
  for (const mode of ['serialExisting', 'parallelExisting', 'parallelOptimized']) {
    const site = mockSite({ latency });
    const start = performance.now();
    let entries;
    if (mode === 'serialExisting') {
      entries = [];
      for (let i = 0; i < 8; i += 1) {
        const [entry] = await initialize(site, 1);
        // Previous implementation always issued this GET after the exact POST.
        await entry.worker.api.get(CUSTOMER);
        entries.push(entry);
      }
    } else {
      entries = await initialize(site, 8);
      if (mode === 'parallelExisting') await Promise.all(entries.map(entry => entry.worker.api.get(CUSTOMER)));
    }
    await requireUniqueWorkerSessions(entries, 8);
    measurements[mode] = { elapsedMs: Number((performance.now() - start).toFixed(1)),
      requests: site.requestCount, maximumConcurrentRequests: site.maximum };
    await close(entries);
  }
  assert.equal(measurements.serialExisting.requests, 32);
  assert.equal(measurements.parallelExisting.requests, 32);
  assert.equal(measurements.parallelOptimized.requests, 24);
  t.diagnostic(JSON.stringify({ kind: 'offline-in-memory-fixed-latency-startup-only', workers: 8,
    perRequestLatencyMs: latency, liveNetworkUsed: false,
    allLoginsBeforeIndependentRecheck: true, measurements }));
});
