import http from 'node:http';
import fs from 'node:fs/promises';
import {createReadStream} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomBytes} from 'node:crypto';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {Store} from './lib/store.mjs';
import {Engine} from './lib/engine.mjs';
import {BrowserProvider} from './lib/browser.mjs';
import {parseInput,summary} from './lib/model.mjs';
import {lockInstance} from './lib/instance-lock.mjs';
import {settings} from './lib/platform.mjs';
const exec=promisify(execFile),ROOT=path.dirname(fileURLToPath(import.meta.url));
const DEFAULTS=settings();
export async function createApp({port=DEFAULTS.port,dataDir=DEFAULTS.dataDir,reportsDir=DEFAULTS.reportsDir,provider=null,delay=1000,loginPollMs=4000,reportEngine=DEFAULTS.reportEngine,onShutdown=null}={}){
  const unlock=await lockInstance(dataDir);let store;
  try{store=await new Store(path.join(dataDir,'jobs')).init();}catch(e){await unlock();throw e;}
  provider||=new BrowserProvider(dataDir);
  const engine=new Engine(store,provider,{delay,loginPollMs}),token=randomBytes(32).toString('hex'),cookie=randomBytes(32).toString('hex');
  const events=new Set();let origin;
  const send=(res,status,data)=>{res.writeHead(status,{'content-type':'application/json;charset=utf-8','cache-control':'no-store'});res.end(JSON.stringify(data));};
  async function body(req){let chunks=[],n=0;for await(const chunk of req){n+=chunk.length;if(n>1_100_000)throw new Error('输入超过大小限制');chunks.push(chunk);}return JSON.parse(Buffer.concat(chunks).toString()||'{}');}
  async function exportJob(job){
    if(job.report&&job.report.completed===job.results.length&&job.report.status===job.status){try{await fs.access(job.report.path);return job.report;}catch{job.report=null;}}
    if(job.exporting)throw new Error('报表正在生成，请稍候');
    if(!['completed','paused','failed','stopped'].includes(job.status)||!job.results.length)throw new Error('请先完成、暂停或停止任务；至少需要一条已查结果');
    job.exporting=true;await store.save(job);
    try{
      const dir=path.join(reportsDir,job.id,String(Date.now()));await fs.mkdir(dir,{recursive:true,mode:0o700});
      const snapshot=path.join(dir,'query.json');await fs.writeFile(snapshot,JSON.stringify(job,null,2),{mode:0o600});
      const file=path.join(dir,job.status==='completed'?'Grab_ICCID_查询结果.xlsx':'Grab_ICCID_部分查询结果.xlsx');
      const worker=reportEngine==='portable'?'report-portable.mjs':'report-worker.mjs';
      const run=await exec(process.execPath,[path.join(ROOT,worker),snapshot,file],{maxBuffer:2_000_000,timeout:180000,windowsHide:true});
      await fs.writeFile(path.join(dir,'export-validation.log'),run.stdout,{mode:0o600});
      const stat=await fs.stat(file);if(stat.size<1000)throw new Error('导出文件为空');
      job.report={path:file,filename:path.basename(file),completed:job.results.length,status:job.status,createdAt:new Date().toISOString()};
      return job.report;
    }finally{job.exporting=false;await store.save(job);}
  }
  const server=http.createServer(async(req,res)=>{
    res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('Referrer-Policy','no-referrer');
    res.setHeader('Content-Security-Policy',"default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'");
    try{
      if(req.headers.host!==new URL(origin).host)return send(res,403,{error:'只允许本机地址访问'});
      const url=new URL(req.url,origin),route=url.pathname;
      if(req.headers.origin&&req.headers.origin!==origin)return send(res,403,{error:'拒绝跨站访问'});
      if(req.headers['sec-fetch-site']==='cross-site'&&!(route==='/'&&req.method==='GET'&&req.headers['sec-fetch-mode']==='navigate'))return send(res,403,{error:'拒绝跨站访问'});
      if(route==='/health'&&req.method==='GET')return send(res,200,{app:'grab-local-query',version:1});
      if(route==='/'&&req.method==='GET'){
        res.setHeader('Set-Cookie',`grab_tool=${cookie}; HttpOnly; SameSite=Strict; Path=/`);
        res.setHeader('content-type','text/html;charset=utf-8');res.setHeader('cache-control','no-store');return res.end(await fs.readFile(path.join(ROOT,'public/index.html')));
      }
      if(['/app.js','/style.css'].includes(route)&&req.method==='GET'){
        res.setHeader('content-type',route.endsWith('.js')?'text/javascript;charset=utf-8':'text/css;charset=utf-8');return res.end(await fs.readFile(path.join(ROOT,'public',route.slice(1))));
      }
      if(!req.headers.cookie?.split(';').some(c=>c.trim()===`grab_tool=${cookie}`))return send(res,401,{error:'请重新打开本地查询页面'});
      if(req.method==='POST'&&(req.headers.origin!==origin||req.headers['x-tool-token']!==token||!req.headers['content-type']?.startsWith('application/json')))return send(res,403,{error:'请求验证失败'});
      if(route==='/api/bootstrap'&&req.method==='GET')return send(res,200,{token,auth:provider.state,jobs:store.list(),active:engine.active});
      if(route==='/api/shutdown'&&req.method==='POST'){
        if(!onShutdown)throw new Error('当前运行方式不支持关闭服务');
        if([...store.jobs.values()].some(j=>j.exporting))throw new Error('正在导出报表，请导出完成后再关闭服务');
        send(res,202,{message:'正在保存断点并关闭本工具'});setImmediate(onShutdown);return;
      }
      if(route==='/api/jobs'&&req.method==='GET')return send(res,200,{jobs:store.list(),active:engine.active,auth:provider.state});
      if(route==='/api/preview'&&req.method==='POST'){const input=parseInput((await body(req)).text);return send(res,200,{originalRows:input.originalRows,uniqueIccids:input.uniqueIccids,duplicateRows:input.duplicateRows});}
      if(route==='/api/jobs'&&req.method==='POST'){
        if(engine.active||engine.work||engine.polling)throw new Error('已有当前任务或登录检测，请先暂停或停止，等待当前请求收尾');
        const job=await store.create((await body(req)).text);await engine.start(job.id);return send(res,201,{...job,...summary(job)});
      }
      if(route==='/api/login'&&req.method==='POST'){
        if(engine.work)throw new Error('正在查询，请等待当前请求结束');
        await provider.openLogin();engine.scheduleLogin();return send(res,200,{auth:provider.state});
      }
      if(route==='/api/login/check'&&req.method==='POST')return send(res,200,{auth:await engine.checkLogin()});
      if(route==='/api/events'&&req.method==='GET'){
        res.writeHead(200,{'content-type':'text/event-stream','cache-control':'no-store','connection':'keep-alive'});res.write('event: ready\ndata: {}\n\n');events.add(res);req.on('close',()=>events.delete(res));return;
      }
      const match=route.match(/^\/api\/jobs\/([a-zA-Z0-9_-]{8,80})(?:\/(resume|pause|stop|export|download))?$/);
      if(match){
        const job=store.get(match[1]),action=match[2];
        if(!action&&req.method==='GET')return send(res,200,{...job,...summary(job),report:job.report?{filename:job.report.filename,createdAt:job.report.createdAt}:null});
        if(['resume','pause','stop'].includes(action)&&req.method==='POST'){await engine.control(job.id,action);return send(res,200,summary(job));}
        if(action==='export'&&req.method==='POST'){await exportJob(job);return send(res,200,{url:`/api/jobs/${job.id}/download`});}
        if(action==='download'&&req.method==='GET'){
          if(!job.report)throw new Error('请先生成报表');const stat=await fs.stat(job.report.path);
          res.writeHead(200,{'content-type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','content-length':stat.size,'content-disposition':`attachment; filename="Grab_ICCID.xlsx"; filename*=UTF-8''${encodeURIComponent(job.report.filename)}`,'cache-control':'no-store'});
          createReadStream(job.report.path).on('error',()=>res.destroy()).pipe(res);return;
        }
      }
      send(res,404,{error:'接口不存在'});
    }catch(e){if(!res.headersSent)send(res,e.httpStatus||400,{error:e.code==='LOGIN_REQUIRED'?'请手工登录 Grab 后继续':e.message,invalid:e.invalid});else res.destroy();}
  });
  store.on('update',()=>{const message=`event: update\ndata: ${JSON.stringify({active:engine.active,auth:provider.state})}\n\n`;for(const res of events)res.write(message);});
  const ping=setInterval(()=>{for(const res of events)res.write(': heartbeat\n\n');},20000);ping.unref();
  try{await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(port,'127.0.0.1',resolve);});}catch(e){clearInterval(ping);await unlock();throw e;}
  origin=`http://127.0.0.1:${server.address().port}`;
  return {server,store,engine,provider,origin,exportJob,async close(){clearInterval(ping);await engine.shutdown();for(const res of events)res.end();server.closeAllConnections();await new Promise(r=>server.close(r));await provider.close?.();await unlock();}};
}
if(process.argv[1]&&fileURLToPath(import.meta.url)===path.resolve(process.argv[1])){
  let closing=false;const finish=async()=>{if(closing)return;closing=true;await app.close();process.exit(0);};
  const app=await createApp({onShutdown:()=>finish().catch(e=>{console.error(e.message);process.exitCode=1;})});console.log(`Grab 查询工具：${app.origin}`);
  for(const signal of ['SIGTERM','SIGINT'])process.once(signal,finish);
}
