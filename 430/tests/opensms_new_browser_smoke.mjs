// Run with OS network access restricted to loopback. No OpenSMS navigation.
import {performance} from 'node:perf_hooks';
import {launchBrowser} from '../scripts/opensms_new_browser.mjs';
import {RawCdpClient, backgroundContextOptions} from '../opensms_protocol_client.mjs';

const started = performance.now();
let launched, cdp, hiddenId;
try {
  launched = await launchBrowser({headless:true});
  await launched.browser.newPage();
  const targets = await (await fetch(`http://127.0.0.1:${launched.port}/json/list`)).json();
  const existing = targets.find(target => target.type === 'page' && target.url === 'about:blank');
  if (!existing) throw new Error('OFFLINE_PAGE_MISSING');
  cdp = new RawCdpClient(`ws://127.0.0.1:${launched.port}${launched.webSocketPath}`);
  await cdp.connect();
  const info = (await cdp.send('Target.getTargetInfo', {targetId:existing.id}, 10000)).targetInfo;
  const context = backgroundContextOptions('cloakbrowser', info, existing.id);
  hiddenId = (await cdp.send('Target.createTarget', {url:'about:blank',
    ...context, hidden:true, background:true, forTab:false}, 10000)).targetId;
  const created = (await cdp.send('Target.getTargetInfo', {targetId:hiddenId}, 10000)).targetInfo;
  if (created.browserContextId !== info.browserContextId) throw new Error('OFFLINE_CONTEXT_CHANGED');
  if (!(await cdp.send('Target.closeTarget', {targetId:hiddenId}, 10000)).success) throw new Error('OFFLINE_CLEANUP_UNCONFIRMED');
  hiddenId = null;
  console.log(JSON.stringify({status:'passed', browserVersion:launched.version,
    scope:'offline-about-blank-only', realBrowserLaunched:true, rawCdp:true,
    hiddenPage:true, exactLoginContext:true, websiteRequests:0, businessWrites:0,
    durationMs:Math.round(performance.now()-started)}));
} catch(error) {
  console.log(JSON.stringify({status:'failed', scope:'offline-about-blank-only', error:error.message.split('\n')[0],
    websiteRequests:0,businessWrites:0,durationMs:Math.round(performance.now()-started)}));
  process.exitCode=2;
} finally {
  if (hiddenId && cdp) await cdp.send('Target.closeTarget',{targetId:hiddenId},10000).catch(()=>{});
  cdp?.close();
  if (launched) await launched.browser.close();
}
