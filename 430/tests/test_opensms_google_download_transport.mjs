import {test} from 'node:test';
import assert from 'node:assert/strict';
import {downloadGoogleWorkbook} from '../scripts/opensms_google_download_transport.mjs';

const url='https://docs.google.com/spreadsheets/d/fixture/export?format=xlsx';
const bytes=Buffer.alloc(11000);bytes[0]=80;bytes[1]=75;
const response=(status=200,data=bytes,headers={})=>({ok:status===200,status,
  headers:{get:key=>headers[key]||null},body:{cancel:async()=>{}},arrayBuffer:async()=>data});
function fixture(items) {
  let time=0,calls=0;const delays=[];
  const options={now:()=>time,sleep:async ms=>{delays.push(ms);time+=ms;},fetchImpl:async (source,opts)=>{
    assert.equal(source,url);assert.equal(opts.method,'GET');assert.ok(opts.signal);
    const item=items[Math.min(calls++,items.length-1)];if(item instanceof Error)throw item;return item;
  }};
  return {run:()=>downloadGoogleWorkbook(url,options),calls:()=>calls,delays};
}
test('valid workbook downloads once without fixed waits',async()=>{
  const f=fixture([response()]);const result=await f.run();
  assert.equal(result.bytes.length,11000);assert.equal(f.calls(),1);assert.deepEqual(f.delays,[]);
  assert.equal(result.attempts[0].timeoutMs,60000);
});
test('a progressing download over 20 seconds retains the full shared deadline',async()=>{
  let time=0,calls=0;
  const result=await downloadGoogleWorkbook(url,{now:()=>time,fetchImpl:async()=>{
    calls++;
    return {...response(),arrayBuffer:async()=>{time=35000;return bytes;}};
  }});
  assert.equal(calls,1);assert.equal(result.elapsedMs,35000);
  assert.equal(result.attempts[0].timeoutMs,60000);
});
test('a retry uses only the remaining total budget, not a new 60 seconds',async()=>{
  let time=0,calls=0;
  const result=await downloadGoogleWorkbook(url,{now:()=>time,sleep:async ms=>{time+=ms;},fetchImpl:async()=>{
    if(calls++===0) {
      time=58000;throw Object.assign(new Error('private'),{cause:{code:'ECONNRESET'}});
    }
    return response();
  }});
  assert.equal(calls,2);assert.equal(result.attempts[1].timeoutMs,1500);
});
test('streamed workbook chunks are assembled only after the response completes',async()=>{
  let reads=0,released=false;
  const result=await downloadGoogleWorkbook(url,{fetchImpl:async()=>({...response(),body:{getReader:()=>({
    read:async()=>reads++===0?{value:bytes.subarray(0,4000),done:false}
      :reads===2?{value:bytes.subarray(4000),done:false}:{done:true},
    releaseLock:()=>{released=true;},
  })}})});
  assert.deepEqual(result.bytes,bytes);assert.equal(released,true);
});
test('partial response timeout preserves safe byte and phase diagnostics without saving content',async()=>{
  let reads=0,released=false;
  await assert.rejects(downloadGoogleWorkbook(url,{maxAttempts:1,fetchImpl:async()=>({...response(),body:{getReader:()=>({
    read:async()=>{
      if(reads++===0)return {value:bytes.subarray(0,4000),done:false};
      throw Object.assign(new Error('private-body'),{name:'TimeoutError'});
    },releaseLock:()=>{released=true;},
  })}})}),e=>{
    assert.equal(e.googleDownloadDiagnostic.category,'read_timeout');
    assert.equal(e.downloadAttempts[0].phase,'response_body');
    assert.equal(e.downloadAttempts[0].httpStatus,200);
    assert.equal(e.downloadAttempts[0].receivedBytes,4000);
    assert.ok(!JSON.stringify(e.downloadAttempts).includes('private'));
    return true;
  });
  assert.equal(released,true);
});
test('partial streamed failure discards all previous chunks before a fresh retry',async()=>{
  let calls=0,reads=0;
  const result=await downloadGoogleWorkbook(url,{sleep:async()=>{},fetchImpl:async()=>calls++===0
    ? {...response(),body:{getReader:()=>({read:async()=>{
      if(reads++===0)return {value:Buffer.from('discard-this-partial-content'),done:false};
      throw Object.assign(new Error('partial'),{cause:{code:'UND_ERR_SOCKET'}});
    },releaseLock:()=>{}})}}:response()});
  assert.equal(calls,2);assert.deepEqual(result.bytes,bytes);
  assert.equal(result.attempts[0].phase,'response_body');
});
test('invalid retry budgets stop before making any request',async()=>{
  for(const settings of [{maxAttempts:1.5},{maxAttempts:4},{totalTimeoutMs:60001},
    {totalTimeoutMs:Infinity},{attemptTimeoutMs:NaN}]) {
    let calls=0;
    await assert.rejects(downloadGoogleWorkbook(url,{...settings,fetchImpl:async()=>{calls++;return response();}}),/参数无效/);
    assert.equal(calls,0);
  }
});
test('transient connection and partial body failures retry a fresh GET',async()=>{
  for(const first of [Object.assign(new TypeError('fetch failed secret'),{cause:{code:'ECONNRESET'}}),
    {...response(),arrayBuffer:async()=>{throw Object.assign(new Error('body'),{cause:{code:'UND_ERR_SOCKET'}});}}]) {
    const f=fixture([first,response()]);const result=await f.run();
    assert.equal(result.attempts.length,2);assert.equal(f.calls(),2);
    assert.ok(!JSON.stringify(result.attempts).includes('secret'));
  }
});
test('HTTP transient failures retry but forbidden and invalid workbook never do',async()=>{
  for(const status of [429,500,502,503,504]) assert.equal((await fixture([response(status),response()]).run()).attempts.length,2);
  for(const first of [response(403),response(401),response(200,Buffer.from('<html>login</html>'))]) {
    const f=fixture([first,response()]);await assert.rejects(f.run(),e=>e.code==='OPENSMS_GOOGLE_DOWNLOAD_FAILED');
    assert.equal(f.calls(),1);
  }
});
test('retry cap and server retry-after do not permit endless loops',async()=>{
  const f=fixture([response(503)]);await assert.rejects(f.run(),e=>e.downloadAttempts.length===3);
  assert.equal(f.calls(),3);
  const limited=fixture([response(429,bytes,{'retry-after':'120'})]);
  await assert.rejects(limited.run(),/超过总时限/);assert.equal(limited.calls(),1);
});
test('unclassified errors are redacted and not retried',async()=>{
  const f=fixture([new Error('secret-marker')]);
  await assert.rejects(f.run(),e=>!e.message.includes('secret-marker')&&e.downloadAttempts[0].status==='UNCLASSIFIED_TRANSPORT_ERROR');
  assert.equal(f.calls(),1);
});
test('failure diagnostics distinguish timeout, HTTP, invalid workbook and transport without secrets',async()=>{
  for(const [item,category,count] of [
    [Object.assign(new Error('private-timeout'),{name:'TimeoutError'}),'read_timeout',3],
    [response(403),'http_error',1],
    [response(200,Buffer.from('private-login-html')),'invalid_xlsx',1],
    [new Error('private-token'),'transport_error',1],
  ]) {
    const f=fixture([item]);
    await assert.rejects(f.run(),e=>{
      assert.equal(e.googleDownloadDiagnostic.category,category);
      assert.equal(e.googleDownloadDiagnostic.attemptCount,count);
      assert.ok(!JSON.stringify(e.googleDownloadDiagnostic).includes('private'));
      return true;
    });
    assert.equal(f.calls(),count);
  }
});
