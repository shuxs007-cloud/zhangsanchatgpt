import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
const exec=promisify(execFile),bundle=path.resolve(process.argv[2]);
const data=await fs.mkdtemp(path.join(os.tmpdir(),'Grab 空格 Launcher-'));
const probe=net.createServer();await new Promise(r=>probe.listen(0,'127.0.0.1',r));const port=probe.address().port;await new Promise(r=>probe.close(r));
const env={...process.env,GRAB_TOOL_PORT:String(port),GRAB_TOOL_DATA_DIR:data,GRAB_TOOL_REPORTS_DIR:path.join(data,'reports'),GRAB_TOOL_REPORT_ENGINE:'portable'};
try{
  const first=await exec(process.execPath,[path.join(bundle,'launch.mjs'),'--no-open'],{cwd:bundle,env,timeout:20000});
  assert.ok(first.stdout.includes(String(port)));const lock=JSON.parse(await fs.readFile(path.join(data,'server.lock'),'utf8'));
  await exec(process.execPath,[path.join(bundle,'launch.mjs'),'--no-open'],{cwd:bundle,env,timeout:20000});
  assert.equal(JSON.parse(await fs.readFile(path.join(data,'server.lock'),'utf8')).pid,lock.pid);
  const root=await fetch(`http://127.0.0.1:${port}`);assert.equal(root.status,200);
  const stop=await exec(process.execPath,[path.join(bundle,'stop.mjs')],{cwd:bundle,env,timeout:95000});
  assert.ok(stop.stdout.includes('服务已关闭'));await assert.rejects(fs.access(path.join(data,'server.lock')));
  console.log(JSON.stringify({ok:true,detachedStart:true,reusesExistingServer:true,gracefulStop:true,chineseSpaceDataPath:true,platform:process.platform}));
}finally{
  try{await exec(process.execPath,[path.join(bundle,'stop.mjs')],{cwd:bundle,env,timeout:95000});}catch{}
}
