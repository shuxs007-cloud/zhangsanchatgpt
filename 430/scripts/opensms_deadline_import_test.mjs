// Manual, explicitly authorized deadline-import test. Never launches a browser.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import readline from 'node:readline';
import {acquireSharedOpenSmsSession} from './opensms_shared_browser.mjs';
import {createRawBackgroundPage,createRawOpenSmsApiSession} from '../opensms_protocol_client.mjs';
import {withOpenSmsRequestPacing} from '../opensms_request_pacing.mjs';

const sourceRun='20261003T070204+0800';
const directory='/Volumes/zs01/chatgpt/OpenSMS/20261003/'+sourceRun;
const sourceReceipt=JSON.parse(await fs.readFile(directory+'/receipts/opensms_system_missing_f_dates_'+sourceRun+'.json','utf8'));
const digest=b=>crypto.createHash('sha256').update(b).digest('hex');
if(sourceReceipt.runId!==sourceRun || sourceReceipt.exportedRowCount!==263 || sourceReceipt.independentReadbackPassed!==true
  || digest(await fs.readFile(sourceReceipt.output))!==sourceReceipt.sha256
  || digest(await fs.readFile(sourceReceipt.sourceDetail))!==sourceReceipt.sourceDetailSha256) throw Error('SOURCE_RECEIPT_CHANGED');
const detail=JSON.parse(await fs.readFile(sourceReceipt.sourceDetail,'utf8'));
const phones=[...new Set(detail.rows.map(r=>String(r[detail.headers.indexOf('电话')])).filter(Boolean))];
if(phones.length!==263 || phones.some(p=>!/^\d{11}$/.test(p))) throw Error('SOURCE_PHONE_IDENTITIES_UNCONFIRMED');
const stamp=new Date().toISOString().replace(/[:.]/g,'-');
const auditDirectory='/Users/zhangsan/Downloads/430/opensms_artifacts/manual_deadline_import/'+stamp;
await fs.mkdir(auditDirectory,{recursive:true,mode:0o700});
let shared,page,apiSession,submitted=false,uploaded=false;
const events=[];
async function save(name,value){await fs.writeFile(path.join(auditDirectory,name+'.json'),JSON.stringify(value,null,2),{flag:'wx',mode:0o600});}
function emit(value){console.log(JSON.stringify(value));}
const snapshot=()=>page.client.evaluate(`(() => ({url:location.href,messages:[...document.querySelectorAll('.el-message,.el-message-box')].map(e=>e.innerText),dialogs:[...document.querySelectorAll('.el-dialog__wrapper')].filter(e=>getComputedStyle(e).display!=='none').map(e=>({text:e.innerText,html:e.innerHTML.slice(0,15000)})),inputs:[...document.querySelectorAll('input[type=file]')].map(e=>({accept:e.accept,multiple:e.multiple,files:[...e.files].map(f=>({name:f.name,size:f.size}))}))}))()`);
async function command(input){
  const {op}=input;
  if(op==='snapshot')return snapshot();
  if(op==='schema')return page.client.evaluate(`(() => {const list=[],seen=new Set();for(const el of document.querySelectorAll('*')){let v=el.__vue__;while(v&&!seen.has(v)){seen.add(v);if(v.$options.name==='PhoneConfig')list.push({name:v.$options.name,methods:Object.keys(v.$options.methods||{}),render:String(v.$options.render).slice(0,65000),upload:{action:v.expireUpload.url,open:v.expireUpload.open,isUploading:v.expireUpload.isUploading,files:v.$refs.expireUpload?.uploadFiles?.map(f=>({name:f.name,size:f.size,status:f.status}))}});if(v.$options.name==='ElTableColumn'&&/截止|激活/.test(v.label||''))list.push({name:v.$options.name,label:v.label,property:v.prop,formatter:String(v.formatter)});v=v.$parent;}}return list;})()`);
  if(op==='components')return page.client.evaluate(`(() => {const list=[],seen=new Set();for(const el of document.querySelectorAll('*')){let v=el.__vue__;while(v&&!seen.has(v)){seen.add(v);const methods=Object.keys(v.$options.methods||{}).filter(k=>/import|upload|deadline|cut|active/i.test(k));if(methods.length)list.push({name:v.$options.name,methods:methods.map(k=>({name:k,source:String(v.$options.methods[k]).slice(0,3500)})),dataKeys:Object.keys(v.$data||{})});v=v.$parent;}}return list;})()`);
  if(op==='dialog'){
    await page.client.evaluate(`(() => {const matches=[...document.querySelectorAll('button')].filter(e=>e.innerText.trim()==='导入截止日'&&getComputedStyle(e).display!=='none');if(matches.length!==1)throw Error('IMPORT_BUTTON_NOT_UNIQUE');matches[0].click();return true;})()`);
    return snapshot();
  }
  if(op==='upload'){
    if(uploaded||submitted)throw Error('FILE_ALREADY_SELECTED');
    const found=await page.client.evaluate(`(() => {const ds=[...document.querySelectorAll('.el-dialog__wrapper')].filter(e=>getComputedStyle(e).display!=='none'&&e.innerText.includes('截止日'));if(ds.length!==1)throw Error('IMPORT_DIALOG_NOT_UNIQUE');const ins=ds[0].querySelectorAll('input[type=file]');if(ins.length!==1)throw Error('FILE_INPUT_NOT_UNIQUE');ins[0].id='codex-authorized-deadline-input';return true;})()`);
    const doc=await page.client.send('DOM.getDocument',{depth:0});
    const node=await page.client.send('DOM.querySelector',{nodeId:doc.root.nodeId,selector:'#codex-authorized-deadline-input'});
    if(!node.nodeId||!found)throw Error('FILE_INPUT_NOT_FOUND');
    await page.client.send('DOM.setFileInputFiles',{nodeId:node.nodeId,files:[sourceReceipt.output]});
    uploaded=true;await save('file_selection',{file:sourceReceipt.output,sha256:sourceReceipt.sha256,rows:263,selectedAt:new Date().toISOString()});
    return snapshot();
  }
  if(op==='query'){
    const result=await apiSession.api('POST','/phone/config/list',{cardNo:phones.map(p=>p.slice(-10)).join('\n'),showAble:null,pageNum:1,pageSize:500},{pageNum:1,pageSize:500});
    if(Number(result.code)!==200||!Array.isArray(result.rows)||Number(result.total)!==result.rows.length||new Set(result.rows.map(r=>String(r.id))).size!==result.rows.length)throw Error('BACKEND_READ_INCOMPLETE');
    await save(input.name||('query-'+Date.now()),{checkedAt:new Date().toISOString(),...result});
    return {code:result.code,total:result.total,fields:Object.keys(result.rows[0]||{}),sample:result.rows.slice(0,2)};
  }
  if(op==='submit'){
    if(submitted||!uploaded||input.authorizedFileSha256!==sourceReceipt.sha256)throw Error('SUBMISSION_NOT_AUTHORIZED_OR_DUPLICATED');
    await save('submit_intent',{file:sourceReceipt.output,sha256:sourceReceipt.sha256,sourceRun,rows:263,authorization:'current-human-upload-and-confirm',at:new Date().toISOString()});
    const check=await page.client.evaluate(`(() => {const ds=[...document.querySelectorAll('.el-dialog__wrapper')].filter(e=>getComputedStyle(e).display!=='none'&&e.innerText.includes('截止日'));if(ds.length!==1)throw Error('IMPORT_DIALOG_NOT_UNIQUE');const file=ds[0].querySelector('input[type=file]');const vm=ds[0].querySelector('.el-upload')?.__vue__;const files=vm?.$parent?.uploadFiles||[];const selected=file?.files.length?[...file.files]:files;if(selected.length!==1||selected[0].name!==${JSON.stringify(path.basename(sourceReceipt.output))})throw Error('WRONG_UPLOAD_FILE');const bs=[...ds[0].querySelectorAll('button')].filter(e=>e.innerText.replace(/\\s/g,'')==='确定');if(bs.length!==1||bs[0].disabled)throw Error('CONFIRM_BUTTON_UNAVAILABLE');return true;})()`);
    if(!check)throw Error('PRE_SUBMIT_CHECK_FAILED');
    submitted=true;
    const result=await withOpenSmsRequestPacing(()=>page.client.evaluate(`(async () => {const ds=[...document.querySelectorAll('.el-dialog__wrapper')].filter(e=>getComputedStyle(e).display!=='none'&&e.innerText.includes('截止日'));const bs=[...ds[0].querySelectorAll('button')].filter(e=>e.innerText.replace(/\\s/g,'')==='确定');bs[0].click();await new Promise(r=>setTimeout(r,5000));return {clicked:true,messages:[...document.querySelectorAll('.el-message,.el-message-box')].map(e=>e.innerText)};})()`,30000));
    await save('submit_ui',result);return result;
  }
  if(op==='events')return events;
  if(op==='response'){
    const event=events.find(e=>e.requestId===input.requestId&&e.kind==='response');
    if(!event||!/import|upload|async|task|process/i.test(event.url))throw Error('RESPONSE_SCOPE_UNCONFIRMED');
    const body=await page.client.send('Network.getResponseBody',{requestId:event.requestId});
    const result={event,...body};await save('response-'+Date.now(),result);return result;
  }
  if(op==='exit')return {exit:true};
  throw Error('UNKNOWN_COMMAND');
}
try{
  shared=await acquireSharedOpenSmsSession({timeoutMs:60000});
  page=await createRawBackgroundPage({cdp:shared.session.cdp,url:'https://adminsms.smsoug.com/phoneConfig',authenticatedTargetId:shared.target.id});
  page.client.socket.addEventListener('message',ev=>{const m=JSON.parse(ev.data);if(m.method==='Network.requestWillBeSent'){const p=m.params;if(new URL(p.request.url).origin==='https://adminsms.smsoug.com')events.push({kind:'request',requestId:p.requestId,method:p.request.method,url:p.request.url,at:new Date().toISOString()});}if(m.method==='Network.responseReceived'){const p=m.params;events.push({kind:'response',requestId:p.requestId,url:p.response.url,status:p.response.status,at:new Date().toISOString()});}});
  await page.client.send('Network.enable');
  process.env.OPENSMS_RAW_API_TARGET_ID=page.targetId;
  apiSession=await createRawOpenSmsApiSession({cdp:shared.session.cdp,pathName:'/phoneConfig'});
  await save('session',{browserName:shared.session.displayName,sessionId:shared.session.sessionId,servicePid:shared.session.pid,targetId:page.targetId,hidden:page.hidden,sourceFile:sourceReceipt.output,sourceSha256:sourceReceipt.sha256});
  emit({ready:true,auditDirectory,sourceRun,fileRows:263,...await snapshot()});
  for await(const line of readline.createInterface({input:process.stdin,terminal:false})){
    if(!line.trim())continue;
    try{const value=await command(JSON.parse(line));emit(value);if(value?.exit)break;}catch(error){emit({commandFailed:true,error:error.message});}
  }
}catch(error){emit({failed:true,error:error.message});process.exitCode=1;}
finally{
  await save('network_events',events);
  apiSession?.client.close();
  if(page)await page.close();
  shared?.release();
  emit({cleanup:true,browserKeptOpen:true,auditDirectory,submitted});
}
