// Task I current-position source: number-management ICCID search/export only.
// Explicit current-user authorization; own hidden page and canonical leases.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {acquireSharedOpenSmsSession} from './opensms_shared_browser.mjs';
import {createRawBackgroundPage,createRawOpenSmsApiSession,encryptPayload} from '../opensms_protocol_client.mjs';
import {waitForPayloadPageReady} from '../opensms_payload_readiness.mjs';
import {withOpenSmsRequestPacing} from '../opensms_request_pacing.mjs';
import {loadPayloadCodec} from '../opensms_payload_codec.mjs';

const normalize=v=>typeof v==='string'?v.trim().replace(/[Ff]$/,''):'';
export function validateSearchExport(response){
  const bytes=Buffer.from(response.base64||'','base64');
  if(!response.ok||response.status!==200||bytes[0]!==0x50||bytes[1]!==0x4b)
    throw Object.assign(Error(`TASK_I_SEARCH_EXPORT_NOT_XLSX_HTTP_${response.status}`),{status:response.status});
  return bytes;
}
export function validateSearchRows(result,keys){
  if(Number(result.code)!==200||!Number.isSafeInteger(result.total)||result.total<0||!Array.isArray(result.rows))throw Error('TASK_I_SEARCH_RESPONSE_INVALID');
  for(const row of result.rows){
    if(!keys.has(normalize(row.cardStore)))throw Error('TASK_I_SEARCH_OUTSIDE_ICCID_SCOPE');
    if(row.slotIdentif!=null&&typeof row.slotIdentif!=='string')throw Error('TASK_I_SLOT_TYPE_INVALID');
  }
}

export async function runExport(directory,queryPath){
  if(!path.isAbsolute(directory)||!directory.startsWith('/Volumes/zs01/chatgpt/OpenSMS/task_i/')||!path.isAbsolute(queryPath))throw Error('TASK_I_OUTPUT_SCOPE_INVALID');
  const input=await fs.readFile(queryPath),queries=JSON.parse(input).queries;
  const keys=[...new Set(queries.map(q=>normalize(q.query)))];
  if(!keys.length||keys.length>500||keys.some(k=>!/^\d{18,22}$/.test(k))||queries.some(q=>q.queryType!=='iccid'))throw Error('TASK_I_EXACT_ICCID_SCOPE_REQUIRED_MAX_500');
  await fs.mkdir(directory); // An existing export directory never replays.
  const save=(name,data)=>fs.writeFile(path.join(directory,name),JSON.stringify(data,null,2),{flag:'wx',mode:0o600});
  await save('input_scope.json',{iccidCount:keys.length,iccids:keys,queryPath,inputSha256:createHash('sha256').update(input).digest('hex')});
  let lease,page,session,cleanupPassed=false,numberManagementRequestCount=0;
  try{
    lease=await acquireSharedOpenSmsSession({timeoutMs:301000});
    page=await createRawBackgroundPage({cdp:lease.session.cdp,url:'https://adminsms.smsoug.com/phoneConfig',authenticatedTargetId:lease.target.id});
    await waitForPayloadPageReady(page.client);process.env.OPENSMS_RAW_API_TARGET_ID=page.targetId;
    session=await createRawOpenSmsApiSession({cdp:lease.session.cdp,pathName:'/phoneConfig'});
    let lastResponse=0;
    const paced=async action=>{const ms=Math.max(0,lastResponse+2000-Date.now());if(ms)await new Promise(r=>setTimeout(r,ms));try{return await action();}finally{lastResponse=Date.now();}};
    let filter,readyDeadline=Date.now()+30000;
    do{filter=await page.client.evaluate(`(() => {const queue=[document.querySelector('#app')?.__vue__].filter(Boolean),seen=new Set();
      for(let i=0;i<queue.length;i++){const vm=queue[i];if(!vm||seen.has(vm))continue;seen.add(vm);queue.push(...(vm.$children||[]));
        if(vm.$options?.name!=='PhoneConfig')continue;
        if(typeof vm.buildListQuery!=='function'||typeof vm.handleExport!=='function')throw Error('TASK_I_SEARCH_EXPORT_METHOD_CHANGED');
        const values=Object.fromEntries(Object.keys(vm.queryParams).map(k=>[k,null]));values.pageNum=1;values.pageSize=500;values.cardStore=${JSON.stringify(keys.join('\n'))};
        vm.queryParams=values;const query=vm.buildListQuery(),exportQuery=vm.buildListQuery({forExport:true});
        if(!query||!exportQuery)throw Error('TASK_I_SEARCH_FILTER_BUILD_FAILED');
        delete exportQuery.pageNum;delete exportQuery.pageSize;
        const order=exportQuery['params[orderByColumn]'],asc=exportQuery['params[isAsc]'];delete exportQuery['params[orderByColumn]'];delete exportQuery['params[isAsc]'];if(order)exportQuery.params={orderByColumn:order,isAsc:asc};
        return {query,exportQuery,searchMethod:String(vm.$options.methods.buildListQuery),exportMethod:String(vm.$options.methods.handleExport)};}
      return null;})()`);
      if(filter)break;await new Promise(r=>setTimeout(r,200));
    }while(Date.now()<readyDeadline);
    if(!filter)throw Error('TASK_I_NUMBER_MANAGEMENT_NOT_READY');
    const actualKeys=filter.query.cardStoreList||[filter.query.cardStore];
    if(JSON.stringify(actualKeys)!==JSON.stringify(keys)||filter.query.showAble!==1||filter.query.cardNo||filter.query.cardNoList)throw Error('TASK_I_FRONTEND_QUERY_SCOPE_CHANGED');
    await save('filters.json',filter);
    const collect=async()=>{
      const all=[];let total;
      for(let pn=1;pn<=100;pn++){
        const q={...filter.query,pageNum:pn,pageSize:500};
        numberManagementRequestCount++;
        const r=await paced(()=>session.api('POST','/phone/config/list',q,{pageNum:pn,pageSize:500}));
        validateSearchRows(r,new Set(keys));
        if(total===undefined)total=r.total;else if(r.total!==total)throw Error('TASK_I_SEARCH_TOTAL_DRIFT');
        all.push(...r.rows);if(all.length===total)break;if(!r.rows.length||all.length>total)throw Error('TASK_I_SEARCH_PAGING_INCOMPLETE');
      }
      if(all.length!==total)throw Error('TASK_I_SEARCH_PAGING_INCOMPLETE');
      return {code:200,total,rows:all};
    };
    const before=await collect();await save('search_before.json',before);
    console.log(JSON.stringify({status:'running',phase:'search-confirmed',requestedIccids:keys.length,searchRows:before.total}));
    const codec=await paced(()=>withOpenSmsRequestPacing(()=>loadPayloadCodec(e=>page.client.evaluate(e,12000))));
    const authorization=session.token.startsWith('Bearer ')?session.token:`Bearer ${session.token}`;
    const body=encryptPayload(JSON.stringify(filter.exportQuery),codec);
    await save('export_intent.json',{method:'POST',apiPath:'/phone/config/export',at:new Date().toISOString(),attempts:1,scopeCount:keys.length,searchTotal:before.total});
    numberManagementRequestCount++;
    const response=await paced(()=>withOpenSmsRequestPacing(async()=>{const r=await page.client.evaluate(`(async()=>{
      const r=await fetch('https://adminsms.smsoug.com/api/phone/config/export',{method:'POST',credentials:'include',headers:{Authorization:${JSON.stringify(authorization)},'Content-Type':'text/plain'},body:${JSON.stringify(body)},signal:AbortSignal.timeout(120000)});
      const bytes=new Uint8Array(await r.arrayBuffer());if(bytes.length>20*1024*1024)throw Error('TASK_I_EXPORT_UNEXPECTEDLY_LARGE');
      let s='';for(let i=0;i<bytes.length;i+=32768)s+=String.fromCharCode(...bytes.subarray(i,i+32768));
      return {status:r.status,ok:r.ok,contentType:r.headers.get('Content-Type'),base64:btoa(s)};})()`,150000);validateSearchExport(r);return r;}));
    const bytes=validateSearchExport(response);
    const exportedAt=new Date().toISOString(),file=path.join(directory,'号码管理_ICCID搜索结果.xlsx');
    await fs.writeFile(file,bytes,{flag:'wx',mode:0o600});
    const after=await collect();await save('search_after.json',after);
    const signature=r=>r.rows.map(x=>JSON.stringify([normalize(x.cardStore),String(x.cardNo||''),x.slotIdentif||''])).sort();
    if(JSON.stringify(signature(before))!==JSON.stringify(signature(after)))throw Error('TASK_I_SEARCH_POSITIONS_CHANGED_DURING_EXPORT');
    await save('export_receipt.json',{status:'downloaded-search-readback-passed',source:'号码管理->指定ICCID->搜索->导出搜索结果',requestedIccids:keys.length,
      searchRows:before.total,path:file,sha256:createHash('sha256').update(bytes).digest('hex'),exportedAt,contentType:response.contentType,
      independentSearchReadback:true,xlsxIndependentReadback:false,sourceScope:keys,silentBrowserMode:true,businessWrites:0,numberManagementRequestCount});
    console.log(JSON.stringify({status:'downloaded-search-readback-passed',requestedIccids:keys.length,searchRows:before.total,path:file,exportedAt,businessWrites:0}));
  }catch(e){await save('failure.json',{status:'failed-no-retry',error:e.message,businessWrites:0,numberManagementRequestCount});throw e;}
  finally{session?.client.close();if(page){await page.close();cleanupPassed=true;}else cleanupPassed=true;if(cleanupPassed)lease?.release();}
}

export async function probe(directory){
  if(!path.isAbsolute(directory)||!directory.startsWith('/Volumes/zs01/chatgpt/OpenSMS/task_i/'))throw Error('TASK_I_OUTPUT_SCOPE_INVALID');
  await fs.mkdir(directory,{recursive:true});
  const lease=await acquireSharedOpenSmsSession({timeoutMs:301000});
  let page,session,cleanupPassed=false;
  try{
    page=await createRawBackgroundPage({cdp:lease.session.cdp,url:'https://adminsms.smsoug.com/phoneConfig',authenticatedTargetId:lease.target.id});
    await waitForPayloadPageReady(page.client);process.env.OPENSMS_RAW_API_TARGET_ID=page.targetId;
    session=await createRawOpenSmsApiSession({cdp:lease.session.cdp,pathName:'/phoneConfig'});
    // Reuse the canonical read-only authentication check; no export or write.
    const auth=await session.api('GET','/getInfo');
    if(Number(auth.code)!==200)throw Error('TASK_I_AUTH_READBACK_FAILED');
    let snapshot;const readyDeadline=Date.now()+30000;
    do{snapshot=await page.client.evaluate(`(() => {
      const roots=[document.querySelector('#app')?.__vue__].filter(Boolean),seen=new Set(),components=[];
      for(let i=0;i<roots.length;i++){const vm=roots[i];if(!vm||seen.has(vm))continue;seen.add(vm);roots.push(...(vm.$children||[]));
        const methods=vm.$options?.methods||{};
        if(vm.$options?.name==='PhoneConfig'){
          components.push({name:vm.$options?.name||'',dataKeys:Object.keys(vm.$data||{}),
            filters:Object.fromEntries(Object.entries(vm.$data||{}).filter(([k,v])=>/queryParams|searchForm|filter/i.test(k)&&v&&typeof v==='object').map(([k,v])=>[k,Object.keys(v)])),
            methods:Object.fromEntries(Object.entries(methods).filter(([k])=>/export|download|getList|handleQuery|handleSearch|buildListQuery|normalizeBatch/i.test(k)).map(([k,f])=>[k,String(f).slice(0,6500)]))});}}
      return {pathname:location.pathname,passwordInput:!!document.querySelector('input[type=password]'),
        inputs:[...document.querySelectorAll('input,textarea')].map(e=>({tag:e.tagName,placeholder:e.getAttribute('placeholder'),type:e.type})),
        buttons:[...document.querySelectorAll('button')].map(e=>String(e.innerText||'').trim()),components,
        scripts:[...new Set(performance.getEntriesByType('resource').map(e=>e.name).filter(s=>s.startsWith('https://adminsms.smsoug.com/static/js/')&&s.endsWith('.js')))].slice(-12)};
    })()`);
      if(snapshot.components.length)break;
      await new Promise(resolve=>setTimeout(resolve,200));
    }while(Date.now()<readyDeadline);
    if(snapshot.components.length!==1||snapshot.passwordInput)throw Error('TASK_I_NUMBER_MANAGEMENT_NOT_READY');
    const exportCode=[];
    for(const url of snapshot.scripts){
      const found=await withOpenSmsRequestPacing(()=>page.client.evaluate(`(async()=>{const r=await fetch(${JSON.stringify(url)});if(!r.ok)throw Error('TASK_I_SOURCE_SCRIPT_FAILED');const s=await r.text();const key='/phone/config/export';const i=s.indexOf(key);return i<0?null:{url:${JSON.stringify(url)},snippet:s.slice(Math.max(0,i-450),i+1700)};})()`));
      if(found){exportCode.push(found);break;}
    }
    snapshot.exportCode=exportCode;
    await fs.writeFile(path.join(directory,'ui_probe.json'),JSON.stringify(snapshot,null,2),{flag:'wx',mode:0o600});
    console.log(JSON.stringify({status:'passed',phase:'readonly-ui-probe',pathname:snapshot.pathname,components:snapshot.components,exportCode}));
  }finally{
    session?.client.close();
    if(page){await page.close();cleanupPassed=true;}else cleanupPassed=true;
    if(cleanupPassed)lease.release();
  }
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href){
  const mode=process.argv[2],directory=process.argv[3];
  if(!['probe','export'].includes(mode)||!process.argv.includes('--current-user-authorized-task-i-search-export'))throw Error('TASK_I_CURRENT_USER_AUTHORIZATION_REQUIRED');
  (mode==='probe'?probe(directory):runExport(directory,process.argv[4])).catch(e=>{console.error(JSON.stringify({status:'failed',error:e.message}));process.exitCode=1;});
}
