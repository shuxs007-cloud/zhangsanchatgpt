import fs from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { fork } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRawBackgroundPage, RawCdpClient } from '../opensms_protocol_client.mjs';
import { waitForPayloadPageReady } from '../opensms_payload_readiness.mjs';
import { loadPayloadCodec } from '../opensms_payload_codec.mjs';

const args = process.argv.slice(2);
const mode = args[0];
const flag = name => { const i=args.indexOf(name); return i>=0 ? args[i+1] : null; };
const runId=flag('--run-id'), receiptPath=flag('--receipt');
if (!/^[0-9]{8}T[0-9]{6}\+0800$/.test(runId || '') || !receiptPath) throw new Error('需要精确本轮run-id及receipt路径');
const cdp=String(process.env.BITBROWSER_CDP || '').replace(/\/$/,'');
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(cdp)) throw new Error('需要本轮精确430调试端点');

async function main() {
  if (mode === 'create') {
    // Hidden targets belong to the creating CDP session. Keep that session
    // alive in a bounded owner process instead of reverting to a visible tab.
    const child = fork(fileURLToPath(import.meta.url), ['hold', ...args.slice(1)], {
      detached: true, stdio: ['ignore', 'ignore', 'ignore', 'ipc'],
      env: {...process.env, OPENSMS_HIDDEN_OWNER_PID: String(process.ppid)},
    });
    const result = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { child.kill(); reject(new Error('隐藏API页面创建超时，清理状态未确认')); }, 120000);
      child.once('message', message => {
        clearTimeout(timer);
        if (message.success) resolve(message);
        else reject(Object.assign(new Error(message.error), {
          navigationReceipt: message.navigationReceipt || null,
          applicationReadiness: message.applicationReadiness || null,
          backgroundCleanup: message.backgroundCleanup || null,
          startupRecovery: message.startupRecovery || null,
        }));
      });
      child.once('exit', code => { clearTimeout(timer); reject(new Error(`隐藏API页面保活进程退出: ${code}`)); });
      child.once('error', error => { clearTimeout(timer); reject(error); });
    });
    child.disconnect();
    child.unref();
    for (const event of result.startupRecovery?.attempts || []) {
      console.error?.(JSON.stringify({backgroundStartupRecovery:event}));
    }
    console.log(JSON.stringify(result));
    return;
  }
  if (mode === 'hold') {
    const requestedUrl='https://adminsms.smsoug.com/day';
    const page=await createRawBackgroundPage({cdp,url:requestedUrl});
    const marker=`opensms-patrol-api:${runId}:${randomUUID()}`;
    try {
      const applicationReadiness=await waitForPayloadPageReady(page.client);
      // Validate the current build before releasing parallel consumers. Persist
      // metadata only: key/IV stay in memory, with no old-build fallback.
      const codec=await loadPayloadCodec(expression=>page.client.evaluate(expression,12000));
      await page.client.evaluate(`window.name=${JSON.stringify(marker)}`, 5000);
      const receipt={success:true,ownedBy:'opensms-patrol-api-target',runId,targetId:page.targetId,
        endpoint:cdp,requestedUrl,marker,createdAt:new Date().toISOString(),
        navigationReceipt:page.navigationReceipt,applicationReadiness,payloadCodec:codec.metadata,
        startupRecovery:page.startupRecovery || null,
        silentBrowserMode:true,foregroundActivationAttempted:false,
        hidden:true,keeperPid:process.pid,ownerPid:Number(process.env.OPENSMS_HIDDEN_OWNER_PID)};
      await fs.writeFile(receiptPath,JSON.stringify(receipt,null,2)+'\n',{flag:'wx'});
      // Leave only this run-owned background target alive. Child API clients
      // connect to its exact ID; none may navigate or close it individually.
      process.send?.({success:true,targetId:page.targetId,receipt:receiptPath,endpoint:cdp,requestedUrl,hidden:true,
        startupRecovery:page.startupRecovery || null});
      const owner = Number(process.env.OPENSMS_HIDDEN_OWNER_PID);
      try {
        for (;;) {
          await new Promise(resolve => setTimeout(resolve, 2000));
          try { process.kill(owner, 0); } catch { break; }
          const response = await fetch(`${cdp}/json/list`, {signal:AbortSignal.timeout(5000)});
          if (!response.ok) throw new Error('隐藏API页面保活检查失败');
          if (!(await response.json()).some(t => t.id === page.targetId)) break;
        }
      } finally { await page.close().catch(() => {}); }
    } catch (error) {
      let closed=false;
      try { await page.close(); closed=true; } catch {}
      error.backgroundCleanup={targetId:page.targetId,closed,status:closed?'confirmed_closed':'cleanup_unconfirmed'};
      if(!closed) {
        // Preserve the owned target identity even if the primary receipt could
        // not be created. Never replace a prior run's successful receipt.
        await fs.writeFile(`${receiptPath}.create_failed.json`,JSON.stringify({
          ownedBy:'opensms-patrol-api-target',runId,endpoint:cdp,targetId:page.targetId,marker,
          success:false,backgroundCleanup:error.backgroundCleanup,
        },null,2)+'\n',{flag:'wx'}).catch(()=>{});
      }
      throw error;
    }
    return;
  }
  if (mode !== 'close') throw new Error('Use create or close');
  const receipt=JSON.parse(await fs.readFile(receiptPath,'utf8'));
  if(receipt.ownedBy!=='opensms-patrol-api-target'||receipt.runId!==runId||receipt.endpoint!==cdp
    || !String(receipt.marker||'').startsWith(`opensms-patrol-api:${runId}:`)) throw new Error('后台API页归属校验失败');
  const response=await fetch(`${cdp}/json/list`,{signal:AbortSignal.timeout(5000)});
  if(!response.ok) throw new Error(`API target list HTTP ${response.status}`);
  const target=(await response.json()).find(t=>t.id===receipt.targetId);
  if(target){
    if(!target.webSocketDebuggerUrl) throw new Error('Owned target websocket missing');
    const client=new RawCdpClient(target.webSocketDebuggerUrl);
    try {
      await client.connect();
      const marker=await client.evaluate('window.name',5000);
      if(marker!==receipt.marker) throw new Error('后台API页标记不匹配，拒绝关闭');
    } finally {client.close();}
    const versionResponse=await fetch(`${cdp}/json/version`,{signal:AbortSignal.timeout(5000)});
    if(!versionResponse.ok) throw new Error(`CDP version HTTP ${versionResponse.status}`);
    const browser=new RawCdpClient((await versionResponse.json()).webSocketDebuggerUrl);
    try {
      await browser.connect();
      const closed=await browser.send('Target.closeTarget',{targetId:receipt.targetId},10000);
      if(!closed.success) throw new Error('后台API页面未确认关闭');
    } finally {browser.close();}
  }
  await fs.writeFile(receiptPath,JSON.stringify({...receipt,closedAt:new Date().toISOString(),closed:true,alreadyClosed:!target},null,2)+'\n');
  console.log(JSON.stringify({success:true,closed:true,alreadyClosed:!target,targetId:receipt.targetId,receipt:receiptPath}));
}
main().catch(error=>{
  if (process.connected) process.send({success:false,error:String(error.message || error),
    navigationReceipt:error.navigationReceipt||null,applicationReadiness:error.applicationReadiness||null,
    backgroundCleanup:error.backgroundCleanup||null,startupRecovery:error.startupRecovery||null});
  console.error(error?.stack||error);
  console.error(JSON.stringify({navigationReceipt:error.navigationReceipt||null,
    applicationReadiness:error.applicationReadiness||null,backgroundCleanup:error.backgroundCleanup||null,
    startupRecovery:error.startupRecovery||null}));
  process.exitCode=1;
});
