// Regression probe for acquisition startup only: no export, carrier changes,
// notifications, task F lookup, or full patrol. Close every run-owned target.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {createRawOpenSmsApiSession} from '../opensms_protocol_client.mjs';
import {downloadGoogleWorkbook} from './opensms_google_download_transport.mjs';

const output=process.argv[2];
if(!output)throw new Error('需要独立只读验证JSON输出路径');
const port=(await fs.readFile('/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/a53ce74e6c9e4fa08aec35f81ce1e2a3/DevToolsActivePort','utf8')).split(/\r?\n/)[0];
if(!/^\d+$/.test(port))throw new Error('430端口无效');
const cdp=`http://127.0.0.1:${port}`;
const dir=await fs.mkdtemp(path.join(path.dirname(path.resolve(output)),'readonly-acquire-'));
const runId=new Date(Date.now()+8*3600*1000).toISOString().replace(/[-:]/g,'').slice(0,15)+'+0800';
const result={runId,startedAt:new Date().toISOString(),silentBrowserMode:true,
  foregroundActivationAttempted:false,businessWrites:0,fullPatrolExecuted:false,hiddenPages:[],receiptDirectory:dir};
const cli=fileURLToPath(new URL('./opensms_api_target_guard_cli.mjs',import.meta.url));
const env={...process.env,BITBROWSER_CDP:cdp,OPENSMS_EXACT_CDP_ONLY:'1'};
for(const key of Object.keys(env))if(/^OPENSMS_(FORCE|ENABLE|EXECUTE|TASK)_/.test(key)||key==='OPENSMS_RAW_API_TARGET_ID')delete env[key];
const run=promisify(execFile);
async function verifyHiddenPages() {
  for(let i=0;i<3;i++) {
    const receipt=path.join(dir,`hidden_${i+1}.json`);
    let created=false;
    const record={trial:i+1};result.hiddenPages.push(record);
    try {
      await run(process.execPath,[cli,'create','--run-id',runId,'--receipt',receipt],{env,timeout:115000});
      created=true;
      const ownership=JSON.parse(await fs.readFile(receipt,'utf8'));
      record.applicationReadiness=ownership.applicationReadiness;
      record.payloadCodec=ownership.payloadCodec;
      process.env.OPENSMS_EXACT_CDP_ONLY='1';
      process.env.OPENSMS_RAW_API_TARGET_ID=ownership.targetId;
      const session=await createRawOpenSmsApiSession({cdp,pathName:'/day'});
      try {
        const response=await session.api('GET','/carrier/config/listNotDeleted',null,{pageNum:1,pageSize:1});
        record.readOnlyApi={code:response.code,rows:Array.isArray(response.rows)?response.rows.length:null};
        if(Number(response.code)!==200||!Array.isArray(response.rows))throw new Error('READ_ONLY_API_FAILED');
        record.success=true;
      } finally {await session.browser.close();}
    } catch(error) {
      record.success=false;record.errorCode=error.code||'READ_ONLY_PROBE_FAILED';
      // Guard CLI stderr is already sanitized; retain readiness diagnostics,
      // not arbitrary exceptions or any business response content.
      const diagnostic=String(error.stderr||'').split('\n').reverse().find(line=>line.startsWith('{'));
      if(diagnostic){try{record.diagnostics=JSON.parse(diagnostic);}catch{}}
    } finally {
      delete process.env.OPENSMS_RAW_API_TARGET_ID;
      if(created) {
        try {await run(process.execPath,[cli,'close','--run-id',runId,'--receipt',receipt],{env,timeout:20000});record.closed=true;}
        catch {record.closed=false;record.success=false;}
      }
    }
    if(!record.success)break;
  }
}
async function verifyGoogle() {
  try {
    const downloaded=await downloadGoogleWorkbook('https://docs.google.com/spreadsheets/d/1GiA9uR_2IQ4xntP_aWvCSaWfmC6WVQ7SrnXsy8YyyvM/export?format=xlsx');
    result.google={success:true,size:downloaded.bytes.length,sha256:crypto.createHash('sha256').update(downloaded.bytes).digest('hex'),
      elapsedMs:downloaded.elapsedMs,attempts:downloaded.attempts};
  } catch(error) {result.google={success:false,errorCode:error.code,attempts:error.downloadAttempts};}
}
await Promise.all([verifyHiddenPages(),verifyGoogle()]);
result.success=result.hiddenPages.length===3&&result.hiddenPages.every(row=>row.success&&row.closed)&&result.google.success;
result.finishedAt=new Date().toISOString();
await fs.writeFile(output,JSON.stringify(result,null,2)+'\n',{flag:'wx'});
console.log(JSON.stringify(result));
if(!result.success)process.exitCode=1;
