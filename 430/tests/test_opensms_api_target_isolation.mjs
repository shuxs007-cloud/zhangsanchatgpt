import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {retryableRead,withReadRetry,readRetryAfterMs} from '../opensms_read_retry.mjs';
import {selectPinnedOpenSmsTarget,selectExistingOpenSmsTarget} from '../opensms_protocol_client.mjs';
import {requestTimeouts} from '../opensms_request_pacing.mjs';
import {validateOpenSmsApiResponse} from '../opensms_api_response.mjs';
import {loadRawPayloadCodec} from '../opensms_payload_build_recovery.mjs';
const source=fs.readFileSync(new URL('../opensms_protocol_client.mjs',import.meta.url),'utf8');
const between=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));
const page=(id,url='https://adminsms.smsoug.com/day')=>({id,type:'page',url,webSocketDebuggerUrl:`ws://local/${id}`});

async function pick(pages, env={OPENSMS_RAW_API_TARGET_ID:'owned'}) {
  const visited=[];
  const context=vm.createContext({URL,process:{env},selectPinnedOpenSmsTarget,selectExistingOpenSmsTarget,BITBROWSER_430_PROFILE_ID:'430',
    readDevtoolsPorts:()=>[{profileId:'430',endpoint:'http://localhost:430'},{profileId:'431',endpoint:'http://localhost:431'}],
    readRawTargets:async endpoint=>{visited.push(endpoint);return pages;}});
  vm.runInContext(between('async function findRawTarget','export async function connectRawOpenSmsPage'),context);
  return {result:await context.findRawTarget('http://localhost:430','/day'),visited};
}
test('pinned API target never borrows the export temporary /day target',async()=>{
  const {result,visited}=await pick([page('export-temporary'),page('owned')]);
  assert.equal(result.target.id,'owned');
  assert.deepEqual(visited,['http://localhost:430']);
});
test('missing or foreign-origin pinned target fails closed without another-profile fallback',async()=>{
  for(const pages of [[page('other')],[page('owned','http://other.example/day')],
    [page('owned','http://adminsms.smsoug.com/day')],[page('owned','https://adminsms.smsoug.com:8080/day')]]) {
    await assert.rejects(pick(pages),/本轮专属API页面缺失/);
  }
});
test('non-pinned legacy exact-route selection remains compatible',async()=>{
  const {result}=await pick([page('phone','https://adminsms.smsoug.com/phoneConfig'),page('day')],{});
  assert.equal(result.target.id,'day');
});

async function makeApi(outcomes) {
  const expressions=[],events=[];
  const client={evaluate:async expression=>{
    if(expression.includes('document.cookie.split'))return 'test-token';
    expressions.push(expression);
    const result=outcomes.shift();
    if(result instanceof Error)throw result;
    return result;
  },close:()=>{},send:async()=>({})};
  const context=vm.createContext({URL,Date,AbortSignal,DEFAULT_CDP:'http://localhost:430',
    console:{error:line=>events.push(JSON.parse(line))}, requestTimeouts, validateOpenSmsApiResponse,
    withOpenSmsRequestPacing:operation=>operation(),
    fetchExportStatusBounded:(client,input)=>client.evaluate(JSON.stringify(input)),
    connectRawOpenSmsPage:async()=>({client,target:page('owned'),endpoint:'http://localhost:430'}),
    loadRawPayloadCodec:(raw,options)=>loadRawPayloadCodec(raw,{...options,
      loadCodec:async()=>({metadata:{source:'offline-fixture'}})}),
    createRawBackgroundPage:async()=>assert.fail('healthy fixture must not create a page'),
    encryptPayload:()=> 'encrypted-test-payload',parseResponseText:JSON.parse,retryableRead,readRetryAfterMs,
    withReadRetry:(op,opts)=>withReadRetry(op,{...opts,sleep:async()=>{}})});
  vm.runInContext(between('export async function createRawOpenSmsApiSession','/**\n * Load one OpenSMS route').replace('export async','async'),context);
  return {api:(await context.createRawOpenSmsApiSession()).api,expressions,events};
}
test('production raw API integrates read retry with target/page diagnostics and no credentials logged',async()=>{
  const {api,expressions,events}=await makeApi([new Error('TypeError: Failed to fetch'),{ok:true,status:200,text:'{"code":200,"rows":[],"total":0}'}]);
  assert.equal((await api('GET','/carrier/config/listNotDeleted',null,{pageNum:2,pageSize:20})).code,200);
  assert.equal(expressions.length,2);
  assert.equal(events[0].readRequest.targetId,'owned');
  assert.equal(events[0].readRequest.pageNum,2);
  assert.ok(!JSON.stringify(events).includes('test-token'));
  assert.ok(expressions[0].includes('AbortSignal.timeout'));
});
test('production raw API never replays a failed POST',async()=>{
  for(const failure of [new Error('Failed to fetch'),{ok:false,status:504,text:'{"code":504}'}]) {
    const {api,expressions}=await makeApi([failure]);
    await assert.rejects(api('POST','/phone/config/batchSetChatExpire',{cardNos:['test'],expireDate:'2026-09-06'}));
    assert.equal(expressions.length,1);
    assert.ok(expressions[0].includes('"readTimeoutMs":null'));
  }
});

test('actual browser DOMException is serialized with its name and activates read-only recovery',async()=>{
  const {api,expressions}=await makeApi([{transportError:{name:'TimeoutError'}},
    {ok:true,status:200,text:'{"code":200}'}]);
  assert.equal((await api('GET','/carrier/config/listNotDeleted')).code,200);
  assert.equal(expressions.length,2);
  const response=await vm.runInNewContext(expressions[0],{AbortSignal,
    fetch:async()=>{throw new DOMException('signal timed out','TimeoutError');}});
  assert.equal(response.transportError.name,'TimeoutError');
  assert.ok(!JSON.stringify(response).includes('test-token'));
});
test('serialized timeout on a business write is not retried',async()=>{
  const {api,expressions}=await makeApi([{transportError:{name:'TimeoutError'}}]);
  await assert.rejects(api('POST','/phone/config/batchSetChatExpire',{cardNos:['test']}),{name:'TimeoutError'});
  assert.equal(expressions.length,1);
});

test('export status 429 honors response Retry-After while never changing taskId',async()=>{
  const {api,expressions,events}=await makeApi([
    {ok:false,status:429,retryAfter:'30',text:'{"retry_after":30}'},
    {ok:true,status:200,text:'{"status":"SUCCESS"}'}]);
  const taskId='f'.repeat(32);
  assert.equal((await api('GET','/adminExport/export4day/async/status',null,{taskId})).status,'SUCCESS');
  assert.equal(expressions.length,2);
  assert.ok(expressions.every(e=>e.includes(taskId)));
  assert.equal(events[0].readRequest.waitMs,30000);
});

test('403 nil cools down a confirmed read once but never replays writes or an export submission',async()=>{
  for (const [method, apiPath] of [['POST','/phone/config/list'],
    ['GET','/adminExport/export4day/async/status'], ['POST','/phone/config/batchSetChatExpire']]) {
    const attempts=method==='GET'?2:1;
    const {api,expressions,events}=await makeApi(Array.from({length:attempts},()=>({ok:false,status:403,text:'nil\n'})));
    await assert.rejects(api(method,apiPath,null,{taskId:'f'.repeat(32)}),error=>
      error.status===403 && error.readDiagnostic.category==='http_403'
      && error.readDiagnostic.retry===false && /响应内容为nil/.test(error.message));
    assert.equal(expressions.length,attempts);
    assert.equal(events[0].readRequest.success,false);
    assert.equal(events[0].readRequest.retry,method==='GET');
    assert.equal(events[0].readRequest.waitMs,method==='GET'?30000:0);
    assert.ok(!JSON.stringify(events).includes('test-token'));
  }
});

test('unparseable success stays a protocol failure without leaking its body or retrying',async()=>{
  const {api,expressions,events}=await makeApi([{ok:true,status:200,text:'private-response-cookie-secret'}]);
  await assert.rejects(api('GET','/carrier/config/listNotDeleted'),error=>
    error.readDiagnostic.category==='response_parse_failed'
    && !error.message.includes('private-response'));
  assert.equal(expressions.length,1);
  assert.equal(events[0].readRequest.success,false);
});
