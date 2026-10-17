// Offline distribution smoke test. Never reads real history, cookies or the website.
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import {createApp} from './server.mjs';
import {Store} from './lib/store.mjs';
import {settings,browserCandidates,openCommand} from './lib/platform.mjs';
const keys=['890000000000000001','890000000000000002','890000000000000003','890000000000000004'];
const dir=await fs.mkdtemp(path.join(os.tmpdir(),'GrabQueryTool-SelfCheck-'));
class OfflineProvider{
  state={status:'authenticated'};calls=[];
  async ensureAuthenticated(){}
  async search(term){this.calls.push(term);const kind=keys.indexOf(term),fields={lblSimCardNumber:term,lblPurchaseDate:'2026-09-10',lblActivationRequestDate:kind<=1?'2026-09-11 12:30':'',lblActivateDate:kind===0?'2026-09-12 06:45':'',lblSKU:'OFFLINE-TEST',lblCost:'0'};const rows=kind===3?[]:[{queryIccid:term,fields}];return {searchValue:term,total:rows.length,rendered:rows.length,rows};}
  async close(){}
}
const provider=new OfflineProvider(),app=await createApp({port:0,dataDir:dir,reportsDir:path.join(dir,'reports'),provider,delay:0,reportEngine:'portable'});
try{
  const win=settings({platform:'win32',env:{LOCALAPPDATA:'C:\\Users\\测试 用户\\AppData\\Local'},home:'C:\\Users\\测试 用户',config:{}});
  assert.equal(win.dataDir,'C:\\Users\\测试 用户\\AppData\\Local\\GrabQueryTool');assert.equal(win.portFile,'');assert.equal(win.reportEngine,'portable');
  assert.ok(browserCandidates('win32',{ProgramFiles:'C:\\Program Files'}).includes('C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'));
  assert.equal(openCommand('http://127.0.0.1:43881','win32',{})[0],'C:\\Windows\\System32\\rundll32.exe');
  const root=await fetch(app.origin),cookie=root.headers.get('set-cookie').split(';')[0];
  const boot=await(await fetch(app.origin+'/api/bootstrap',{headers:{cookie}})).json();
  const post=(route,data)=>fetch(app.origin+route,{method:'POST',headers:{cookie,origin:app.origin,'content-type':'application/json','x-tool-token':boot.token},body:JSON.stringify(data)});
  const r=await post('/api/jobs',{text:keys.join('F\n')+'F\n'+keys[0]+'f'});assert.equal(r.status,201);const job=app.store.get((await r.json()).id);await app.engine.work;
  assert.equal(job.status,'completed');assert.equal(job.results.length,4);assert.equal(job.originalRows,5);
  assert.deepEqual(job.results.map(r=>r.status),['激活成功','激活中','未激活','不存在']);
  const exported=await post(`/api/jobs/${job.id}/export`,{});assert.equal(exported.status,200);
  const bytes=Buffer.from(await(await fetch(app.origin+(await exported.json()).url,{headers:{cookie}})).arrayBuffer());assert.equal(bytes.subarray(0,2).toString(),'PK');
  const restored=await new Store(path.join(dir,'jobs')).init();assert.equal(restored.get(job.id).results.length,4);
  assert.equal((await fetch(app.origin+'/api/jobs')).status,401);
  assert.equal((await fetch(app.origin+'/api/jobs',{headers:{cookie,origin:'https://untrusted.example'}})).status,403);
  console.log(JSON.stringify({ok:true,offlineOnly:true,checks:['Windows paths','F normalization','four statuses','live queue','history persistence','HTTP security','Excel export and readback'],output:job.report.path},null,2));
}finally{await app.close();}
