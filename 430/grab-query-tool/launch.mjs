import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {settings,openCommand} from './lib/platform.mjs';
const options=settings(),root=path.dirname(fileURLToPath(import.meta.url)),url=`http://127.0.0.1:${options.port}`;
async function healthy(){try{const r=await fetch(url+'/health',{signal:AbortSignal.timeout(1000)});const j=await r.json();if(j.app!=='grab-local-query')throw new Error(`${options.port} 端口已被其他程序占用`);return true;}catch(e){if(e.message.includes('已被'))throw e;return false;}}
if(!await healthy()){
  const logDir=options.dataDir;fs.mkdirSync(logDir,{recursive:true,mode:0o700});
  const log=fs.openSync(path.join(logDir,'server.log'),'a',0o600);
  const child=spawn(process.execPath,[path.join(root,'server.mjs')],{cwd:root,detached:true,windowsHide:true,stdio:['ignore',log,log]});child.unref();fs.closeSync(log);
  let ready=false;for(let i=0;i<30;i++){await new Promise(r=>setTimeout(r,300));if(await healthy()){ready=true;break;}}
  if(!ready)throw new Error(`本地服务未能启动，请查看 ${path.join(logDir,'server.log')}`);
}
console.log(url);
if(!process.argv.includes('--no-open')){const [command,args]=openCommand(url);const child=spawn(command,args,{stdio:'ignore',windowsHide:true});child.on('error',()=>console.error(`请手动打开 ${url}`));child.unref();}
