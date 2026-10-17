// Explicit integration verification: two owned hidden routes, one exact context
// and CK, no carrier binding/maintenance action or export is submitted.
import fs from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {acquireSharedOpenSmsSession} from './opensms_shared_browser.mjs';
import {connectRawOpenSmsPage,createRawBackgroundPage} from '../opensms_protocol_client.mjs';
const shared=await acquireSharedOpenSmsSession({timeoutMs:0});
let raw;
const receipt={name:shared.session.displayName,servicePid:shared.session.pid,
  sessionId:shared.session.sessionId,port:shared.session.port,
  scope:'shared-context-readiness-only',browserLaunched:false,browserClosed:false,
  cookiesImported:false,credentialsPrinted:false,foregroundAttempted:false,
  exportSubmitted:false,bindingWrites:0,deviceMaintenanceSubmitted:false,
  startedAt:new Date().toISOString(),routes:[]};
try {
  raw=await connectRawOpenSmsPage({cdp:shared.session.cdp,pathName:new URL(shared.target.url).pathname});
  const sourceInfo=(await raw.client.send('Target.getTargetInfo')).targetInfo;
  const sourceCookies=(await raw.client.send('Network.getCookies',{urls:['https://adminsms.smsoug.com/']})).cookies;
  const source=sourceCookies.find(c=>c.name==='Admin-Token' && c.value);
  if(!source || !sourceInfo.browserContextId) throw new Error('SHARED_ANCHOR_CONTEXT_UNCONFIRMED');
  for(const route of ['/proxyCarrierConfig','/machineconfig']) {
    let page;
    try {
      page=await createRawBackgroundPage({cdp:shared.session.cdp,
        url:'https://adminsms.smsoug.com'+route,authenticatedTargetId:shared.target.id});
      const info=(await page.browserClient.send('Target.getTargetInfo',{targetId:page.targetId})).targetInfo;
      const cookies=(await page.client.send('Network.getCookies',{urls:['https://adminsms.smsoug.com/']})).cookies;
      const token=cookies.find(c=>c.name==='Admin-Token' && c.value);
      const url=await page.client.evaluate('location.href');
      const row={route,sameBrowserContext:info.browserContextId===sourceInfo.browserContextId,
        sameCookie:Boolean(token && token.value===source.value),hidden:page.hidden===true,
        sameOrigin:new URL(url).origin==='https://adminsms.smsoug.com',
        atExpectedRoute:new URL(url).pathname===route};
      receipt.routes.push(row);
      if(!Object.entries(row).filter(([k])=>k!=='route').every(([,v])=>v===true)) {
        throw new Error('SHARED_ROUTE_CONTEXT_READBACK_FAILED');
      }
    } finally {await page?.close();}
    receipt.routes.at(-1).ownedTargetClosed=true;
  }
  receipt.status='passed';
} catch(error) {
  receipt.status='failed';receipt.error=/^[A-Z0-9_]+$/.test(error.message)?error.message:'SHARED_CONTEXT_VERIFICATION_UNCONFIRMED';
  process.exitCode=1;
} finally {
  raw?.client.close();shared.release();
  const output='/Users/zhangsan/Downloads/430/opensms_artifacts/state/new_browser/shared_verify_'+randomUUID()+'.json';
  await fs.writeFile(output,JSON.stringify(receipt,null,2),{flag:'wx',mode:0o600});
  console.log(JSON.stringify({...receipt,receiptPath:output}));
}
