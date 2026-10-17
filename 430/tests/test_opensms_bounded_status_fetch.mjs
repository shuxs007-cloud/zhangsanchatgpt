import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import {fetchExportStatusBounded,fetchExportDownloadBounded} from '../opensms_bounded_status_fetch.mjs';

const input = {
  requestUrl:'https://adminsms.smsoug.com/api/adminExport/export4day/async/status?taskId='+'a'.repeat(32),
  requestMethod:'GET', requestBody:null, authorization:'offline-secret', extraHeaders:{}, readTimeoutMs:45000,
};
function harness(mode='success', download=false) {
  const request = download ? {...input, requestUrl:input.requestUrl.replace('/status','/download'),
    readTimeoutMs:120000} : input;
  let now=0, fetches=0, aborts=0, reads=0;
  const expressions=[];
  const pending=signal=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>{
    aborts++;
    if(mode!=='ignore-abort') reject(new DOMException('aborted','AbortError'));
  },{once:true}));
  const context=vm.createContext({AbortController, Uint8Array, btoa,
    location:{origin:'https://adminsms.smsoug.com'},
    fetch:async(url,options)=>{
      fetches++;
      assert.equal(url,request.requestUrl); assert.equal(options.method,'GET');
      if(['hang','ignore-abort','renderer-timeout','context-lost'].includes(mode)) return pending(options.signal);
      if(mode==='network') throw new TypeError('Failed to fetch private text');
      const limited=['429','late-429'].includes(mode);
      return {ok:!limited,status:limited?429:200,url,
        headers:{get:()=>limited?'60':null},
        arrayBuffer:async()=>mode==='body-hang'?pending(options.signal):Uint8Array.from([80,75,1,2]).buffer,
        text:async()=>mode==='body-hang'?pending(options.signal):'{"status":"SUCCESS"}'};
    }});
  const client={evaluate:async(expression,timeout)=>{
    assert.ok(timeout>0 && timeout<=5000);
    assert.ok(expression.startsWith('(() => {'), 'outer CDP expression must finish synchronously');
    expressions.push(expression);
    if(!expression.includes('const input =')) {
      reads++;
      if(['reply-lost','late-429'].includes(mode) && reads===1) {
        await Promise.resolve();
        throw Object.assign(new Error('CDP command timeout: Runtime.evaluate'),
          {code:'CDP_COMMAND_TIMEOUT',cdpMethod:'Runtime.evaluate'});
      }
      if(mode==='renderer-timeout') throw Object.assign(new Error('CDP command timeout: Runtime.evaluate'),
        {code:'CDP_COMMAND_TIMEOUT',cdpMethod:'Runtime.evaluate'});
      if(mode==='context-lost' && reads===1) {
        vm.runInContext("for(const key of Object.keys(globalThis).filter(k=>k.startsWith('__opensms_'))) delete globalThis[key];",context);
      }
    }
    const value=vm.runInContext(expression,context);
    assert.equal(typeof value?.then,'undefined');
    return value;
  }};
  return {run:(overrides={})=>(download?fetchExportDownloadBounded:fetchExportStatusBounded)(client,{...request,...overrides}, {
    clock:()=>now,sleep:async ms=>{now+=ms;}}), context,expressions,
    counts:()=>({fetches,aborts,reads,now})};
}

test('one same-task GET, synchronous CDP snapshots, complete cleanup',async()=>{
  const h=harness(); const result=await h.run();
  assert.equal(result.status,200); assert.equal(JSON.parse(result.text).status,'SUCCESS');
  assert.equal(h.counts().fetches,1);
  assert.equal(Object.keys(h.context).filter(k=>k.startsWith('__opensms_status_')).length,0);
  assert.ok(h.expressions.every(e=>!e.includes('bringToFront')&&!e.includes('Page.navigate')));
});

test('download uses one binary GET with synchronous snapshots and no foreground activation',async()=>{
  const h=harness('success',true), response=await h.run();
  assert.equal(response.status,200);
  assert.equal(response.base64,Buffer.from([80,75,1,2]).toString('base64'));
  assert.equal(h.counts().fetches,1);
  assert.equal(Object.keys(h.context).filter(k=>k.startsWith('__opensms_download_')).length,0);
  assert.ok(h.expressions.every(e=>!e.includes('bringToFront')&&!e.includes('Page.navigate')));
});
test('download deadlines abort and drain; unconfirmed completion fails closed',async()=>{
  for(const mode of ['hang','body-hang','ignore-abort','renderer-timeout','context-lost']) {
    const h=harness(mode,true);
    await assert.rejects(h.run(),error=>{
      assert.equal(error.requestCompletionConfirmed,['hang','body-hang'].includes(mode),mode);
      assert.equal(error.requestCompletionUnconfirmed,!['hang','body-hang'].includes(mode),mode);
      assert.equal(error.downloadFetchDiagnostic.timeoutMs,120000);
      assert.ok(!JSON.stringify(error.downloadFetchDiagnostic).includes('offline-secret'));
      return true;
    });
    assert.equal(h.counts().fetches,1); assert.ok(h.counts().now<=130000);
  }
});
test('download lost replies consume completed binary or 429 response without replay',async()=>{
  for(const mode of ['reply-lost','late-429']) {
    const h=harness(mode,true),response=await h.run();
    assert.equal(response.status,mode==='late-429'?429:200);
    assert.equal(h.counts().fetches,1);
  }
});
test('download rejects foreign or unconfirmed task requests before dispatch',async()=>{
  for(const overrides of [{requestMethod:'POST'},{requestBody:'x'},{readTimeoutMs:45000},
    {requestUrl:input.requestUrl},{requestUrl:input.requestUrl.replace('adminsms.smsoug.com','other.invalid')},
    {requestUrl:input.requestUrl.replace('/status','/download').replace('a'.repeat(32),'unknown')}]) {
    const h=harness('success',true);await assert.rejects(h.run(overrides),/Invalid bounded/);
    assert.equal(h.counts().fetches,0);
  }
});
test('host deadline works when page timeout timers never fire, abort drains before retry',async()=>{
  for(const mode of ['hang','body-hang']) {
    const h=harness(mode);
    await assert.rejects(h.run(),error=>error.name==='TimeoutError'
      && error.requestCompletionConfirmed===true && !error.requestCompletionUnconfirmed
      && error.statusFetchDiagnostic.timeoutMs===45000);
    assert.equal(h.counts().fetches,1); assert.equal(h.counts().aborts,1);
    assert.ok(h.counts().now>=45000 && h.counts().now<=55000);
    assert.equal(Object.keys(h.context).filter(k=>k.startsWith('__opensms_status_')).length,0);
  }
});
test('unresponsive renderer or unconfirmed abort fails closed without another GET',async()=>{
  for(const mode of ['renderer-timeout','ignore-abort','context-lost']) {
    const h=harness(mode);
    await assert.rejects(h.run(),error=>{
      assert.equal(error.requestCompletionUnconfirmed,true,mode);
      assert.equal(error.requestCompletionConfirmed,false,mode);
      return true;
    });
    assert.equal(h.counts().fetches,1); assert.ok(h.counts().now<=55000);
  }
});
test('response status and Retry-After survive; fetch errors disclose only category',async()=>{
  const limited=await harness('429').run(); assert.equal(limited.status,429);assert.equal(limited.retryAfter,'60');
  const failed=await harness('network').run();assert.equal(failed.transportError.name,'NetworkError');
  assert.ok(!JSON.stringify(failed).includes('private'));assert.ok(!JSON.stringify(failed).includes('offline-secret'));
});
test('lost CDP snapshot consumes the same completed HTTP response, including rate limits',async()=>{
  for(const mode of ['reply-lost','late-429']) {
    const h=harness(mode); const response=await h.run();
    assert.equal(response.status, mode==='late-429'?429:200);
    if(mode==='late-429') assert.equal(response.retryAfter,'60');
    assert.equal(h.counts().fetches,1);
  }
});
test('reject business writes, downloads, foreign origins and invalid task identity before dispatch',async()=>{
  for(const overrides of [{requestMethod:'POST'}, {requestBody:'payload'}, {readTimeoutMs:60000},
    {requestUrl:input.requestUrl.replace('adminsms.smsoug.com','other.invalid')},
    {requestUrl:input.requestUrl.replace('/status','/download')},
    {requestUrl:input.requestUrl.replace('a'.repeat(32),'bad')},
    {requestUrl:input.requestUrl+'&taskId='+'a'.repeat(32)},
    {requestUrl:input.requestUrl+'&extra=x'}, {requestUrl:input.requestUrl+'#x'}]) {
    const h=harness();await assert.rejects(h.run(overrides),/Invalid bounded/);assert.equal(h.counts().fetches,0);
  }
});
test('protocol uses this transport only for status and still uses shared pacing and retry wrapper',()=>{
  const source=fs.readFileSync(new URL('../opensms_protocol_client.mjs',import.meta.url),'utf8');
  assert.match(source,/method === 'GET' && apiPath === '\/adminExport\/export4day\/async\/status'/);
  assert.match(source,/await fetchExportStatusBounded\(client, input\)/);
  assert.match(source,/withReadRetry\(\(\) => withOpenSmsRequestPacing\(/);
});
