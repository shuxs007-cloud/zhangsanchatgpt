// Explicit local naming/migration only. Never invoked by a scheduled patrol.
import fs from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {acquireSharedOpenSmsSession,SHARED_BROWSER_NAME} from './opensms_shared_browser.mjs';
import {connectRawOpenSmsPage} from '../opensms_protocol_client.mjs';

const mode=process.argv[2];
if(!['label','merge-legacy','verify-merge'].includes(mode)) throw new Error('Explicit label, merge-legacy or verify-merge required');
const shared=await acquireSharedOpenSmsSession({timeoutMs:0});
let raw;
const receipt={action:mode,name:SHARED_BROWSER_NAME,sessionId:shared.session.sessionId,
  servicePid:shared.session.pid,port:shared.session.port,browserLaunched:false,
  credentialsCopied:false,siteBusinessRequests:0,businessWrites:0,
  foregroundAttempted:false,startedAt:new Date().toISOString()};
try {
  if(mode==='merge-legacy' || mode==='verify-merge') {
    const old=JSON.parse(await fs.readFile('/Users/zhangsan/Documents/数据分析/cloakbrowser_free_test/login_browser_status.json','utf8'));
    if(!Number.isInteger(old.pid) || old.pid===shared.session.pid || old.headless!==true
        || !Number.isInteger(old.port) || old.port===shared.session.port) throw new Error('LEGACY_IDLE_BROWSER_IDENTITY_UNCONFIRMED');
    if(mode==='verify-merge') {
      let gone=false;
      try {process.kill(old.pid,0);} catch(error) {if(error.code==='ESRCH') gone=true;else throw error;}
      if(!gone) throw new Error('LEGACY_BROWSER_SERVICE_STILL_ACTIVE');
    } else {
    const command=execFileSync('/bin/ps',['-p',String(old.pid),'-o','args='],{encoding:'utf8'}).trim();
    if(!command.includes('/Users/zhangsan/Documents/数据分析/cloakbrowser_free_test/login_browser.mjs')
        || !command.includes('--serve')) throw new Error('LEGACY_BROWSER_SERVICE_CHANGED');
    const version=await fetch(`http://127.0.0.1:${old.port}/json/version`,{signal:AbortSignal.timeout(5000)});
    if(!version.ok) throw new Error('LEGACY_BROWSER_CDP_UNCONFIRMED');
    const ws=new URL((await version.json()).webSocketDebuggerUrl);
    if(ws.hostname!=='127.0.0.1' || ws.port!==String(old.port)
        || !ws.pathname.startsWith('/devtools/browser/')) throw new Error('LEGACY_BROWSER_CDP_IDENTITY_CHANGED');
    // The canonical site/report/request guards are held; this independently
    // identified old headless service is idle. Graceful close preserves files.
    process.kill(old.pid,'SIGTERM');
    const deadline=Date.now()+15000;
    let closed=false;
    while(Date.now()<deadline) {
      try {process.kill(old.pid,0);} catch(error) {
        if(error.code==='ESRCH') {closed=true;break;} throw error;
      }
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    if(!closed) throw new Error('LEGACY_BROWSER_CLOSE_UNCONFIRMED');
    }
    let endpointGone=false;
    try {await fetch(`http://127.0.0.1:${old.port}/json/version`,{signal:AbortSignal.timeout(2000)});}
    catch {endpointGone=true;}
    if(!endpointGone) throw new Error('LEGACY_BROWSER_ENDPOINT_STILL_ACTIVE');
    receipt.legacy={servicePid:old.pid,port:old.port,closed:true,endpointGone:true,
      closurePerformedInThisAction:mode==='merge-legacy',
      profileAndOldFilesPreserved:true};
  }
  raw=await connectRawOpenSmsPage({cdp:shared.session.cdp,pathName:new URL(shared.target.url).pathname});
  const label=await raw.client.evaluate(`(() => {
    document.title=${JSON.stringify(SHARED_BROWSER_NAME+'｜短信后台')};
    return {title:document.title,href:location.href};
  })()`);
  if(label.title!==SHARED_BROWSER_NAME+'｜短信后台' || new URL(label.href).origin!=='https://adminsms.smsoug.com') {
    throw new Error('SHARED_BROWSER_LABEL_READBACK_FAILED');
  }
  receipt.windowLabel=label.title;
  receipt.windowLabelReadbackPassed=true;
  receipt.dockApplicationNameChanged=false; // Preserve the running signed app.
  receipt.status='passed';
  const output='/Users/zhangsan/Downloads/430/opensms_artifacts/state/new_browser/shared_browser_'+randomUUID()+'.json';
  await fs.writeFile(output,JSON.stringify(receipt,null,2),{flag:'wx',mode:0o600});
  console.log(JSON.stringify({...receipt,receiptPath:output}));
} catch(error) {
  receipt.status='failed';
  receipt.error=/^[A-Z0-9_]+$/.test(error.message)?error.message:'SHARED_BROWSER_ADMIN_UNCONFIRMED';
  const output='/Users/zhangsan/Downloads/430/opensms_artifacts/state/new_browser/shared_browser_'+randomUUID()+'.json';
  await fs.writeFile(output,JSON.stringify(receipt,null,2),{flag:'wx',mode:0o600});
  console.log(JSON.stringify({...receipt,receiptPath:output}));
  process.exitCode=1;
} finally {
  raw?.client.close();
  shared.release();
}
