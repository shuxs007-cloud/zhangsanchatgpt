import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {validateSharedSession,sharedSessionEnvironment,acquireSharedOpenSmsSession,
  SHARED_BROWSER_ID, SHARED_BROWSER_NAME} from '../scripts/opensms_shared_browser.mjs';

const saved={browser:'cloakbrowser',status:'ready',authentication:'verified-readonly-api',
  pid:12345,port:60904,sessionId:'d82e83ca-9e2a-4511-8fd3-da15456847eb',
  webSocketPath:'/devtools/browser/fdba4143-8b00-4069-82bc-401b00072469',lastVerifiedTargetId:'verified'};
const owner={pid:saved.pid,browserSessionId:saved.sessionId,token:'test-owner-token'};
const target={id:'verified',type:'page',url:'https://adminsms.smsoug.com/day',
  webSocketDebuggerUrl:'ws://127.0.0.1:60904/devtools/page/verified'};
function dependencies(overrides={}) {
  let releases=0,checks=0; const fetched=[];
  return {get releases(){return releases;},get checks(){return checks;},fetched,
    args:{acquire:async({browserId})=>({file:browserId+'.lock',release(){releases++;}}),
      readFile:async name=>JSON.stringify(name.endsWith('.lock')
        ?{pid:process.pid,token:name.endsWith('report.lock')?'report-test-token':'site-test-token'}
        :name.endsWith('identity.json')?owner:saved),
      processCheck:()=>{checks++;},requestCheck:async()=>false,
      commandForPid:()=>'/node /Users/zhangsan/Downloads/430/scripts/opensms_new_browser.mjs --serve',
      fetcher:async url=>{fetched.push(url);return {ok:true,json:async()=>url.endsWith('/json/version')
        ?{webSocketDebuggerUrl:`ws://127.0.0.1:${saved.port}${saved.webSocketPath}`}: [target]};},
      ...overrides}};
}
test('one named session and environment, with no CK copy',()=>{
  const s=validateSharedSession(saved,owner);
  assert.equal(s.displayName,SHARED_BROWSER_NAME);
  const env=sharedSessionEnvironment(s);
  assert.equal(env.OPENSMS_CDP_PORT,'60904');
  assert.equal(env.OPENSMS_RAW_API_TARGET_ID,'verified');
  assert.equal(env.OPENSMS_EXACT_CDP_ONLY,'1');
  assert.ok(!Object.keys(env).some(k=>/cookie|token/i.test(k)));
});
test('changed identity, stale authentication and missing exact target fail closed',()=>{
  for(const change of [{pid:0},{port:70000},{sessionId:'wrong'},{lastVerifiedTargetId:null},
      {status:'login-required'},{authentication:'expired'}])
    assert.throws(()=>validateSharedSession({...saved,...change},owner));
  assert.throws(()=>validateSharedSession(saved,{...owner,pid:7}));
  assert.throws(()=>validateSharedSession(saved,{...owner,browserSessionId:'different'}));
});
test('identity check only uses localhost and restores environment/lock',async()=>{
  const prior=process.env.OPENSMS_CDP_PORT;process.env.OPENSMS_CDP_PORT='prior';
  const d=dependencies();const shared=await acquireSharedOpenSmsSession(d.args);
  assert.deepEqual(d.fetched,['http://127.0.0.1:60904/json/version','http://127.0.0.1:60904/json/list']);
  assert.equal(d.releases,0);assert.equal(process.env.OPENSMS_CDP_PORT,'60904');
  assert.equal(process.env.OPENSMS_SITE_LOCK_TOKEN,'site-test-token');
  assert.equal(process.env.OPENSMS_SITE_JOB_LOCK_TOKEN,'report-test-token');
  shared.release();shared.release();assert.equal(d.releases,2);
  assert.equal(process.env.OPENSMS_CDP_PORT,'prior');
  if(prior===undefined) delete process.env.OPENSMS_CDP_PORT;else process.env.OPENSMS_CDP_PORT=prior;
});
test('busy request stops before reading browser or fetching',async()=>{
  const d=dependencies({requestCheck:async()=>true});
  await assert.rejects(acquireSharedOpenSmsSession(d.args),/PACING_REQUEST_UNCONFIRMED/);
  assert.equal(d.releases,1);assert.equal(d.checks,0);assert.equal(d.fetched.length,0);
});
test('dead service and wrong browser websocket never fall back or launch',async()=>{
  const dead=dependencies({processCheck:()=>{throw new Error('dead');}});
  await assert.rejects(acquireSharedOpenSmsSession(dead.args),/dead/);
  assert.equal(dead.releases,2);assert.equal(dead.fetched.length,0);
  const changed=dependencies({fetcher:async()=>({ok:true,json:async()=>({webSocketDebuggerUrl:'ws://127.0.0.1:1/devtools/browser/other'})})});
  await assert.rejects(acquireSharedOpenSmsSession(changed.args),/IDENTITY_CHANGED/);
  assert.equal(changed.releases,2);
});
test('wrong service or target cannot borrow another page',async()=>{
  const wrong=dependencies({commandForPid:()=>'/node other_browser.mjs --serve'});
  await assert.rejects(acquireSharedOpenSmsSession(wrong.args),/SERVICE_CHANGED/);
  assert.equal(wrong.releases,2);
  const missing=dependencies({fetcher:async url=>({ok:true,json:async()=>url.endsWith('/json/version')
    ?{webSocketDebuggerUrl:`ws://127.0.0.1:${saved.port}${saved.webSocketPath}`}:[{...target,id:'other'}]})});
  await assert.rejects(acquireSharedOpenSmsSession(missing.args),/TARGET_CHANGED/);
  assert.equal(missing.releases,2);
});
test('an inherited child presents the report token without losing its website token',async()=>{
  const keys=['OPENSMS_SITE_LOCK_TOKEN','OPENSMS_SITE_LOCK_PID','OPENSMS_SITE_JOB_LOCK_TOKEN','OPENSMS_SITE_JOB_LOCK_PID'];
  const before=Object.fromEntries(keys.map(k=>[k,process.env[k]]));
  Object.assign(process.env,{OPENSMS_SITE_LOCK_TOKEN:'parent-site',OPENSMS_SITE_LOCK_PID:'123',
    OPENSMS_SITE_JOB_LOCK_TOKEN:'parent-report',OPENSMS_SITE_JOB_LOCK_PID:'123'});
  const presented=[];
  const d=dependencies({acquire:async({browserId})=>{
    presented.push({browserId,token:process.env.OPENSMS_SITE_LOCK_TOKEN});
    return {file:browserId+'.lock',release(){}};
  }});
  const shared=await acquireSharedOpenSmsSession(d.args);shared.release();
  assert.deepEqual(presented.map(v=>v.token),['parent-site','parent-report']);
  assert.equal(process.env.OPENSMS_SITE_LOCK_TOKEN,'parent-site');
  assert.equal(process.env.OPENSMS_SITE_JOB_LOCK_TOKEN,'parent-report');
  for(const [k,v] of Object.entries(before)) {if(v===undefined) delete process.env[k];else process.env[k]=v;}
});
test('report-lock failure cleans website ownership before any browser access',async()=>{
  let released=0;
  const d=dependencies({acquire:async({browserId})=>{
    if(browserId==='opensms-new-browser-report') throw new Error('report busy');
    return {file:browserId+'.lock',release(){released++;}};
  }});
  await assert.rejects(acquireSharedOpenSmsSession(d.args),/report busy/);
  assert.equal(released,1);assert.equal(d.checks,0);assert.equal(d.fetched.length,0);
});
test('browser and report locks share one wait budget',async()=>{
  let clock=0;const waits=[];
  const d=dependencies({now:()=>clock,acquire:async({browserId,timeoutMs})=>{
    waits.push({browserId,timeoutMs});
    clock=browserId===SHARED_BROWSER_ID?700:clock;
    return {file:browserId+'.lock',release(){}};
  }});
  const shared=await acquireSharedOpenSmsSession({...d.args,timeoutMs:1000});
  shared.release();
  assert.deepEqual(waits.map(entry=>entry.timeoutMs),[1000,300]);
  const overdue=dependencies({now:()=>clock,acquire:async({browserId})=>{
    if(browserId===SHARED_BROWSER_ID) clock=1100;
    return {file:browserId+'.lock',release(){}};
  }});
  clock=0;
  await assert.rejects(acquireSharedOpenSmsSession({...overdue.args,timeoutMs:1000}),
    /OPENSMS_SHARED_SESSION_WAIT_TIMEOUT/);
});
test('zero timeout keeps both locks nonblocking',async()=>{
  const waits=[];
  const d=dependencies({acquire:async({browserId,timeoutMs})=>{
    waits.push({browserId,timeoutMs});
    return {file:browserId+'.lock',release(){}};
  }});
  const shared=await acquireSharedOpenSmsSession({...d.args,timeoutMs:0});
  shared.release();
  assert.deepEqual(waits.map(entry=>entry.timeoutMs),[0,0]);
});
test('all non-A workflows use the shared resolver; old launcher cannot start its own browser',()=>{
  for(const name of ['opensms_device_maintenance_force.mjs','opensms_device_followup.mjs']) {
    const source=fs.readFileSync(new URL(`../scripts/${name}`,import.meta.url),'utf8');
    assert.match(source,/acquireSharedOpenSmsSession/);assert.match(source,/createRawBackgroundPage/);
    assert.doesNotMatch(source,/DevToolsActivePort|PROFILE_DIR|bringToFront|activate\(/);
  }
  const root='/Users/zhangsan/Documents/数据分析/';
  for(const name of ['opensms_ops.mjs','opensms_raw_cdp_binding_tmp.mjs']) {
    assert.match(fs.readFileSync(root+name,'utf8'),/acquireSharedOpenSmsSession/);
  }
  const legacy=fs.readFileSync(root+'cloakbrowser_free_test/login_browser.mjs','utf8');
  assert.match(legacy,/scripts\/opensms_new_browser.mjs/);
  assert.doesNotMatch(legacy,/launchPersistentContext|private-session|storage\.json|newPage\(/);
  for(const name of ['run.mjs','test_free.mjs']) {
    const source=fs.readFileSync(root+'cloakbrowser_free_test/'+name,'utf8');
    assert.match(source,/scripts\/opensms_shared_browser_verify\.mjs/);
    assert.doesNotMatch(source,/launch\(|newPage\(|browser\.close\(|acquireSiteLock/);
  }
  const saver=fs.readFileSync(root+'cloakbrowser_free_test/save_session.mjs','utf8');
  assert.match(saver,/acquireSharedOpenSmsSession/);
  assert.doesNotMatch(saver,/storage\.json|Network\.getCookies|writeFile|launch\(/);
  for(const name of ['bind_requested.mjs','account_operation.mjs']) {
    const source=fs.readFileSync(root+'cloakbrowser_free_test/'+name,'utf8');
    assert.doesNotMatch(source,/login_browser_status|private-session|owner\.token|storage\.json/);
    assert.match(source,/delete env\.OPENSMS_SITE_JOB_LOCK_TOKEN/);
  }
  assert.match(fs.readFileSync(root+'cloakbrowser_free_test/bind_requested.mjs','utf8'),/includes\('--apply'\)/);
});
