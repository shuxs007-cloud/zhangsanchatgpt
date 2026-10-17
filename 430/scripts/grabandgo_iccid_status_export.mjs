// Read-only, explicit ICCID list lookup using the existing BitBrowser 430 page.
// Search batches are adaptive; only exact ICCID matches are retained. No login,
// activation, navigation, operator changes, or database updates are performed.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';

const [outputDir, mode = 'run'] = process.argv.slice(2);
if (!outputDir || !['inspect', 'run', 'resume', 'verify'].includes(mode)) throw new Error('Usage: OUTPUT_DIR [inspect|run|resume|verify]');
const attachment = '/Users/zhangsan/.codex/attachments/d669fe64-ab6a-4a87-82ca-5105bb292168/已粘贴的文本.txt';
const inline = `8912230200277885949F
8912230200277884827F
8912230200278118233F
8912230200277963977F
8912230200278137225F
8912230200278134487F
8912230200278133067F
8912230200278134198F
8912230200278194887F
8912230200278699794F
8912230200278695685F
8912230200278120775F
8912230200278695651F
8912230200278122607F
8912230200278120122F
8912230200278121955F
8912230200278698077F
8912230200278148347F
8912230200278097627F
8912230200278104498F
8912230200278096652F
8912230200278701491F
8912230200278095514F
8912230200278093337F
8912230200278194887F
8912230200278699794F
8912230200278150137F
8912230200278104902F
8912230200278095514F
8912230200278093337F
8912230200278152349F
8912230200251591653F
8912230200252111592F
8912230200278193848F
8912230200278195926F
8912230200278197880F
8912230200278701491F
8912230200278701210F
8912230200278196528F
8912230200278194457F
8912230200278698770F
8912230200252179920F
89302720536828320834
8912230200252174160F
8912230200252070566F
8912230200278134016F`.split(/\s+/);
const normalize = value => value.trim().replace(/[Ff]$/, '');
const sourceRows = [];
for (const [source, list] of [['附件', (await fs.readFile(attachment, 'utf8')).split(/\s+/).filter(Boolean)], ['消息清单', inline]]) {
  list.forEach((iccid, index) => sourceRows.push({ source, sourceRow:index+1, originalIccid:iccid, queryIccid:normalize(iccid) }));
}
for(const row of sourceRows) assert.match(row.queryIccid, /^\d{18,22}$/);
const groups = new Map();
for(const row of sourceRows) {
  if(!groups.has(row.queryIccid)) groups.set(row.queryIccid, []);
  groups.get(row.queryIccid).push(row);
}
const origin = 'https://www.grabandgoplus.com';
const url = `${origin}/Retailer_CustomerSIMCards`;
const receiptFile = path.join(outputDir, 'query-receipt.json');
let receipt = {startedAt:new Date().toISOString(), status:'running', silentBrowserMode:true,
  sourceUrl:url, sourceRows, originalRows:sourceRows.length, uniqueIccids:groups.size,
  duplicateRows:sourceRows.length-groups.size, businessWrites:0, foregroundActions:0,
  navigationActions:0, autoLoginAttempted:false, batches:[], results:[], errors:[]};
if(mode === 'resume' || mode === 'verify') {
  receipt = JSON.parse(await fs.readFile(receiptFile,'utf8'));
  assert.deepEqual(receipt.sourceRows, sourceRows);
  if(mode === 'resume'){
    receipt.status = 'running';
    receipt.resumedAt = new Date().toISOString();
  } else assert.equal(receipt.status,'complete');
}
let socket;
let seq=0;
let html='';
const pending = new Map();
const results = new Map(receipt.results.map(row => [row.queryIccid,row]));
await fs.mkdir(outputDir,{recursive:true});
async function checkpoint() {
  receipt.results = [...groups.keys()].filter(key=>results.has(key)).map(key=>results.get(key));
  receipt.completed = results.size;
  const temp = receiptFile + '.tmp';
  await fs.writeFile(temp, JSON.stringify(receipt,null,2)+'\n', {mode:0o600});
  await fs.rename(temp,receiptFile);
}
async function connect() {
  const portPath='/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/a53ce74e6c9e4fa08aec35f81ce1e2a3/DevToolsActivePort';
  const port=(await fs.readFile(portPath,'utf8')).split(/\r?\n/)[0].trim();
  assert.match(port,/^\d+$/);
  const targets=await(await fetch(`http://127.0.0.1:${port}/json/list`,{signal:AbortSignal.timeout(10000)})).json();
  const target=targets.find(t=>t.type==='page'&&t.url===url)||targets.find(t=>t.type==='page'&&t.url===`${origin}/Retailer_Account`);
  if(!target) throw new Error('430现有页面中未找到Grab页面，请先登录');
  receipt.targetId=target.id;
  socket=new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('CDP timeout')),10000);
    socket.addEventListener('open',()=>{clearTimeout(timer);resolve();},{once:true});
    socket.addEventListener('error',()=>{clearTimeout(timer);reject(new Error('CDP error'));},{once:true});
  });
  socket.addEventListener('message',event=>{
    const msg=JSON.parse(String(event.data)), call=pending.get(msg.id);
    if(!call)return;
    pending.delete(msg.id);clearTimeout(call.timer);
    msg.error?call.reject(new Error(msg.error.message)):call.resolve(msg.result);
  });
}
async function evaluate(expression) {
  const id=++seq;
  const result=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{pending.delete(id);reject(new Error('Read timeout'));},70000);
    pending.set(id,{resolve,reject,timer});
    socket.send(JSON.stringify({id,method:'Runtime.evaluate',params:{expression,awaitPromise:true,returnByValue:true,userGesture:false}}));
  });
  if(result.exceptionDetails)throw new Error(result.result?.description||result.exceptionDetails.text);
  return result.result?.value;
}
// Detached DOM only. Form state is kept in process memory and never written to disk.
async function request(action,term='') {
  assert.ok(['get','showAll','search'].includes(action));
  const input={url,origin,action,term,html};
  const payload=await evaluate(`(async input=>{
    if(location.origin!==input.origin)throw new Error('Grab origin changed');
    const options={method:'GET',credentials:'include',cache:'no-store',signal:AbortSignal.timeout(60000)};
    if(input.action!=='get'){
      const base=new DOMParser().parseFromString(input.html,'text/html');
      const form=base.querySelector('form');if(!form)throw new Error('Missing query form');
      const params=new URLSearchParams(new FormData(form));
      for(const key of [...params.keys()])if(/btnFilter|btnShowAllSimCards|btnExtendBuffer$/.test(key))params.delete(key);
      params.set('__EVENTTARGET','');params.set('__EVENTARGUMENT','');
      params.set('ctl00$GagPRetailerHolder$txtSearchCriteria',input.action==='search'?input.term:'');
      params.set(input.action==='search'?'ctl00$GagPRetailerHolder$btnFilter':'ctl00$GagPRetailerHolder$btnShowAllSimCards',input.action==='search'?'Go':'Show All');
      options.method='POST';options.body=params.toString();options.headers={'content-type':'application/x-www-form-urlencoded'};
    }
    const res=await fetch(input.url,options);const body=await res.text();
    const doc=new DOMParser().parseFromString(body,'text/html');
    if(res.status!==200||!res.url.startsWith(input.url)||doc.querySelector('input[type=password]')||!doc.querySelector('#GagPRetailerHolder_btnFilter'))
      throw new Error('Grab登录失效或响应异常: HTTP='+res.status+' URL='+res.url);
    const rows=[...doc.querySelectorAll('[id^=GagPRetailerHolder_dlCustomerSimCards_lblSimCardNumber_]')].map(el=>{
      const index=el.id.match(/_(\\d+)$/)[1];const fields={};
      for(const element of doc.querySelectorAll('[id^=GagPRetailerHolder_dlCustomerSimCards_]')) {
        const match=element.id.match(/^GagPRetailerHolder_dlCustomerSimCards_(.+)_(\\d+)$/);
        if(match&&match[2]===index)fields[match[1]]=(element.textContent||'').replace(/\\s+/g,' ').trim();
      }
      return {fields,queryIccid:(fields.lblSimCardNumber||'').replace(/\\s+/g,'').replace(/[Ff]$/,'')};
    });
    const totalRaw=doc.querySelector('#hfTotal')?.value, renderedRaw=doc.querySelector('#hfRendered')?.value;
    if(!/^\\d+$/.test(totalRaw||'')||!/^\\d+$/.test(renderedRaw||''))throw new Error('Missing server result counters');
    const controls=[...doc.querySelectorAll('input:not([type=hidden]),select')].filter(el=>!/password/i.test(el.type||'')).map(el=>({id:el.id,type:el.type,value:el.value,options:el.tagName==='SELECT'?[...el.options].map(o=>({value:o.value,text:o.textContent,selected:o.selected})):undefined}));
    return {html:body,status:res.status,url:res.url,total:Number(totalRaw),rendered:Number(renderedRaw),rows,
      searchValue:doc.querySelector('#GagPRetailerHolder_txtSearchCriteria')?.value||'',showAllPresent:!!doc.querySelector('#GagPRetailerHolder_btnShowAllSimCards'),controls};
  })(${JSON.stringify(input)})`);
  html=payload.html;
  assert.equal(payload.rows.length,payload.rendered,'Parsed row count differs from server rendered count');
  assert.ok(payload.total>=payload.rendered);
  assert.equal(new Set(payload.rows.map(r=>r.queryIccid)).size,payload.rows.length,'Duplicate server SIM identifiers');
  if(action==='search')assert.equal(payload.searchValue,term,'Search echo mismatch');
  return payload;
}
function rowResult(key,match,batch) {
  const rows=groups.get(key), fields=match?.fields||{};
  let status='不存在';
  if(match){
    for(const name of ['lblPurchaseDate','lblActivationRequestDate','lblActivateDate','lblSimCardNumber','lblSKU'])assert.ok(Object.hasOwn(fields,name),`Missing ${name}`);
    assert.equal(normalize(fields.lblSimCardNumber),key);
    const activation=fields.lblActivateDate.trim(), request=fields.lblActivationRequestDate.trim();
    for(const value of [activation,request])if(value)assert.match(value,/^\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?$/,'Unexpected date, not classified');
    status=activation?'激活成功':request?'激活中':'未激活';
  }
  return {queryIccid:key,originalIccid:rows[0].originalIccid,occurrences:rows.length,sources:[...new Set(rows.map(r=>r.source))],
    status,purchaseDate:fields.lblPurchaseDate||'',actRequestDate:fields.lblActivationRequestDate||'',activationDate:fields.lblActivateDate||'',
    simNumber:fields.lblSimCardNumber||'',sku:fields.lblSKU||'',rawFields:fields,queriedAt:batch.queriedAt,batchId:batch.id,
    matchMethod:match?'exact_iccid_from_authenticated_search':'exact_query_zero_server_results',sourceUrl:url};
}
async function searchBatch(keys,prefix) {
  keys=keys.filter(k=>!results.has(k));if(!keys.length)return;
  const term=keys.length===1?keys[0]:prefix;
  const response=await request('search',term);
  const batch={id:receipt.batches.length+1,term,requested:keys.length,total:response.total,rendered:response.rendered,queriedAt:new Date().toISOString(),httpStatus:response.status,matched:0};
  const exactMap=new Map(response.rows.map(row=>[row.queryIccid,row]));
  for(const key of keys)if(exactMap.has(key)){results.set(key,rowResult(key,exactMap.get(key),batch));batch.matched++;}
  if(term===keys[0]&&keys.length===1&&!results.has(keys[0])){
    assert.equal(response.total,0,'Exact ICCID query returned unexpected other records');
    results.set(keys[0],rowResult(keys[0],null,batch));
  }
  receipt.batches.push(batch);
  await checkpoint();
  console.log(JSON.stringify({batch:batch.id,completed:results.size,total:groups.size,serverRows:batch.total,rendered:batch.rendered,matched:batch.matched}));
  await new Promise(resolve=>setTimeout(resolve,1000));
  const missing=keys.filter(k=>!results.has(k));
  if(!missing.length)return;
  if(response.total===response.rendered){
    // A miss in a broad search is independently queried by full ICCID.
    for(const key of missing)await searchBatch([key],key);
    return;
  }
  const subgroups=new Map();
  for(const key of missing){const sub=key.slice(0,prefix.length+1);if(!subgroups.has(sub))subgroups.set(sub,[]);subgroups.get(sub).push(key);}
  for(const [sub,list] of subgroups)await searchBatch(list,sub);
}
try {
  await connect();
  const initial=await request('get');
  if(mode==='inspect'){
    console.log(JSON.stringify({inputRows:sourceRows.length,unique:groups.size,duplicates:sourceRows.length-groups.size,total:initial.total,rendered:initial.rendered,showAllPresent:initial.showAllPresent,controls:initial.controls},null,2));
  } else if(mode==='verify') {
    if(initial.showAllPresent)await request('showAll');
    const selected = new Set(inline.slice(0,3).map(normalize));
    for(const status of ['激活成功','激活中','未激活','不存在']) {
      for(const row of receipt.results.filter(row=>row.status===status).slice(0,2))selected.add(row.queryIccid);
    }
    receipt.exactVerification=[];
    for(const key of selected){
      const response=await request('search',key);
      const previous=results.get(key);
      const match=response.rows.find(row=>row.queryIccid===key);
      if(!match)assert.equal(response.total,0);
      else assert.equal(response.rows.length,1);
      const current=rowResult(key,match,{id:previous.batchId,queriedAt:new Date().toISOString()});
      for(const field of ['status','simNumber','purchaseDate','actRequestDate','activationDate','sku'])assert.equal(current[field],previous[field],`Independent exact check differs: ${key} ${field}`);
      receipt.exactVerification.push({queryIccid:key,status:current.status,total:response.total,queriedAt:current.queriedAt,consistent:true});
      await checkpoint();
      console.log(JSON.stringify({verified:receipt.exactVerification.length,total:selected.size}));
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
    receipt.verificationFinishedAt=new Date().toISOString();await checkpoint();
    console.log(JSON.stringify({status:'verified',count:receipt.exactVerification.length,receiptFile}));
  } else {
    if(initial.showAllPresent)await request('showAll');
    receipt.authenticatedAt=new Date().toISOString();
    const prefixes=new Map();
    for(const key of groups.keys()){const prefix=key.slice(0,15);if(!prefixes.has(prefix))prefixes.set(prefix,[]);prefixes.get(prefix).push(key);}
    for(const [prefix,keys] of prefixes)await searchBatch(keys,prefix);
    assert.equal(results.size,groups.size);
    receipt.status='complete';receipt.finishedAt=new Date().toISOString();
    receipt.counts=Object.fromEntries(['不存在','未激活','激活中','激活成功'].map(status=>[status,[...results.values()].filter(r=>r.status===status).length]));
    await checkpoint();console.log(JSON.stringify({status:receipt.status,unique:groups.size,original:sourceRows.length,duplicates:receipt.duplicateRows,counts:receipt.counts,receiptFile}));
  }
} catch(error){
  receipt.status='failed';receipt.errors.push({at:new Date().toISOString(),message:String(error.message||error)});
  await checkpoint();console.error(JSON.stringify({status:'failed',completed:results.size,receiptFile,error:String(error.message||error)}));process.exitCode=1;
} finally {socket?.close();}
