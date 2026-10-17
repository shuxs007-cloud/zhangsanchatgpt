import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Store} from '../lib/store.mjs';
import {Engine} from '../lib/engine.mjs';
import {parseInput,classify,LoginRequired,summary} from '../lib/model.mjs';
import {createApp} from '../server.mjs';
import {lockInstance} from '../lib/instance-lock.mjs';
const A='8912230200277885949',B='8912230200278118233',C='89302720536828320834';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
export const record=(key,kind='active')=>({queryIccid:key,fields:{lblPurchaseDate:'2026-09-11',lblActivationRequestDate:kind==='unused'?'':'2026-09-23 20:54',lblActivateDate:kind==='active'?'2026-09-24 19:14':'',lblSimCardNumber:key,lblSKU:'TEST-SKU',lblCost:'$0'}});
export class FakeProvider{
  constructor(){this.state={status:'authenticated',message:'offline test'};this.calls=[];this.authenticated=true;this.opened=0;this.wait=null;this.fail=false;}
  async ensureAuthenticated(){if(!this.authenticated){this.state.status='login_required';throw new LoginRequired();}this.state.status='authenticated';}
  async openLogin(){this.opened++;this.state.status='login_required';}
  async search(term){this.calls.push(term);if(this.wait)await this.wait;if(this.fail)throw new Error('模拟网络错误');if(!this.authenticated)throw new LoginRequired();const rows=term===C?[]:[record(term,term===B?'unused':'active')];return {searchValue:term,total:rows.length,rendered:rows.length,rows};}
  async close(){}
}
async function setup(options={}){const dir=await fs.mkdtemp(path.join(os.tmpdir(),'grab-tool-test-'));const store=await new Store(path.join(dir,'jobs')).init();const provider=new FakeProvider(),engine=new Engine(store,provider,{delay:0,loginPollMs:20,...options});return {dir,store,provider,engine};}
async function idle(engine){await engine.work;await sleep(0);}
async function until(predicate){for(let i=0;i<150;i++){if(predicate())return;await sleep(10);}throw new Error('test condition timed out');}
test('去掉恰好一个末尾 F/f，并按字符串去重',()=>{const p=parseInput(`${A}F\n${A}f，${C}`);assert.equal(p.uniqueIccids,2);assert.equal(p.duplicateRows,1);assert.equal(p.sourceRows[0].originalIccid,A+'F');assert.equal(p.keys[1],C);});
test('跨进程实例锁阻止双开，释放后可重新打开',async()=>{const dir=await fs.mkdtemp(path.join(os.tmpdir(),'grab-lock-test-'));const release=await lockInstance(dir);await assert.rejects(lockInstance(dir));await release();const again=await lockInstance(dir);await again();});
for(const value of ['',`${A}FF`,'1.234E+19',`${A}F hello`,'123','0'.repeat(23)])test('拒绝非法清单 '+value,()=>assert.throws(()=>parseInput(value)));
test('四种状态只使用明确字段，不用购买日期冒充激活日期',()=>{const source=parseInput(A).sourceRows,batch={id:1,term:A,total:1,rendered:1,queriedAt:new Date().toISOString()};for(const [kind,status]of [['active','激活成功'],['pending','激活中'],['unused','未激活']])assert.equal(classify(A,record(A,kind),batch,source).status,status);assert.equal(classify(A,null,{...batch,total:0,rendered:0},source).status,'不存在');});
test('广泛查询没命中不能直接标不存在，缺字段/身份异常不能当成功',()=>{const s=parseInput(A).sourceRows;assert.throws(()=>classify(A,null,{term:A.slice(0,15),total:0,rendered:0},s));const r=record(A);delete r.fields.lblActivateDate;assert.throws(()=>classify(A,r,{},s));assert.throws(()=>classify(A,record(B),{},s));});
test('新建两个相同任务仍会分别实时查询',async()=>{const {store,provider,engine}=await setup();for(let i=0;i<2;i++){const j=await store.create(A+'F');await engine.start(j.id);await idle(engine);assert.equal(j.status,'completed');}assert.deepEqual(provider.calls,[A,A]);});
test('暂停保存当前回包，继续只查剩余号码',async()=>{const {store,provider,engine}=await setup();let release;provider.wait=new Promise(r=>release=r);const j=await store.create(`${A}\n${B}`);await engine.start(j.id);await until(()=>provider.calls.length===1);await engine.control(j.id,'pause');release();await idle(engine);assert.equal(j.status,'paused');assert.equal(j.results.length,1);provider.wait=null;await engine.start(j.id);await idle(engine);assert.equal(j.status,'completed');assert.deepEqual(provider.calls,[A,B]);});
test('停止丢弃在途结果，不再派发，也不能继续',async()=>{const {store,provider,engine}=await setup();let release;provider.wait=new Promise(r=>release=r);const j=await store.create(`${A}\n${B}`);await engine.start(j.id);await until(()=>provider.calls.length===1);await engine.control(j.id,'stop');release();await idle(engine);assert.equal(j.status,'stopped');assert.equal(j.results.length,0);assert.equal(provider.calls.length,1);await assert.rejects(engine.start(j.id));});
test('异常不是不存在，允许只重试未完成项',async()=>{const {store,provider,engine}=await setup();provider.fail=true;const j=await store.create(A);await engine.start(j.id);await idle(engine);assert.equal(j.status,'failed');assert.equal(j.results.length,0);provider.fail=false;await engine.start(j.id);await idle(engine);assert.equal(j.status,'completed');});
test('无登录时打开人工登录入口，成功后从断点自动继续',async()=>{const {store,provider,engine}=await setup();provider.authenticated=false;const j=await store.create(A);await engine.start(j.id);await idle(engine);assert.equal(j.status,'waiting_login');assert.equal(provider.opened,1);provider.authenticated=true;await until(()=>j.status==='completed');await engine.shutdown();});
test('等待登录时手动暂停，登录成功不擅自恢复',async()=>{const {store,provider,engine}=await setup();provider.authenticated=false;const j=await store.create(A);await engine.start(j.id);await idle(engine);await engine.control(j.id,'pause');provider.authenticated=true;await sleep(80);assert.equal(j.status,'paused');assert.equal(provider.calls.length,0);await engine.shutdown();});
test('不允许两份任务共享会话并行运行',async()=>{const {store,provider,engine}=await setup();let release;provider.wait=new Promise(r=>release=r);const a=await store.create(A),b=await store.create(B);await engine.start(a.id);await assert.rejects(engine.start(b.id));release();await idle(engine);});
test('批量结果不完整时递归补查；未找到用完整 ICCID 再确认',async()=>{
  const {store,provider,engine}=await setup();const b='8912230200277884827';const j=await store.create(A+'\n'+b);
  provider.search=async term=>{provider.calls.push(term);if(term===A.slice(0,15))return {searchValue:term,total:10,rendered:1,rows:[record(A)]};return {searchValue:term,total:0,rendered:0,rows:[]};};
  await engine.start(j.id);await idle(engine);assert.equal(j.status,'completed');assert.deepEqual(provider.calls,[A.slice(0,15),b]);assert.equal(j.results[1].status,'不存在');
});
test('重启时活动任务转暂停；停止中的转停止；历史结果不丢失',async()=>{const {store,dir}=await setup();const j=await store.create(A);j.status='running';j.results=[classify(A,record(A),{id:1,term:A,total:1,rendered:1,queriedAt:new Date().toISOString()},j.sourceRows)];await store.save(j);const restored=await new Store(path.join(dir,'jobs')).init();assert.equal(restored.get(j.id).status,'paused');assert.equal(summary(restored.get(j.id)).completed,1);assert.equal(restored.get(j.id).results[0].queryIccid,A);});
test('HTTP 防跨站、Cookie 保护、任务接口与重查',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'grab-api-test-')),provider=new FakeProvider();const app=await createApp({port:0,dataDir:dir,reportsDir:path.join(dir,'reports'),provider,delay:0});
  try{
    let r=await fetch(app.origin+'/');const cookie=r.headers.get('set-cookie').split(';')[0];assert.equal(r.status,200);
    r=await fetch(app.origin+'/api/bootstrap');assert.equal(r.status,401);
    const boot=await(await fetch(app.origin+'/api/bootstrap',{headers:{cookie}})).json();
    r=await fetch(app.origin+'/api/jobs',{method:'POST',headers:{cookie,origin:'https://evil.example','content-type':'application/json','x-tool-token':boot.token},body:JSON.stringify({text:A})});assert.equal(r.status,403);
    const post=(url,data={})=>fetch(app.origin+url,{method:'POST',headers:{cookie,origin:app.origin,'content-type':'application/json','x-tool-token':boot.token},body:JSON.stringify(data)});
    r=await post('/api/jobs',{text:A+'F'});assert.equal(r.status,201);const job=await r.json();await idle(app.engine);
    const detail=await(await fetch(app.origin+'/api/jobs/'+job.id,{headers:{cookie}})).json();assert.equal(detail.status,'completed');assert.equal(detail.completed,1);assert.ok(!JSON.stringify(detail).includes('cookieApi'));
    r=await post('/api/jobs',{text:A+'F'});assert.equal(r.status,201);await idle(app.engine);assert.equal(provider.calls.length,2);
    r=await fetch(app.origin+'/api/jobs/../../etc/passwd',{headers:{cookie}});assert.equal(r.status,404);
  }finally{await app.close();}
});
