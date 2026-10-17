// Human-authorized login only. No ICCID query, browser launch or business write.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
import {acquireSiteLock} from '/Users/zhangsan/Documents/数据分析/opensms_site_lock.mjs';
import {authenticateWorkerWithCredential} from './grabandgo_task_f_lookup.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const python='/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3';
const require=createRequire(import.meta.url);
const {request}=require('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const keychain=path.join(root,'scripts/grabandgo_auth_keychain.py');
async function main(){
  if(process.env.GRAB_CURRENT_USER_LOGIN_AUTHORIZED!=='1')throw Error('GRAB_LOGIN_AUTHORIZATION_MISSING');
  const chunks=[];let length=0;
  for await(const chunk of process.stdin){length+=chunk.length;if(length>4096)throw Error('GRAB_LOGIN_INPUT_INVALID');chunks.push(chunk);}
  const credential=JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if(typeof credential.username!=='string'||!credential.username.includes('@')||typeof credential.password!=='string'||!credential.password)throw Error('GRAB_LOGIN_INPUT_INVALID');
  let lease,api;
  try{
    lease=await acquireSiteLock({browserId:'a53ce74e6c9e4fa08aec35f81ce1e2a3',timeoutMs:1500});
    api=await request.newContext({storageState:{cookies:[],origins:[]},ignoreHTTPSErrors:false});
    await authenticateWorkerWithCredential(api,credential,1);
    // Independent second protected-page read, without searching or selecting SIMs.
    const response=await api.get('https://www.grabandgoplus.com/Retailer_CustomerSIMCards',{timeout:60000,failOnStatusCode:false});
    const html=await response.text();
    if(response.status()!==200||!/^https:\/\/www\.grabandgoplus\.com\/Retailer_CustomerSIMCards(?:[/?#]|$)/i.test(response.url())||!/\bCustomer\s+SIM\s+Cards\b/i.test(html)||/type=["']password["']/i.test(html))throw Error('GRAB_LOGIN_PROTECTED_READBACK_FAILED');
    const saved=JSON.parse(execFileSync(python,[keychain,'put'],{input:JSON.stringify(credential),stdio:['pipe','pipe','ignore'],encoding:'utf8'}));
    const checked=JSON.parse(execFileSync(python,[keychain,'get'],{stdio:['ignore','pipe','ignore'],encoding:'utf8'}));
    if(saved.status!=='saved'||checked.status!=='found'||Buffer.from(checked.data,'base64').toString('utf8')!==JSON.stringify(credential))throw Error('GRAB_KEYCHAIN_READBACK_FAILED');
    const receipt={version:1,status:'passed',authenticated:true,protectedPageReadbackPassed:true,credentialStore:'macOS Keychain, local non-synchronizable',credentialReadbackPassed:true,credentialsWrittenToReceipt:false,sensitiveIccidSubmittedCount:0,browserLaunched:false,browserForegroundActivationAttempted:false,at:new Date().toISOString()};
    const directory=path.join(root,'opensms_artifacts/manual_grab_login');await fs.mkdir(directory,{recursive:true,mode:0o700});
    const file=path.join(directory,Date.now()+'.json');await fs.writeFile(file,JSON.stringify(receipt,null,2),{flag:'wx',mode:0o600});
    console.log(JSON.stringify({...receipt,receipt:file}));
  }finally{credential.password='';await api?.dispose();lease?.release();}
}
main().catch(()=>{console.log(JSON.stringify({status:'failed',error:'GRAB_AUTHORIZED_LOGIN_NOT_VERIFIED',credentialsWrittenToReceipt:false}));process.exitCode=1;});
