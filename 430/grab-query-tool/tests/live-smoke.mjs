// Explicit, small read-only acceptance check. Never included in offline *.test.mjs.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {createApp} from '../server.mjs';
const base='/Users/zhangsan/Downloads/430';
const previous=JSON.parse(await fs.readFile(path.join(base,'outputs/grab-iccid-20260926-T1Gm5c/query-receipt.json'),'utf8'));
const selected=['激活成功','未激活','不存在'].map(status=>previous.results.find(r=>r.status===status));
const dir=await fs.mkdtemp(path.join(base,'outputs/grab-query-acceptance-'));
const app=await createApp({port:0,dataDir:path.join(dir,'state'),reportsDir:path.join(dir,'reports')});
try{
  const landing=await fetch(app.origin+'/'),cookie=landing.headers.get('set-cookie').split(';')[0];
  const boot=await(await fetch(app.origin+'/api/bootstrap',{headers:{cookie}})).json();
  const headers={cookie,origin:app.origin,'content-type':'application/json','x-tool-token':boot.token};
  const check=await fetch(app.origin+'/api/login/check',{method:'POST',headers,body:'{}'});
  if(!check.ok)throw new Error('真实会话未确认，未开始验收查询；请先手工登录');
  const created=await fetch(app.origin+'/api/jobs',{method:'POST',headers,body:JSON.stringify({text:selected.map(r=>r.originalIccid).join('\n')})});assert.equal(created.status,201);
  const item=await created.json();
  for(let i=0;i<120;i++){
    const job=app.store.get(item.id);if(['completed','failed','waiting_login'].includes(job.status))break;
    await new Promise(r=>setTimeout(r,1000));
  }
  const job=app.store.get(item.id);assert.equal(job.status,'completed',job.error);assert.equal(job.results.length,3);
  for(const expected of selected){const actual=job.results.find(r=>r.queryIccid===expected.queryIccid);assert.equal(actual.status,expected.status);}
  const response=await fetch(`${app.origin}/api/jobs/${job.id}/export`,{method:'POST',headers,body:'{}'});assert.equal(response.status,200);
  const {url}=await response.json(),download=await fetch(app.origin+url,{headers:{cookie}});assert.equal(download.status,200);
  const bytes=Buffer.from(await download.arrayBuffer());assert.equal(bytes.subarray(0,2).toString(),'PK');
  const out={dir,jobFile:app.store.file(job.id),report:job.report.path,completed:job.results.length,status:'passed',auth:{...app.provider.state},statuses:job.results.map(r=>r.status)};
  await fs.writeFile(path.join(dir,'acceptance.json'),JSON.stringify(out,null,2));console.log(JSON.stringify(out));
}finally{await app.close();}
