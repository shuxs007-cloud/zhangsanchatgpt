import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { ADMIN_ORIGIN, encryptPayload, parseResponseText } from './opensms_binding_protocol.mjs';
import { acquireSiteLock } from './opensms_site_lock.mjs';

await acquireSiteLock({browserId: 'a53ce74e6c9e4fa08aec35f81ce1e2a3'});

const folder = process.env.OPENSMS_BATCH_DIR || path.dirname(fileURLToPath(import.meta.url));
fs.mkdirSync(folder, { recursive: true });
const policy = JSON.parse(fs.readFileSync(new URL('./opensms_rules.json', import.meta.url))).automaticB12;
const sourceCodes = policy.sources;
if (policy.target !== 'B12' || new Set(sourceCodes).size !== 4 || !['A3','B1','B11','B3'].every(c => sourceCodes.includes(c))) throw new Error('Unexpected automatic binding policy');
const checkpoint = process.argv[2];
if (!/^\d{8}_\d{4}$/.test(checkpoint || '')) throw new Error('Expected YYYYMMDD_HHMM');
const bj = () => new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Hong_Kong', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date());
const date = `${checkpoint.slice(0,4)}-${checkpoint.slice(4,6)}-${checkpoint.slice(6,8)}`;
function timeGate() {
  const now = bj();
  if (now.slice(0,10) !== date || now.slice(11) < policy.writeStart || now.slice(11) > policy.writeEnd) throw new Error(`Outside write window: ${now}`);
  return now;
}
const receiptPath = path.join(folder, `opensms_guarded_b12_${checkpoint}.json`);
const batchPath = path.join(folder, `opensms_b12_union_batch_${checkpoint}.json`);
const lockPath = `${receiptPath}.lock`;
if (fs.existsSync(receiptPath) || fs.existsSync(batchPath)) throw new Error('Checkpoint already recorded; inspect and verify only');
timeGate();
const lock = fs.openSync(lockPath, 'wx');
const receipt = { checkpoint, startedAtBeijing: bj(), silentBrowserMode: true, browserForegroundActivationAttempted: false, sourceBindingCounts: {}, created: 0, alreadyBound: 0, failures: [], deleteCount: 0, createdBindings: [], receiptPath };
const save = () => fs.writeFileSync(receiptPath, JSON.stringify(receipt, null, 2));
let ws;
try {
  const port = fs.readFileSync('/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/a53ce74e6c9e4fa08aec35f81ce1e2a3/DevToolsActivePort', 'utf8').split(/\r?\n/)[0].trim();
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = targets.find(p => p.type === 'page' && p.url.startsWith(`${ADMIN_ORIGIN}/proxyCarrierConfig`)) || targets.find(p => p.type === 'page' && p.url.startsWith(`${ADMIN_ORIGIN}/`) && !p.url.includes('/login'));
  if (!page) throw new Error('No existing logged-in target');
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve,reject) => { ws.onopen=resolve; ws.onerror=()=>reject(new Error('CDP connection failed')); });
  let seq=0;
  const pending=new Map();
  ws.onmessage=event=>{const m=JSON.parse(String(event.data));const p=pending.get(m.id);if(!p)return;pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(new Error(m.error.message)):p.resolve(m.result);};
  const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;const timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout'));},30000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));});
  const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw new Error('Page evaluation failed');return r.result.value;};
  const url=await evaluate('location.href');
  if (new URL(url).origin!==ADMIN_ORIGIN || /\/login(?:\?|$)/i.test(url)) throw new Error('Login/origin check failed');
  let next=0;
  async function api(method,endpoint,data=null,params={}) {
    await new Promise(r=>setTimeout(r,Math.max(0,next-Date.now())));
    if(method!=='GET')timeGate();
    const u=new URL(`${ADMIN_ORIGIN}/api${endpoint}`);Object.entries(params).forEach(([k,v])=>u.searchParams.set(k,String(v)));
    const body=data===null?null:encryptPayload(JSON.stringify(data));
    const r=await evaluate(`(async()=>{const raw=document.cookie.split('; ').find(x=>x.startsWith('Admin-Token='));if(!raw)return {status:401,text:'',contentType:''};const token=decodeURIComponent(raw.slice(12));const headers={Accept:'application/json, text/plain, */*',Authorization:token.startsWith('Bearer ')?token:'Bearer '+token};const body=${JSON.stringify(body)};if(body!==null)headers['Content-Type']='text/plain';const r=await fetch(${JSON.stringify(u.href)},{method:${JSON.stringify(method)},headers,body,credentials:'include'});return {status:r.status,text:await r.text(),contentType:r.headers.get('content-type')||''};})()`);
    next=Date.now()+2000;
    if(r.status!==200)throw new Error(`${method} ${endpoint}: HTTP ${r.status}`);
    const parsed=parseResponseText(r.text,r.contentType,r.status);
    if(Number(parsed.code)!==200)throw new Error(`${method} ${endpoint}: business failure`);
    return parsed;
  }
  const resolveCarrier=(rows,code)=>{const matches=rows.filter(r=>r.name===code||r.name?.startsWith(`${code}-`));if(matches.length!==1||!matches[0].identif)throw new Error(`Carrier not unique: ${code}`);return matches[0];};
  async function gate() {
    timeGate();
    const r=await api('GET','/carrier/config/listNotDeleted',null,{pageNum:1,pageSize:9999});
    const mapped=Object.fromEntries([...sourceCodes,policy.target].map(c=>[c,resolveCarrier(r.rows,c)]));
    let remaining=0,total=0;
    const values={};
    for(const c of ['A3','B1','B11','B3']){const row=mapped[c];if(row.dailyRemaining==null||row.dailyTotal==null||row.dailyRemaining===''||row.dailyTotal==='')throw new Error('Missing balance');const rem=Number(row.dailyRemaining),tot=Number(row.dailyTotal);if(!Number.isFinite(rem)||!Number.isFinite(tot)||rem<0||tot<=0||rem>tot)throw new Error(`Invalid balance: ${c}`);remaining+=rem;total+=tot;values[c]={name:row.name,identif:String(row.identif),remaining:rem,total:tot};}
    receipt.balance={at:bj(),values,remaining,total,ratio:remaining/total};
    if(remaining/total>=policy.thresholdStrictlyBelow)throw new Error('Combined balance is not below threshold');
    timeGate();return mapped;
  }
  async function list(identif){const rows=[];const ids=new Set();for(let pageNum=1;pageNum<10000;pageNum++){const r=await api('GET','/proxycarrier/list',null,{pageNum,pageSize:100,carrireIdentif:String(identif)});if(!Array.isArray(r.rows)||!Number.isInteger(Number(r.total)))throw new Error('Invalid pagination');for(const row of r.rows){if(String(row.carrireIdentif)!==String(identif)||ids.has(String(row.id)))throw new Error('Pagination/filter mismatch');ids.add(String(row.id));rows.push(row);}if(rows.length===Number(r.total))return rows;if(rows.length>Number(r.total)||!r.rows.length)throw new Error('Incomplete pagination');}throw new Error('Pagination limit');}
  const initial=await gate();receipt.initialBalance=receipt.balance;
  const users=new Map();
  for(const c of ['A3','B1','B11','B3']){const rows=await list(initial[c].identif);const source=new Map(rows.map(r=>[String(r.proxyUserId),{proxyUserId:String(r.proxyUserId),proxyUserName:r.proxyUserName}]));receipt.sourceBindingCounts[c]=source.size;for(const [id,u] of source)users.set(id,u);}
  receipt.uniqueAccountCount=users.size;
  const existing=await list(initial.B12.identif);
  const existingUsers=new Set(existing.map(r=>String(r.proxyUserId)));
  receipt.alreadyBound=[...users.keys()].filter(id=>existingUsers.has(id)).length;
  receipt.initialB12Ids=existing.map(r=>String(r.id));
  const current=await gate();
  for(const c of Object.keys(initial))if(String(initial[c].identif)!==String(current[c].identif))throw new Error('Carrier identifier changed');
  const identif=String(current.B12.identif);
  const pendingUsers=[...users.values()].filter(u=>!existingUsers.has(u.proxyUserId));
  receipt.pendingCount=pendingUsers.length;save();
  const account=async u=>{const r=await api('GET',`/proxycarrier/listByProxyUserId/${u.proxyUserId}`,null,{pageNum:1,pageSize:1000});if(!Array.isArray(r.rows)||r.rows.length>=1000)throw new Error('Invalid account bindings');return r.rows;};
  for(const u of pendingUsers){
    const before=await account(u);
    if(before.some(r=>String(r.carrireIdentif)===identif)){receipt.alreadyBound++;save();continue;}
    timeGate();
    await api('POST','/proxycarrier',{carrireIdentif:identif,proxyUserId:Number(u.proxyUserId),smsCost:0,smsRetail:0,mmsCost:0,mmsRetail:0,monthlyCost:0,remark:''});
    const after=await account(u);
    const beforeIds=new Set(before.map(r=>String(r.id)));
    const added=after.filter(r=>!beforeIds.has(String(r.id))&&String(r.carrireIdentif)===identif);
    if(added.length!==1)throw new Error(`New binding not uniquely resolved: ${u.proxyUserId}`);
    const record={id:String(added[0].id),...u,carrireIdentif:identif,carrierName:current.B12.name,checkpoint:`${date} ${checkpoint.slice(9,11)}:${checkpoint.slice(11,13)}`,cancelAtBeijing:`${date} 13:00:00`};
    receipt.createdBindings.push(record);receipt.created++;
    fs.writeFileSync(batchPath,JSON.stringify({checkpoint,cancelAtBeijing:`${date} 13:00:00`,bindings:receipt.createdBindings},null,2));
    if(before.some(r=>!after.some(a=>String(a.id)===String(r.id))))throw new Error('Existing binding disappeared during execution');
    save();if(receipt.created%10===0)console.error(JSON.stringify({created:receipt.created,pending:pendingUsers.length}));
  }
  const final=await list(identif);
  const finalUsers=new Set(final.map(r=>String(r.proxyUserId)));
  receipt.finalMissing=[...users.values()].filter(u=>!finalUsers.has(u.proxyUserId));
  receipt.originalB12Missing=existing.filter(r=>!final.some(a=>String(a.id)===String(r.id))).map(r=>String(r.id));
  receipt.finalMissingCount=receipt.finalMissing.length;
  receipt.ok=!receipt.finalMissingCount&&!receipt.originalB12Missing.length;
  receipt.batchPath=receipt.created?batchPath:null;
}catch(e){receipt.ok=false;receipt.failures.push({error:e.message});}
finally{receipt.finishedAtBeijing=bj();save();ws?.close();fs.closeSync(lock);fs.unlinkSync(lockPath);}
console.log(JSON.stringify(receipt,null,2));
process.exitCode=receipt.ok?0:1;
