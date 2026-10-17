import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {EventEmitter} from 'node:events';
const raw=fs.readFileSync(new URL('../scripts/opensms_api_target_guard_cli.mjs',import.meta.url),'utf8');
const stripped=raw.replace(/^import .+;\n/gm,'').replaceAll('import.meta.url',JSON.stringify(import.meta.url));
const code=stripped.slice(0,stripped.indexOf('main().catch'))+'\nthis.runMain=main;';
const runId='20260906T001500+0800';
function ctx(overrides={}) {
  return vm.createContext({process:{argv:['node','cli','hold','--run-id',runId,'--receipt','/test/receipt.json'],env:{BITBROWSER_CDP:'http://127.0.0.1:12345'}},
    console:{log:()=>{}},Date,JSON,randomUUID:()=> 'nonce',
    waitForPayloadPageReady:async()=>({status:'ready'}),
    loadPayloadCodec:async()=>({metadata:{source:'current-page-app-bundle'}}),...overrides});
}
test('keeper waits and validates current codec before publishing ready, with no early dispatch',async()=>{
  const events=[];
  const context=ctx({
    waitForPayloadPageReady:async()=>{events.push('wait');return {status:'ready'};},
    loadPayloadCodec:async()=>{events.push('codec');return {metadata:{source:'current-page-app-bundle'}};},
    createRawBackgroundPage:async()=>({targetId:'owned',client:{evaluate:async()=>{events.push('marker');}},close:async()=>{events.push('close');}}),
    fs:{writeFile:async(_p,text)=>{events.push('receipt');const r=JSON.parse(text);assert.equal(r.applicationReadiness.status,'ready');}},
    process:{argv:['node','cli','hold','--run-id',runId,'--receipt','/test/receipt.json'],
      env:{BITBROWSER_CDP:'http://127.0.0.1:12345',OPENSMS_HIDDEN_OWNER_PID:'321'},
      send:()=>events.push('dispatch'),kill:()=>{throw new Error('owner gone');}},
    setTimeout:fn=>fn(),
  });
  vm.runInContext(code,context);await context.runMain();
  assert.deepEqual(events,['wait','codec','marker','receipt','dispatch','close']);
});
test('failed application barrier closes only owned page and never dispatches workers',async()=>{
  let closed=0;
  const context=ctx({waitForPayloadPageReady:async()=>{throw Object.assign(new Error('app timeout'),{applicationReadiness:{status:'timeout'}});},
    loadPayloadCodec:async()=>assert.fail('must not validate codec'),
    fs:{writeFile:async()=>assert.fail('must not publish ready')},
    createRawBackgroundPage:async()=>({targetId:'owned',client:{},close:async()=>{closed++;}})});
  vm.runInContext(code,context);
  await assert.rejects(context.runMain(),e=>e.applicationReadiness.status==='timeout'&&e.backgroundCleanup.closed===true);
  assert.equal(closed,1);
});
test('create receipt failure retains original error and confirms successful cleanup',async()=>{
  let closed=0;
  const context=ctx({fs:{writeFile:async()=>{throw new Error('disk full');}},
    createRawBackgroundPage:async()=>({targetId:'owned',client:{evaluate:async()=>{},close:()=>{}},browserClient:{close:()=>{}},close:async()=>{closed++;}})});
  vm.runInContext(code,context);
  await assert.rejects(context.runMain(),e=>e.message==='disk full'&&e.backgroundCleanup.closed===true);
  assert.equal(closed,1);
});
test('unconfirmed cleanup preserves owned target diagnostics and never claims closed',async()=>{
  const writes=[];
  const context=ctx({fs:{writeFile:async(p,data)=>{writes.push({p,data});if(writes.length===1)throw new Error('receipt failed');}},
    createRawBackgroundPage:async()=>({targetId:'owned',client:{evaluate:async()=>{},close:()=>{}},browserClient:{close:()=>{}},close:async()=>{throw new Error('close disconnected');}})});
  vm.runInContext(code,context);
  await assert.rejects(context.runMain(),e=>e.message==='receipt failed'&&e.backgroundCleanup.status==='cleanup_unconfirmed');
  assert.equal(writes.length,2);
  assert.ok(writes[1].p.endsWith('.create_failed.json'));
  assert.equal(JSON.parse(writes[1].data).targetId,'owned');
});
test('close refuses receipt from a different run before any browser access',async()=>{
  const context=ctx({process:{argv:['node','cli','close','--run-id',runId,'--receipt','/test/receipt.json'],env:{BITBROWSER_CDP:'http://127.0.0.1:12345'}},
    fs:{readFile:async()=>JSON.stringify({ownedBy:'opensms-patrol-api-target',runId:'other'})},
    fetch:()=>{throw new Error('must not access browser');}});
  vm.runInContext(code,context);
  await assert.rejects(context.runMain(),/归属校验失败/);
});

test('create delegates ownership to a keeper and returns only after its receipt',async()=>{
  const events=[],child=new EventEmitter();
  child.disconnect=()=>events.push('disconnect');child.unref=()=>events.push('unref');
  const context=ctx({process:{argv:['node','cli','create','--run-id',runId,'--receipt','/test/receipt.json'],
    ppid:123,env:{BITBROWSER_CDP:'http://127.0.0.1:12345'}},setTimeout,clearTimeout,
    fileURLToPath:value=>value,
    fork:(_file,args,options)=>{
      assert.equal(args[0],'hold');assert.equal(options.env.OPENSMS_HIDDEN_OWNER_PID,'123');
      assert.equal(options.detached,true);assert.deepEqual(Array.from(options.stdio),['ignore','ignore','ignore','ipc']);
      queueMicrotask(()=>child.emit('message',{success:true,targetId:'owned',hidden:true}));return child;
    },console:{log:line=>events.push(JSON.parse(line))}});
  vm.runInContext(code,context);await context.runMain();
  assert.deepEqual(events,['disconnect','unref',{success:true,targetId:'owned',hidden:true}]);
});

test('keeper creation failure is reported without a visible-page fallback',async()=>{
  const child=new EventEmitter();let forks=0;
  const context=ctx({process:{argv:['node','cli','create','--run-id',runId,'--receipt','/test/receipt.json'],
    ppid:123,env:{BITBROWSER_CDP:'http://127.0.0.1:12345'}},setTimeout,clearTimeout,fileURLToPath:value=>value,
    fork:()=>{forks++;queueMicrotask(()=>child.emit('message',{success:false,error:'hidden unavailable'}));return child;}});
  vm.runInContext(code,context);await assert.rejects(context.runMain(),/hidden unavailable/);assert.equal(forks,1);
});

test('keeper forwards confirmed cleanup and navigation diagnostics to caller',async()=>{
  const child=new EventEmitter();
  const cleanup={targetId:'owned',closed:true,status:'confirmed_closed'};
  const context=ctx({process:{argv:['node','cli','create','--run-id',runId,'--receipt','/test/receipt.json'],
    ppid:123,env:{BITBROWSER_CDP:'http://127.0.0.1:12345'}},setTimeout,clearTimeout,fileURLToPath:value=>value,
    fork:()=>{queueMicrotask(()=>child.emit('message',{success:false,error:'CDP command timeout: Runtime.enable',backgroundCleanup:cleanup}));return child;}});
  vm.runInContext(code,context);
  await assert.rejects(context.runMain(),e=>e.message.includes('Runtime.enable')&&e.backgroundCleanup===cleanup);
});
