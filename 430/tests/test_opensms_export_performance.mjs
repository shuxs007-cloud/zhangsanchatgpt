import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { withReadRetry, readRetryAfterMs } from '../opensms_read_retry.mjs';
import { validateExportDownloadResponse } from '../scripts/opensms_export_download_response.mjs';
import { readExportClockBeforeSubmit } from '../scripts/opensms_export_clock_recovery.mjs';
import { publishSubmissionReady } from '../scripts/opensms_export_submission_ready.mjs';

// Run with: node --test tests/test_opensms_export_performance.mjs
// The production entry points are never imported or launched. Only their
// function definitions execute inside contexts with fully stubbed I/O.
const scripts = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../scripts');
const exportText = fs.readFileSync(path.join(scripts, 'opensms_export_fast_raw.mjs'), 'utf8');
const dateText = fs.readFileSync(path.join(scripts, 'opensms_read_server_dates_fast_raw.mjs'), 'utf8');
const forbiddenNetwork = () => { throw new Error('Live network access is forbidden in this test'); };
const row = href => ({ href, bodyText: '2026-09-05 00:16:25', loginVisible: false });
const header = [{ text: '2026-09-05 00:16:25', top: 10, left: 900, width: 180 }];

function downloadContext(responses, { invalidPayload = false, failWrite = false } = {}) {
  const calls = [], waits = [], writes = [], attempts = [];
  const bytes = Buffer.alloc(invalidPayload ? 10 : 100001);
  bytes.write('PK');
  const context = vm.createContext({ Buffer, path, Date, exportDirectory:'/offline', readRetryAfterMs,
    validateExportDownloadResponse, withOpenSmsRequestPacing: operation => operation(),
    fetchExportDownloadBounded: (client,input) => client.evaluate(
      `method: 'GET' ${input.requestUrl} ${input.authorization}`, input.readTimeoutMs),
    withReadRetry:(operation, options)=>withReadRetry(operation, {...options, sleep:async ms=>waits.push(ms)}),
    fs:{writeFile:async (...args)=>{writes.push(args); if(failWrite) throw new Error('disk failure');},
      stat:async()=>({mtimeMs:123,size:bytes.length})},
  });
  vm.runInContext(sourceBetween(exportText, 'async function downloadExport', 'async function main'), context);
  const client = {evaluate:async (source, timeout)=>{
    calls.push({source,timeout});
    const response = responses.shift();
    if (response instanceof Error) throw response;
    return response ?? {ok:true,status:200,base64:bytes.toString('base64')};
  }};
  return {calls,waits,writes,attempts,run:(taskId='a'.repeat(32))=>context.downloadExport(
    client,'offline-secret',taskId,async event=>attempts.push(event))};
}

test('download 429 waits 30/60 seconds then retries only the same confirmed GET', async()=>{
  const state=downloadContext([{ok:false,status:429},{ok:false,status:429}]);
  const result=await state.run();
  assert.deepEqual(state.waits,[30000,60000]);
  assert.equal(state.calls.length,3);
  assert.ok(state.calls.every(call=>call.source===state.calls[0].source && call.timeout===120000));
  assert.match(state.calls[0].source,/method: 'GET'/);
  assert.match(state.calls[0].source,/async\/download\?taskId=/);
  assert.equal(state.writes.length,1);
  assert.equal(state.writes[0][2].flag,'wx');
  assert.equal(result.retryWaitMs,90000);
  assert.equal(result.downloadAttempts.length,3);
  assert.equal(state.attempts.at(-1).success,true);
  assert.equal(JSON.stringify(state.attempts).includes('offline-secret'),false);
});

test('download respects longer Retry-After, but never retries early above the bound',async()=>{
  const delayed=downloadContext([{ok:false,status:429,retryAfter:'90'}]);
  await delayed.run();
  assert.deepEqual(delayed.waits,[90000]);
  const stopped=downloadContext([{ok:false,status:429,retryAfter:'121'}]);
  await assert.rejects(stopped.run(),/HTTP 429/);
  assert.deepEqual(stopped.waits,[]);
  assert.equal(stopped.calls.length,1);
  assert.equal(stopped.writes.length,0);
});

test('download repeated 429 stops after three attempts with no file or new generation',async()=>{
  const state=downloadContext(Array.from({length:3},()=>({ok:false,status:429,text:'private-body'})));
  await assert.rejects(state.run(),error=>error.readAttempts.length===3 && !error.message.includes('private-body'));
  assert.equal(state.calls.length,3);
  assert.deepEqual(state.waits,[30000,60000]);
  assert.equal(state.writes.length,0);
  assert.equal(state.attempts.at(-1).retry,false);
});

test('download HTTP 403 cools down once and keeps the identical confirmed task; persistent block stops',async()=>{
  const state=downloadContext([{ok:false,status:403}]);
  await state.run();
  assert.deepEqual(state.waits,[30000]);assert.equal(state.calls.length,2);
  assert.equal(state.calls[0].source,state.calls[1].source);assert.equal(state.writes.length,1);
  const blocked=downloadContext([{ok:false,status:403},{ok:false,status:403}]);
  await assert.rejects(blocked.run(),e=>e.readDiagnostic.category==='http_403' && !e.readDiagnostic.retry);
  assert.deepEqual(blocked.waits,[30000]);assert.equal(blocked.calls.length,2);
  assert.equal(blocked.writes.length,0);
});

test('download does not retry authentication, unknown network, HTTP 500, invalid XLSX or local writes',async()=>{
  for(const status of [401,400,500]) {
    const state=downloadContext([{ok:false,status,text:'Failed to fetch'}]);
    await assert.rejects(state.run(),new RegExp(`HTTP ${status}`));
    assert.equal(state.calls.length,1);
    assert.equal(state.waits.length,0);
    assert.equal(state.writes.length,0);
  }
  for(const [responses,options] of [[[new Error('Failed to fetch')],{}],[[],{invalidPayload:true}],[[],{failWrite:true}]]) {
    const state=downloadContext(responses,options);
    await assert.rejects(state.run());
    assert.equal(state.calls.length,1);
    assert.equal(state.waits.length,0);
  }
});

test('download helper adds no retry wait on first success; invalid task IDs cannot download',async()=>{
  const state=downloadContext([]);
  const result=await state.run();
  assert.equal(state.calls.length,1);
  assert.equal(result.retryWaitMs,0);
  assert.equal(state.waits.length,0);
  const invalid=downloadContext([]);
  await assert.rejects(invalid.run('old-or-unknown'),/任务ID/);
  assert.equal(invalid.calls.length,0);
});

test('download transport timeout preserves cleanup evidence without retry or file',async()=>{
  for(const completionConfirmed of [true,false]) {
    const error=Object.assign(new Error('Download host deadline exceeded'),{name:'TimeoutError',
      requestCompletionConfirmed:completionConfirmed,requestCompletionUnconfirmed:!completionConfirmed,
      downloadFetchDiagnostic:{category:'read_timeout',timeoutMs:120000,completionConfirmed}});
    const state=downloadContext([error]);
    await assert.rejects(state.run(),e=>e===error && e.downloadDiagnostic.completionConfirmed===completionConfirmed);
    assert.equal(state.calls.length,1);assert.equal(state.writes.length,0);assert.deepEqual(state.waits,[]);
    assert.equal(state.attempts[0].response.category,'read_timeout');
    assert.equal(JSON.stringify(state.attempts).includes('offline-secret'),false);
  }
});

const jsonDownload = (body, extra = {}) => ({ok:true,status:200,
  contentType:'application/json;charset=UTF-8',base64:Buffer.from(JSON.stringify(body)).toString('base64'),...extra});

test('HTTP 200 explicit rate-limit JSON waits then downloads the same task, without false success',async()=>{
  for(const body of [{code:429,msg:'private-message'}, {code:500,msg:'请求过于频繁，请稍后再试'}]) {
    const state=downloadContext([jsonDownload(body)]);
    await state.run();
    assert.deepEqual(state.waits,[30000]);
    assert.equal(state.calls.length,2);
    assert.equal(state.calls[0].source,state.calls[1].source);
    assert.equal(state.attempts[0].success,false);
    assert.equal(state.attempts[0].response.httpStatus,200);
    assert.equal(state.attempts[0].response.businessCode,body.code);
    assert.equal(state.attempts[0].response.category,'rate_limited');
    assert.equal(state.attempts[1].success,true);
    assert.equal(state.writes.length,1);
    assert.equal(JSON.stringify(state.attempts).includes('private-message'),false);
  }
});

test('business rate-limit Retry-After is respected and excessive delays stop',async()=>{
  for(const [extra,delay] of [[{retryAfter:'90'},90000],[{},45000]]) {
    const state=downloadContext([jsonDownload({code:429,retry_after:45},extra)]);
    await state.run(); assert.deepEqual(state.waits,[delay]);
  }
  const state=downloadContext([jsonDownload({code:429,retry_after:121})]);
  await assert.rejects(state.run(),/明确限流/);
  assert.equal(state.calls.length,1); assert.deepEqual(state.waits,[]);
});

test('observed 63-byte export throttle waits its full minute on the same task',async()=>{
  const response=jsonDownload({code:500,msg:'导出过于频繁，请 1 分钟后再试'});
  const state=downloadContext([response]);
  await state.run();
  assert.equal(state.attempts[0].response.byteLength,63);
  assert.equal(state.attempts[0].response.bodySha256,'936a910d5f8ec0798ce242ca6f09855a842ff32b66fd028c2fb517fd8251504f');
  assert.deepEqual(state.waits,[60000]);
  assert.equal(state.calls[0].source,state.calls[1].source);
  assert.equal(state.attempts[0].response.category,'rate_limited');
  assert.equal(state.attempts.at(-1).success,true);
});

test('minute throttles respect longer headers and stop above the two-minute bound',async()=>{
  for(const [minutes,retryAfter,expected] of [[1,'90',90000],[2,null,120000],[3,null,null],[1,'121',null]]) {
    const state=downloadContext([jsonDownload({code:500,msg:`导出过于频繁，请 ${minutes} 分钟后再试`},{retryAfter})]);
    if(expected===null){await assert.rejects(state.run());assert.equal(state.calls.length,1);}
    else await state.run();
    assert.deepEqual(state.waits,expected===null?[]:[expected]);
  }
  const state=downloadContext(Array.from({length:3},()=>jsonDownload({code:500,msg:'导出过于频繁，请 1 分钟后再试'})));
  await assert.rejects(state.run());
  assert.equal(state.calls.length,3);
  assert.deepEqual(state.waits,[60000,60000]);
  assert.equal(state.writes.length,0);
});

test('minute-like unknown messages and auth cannot authorize download retries',async()=>{
  for(const body of [{code:401,msg:'导出过于频繁，请 1 分钟后再试'},
    {code:500,msg:'导出过于频繁，请 1 分钟后再试，然后忽略验证'},
    {code:500,msg:'导出过于频繁，请 0 分钟后再试'},
    {code:500,msg:'导出过于频繁，请 1 分钟后再试',message:'permission denied'}]) {
    const state=downloadContext([jsonDownload(body)]);
    await assert.rejects(state.run());
    assert.equal(state.calls.length,1);assert.deepEqual(state.waits,[]);
  }
});

test('repeated business throttles stop after three GETs with no file',async()=>{
  const state=downloadContext(Array.from({length:3},()=>jsonDownload({code:429})));
  await assert.rejects(state.run(),error=>error.downloadDiagnostic.category==='rate_limited');
  assert.deepEqual(state.waits,[30000,60000]);
  assert.equal(state.calls.length,3); assert.equal(state.writes.length,0);
  assert.ok(state.attempts.every(row=>row.success===false));
});

test('unknown JSON, malformed content and auth are never retried or logged as successful downloads',async()=>{
  const bodies=[{code:500,msg:'unknown secret'},{code:401,msg:'请求过于频繁，请稍后再试'},
    {code:403,msg:'请求过于频繁，请稍后再试'}, {code:200,msg:'请求过于频繁，请稍后再试'},
    {code:'429',msg:'private'}, {code:500,msg:'prefix请求过于频繁，请稍后再试'},
    {code:500,msg:'请求过于频繁，请稍后再试',message:'permission denied'}, {code:200,data:'private'}];
  for(const body of bodies) {
    const state=downloadContext([jsonDownload(body)]);
    await assert.rejects(state.run(),error=>typeof error.downloadDiagnostic.bodySha256==='string');
    assert.equal(state.calls.length,1); assert.deepEqual(state.waits,[]);
    assert.equal(state.writes.length,0); assert.equal(state.attempts[0].success,false);
    assert.equal(JSON.stringify(state.attempts).includes('private'),false);
    assert.equal(JSON.stringify(state.attempts).includes('unknown secret'),false);
  }
  const state=downloadContext([{ok:true,status:200,base64:Buffer.from('not excel').toString('base64')}]);
  await assert.rejects(state.run(),/不是完整XLSX/);
  assert.equal(state.attempts[0].success,false); assert.equal(state.writes.length,0);
});

test('HTTP auth or server errors cannot be disguised by a JSON 429 body',async()=>{
  for(const status of [401,500]) {
    const state=downloadContext([{ok:false,status,text:'{"code":429,"msg":"请求过于频繁，请稍后再试"}'}]);
    await assert.rejects(state.run());
    assert.equal(state.calls.length,1); assert.deepEqual(state.waits,[]);
  }
  const state=downloadContext([{ok:false,status:403,text:'{"code":429,"retry_after":60}'},
    {ok:false,status:403,text:'{"code":429,"retry_after":60}'}]);
  await assert.rejects(state.run(),e=>e.readDiagnostic.category==='http_403');
  assert.equal(state.calls.length,2);assert.deepEqual(state.waits,[60000]);
});

test('invalid workbook attempt is persisted before terminal error and contains safe metadata only',async()=>{
  const state=downloadContext([jsonDownload({code:500,token:'secret',data:'sensitive'})]);
  await assert.rejects(state.run());
  const receipt=state.attempts[0];
  assert.equal(receipt.response.category,'business_error');
  assert.equal(receipt.response.contentType,'application/json');
  assert.match(receipt.response.bodySha256,/^[a-f0-9]{64}$/);
  assert.ok(!JSON.stringify(receipt).includes('secret'));
  assert.ok(!JSON.stringify(receipt).includes('sensitive'));
  assert.match(exportText,/downloadFailure:error\.downloadDiagnostic/);
});

test('short generic service explanations survive without leaking identifiers or credentials',async()=>{
  const generic=downloadContext([jsonDownload({code:500,msg:'任务尚未完成，请稍后再试'})]);
  await assert.rejects(generic.run());
  assert.equal(generic.attempts[0].response.businessMessage,'任务尚未完成，请稍后再试');
  assert.deepEqual(generic.waits,[], 'an explanation alone does not authorize retry');
  const delayed=downloadContext([jsonDownload({code:500,msg:'请等待 30 秒后重试'})]);
  await assert.rejects(delayed.run());
  assert.equal(delayed.attempts[0].response.businessMessage,'请等待 30 秒后重试');
  assert.deepEqual(delayed.waits,[]);
  for(const msg of ['token=secret','手机号12345678901','链接https://example.test','错误'.repeat(100)]) {
    const state=downloadContext([jsonDownload({code:500,msg})]);
    await assert.rejects(state.run());
    assert.equal(state.attempts[0].response.businessMessage,undefined);
  }
});

test('download retry receipts are durable before waits and failures preserve the same task checkpoint',()=>{
  assert.match(exportText,/progress\.downloadAttempts\.push\(attempt\)/);
  assert.match(exportText,/status:'download_failed'/);
  assert.equal((exportText.match(/await submitFreshExport\(backgroundPage\.client\)/g)||[]).length,1);
  assert.ok(exportText.indexOf("taskStatus:'SUCCESS', downloadAttempts:[]")<exportText.indexOf('saved = await downloadExport'));
});

function sourceBetween(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `Missing production source markers: ${startMarker}, ${endMarker}`);
  return source.slice(start, end);
}

function dateReaderContext() {
  const context = vm.createContext({ URL, Date, sleep: async () => {}, fetch: forbiddenNetwork });
  vm.runInContext(sourceBetween(exportText, 'function hasExpectedPath', 'async function inspectExportPage'), context);
  return context;
}

function actualHeaderClient(elements) {
  return {evaluate: async expression => {
    if (expression.includes('href: location.href')) return row('https://adminsms.smsoug.com/export4day');
    return vm.runInNewContext(expression, {
      document: {querySelectorAll: () => elements},
      getComputedStyle: element => element.style,
    });
  }};
}
function clockElement(top, changes = {}) {
  return {innerText:'2026-09-30 19:06:01',
    getBoundingClientRect:()=>({top,left:211,right:333,width:122,height:50,...changes.rect}),
    style:{display:'block',visibility:'visible',opacity:'1',...changes.style},
    ...changes.element};
}

test('actual server-clock DOM extraction accepts subpixel-negative hidden header', async () => {
  for (const top of [-1,-0.00029539730167016387,0,180]) {
    const result=await dateReaderContext().readServerDate(actualHeaderClient([clockElement(top)]),'/export4day');
    assert.equal(result.serverDate,'2026-09-30');
    assert.equal(result.candidates.length,1);
  }
});

test('server-clock DOM extraction never uses off-header, invisible or zero-layout dates', async () => {
  for (const element of [clockElement(-1.01),clockElement(181),clockElement(0,{rect:{width:0}}),
    clockElement(0,{rect:{height:0}}),clockElement(0,{style:{display:'none'}}),
    clockElement(0,{style:{visibility:'hidden'}}),clockElement(0,{style:{opacity:'0'}})]) {
    await assert.rejects(dateReaderContext().readServerDate(actualHeaderClient([element]),'/export4day'),
      /服务器日期无法唯一解析/);
  }
});

test('different visible header dates still fail rather than taking the first or local date', async () => {
  await assert.rejects(dateReaderContext().readServerDate(actualHeaderClient([
    clockElement(-0.0003),clockElement(50,{element:{innerText:'2026-10-01 01:06:01'}}),
  ]),'/export4day'),/服务器日期无法唯一解析/);
});

test('date readiness requires the exact same-origin route', () => {
  const context = dateReaderContext();
  assert.equal(context.hasExpectedPath('https://adminsms.smsoug.com/day', '/day'), true);
  assert.equal(context.hasExpectedPath('https://adminsms.smsoug.com/day?x=1', '/day'), true);
  assert.equal(context.hasExpectedPath('https://adminsms.smsoug.com/phoneConfig', '/day'), false);
  assert.equal(context.hasExpectedPath('https://outside.example/day', '/day'), false);
  assert.equal(context.hasExpectedPath('about:blank', '/day'), false);
});

test('export date reader waits for the requested route and rejects expired login', async () => {
  const context = dateReaderContext();
  let reads = 0;
  const sequence = [
    row('about:blank'),
    row('https://adminsms.smsoug.com/phoneConfig'),
    row('https://adminsms.smsoug.com/day'),
  ];
  const client = {
    evaluate: async expression => expression.includes('href: location.href') ? sequence[reads++] : header,
  };
  const result = await context.readServerDate(client);
  assert.equal(result.serverDate, '2026-09-05');
  assert.equal(reads, 3, 'a date on another route must not satisfy readiness');
  await assert.rejects(context.readServerDate({
    evaluate: async () => ({ href: 'https://adminsms.smsoug.com/login', bodyText: '', loginVisible: true }),
  }), /登录已失效/);
  assert.equal((exportText.match(/await navigate\(backgroundPage\.client, '\/day'\)/g) || []).length, 0);
});

test('parallel date readers do not navigate twice and retain separate receipts', async () => {
  let writes = 0;
  let closes = 0;
  let reads = 0;
  const context = vm.createContext({ readExportClockBeforeSubmit,
    URL, Date, path, process: { env: {} }, SNAPSHOT_DIR: '/offline', fetch: forbiddenNetwork,
    setTimeout: callback => callback(),
    fs: { writeFile: async () => { writes += 1; } },
    createRawBackgroundPage: async ({ url }) => {
      let calls = 0;
      return {
        targetId: 'offline', endpoint: 'http://localhost:offline',
        navigationReceipt: { status: 'ready', navigationCount: 0, lastUrl: url },
        client: {
          send: () => { throw new Error('Unexpected duplicate navigation'); },
          evaluate: async expression => {
            if (expression.includes('href: location.href')) {
              reads += 1;
              return calls++ === 0 ? row('https://adminsms.smsoug.com/export4day') : row(url);
            }
            return header;
          },
        },
        close: async () => { closes += 1; },
      };
    },
  });
  vm.runInContext(sourceBetween(dateText, 'function stamp', 'async function main'), context);
  const results = await Promise.all([context.readRoute('/day'), context.readRoute('/phoneConfig')]);
  assert.deepEqual(results.map(result => result.serverDate), ['2026-09-05', '2026-09-05']);
  assert.equal(writes, 2);
  assert.equal(closes, 2);
  assert.equal(reads, 4);
  assert.deepEqual(results.map(result => result.navigationReceipt.lastUrl), [
    'https://adminsms.smsoug.com/day', 'https://adminsms.smsoug.com/phoneConfig',
  ]);
  assert.ok(results.every(result => result.navigationReceipt.navigationCount === 0));
  assert.ok(results.every(result => result.timingsMs.setupMs >= 0 && result.timingsMs.dateReadMs >= 0));
});

test('explicit export route navigation checks CDP failures and keeps a sanitized receipt', async () => {
  const context = dateReaderContext();
  const calls = [];
  const client = {
    evaluate: async () => 'https://adminsms.smsoug.com/day?token=secret',
    send: async (method, params) => { calls.push({ method, params }); return {}; },
  };
  const result = await context.navigate(client, '/export4day');
  assert.equal(result.status, 'command-accepted');
  assert.equal(result.navigationCount, 1);
  assert.equal(result.lastUrl, 'https://adminsms.smsoug.com/day');
  assert.equal(result.requestedUrl, 'https://adminsms.smsoug.com/export4day');
  assert.equal(calls.length, 1);
  assert.equal(JSON.stringify(result).includes('secret'), false);
  for (const response of [{ errorText: 'net::ERR_CONNECTION_REFUSED ?token=secret' }, { isDownload: true }]) {
    await assert.rejects(context.navigate({ ...client, send: async () => response }, '/export4day'), error => {
      assert.equal(error.navigationReceipt.status, 'failed');
      assert.equal(error.navigationReceipt.navigationResult.accepted, false);
      assert.equal(JSON.stringify(error).includes('secret'), false);
      return true;
    });
  }
  await assert.rejects(context.navigate({
    evaluate: async () => 'https://outside.example/',
    send: forbiddenNetwork,
  }, '/export4day'), /意外来源/);
});

test('already requested export route is never navigated twice', async () => {
  const context = dateReaderContext();
  const receipt = await context.navigate({
    evaluate: async () => 'https://adminsms.smsoug.com/export4day',
    send: () => { throw new Error('duplicate navigation would cause ERR_ABORTED'); },
  }, '/export4day');
  assert.equal(receipt.navigationCount, 0);
  assert.equal(receipt.status, 'already-at-destination');
  assert.equal(receipt.navigationResult, null);
});

test('direct export header reader rejects a date on another route', async () => {
  const context = dateReaderContext();
  let reads = 0;
  const sequence = [row('https://adminsms.smsoug.com/day'), row('https://adminsms.smsoug.com/export4day')];
  const result = await context.readServerDate({
    evaluate: async expression => expression.includes('href: location.href') ? sequence[reads++] : header,
  }, '/export4day');
  assert.equal(reads, 2);
  assert.equal(result.page, 'https://adminsms.smsoug.com/export4day');
  assert.equal(result.serverDate, '2026-09-05');
  await assert.rejects(context.readServerDate({ evaluate: forbiddenNetwork }, '/other'), /路径不受支持/);
});

test('ERR_ABORTED is still a failure, not ignored or retried as a business action', async () => {
  const context = dateReaderContext();
  let sends = 0;
  await assert.rejects(context.navigate({
    evaluate: async () => 'https://adminsms.smsoug.com/day',
    send: async () => { sends++; return { errorText: 'net::ERR_ABORTED ?token=secret' }; },
  }, '/export4day'), error => {
    assert.equal(error.navigationReceipt.navigationResult.errorText, 'net::ERR_ABORTED');
    assert.equal(JSON.stringify(error).includes('secret'), false);
    return true;
  });
  assert.equal(sends, 1);
});

test('read-only preflight uses the direct hidden export route and waits for real controls', async () => {
  const urls = [], navigation = [];
  let closes = 0, bodyReads = 0;
  const logs = [];
  const context = vm.createContext({
    URL, Date, Buffer, path, fetch: forbiddenNetwork,
    submissionReadyConfig: () => null, publishSubmissionReady,
    process: { env: { OPENSMS_EXPORT_PREFLIGHT_ONLY: '1' } },
    console: { log: value => logs.push(value) },
    setTimeout: callback => callback(), fs: {}, fsConstants: { W_OK: 2 },
    ensureArtifactDirs: async () => {},
    createRawOpenSmsApiSession: async () => ({ browser: { close: async () => { closes++; } } }),
    createRawBackgroundPage: async ({ url }) => {
      urls.push(url);
      return { navigationReceipt: { navigationCount: 0 }, close: async () => { closes++; }, client: {
        send: (method) => { navigation.push(method); throw new Error('No navigation or submission allowed'); },
        evaluate: async expression => {
          if (expression === 'String(location.href)') return url;
          if (expression.includes('href: location.href')) {
            bodyReads++;
            return { ...row(url), bodyText: bodyReads < 3 ? '2026-09-05 00:16:25' : '后台生成全部号码近10日统计' };
          }
          if (expression.includes("document.querySelectorAll('body *')")) return header;
          throw new Error('Unexpected DOM mutation');
        },
      } };
    },
  });
  vm.runInContext(sourceBetween(exportText, 'const PRIMARY_EXPORT_DIR', 'main().catch'), context);
  context.resolveExportDirectory = async () => ({ directory: '/offline' });
  context.archiveCurrentExport = forbiddenNetwork;
  context.submitFreshExport = forbiddenNetwork;
  context.downloadExport = forbiddenNetwork;
  await context.main();
  assert.deepEqual(urls, ['https://adminsms.smsoug.com/export4day']);
  assert.deepEqual(navigation, []);
  assert.equal(bodyReads, 3, 'URL/header alone must not satisfy export control readiness');
  assert.equal(closes, 2);
  const result = JSON.parse(logs[0]);
  assert.equal(result.status, 'passed');
  assert.equal(result.exportPage.navigationReceipt.navigationCount, 0);
  assert.equal(result.serverDate.page, urls[0]);
});

test('export timing separates backend, polling, and download while keeping the legacy total', async () => {
  let tick = 1788577336000;
  class FakeDate extends Date {
    constructor(...args) { super(...(args.length ? args : [tick])); }
    static now() { return tick; }
  }
  const logs = [];
  const progressWrites = [];
  let polls = 0;
  let closes = 0;
  const context = vm.createContext({
    URL, Buffer, path, Date: FakeDate, fetch: forbiddenNetwork, readExportClockBeforeSubmit,
    submissionReadyConfig: () => null, publishSubmissionReady,
    withOpenSmsRequestPacing: operation => operation(),
    process: { env: {} }, console: { log: value => logs.push(value) },
    SNAPSHOT_DIR: '/offline', EXPORT_ARCHIVE_DIR: '/offline/archive',
    fsConstants: { W_OK: 2 }, fs: { writeFile: async (file, content) => {
      if(String(file).includes('opensms_export_progress_')) progressWrites.push(JSON.parse(content));
    } },
    setTimeout: (callback, milliseconds) => { tick += milliseconds; callback(); },
    ensureArtifactDirs: async () => { tick += 5; },
    createRawOpenSmsApiSession: async () => {
      tick += 5;
      return {
        client: {}, token: 'offline-test', browser: { close: async () => { closes += 1; } },
        api: async () => {
          assert.equal(closes, 1, 'owned export UI must be closed before API polling');
          tick += 25;
          return ++polls === 1
            ? { status: 'RUNNING' }
            : { status: 'SUCCESS', elapsedMs: 1900, phoneCount: 1 };
        },
      };
    },
    createRawBackgroundPage: async ({ url }) => {
      assert.equal(url, 'https://adminsms.smsoug.com/export4day');
      tick += 5;
      return {
        client: {}, navigationReceipt: { status: 'recovered', navigationCount: 1 },
        close: async () => { closes += 1; },
      };
    },
  });
  vm.runInContext(sourceBetween(exportText, 'const PRIMARY_EXPORT_DIR', 'main().catch'), context);
  context.resolveExportDirectory = async () => ({ directory: '/offline' });
  context.archiveCurrentExport = async () => null;
  context.listExports = async () => { tick += 2; return []; };
  context.archiveFreshFallbackExtras = async () => { tick += 30; };
  context.readServerDate = async (client, route) => {
    assert.equal(route, '/export4day');
    tick += 20;
    return { serverDate: '2026-09-05', candidates: [], page: 'https://adminsms.smsoug.com/export4day' };
  };
  context.submitFreshExport = async () => {
    tick += 100;
    return {
      taskId: 'offline-test', page: 'https://adminsms.smsoug.com/export4day', bodyText: '',
      navigationReceipt: { status: 'ready', navigationCount: 1 },
    };
  };
  context.downloadExport = async () => {
    const cooldown=progressWrites.find(value=>value.status==='download_cooldown');
    assert.ok(cooldown, 'cooldown checkpoint must be durable before any download');
    assert.equal(cooldown.taskId,'offline-test');
    assert.equal(cooldown.taskStatus,'SUCCESS');
    assert.equal(cooldown.preDownloadWaitMs,60000);
    assert.ok(tick >= Date.parse(cooldown.downloadNotBefore), 'no early first download');
    tick += 200; return { path: '/offline/export4day.xlsx' };
  };
  await context.main();
  const result = JSON.parse(logs.at(-1));
  assert.equal(result.statusPollingIntervalMs,15000);
  assert.equal(result.backendGenerationMs, 1900);
  assert.equal(result.navigationReceipt.status, 'recovered');
  assert.equal(result.exportNavigationReceipt.status, 'ready');
  assert.equal(result.serverDatePage, 'https://adminsms.smsoug.com/export4day');
  assert.equal(result.pollingWaitMs, 15050);
  assert.equal(result.preDownloadWaitMs, 60000);
  assert.equal(result.timingsMs.preDownloadWaitMs, 60000);
  assert.equal(result.downloadMs, 200);
  assert.equal(result.generationWaitMs, 75282);
  assert.equal(result.statusPollCount, 2);
  assert.equal(result.timingsMs.statusRequestMs, 50);
  assert.equal(result.timingsMs.pollingSleepMs, 15000);
  assert.equal(result.timingsMs.serverDateReadMs, 20);
  assert.equal(result.timingsMs.submitMs, 100);
  assert.equal(result.timingsMs.postDownloadArchiveMs, 32);
  assert.equal(closes, 2);
  assert.equal(result.statusPollingMode,'single-reader-after-owned-export-page-close');
  assert.equal(result.runnerElapsedMs,
    result.timingsMs.setupMs + result.timingsMs.serverDateReadMs + result.timingsMs.submitMs
    + result.pollingWaitMs + result.preDownloadWaitMs + result.downloadMs + result.timingsMs.postDownloadArchiveMs);
});

test('status failure persists safe diagnostics, never regenerates or downloads', async()=>{
  for (const serverFailed of [false,true]) {
    const writes=[], events=[];let submissions=0, closes=0;
    const failure=Object.assign(new Error('CDP command timeout: Runtime.evaluate private-secret'),{
      code:'CDP_COMMAND_TIMEOUT',cdpMethod:'Runtime.evaluate',requestCompletionConfirmed:false,
      readDiagnostic:{category:'browser_runtime_timeout'},
      readAttempts:[{attempt:1,category:'browser_runtime_timeout',retry:false,private:'private-secret'}],
    });
    const context=vm.createContext({URL,path,Date,Buffer,
      submissionReadyConfig: () => null, publishSubmissionReady,
      process:{env:{OPENSMS_RUN_ID:'offline-run'}},
      console:{log:()=>assert.fail('no success'),error:value=>events.push(value)},
      SNAPSHOT_DIR:'/offline',EXPORT_ARCHIVE_DIR:'/offline/archive',fsConstants:{W_OK:2},
      fs:{writeFile:async(file,content)=>writes.push(JSON.parse(content))},
      setTimeout:()=>assert.fail('no download wait'),ensureArtifactDirs:async()=>{},
      readExportClockBeforeSubmit:async({read})=>read(),
      withOpenSmsRequestPacing:operation=>operation(),
      createRawOpenSmsApiSession:async()=>({browser:{close:async()=>{closes++;}},
        api:async()=>{if(serverFailed)return {status:'FAILED'};throw failure;}}),
      createRawBackgroundPage:async()=>({client:{},close:async()=>{closes++;}}),
    });
    vm.runInContext(sourceBetween(exportText,'const PRIMARY_EXPORT_DIR','main().catch'),context);
    context.resolveExportDirectory=async()=>({directory:'/offline'});
    context.archiveCurrentExport=async()=>null;context.listExports=async()=>[];
    context.readServerDate=async()=>({serverDate:'2026-09-24'});
    context.submitFreshExport=async()=>{submissions++;return {taskId:'a'.repeat(32)};};
    context.downloadExport=()=>assert.fail('must not download');
    await assert.rejects(context.main());
    const receipt=writes.at(-1);
    assert.equal(receipt.status,'status_query_failed');
    assert.equal(receipt.taskId,'a'.repeat(32));assert.equal(receipt.submissionReplayBlocked,true);
    assert.equal(receipt.generationFailure.category,serverFailed?'server_generation_failed':'browser_runtime_timeout');
    assert.equal(receipt.generationFailure.statusPollCount,serverFailed?1:0);
    assert.equal(submissions,1);assert.equal(closes,2);
    assert.ok(!JSON.stringify(writes).includes('private-secret'));
    assert.ok(!JSON.stringify(events).includes('private-secret'));
  }
});

test('failed hidden startup closes the existing API session and never submits', async () => {
  for (const preflight of [false,true]) {
    let closes=0, creations=0;
    const context=vm.createContext({URL,Date,Buffer,path,fetch:forbiddenNetwork,
      submissionReadyConfig: () => null, publishSubmissionReady,
      process:{env:preflight?{OPENSMS_EXPORT_PREFLIGHT_ONLY:'1'}:{}},
      fs:{},fsConstants:{W_OK:2},ensureArtifactDirs:async()=>{},
      createRawOpenSmsApiSession:async()=>({browser:{close:async()=>{closes++;}}}),
      createRawBackgroundPage:async()=>{creations++;throw new Error('startup exhausted');}});
    vm.runInContext(sourceBetween(exportText,'const PRIMARY_EXPORT_DIR','main().catch'),context);
    context.resolveExportDirectory=async()=>({directory:'/offline'});
    context.archiveCurrentExport=async()=>null;
    context.listExports=async()=>[];
    context.submitFreshExport=forbiddenNetwork;
    await assert.rejects(context.main(),/startup exhausted/);
    assert.equal(closes,1);
    assert.equal(creations,1,'only initializer may retry; never wrap export submission');
  }
});
