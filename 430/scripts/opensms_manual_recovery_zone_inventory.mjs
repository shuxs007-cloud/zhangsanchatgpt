// Read-only full recovery-zone inventory through the canonical shared session.
import path from 'node:path';
import {isDeepStrictEqual} from 'node:util';
import {acquireSharedOpenSmsSession} from './opensms_shared_browser.mjs';
import {createRawBackgroundPage,createRawOpenSmsApiSession} from '../opensms_protocol_client.mjs';
import {getCarrierConfigsReadOnly} from './opensms_carriers_readonly.mjs';
import {collectPopulation,readPlatformDate} from './opensms_expired_card_probe.mjs';
import {waitForPayloadPageReady} from '../opensms_payload_readiness.mjs';
import {atomicJson} from './opensms_execute_task_g.mjs';
import {carrierName,carrierId,liveIdentity} from './opensms_swap_t_restore_engine.mjs';

const [file,...extra]=process.argv.slice(2);
if(extra.length||!path.isAbsolute(file||''))throw Error('RECOVERY_INVENTORY_OUTPUT_INVALID');
const canonical=rows=>rows.map(liveIdentity).sort((a,b)=>a.id.localeCompare(b.id));
const lease=await acquireSharedOpenSmsSession({timeoutMs:301000});
let page,session,result;
try{
  page=await createRawBackgroundPage({cdp:lease.session.cdp,url:'https://adminsms.smsoug.com/day',
    authenticatedTargetId:lease.session.lastVerifiedTargetId});
  await waitForPayloadPageReady(page.client);process.env.OPENSMS_RAW_API_TARGET_ID=page.targetId;
  session=await createRawOpenSmsApiSession({cdp:lease.session.cdp,pathName:'/day'});
  let responseAt=0;
  const api=async(...args)=>{const gap=Math.max(0,responseAt+2000-Date.now());
    if(gap)await new Promise(r=>setTimeout(r,gap));
    try{return await session.api(...args);}finally{responseAt=Date.now();}};
  const carriers=await getCarrierConfigsReadOnly(api,{concurrency:1});
  const zones=carriers.filter(c=>c.delData!==true&&(/休息区/.test(carrierName(c))||/^换号区/.test(carrierName(c))))
    .map(c=>({name:carrierName(c),identif:carrierId(c)}));
  if(!zones.length||new Set(zones.map(z=>z.name)).size!==zones.length
    ||new Set(zones.map(z=>z.identif)).size!==zones.length)throw Error('RECOVERY_INVENTORY_ZONES_AMBIGUOUS');
  const first=await collectPopulation(api,zones),second=await collectPopulation(api,zones);
  const a=canonical(first),b=canonical(second);
  if(new Set(b.map(r=>r.phone)).size!==b.length||!isDeepStrictEqual(a,b))
    throw Error('RECOVERY_INVENTORY_POPULATION_CHANGED_OR_DUPLICATED');
  const serverDate=await readPlatformDate(page.client);
  result={status:'passed',readOnly:true,businessWrites:0,checkedAt:new Date().toISOString(),serverDate,
    zones,carriers,firstPopulation:first,population:second,total:b.length,
    zoneCounts:zones.map(z=>({...z,count:b.filter(r=>r.carrierIdentif===z.identif).length})),
    independentReadbackPassed:true};
}finally{
  session?.client.close();
  if(page)await page.close();
  lease.release();
}
result.ownedHiddenPageClosed=true;
await atomicJson(file,result,{exclusive:true});
console.log(JSON.stringify({status:result.status,path:file,serverDate:result.serverDate,
  total:result.total,zoneCounts:result.zoneCounts,independentReadbackPassed:true,ownedHiddenPageClosed:true,businessWrites:0}));
