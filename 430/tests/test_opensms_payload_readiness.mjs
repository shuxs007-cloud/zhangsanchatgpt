import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readPayloadPageState, waitForPayloadPageReady} from '../opensms_payload_readiness.mjs';

const origin='https://adminsms.smsoug.com';
const ready={origin,readyState:'interactive',appScriptCount:1,scriptUrl:`${origin}/static/js/app.abc123.js`};
function fixture(states, options={}) {
  let time=0, calls=0;
  const client={evaluate:async(_expression,budget)=>{
    assert.ok(budget>0 && budget<=5000);
    const state=states[Math.min(calls++,states.length-1)];
    if(state instanceof Error) throw state;
    return state;
  }};
  return {run:()=>waitForPayloadPageReady(client,{timeoutMs:1000,pollMs:100,now:()=>time,sleep:async ms=>{time+=ms;},...options}),
    calls:()=>calls};
}
test('ready page has no fixed wait',async()=>{
  const f=fixture([ready]); const result=await f.run();
  assert.equal(result.status,'ready');assert.equal(result.elapsedMs,0);assert.equal(f.calls(),1);
});
test('same-origin loading URL does not release consumers before script readiness',async()=>{
  const f=fixture([{origin,readyState:'loading',appScriptCount:0},
    {...ready,readyState:'loading'},ready]);
  const result=await f.run();assert.equal(result.polls,3);assert.equal(result.elapsedMs,200);
});
test('never-ready page times out with metadata not secrets',async()=>{
  const f=fixture([{origin,readyState:'loading',appScriptCount:0}]);
  await assert.rejects(f.run(),e=>e.code==='OPENSMS_APP_READY_TIMEOUT'
    &&e.applicationReadiness.elapsedMs===1000&&e.applicationReadiness.appScriptCount===0);
  assert.equal(f.calls(),10);
});
test('foreign origin, ambiguous build and completed challenge page fail closed',async()=>{
  for(const [state,code] of [
    [{...ready,origin:'https://other.invalid'},'OPENSMS_APP_ORIGIN_MISMATCH'],
    [{...ready,appScriptCount:2},'OPENSMS_APP_BUNDLE_AMBIGUOUS'],
    [{origin,readyState:'complete',appScriptCount:0},'OPENSMS_APP_BUNDLE_MISSING'],
  ]) await assert.rejects(fixture([state]).run(),e=>e.code===code);
});
test('only initial context replacements are retried',async()=>{
  assert.equal((await fixture([new Error('Execution context was destroyed'),ready]).run()).polls,2);
  await assert.rejects(fixture([new Error('secret-marker')]).run(),e=>
    e.code==='OPENSMS_APP_PROBE_FAILED'&&!JSON.stringify(e).includes('secret-marker'));
});
test('probe only accepts unique exact same-origin app asset',()=>{
  const context={URL,Set,location:{origin},document:{readyState:'complete',scripts:[
    {src:`${origin}/static/js/app.abc123.js`},{src:`${origin}/static/js/app.abc123.js`},
    {src:'https://evil.invalid/static/js/app.abc123.js'},
    {src:`${origin}/static/js/app.other.js?secret=x`},
  ]}};
  const result=vm.runInNewContext(`(${readPayloadPageState.toString()})()`,context);
  assert.equal(result.appScriptCount,1);assert.equal(result.scriptUrl,ready.scriptUrl);
  assert.equal(JSON.stringify(result).includes('secret'),false);
});
