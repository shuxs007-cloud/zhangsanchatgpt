// Read-only discovery for Task H. Never changes carrier settings or Task F.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {acquireSharedOpenSmsSession} from './opensms_shared_browser.mjs';
import {createRawBackgroundPage,createRawOpenSmsApiSession} from '../opensms_protocol_client.mjs';
import {waitForPayloadPageReady} from '../opensms_payload_readiness.mjs';
import {collectCarrierConfigPagesReadOnly} from './opensms_carriers_readonly.mjs';
import {withOpenSmsRequestPacing} from '../opensms_request_pacing.mjs';

export async function inspectTaskH(){
  const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
  const lease=await acquireSharedOpenSmsSession({timeoutMs:0});
  let page,session;
  try{
    page=await createRawBackgroundPage({cdp:lease.session.cdp,url:'https://adminsms.smsoug.com/index'});
    await waitForPayloadPageReady(page.client);
    process.env.OPENSMS_RAW_API_TARGET_ID=page.targetId;
    session=await createRawOpenSmsApiSession({cdp:lease.session.cdp,pathName:'/index'});
    const snapshot=await collectCarrierConfigPagesReadOnly(session.api,{concurrency:1});
    const frontend=await page.client.evaluate(`(() => {
      const app=document.getElementById('app'),vm=app?.__vue__;
      const router=vm?.$router, routes=router?.getRoutes?.()||router?.options?.routes||[];
      const flatten=(rows,parent='')=>rows.flatMap(r=>{
        const route=r.path?.startsWith('/')?r.path:[parent,r.path].filter(Boolean).join('/');
        return [{path:route,name:r.name||null,componentFactory:String(r.component||r.components?.default||'')},
          ...flatten(r.children||[],route)];
      });
      const hashes=[...new Set([...document.scripts].filter(s=>!s.src).flatMap(s=>
        [...s.textContent.matchAll(/"chunk-b2c8e884":"([a-f0-9]{8})"/g)].filter(m=>
          s.textContent.lastIndexOf('static/js/',m.index)>s.textContent.lastIndexOf('static/css/',m.index)).map(m=>m[1])))];
      return {routes:flatten(routes).filter(r=>/carrier/i.test(r.path)),carrierChunkHashes:hashes,
        scripts:[...new Set([...document.scripts].map(s=>s.src).concat(
          performance.getEntriesByType('resource').map(r=>r.name)))].filter(u=>
            u.startsWith(location.origin+'/static/js/')&&u.endsWith('.js'))};
    })()`,5000);
    const result={version:1,task:'H',status:'readonly-inspection',checkedAt:new Date().toISOString(),
      browserSessionId:lease.session.sessionId,businessWrites:0,snapshot,frontend};
    const dir=path.join(root,'opensms_artifacts/state/task_h');await fs.mkdir(dir,{recursive:true});
    const uid=crypto.randomUUID(),output=path.join(dir,`inspection_${uid}.json`);
    const appAsset=frontend.scripts.find(u=>/^https:\/\/adminsms\.smsoug\.com\/static\/js\/app\.[a-f0-9]+\.js$/.test(u));
    if(appAsset){
      const source=await withOpenSmsRequestPacing(()=>page.client.evaluate(`(async()=>{
        const r=await fetch(${JSON.stringify(appAsset)},{credentials:'include',signal:AbortSignal.timeout(45000)});
        if(!r.ok)throw Error('TASK_H_FRONTEND_READ_FAILED');return await r.text();
      })()`,60000));
      const sourcePath=path.join(dir,`inspection_${uid}_app.js`);
      const sourceFile=await fs.open(sourcePath,'wx',0o600);
      try{await sourceFile.writeFile(source);await sourceFile.sync();}finally{await sourceFile.close();}
      result.frontendSource={url:appAsset,path:sourcePath,sha256:crypto.createHash('sha256').update(source).digest('hex')};
    }
    const carrierAsset=frontend.scripts.find(u=>/^https:\/\/adminsms\.smsoug\.com\/static\/js\/chunk-b2c8e884\.[a-f0-9]+\.js$/.test(u))||
      (frontend.carrierChunkHashes.length===1?`https://adminsms.smsoug.com/static/js/chunk-b2c8e884.${frontend.carrierChunkHashes[0]}.js`:null);
    if(carrierAsset){
      const source=await withOpenSmsRequestPacing(()=>page.client.evaluate(`(async()=>{
        const r=await fetch(${JSON.stringify(carrierAsset)},{credentials:'include',signal:AbortSignal.timeout(45000)});
        if(!r.ok)throw Error('TASK_H_FRONTEND_READ_FAILED');return await r.text();
      })()`,60000));
      const sourcePath=path.join(dir,`inspection_${uid}_carrier.js`);
      const sourceFile=await fs.open(sourcePath,'wx',0o600);
      try{await sourceFile.writeFile(source);await sourceFile.sync();}finally{await sourceFile.close();}
      result.carrierFrontendSource={url:carrierAsset,path:sourcePath,sha256:crypto.createHash('sha256').update(source).digest('hex')};
    }
    const handle=await fs.open(output,'wx',0o600);
    try{await handle.writeFile(JSON.stringify(result,null,2));await handle.sync();}finally{await handle.close();}
    console.log(JSON.stringify({output,businessWrites:0,frontend,frontendSource:result.frontendSource,carrierFrontendSource:result.carrierFrontendSource,carriers:snapshot.rows.filter(r=>/^[AB]\d+(?:V)?(?:$|[-\s])/i.test(r.name||'')).map(r=>({
      name:r.name||r.carrierName||r.carrireName||null,
      fields:Object.keys(r),limits:Object.fromEntries(Object.entries(r).filter(([k,v])=>
        /limit|daily|size|send|dispatch|day/i.test(k)&&['number','boolean'].includes(typeof v)))}))}));
    return result;
  }finally{
    session?.client.close();try{if(page)await page.close();}finally{lease.release();}
  }
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  if(process.argv.slice(2).join(' ')!=='--json')throw Error('TASK_H_INSPECTION_ONLY');
  inspectTaskH().catch(error=>{console.error(error.message);process.exitCode=1;});
}
