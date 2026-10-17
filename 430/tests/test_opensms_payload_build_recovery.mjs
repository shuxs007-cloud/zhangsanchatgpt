import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {loadRawPayloadCodec} from '../opensms_payload_build_recovery.mjs';
import {PAYLOAD_ORIGIN, createPayloadCodec} from '../opensms_payload_codec.mjs';
import {probeExistingSession} from '../scripts/opensms_login_recovery_cli.mjs';

const cdp='http://127.0.0.1:12345';
const oldUrl=`${PAYLOAD_ORIGIN}/static/js/app.old.js`, newUrl=`${PAYLOAD_ORIGIN}/static/js/app.new.js`;
const cfg={origin:PAYLOAD_ORIGIN,scriptUrl:newUrl,
  key:Buffer.alloc(32,4).toString('base64'),iv:Buffer.alloc(16,5).toString('base64')};
const codec=createPayloadCodec(cfg);
function staleError() {
  return Object.assign(new Error('known-stale'),{code:'OPENSMS_PROTOCOL_ERROR',payloadFailureKind:'stale-app-bundle',
    payloadDiagnostic:{origin:PAYLOAD_ORIGIN,scriptUrl:oldUrl,replacementScriptUrl:newUrl}});
}
function fixture(overrides={}) {
  const events=[];
  let loads=0;
  const originalInfo={targetId:'original',type:'page',url:`${PAYLOAD_ORIGIN}/index`,browserContextId:'same-context'};
  const freshInfo={targetId:'owned',type:'other',url:`${PAYLOAD_ORIGIN}/index`,browserContextId:'same-context'};
  const raw={target:{id:'original',type:'page',url:originalInfo.url},client:{
    evaluate:async()=>{events.push('original-read');},
    send:async(method,params)=>{assert.equal(method,'Target.getTargetInfo');events.push('original-info');
      return {targetInfo:params.targetId==='anchor'?{...originalInfo,targetId:'anchor',type:'page'}:originalInfo};},
  }};
  const owned={targetId:'owned',hidden:true,client:{evaluate:async()=>events.push('owned-read')},
    browserClient:{send:async()=>{events.push('owned-info');return {targetInfo:freshInfo};}},
    close:async()=>events.push('owned-close')};
  const options={cdp,browserKind:'cloakbrowser',
    createBackgroundPage:async input=>{events.push('owned-create');assert.equal(input.cdp,cdp);
      assert.equal(input.url,`${PAYLOAD_ORIGIN}/index`);
      assert.equal(input.authenticatedTargetId,raw.target.type==='page'?'original':'anchor');return owned;},
    loadCodec:async evaluate=>{loads++;await evaluate('fixture');if(loads===1)throw staleError();return codec;},
    waitUntilReady:async()=>{events.push('ready');return {status:'ready'};},
    withReadPacing:async operation=>{events.push('paced');return operation();},
    fetcher:async(url)=>{events.push('independent-close-check');assert.equal(url,`${cdp}/json/list`);
      return {ok:true,json:async()=>[{id:'original'}]};},...overrides};
  return {raw,owned,events,originalInfo,freshInfo,options,run:()=>loadRawPayloadCodec(raw,options),loads:()=>loads};
}

test('healthy build makes no hidden page, refresh, recovery or extra requests',async()=>{
  const f=fixture({loadCodec:async()=>codec});
  const result=await f.run();assert.equal(result.codec,codec);assert.equal(result.recovery,null);
  assert.deepEqual(f.events,[]);
});
test('proven stale build uses same-context fresh hidden page once, closes and independently verifies before return',async()=>{
  const f=fixture();const result=await f.run();
  assert.equal(result.codec,codec);assert.equal(f.loads(),2);
  assert.deepEqual(f.events,['original-read','original-info','owned-create','owned-info','ready','paced','owned-read','owned-close','independent-close-check']);
  assert.equal(result.recovery.status,'recovered');assert.equal(result.recovery.attempts,1);
  assert.equal(result.recovery.sameBrowserContext,true);assert.equal(result.recovery.ownedTargetClosed,true);
  assert.equal(result.recovery.originalPageNavigated,false);assert.equal(result.recovery.businessRequestsDuringRecovery,0);
  assert.doesNotMatch(JSON.stringify(result.recovery),new RegExp(cfg.key.replace(/[+]/g,'\\+')));
  assert.ok(!JSON.stringify(result.recovery).includes(cfg.iv));
});
test('protocol, HTTP, authentication and unknown errors never create a recovery page',async()=>{
  for(const error of [Object.assign(new Error('protocol'),{code:'OPENSMS_PROTOCOL_ERROR'}),
    ...[401,403,429,502].map(status=>Object.assign(new Error('http'),{status})),new Error('unknown')]) {
    const f=fixture({loadCodec:async()=>{throw error;}});
    await assert.rejects(f.run(),e=>e===error);assert.deepEqual(f.events,[]);
  }
});
test('unsupported browser, foreign endpoint and invalid stale evidence do not recover',async()=>{
  for(const overrides of [{browserKind:'bitbrowser'},{cdp:'http://other.invalid:12345'},
    {loadCodec:async()=>{const e=staleError();e.payloadDiagnostic.replacementScriptUrl='https://evil.invalid/static/js/app.new.js';throw e;}}]) {
    const f=fixture(overrides);await assert.rejects(f.run(),e=>e.payloadFailureKind==='stale-app-bundle');
    assert.ok(!f.events.includes('owned-create'));assert.ok(f.loads()<=1);
  }
});
test('unconfirmed original context blocks page creation',async()=>{
  const f=fixture();f.originalInfo.browserContextId='';
  await assert.rejects(f.run(),{code:'OPENSMS_APP_BUILD_CONTEXT_UNCONFIRMED'});
  assert.ok(!f.events.includes('owned-create'));
});
test('fresh page must be in exactly the original browser context',async()=>{
  const f=fixture();f.freshInfo.browserContextId='other-context';
  await assert.rejects(f.run(),e=>e.code==='OPENSMS_APP_BUILD_CONTEXT_UNCONFIRMED'&&e.payloadBuildRecovery.ownedTargetClosed);
  assert.equal(f.loads(),1);assert.ok(f.events.includes('owned-close'));
});
test('run-owned other target resolves the precise original anchor in the same context',async()=>{
  const f=fixture({anchorTargetId:'anchor'});f.raw.target.type='other';f.originalInfo.type='other';
  assert.equal((await f.run()).recovery.status,'recovered');
});
test('hidden source without confirmed original anchor stops without another page',async()=>{
  const f=fixture({anchorTargetId:null});f.raw.target.type='other';
  await assert.rejects(f.run(),{code:'OPENSMS_APP_BUILD_CONTEXT_UNCONFIRMED'});
  assert.ok(!f.events.includes('owned-create'));
});
test('factory cannot return original/visible page and recovery never closes it',async()=>{
  for(const change of [{targetId:'original'},{hidden:false}]) {
    const f=fixture();Object.assign(f.owned,change);
    await assert.rejects(f.run(),{code:'OPENSMS_APP_BUILD_TARGET_UNCONFIRMED'});
    assert.ok(!f.events.includes('owned-close'));
  }
});
test('readiness failure closes owned target and does not read newer keys or retry',async()=>{
  const f=fixture({waitUntilReady:async()=>{throw Object.assign(new Error('secret-marker'),{code:'OPENSMS_APP_READY_TIMEOUT'});}});
  await assert.rejects(f.run(),e=>e.code==='OPENSMS_APP_READY_TIMEOUT'&&e.payloadBuildRecovery.ownedTargetClosed&&!e.message.includes('secret'));
  assert.equal(f.loads(),1);assert.equal(f.events.filter(x=>x==='owned-create').length,1);
});
test('second build mismatch or failed fresh codec stops and cleans up, never loops',async()=>{
  for(const freshResult of [createPayloadCodec({...cfg,scriptUrl:`${PAYLOAD_ORIGIN}/static/js/app.third.js`}),staleError(),Object.assign(new Error('secret'),{status:403})]) {
    let calls=0;
    const f=fixture({loadCodec:async()=>{if(++calls===1)throw staleError();if(freshResult instanceof Error)throw freshResult;return freshResult;}});
    await assert.rejects(f.run(),e=>e.payloadBuildRecovery.status==='failed'&&e.payloadBuildRecovery.ownedTargetClosed);
    assert.equal(calls,2);assert.equal(f.events.filter(x=>x==='owned-create').length,1);
  }
});
test('close command alone is insufficient; target remaining, unavailable list and close error all stop',async()=>{
  for(const mode of ['present','unavailable','close-error']) {
    const f=fixture({fetcher:async()=>({ok:mode!=='unavailable',json:async()=>[{id:'owned'}]})});
    if(mode==='close-error')f.owned.close=async()=>{throw new Error('secret');};
    await assert.rejects(f.run(),e=>e.code==='OPENSMS_APP_BUILD_CLEANUP_UNCONFIRMED'&&!e.payloadBuildRecovery.ownedTargetClosed&&!e.message.includes('secret'));
  }
});
test('successful recovery still needs one real read-only API authentication probe on the original target',async()=>{
  const f=fixture();let verified,apiCalls=0;
  const sessionFactory=async()=>{const loaded=await f.run();return {target:f.raw.target,payloadProtocol:loaded.codec.metadata,
    payloadBuildRecovery:loaded.recovery,browser:{close:async()=>f.events.push('original-disconnect')},
    api:async(method,path,_body,query)=>{apiCalls++;assert.equal(method,'GET');assert.equal(path,'/carrier/config/listNotDeleted');
      assert.equal(query.pageSize,1);assert.ok(f.events.includes('independent-close-check'));return {code:200,rows:[],total:0};}};};
  assert.equal(await probeExistingSession(sessionFactory,value=>{verified=value;}),true);
  assert.equal(apiCalls,1);assert.equal(verified.targetId,'original');assert.equal(verified.payloadBuildRecovery.status,'recovered');
  assert.equal(f.events.at(-1),'original-disconnect');
});
test('recovered configuration is not proof of login and does not mask API 401',async()=>{
  const f=fixture();
  assert.equal(await probeExistingSession(async()=>{await f.run();return {api:async()=>({code:401}),browser:{close:async()=>{}}};}),false);
});
test('normal protocol session uses guarded recovery before any API fetch and preflight receipts retain evidence',async()=>{
  const protocol=await fs.readFile(new URL('../opensms_protocol_client.mjs',import.meta.url),'utf8');
  const body=protocol.split('export async function createRawOpenSmsApiSession')[1].split('/**')[0];
  assert.ok(body.indexOf('await loadRawPayloadCodec')<body.indexOf('async function fetchFromPage'));
  assert.match(body,/withReadPacing: withOpenSmsRequestPacing/);
  assert.match(body,/catch \(error\) \{ client.close\(\); throw error; \}/);
  const cli=await fs.readFile(new URL('../scripts/opensms_login_recovery_cli.mjs',import.meta.url),'utf8');
  assert.match(cli,/payloadBuildRecovery: verified.payloadBuildRecovery/);
  assert.match(cli,/payloadBuildRecovery:error.payloadBuildRecovery/);
});
