import {settings} from './lib/platform.mjs';
const origin=`http://127.0.0.1:${settings().port}`;
try{
  const health=await(await fetch(origin+'/health',{signal:AbortSignal.timeout(2000)})).json();
  if(health.app!=='grab-local-query')throw new Error('端口不是本工具，拒绝关闭');
  const root=await fetch(origin),cookie=root.headers.get('set-cookie')?.split(';')[0];
  const boot=await(await fetch(origin+'/api/bootstrap',{headers:{cookie}})).json();
  const response=await fetch(origin+'/api/shutdown',{method:'POST',headers:{cookie,origin,'content-type':'application/json','x-tool-token':boot.token},body:'{}'});
  const data=await response.json();if(!response.ok)throw new Error(data.error||'关闭失败');
  console.log(data.message);
  for(let i=0;i<90;i++){
    await new Promise(r=>setTimeout(r,1000));
    let running=false;try{running=(await fetch(origin+'/health',{signal:AbortSignal.timeout(1000)})).ok;}catch{}
    if(!running){console.log('服务已关闭，历史记录与断点已保留');process.exit(0);}
  }
  throw new Error('关闭仍在等待当前请求，请稍后检查；没有强制终止任何浏览器');
}catch(e){console.error(e.cause?.code==='ECONNREFUSED'?'本工具没有运行':e.message);process.exitCode=1;}
