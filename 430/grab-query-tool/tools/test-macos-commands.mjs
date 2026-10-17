import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
const exec=promisify(execFile),bundle=path.resolve(process.argv[2]);
const data=await fs.mkdtemp(path.join(os.tmpdir(),'Grab Mac 测试-'));
const probe=net.createServer();await new Promise(r=>probe.listen(0,'127.0.0.1',r));const port=probe.address().port;await new Promise(r=>probe.close(r));
const env={...process.env,GRAB_TOOL_PORT:String(port),GRAB_TOOL_DATA_DIR:data,GRAB_TOOL_REPORTS_DIR:path.join(data,'reports')};delete env.GRAB_TOOL_REPORT_ENGINE;
const call=(file,args=[])=>exec(path.join(bundle,file),args,{cwd:bundle,env,maxBuffer:300000,timeout:95000});
try{
  const runtime=await call('runtime/run-node.sh',['-p','JSON.stringify({version:process.version,arch:process.arch,platform:process.platform})']);
  const runtimeInfo=JSON.parse(runtime.stdout);assert.equal(runtimeInfo.platform,'darwin');assert.equal(runtimeInfo.version,'v24.21.0');
  const defaults=JSON.parse((await call('runtime/run-node.sh',['--input-type=module','-e','import {settings} from "./lib/platform.mjs";console.log(JSON.stringify(settings()))'])).stdout);assert.equal(defaults.reportEngine,'portable');assert.equal(defaults.portFile,'');
  assert.ok((await call('Start.command',['--no-open'])).stdout.includes(`:${port}`));
  const lock=JSON.parse(await fs.readFile(path.join(data,'server.lock'),'utf8'));
  await call('Start.command',['--no-open']);assert.equal(JSON.parse(await fs.readFile(path.join(data,'server.lock'),'utf8')).pid,lock.pid);
  const page=await fetch(`http://127.0.0.1:${port}`);assert.equal(page.status,200);
  const cookie=page.headers.get('set-cookie').split(';')[0];const boot=await(await fetch(`http://127.0.0.1:${port}/api/bootstrap`,{headers:{cookie}})).json();assert.equal(boot.jobs.length,0);
  assert.ok((await call('Stop.command')).stdout.includes('服务已关闭'));await assert.rejects(fs.access(path.join(data,'server.lock')));
  const selfCheck=JSON.parse((await call('Check.command')).stdout);assert.equal(selfCheck.ok,true);assert.equal(selfCheck.offlineOnly,true);
  console.log(JSON.stringify({ok:true,runtime:runtimeInfo,startCommand:true,stopCommand:true,checkCommand:true,portableDefaults:true,emptyHistory:true,chineseSpaceDataPath:true,selfCheckReport:selfCheck.output},null,2));
}finally{try{await call('Stop.command');}catch{}}
