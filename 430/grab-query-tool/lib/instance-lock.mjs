import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
export async function lockInstance(dir){
  await fs.mkdir(dir,{recursive:true,mode:0o700});
  const file=path.join(dir,'server.lock'),owner=randomUUID();
  async function create(){const handle=await fs.open(file,'wx',0o600);try{await handle.writeFile(JSON.stringify({pid:process.pid,owner}));await handle.sync();}finally{await handle.close();}}
  try{await create();}catch(error){
    if(error.code!=='EEXIST')throw error;
    const previous=JSON.parse(await fs.readFile(file,'utf8'));
    if(!Number.isInteger(previous.pid)||previous.pid<=0)throw new Error('服务锁文件无效，请检查本地服务状态');
    try{process.kill(previous.pid,0);throw new Error('查询工具已在运行，请打开已有页面，不要启动第二个实例');}
    catch(e){if(e.code!=='ESRCH')throw e;}
    await fs.rename(file,file+'.stale-'+Date.now());await create();
  }
  return async()=>{const current=JSON.parse(await fs.readFile(file,'utf8'));if(current.owner===owner)await fs.unlink(file);};
}
