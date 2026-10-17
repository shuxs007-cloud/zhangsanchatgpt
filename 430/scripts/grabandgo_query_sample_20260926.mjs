// Explicit-user, read-only sample. Reuses the maintained exact-identity query.
import fs from 'node:fs/promises';
import { queryOne } from './grabandgo_task_f_lookup.mjs';

const root = '/Users/zhangsan/Downloads/430';
const iccids = [
  '8912230200277885949F', '8912230200277884827F',
  '8912230200278118233F', '8912230200277963977F',
  '8912230200278137225F', '8912230200278134487F',
  '8912230200278133067F', '8912230200278134198F',
  '8912230200278194887F', '8912230200278699794F',
];
const origin = 'https://www.grabandgoplus.com';
const customerUrl = `${origin}/Retailer_CustomerSIMCards`;
const portFile = '/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/a53ce74e6c9e4fa08aec35f81ce1e2a3/DevToolsActivePort';
const startedAt = new Date().toISOString();
const receiptPath = `${root}/opensms_artifacts/logs/grab_sample_10_${startedAt.replace(/[:.]/g, '-')}.json`;
const receipt = { startedAt, scope: 'current_user_message_first_10', silentBrowserMode: true,
  foregroundActions: 0, navigationActions: 0, businessWrites: 0, automaticLogin: false,
  liveQueries: true, rows: [], errors: [], requests: [], status: 'running' };
let socket;
const pending = new Map();
let sequence = 0;

async function checkpoint() {
  await fs.mkdir(`${root}/opensms_artifacts/logs`, { recursive: true });
  await fs.writeFile(receiptPath, JSON.stringify(receipt, null, 2) + '\n', { mode: 0o600 });
}

async function connect() {
  const port = (await fs.readFile(portFile, 'utf8')).split(/\r?\n/)[0].trim();
  if (!/^\d+$/.test(port)) throw new Error('Invalid 430 endpoint');
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(10000) })).json();
  const pages = targets.filter(t => t.type === 'page' && new URL(t.url).origin === origin);
  const target = pages.find(t => t.url === customerUrl) || pages.find(t => t.url === `${origin}/Retailer_Account`);
  if (!target) throw new Error('No existing Grab page in BitBrowser 430; no page was opened.');
  receipt.targetId = target.id;
  receipt.pageUrl = target.url;
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('CDP connection timeout')), 10000);
    socket.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
    socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('CDP connection failed')); }, { once: true });
  });
  socket.addEventListener('message', event => {
    const message = JSON.parse(String(event.data));
    const call = pending.get(message.id);
    if (!call) return;
    pending.delete(message.id);
    clearTimeout(call.timer);
    if (message.error) call.reject(new Error(message.error.message));
    else call.resolve(message.result);
  });
  socket.addEventListener('close', () => {
    for (const call of pending.values()) { clearTimeout(call.timer); call.reject(new Error('CDP closed')); }
    pending.clear();
  });
}

async function evaluate(expression) {
  const id = ++sequence;
  const result = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('Read request timed out')); }, 75000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method: 'Runtime.evaluate', params: {
      expression, awaitPromise: true, returnByValue: true, userGesture: false,
    } }));
  });
  if (result.exceptionDetails) throw new Error(result.result?.description || result.exceptionDetails.text);
  return result.result?.value;
}

async function request(method, url, options = {}) {
  const parsed = new URL(url);
  if (parsed.origin !== origin || !/^\/Retailer_CustomerSIMCards\/?$/i.test(parsed.pathname)) {
    throw new Error(`Read-only URL guard rejected ${parsed.pathname}`);
  }
  if (method === 'POST') {
    const form = new URLSearchParams(options.data);
    const filter = form.get('ctl00$GagPRetailerHolder$btnFilter') === 'Go';
    const details = /^ctl00\$GagPRetailerHolder\$dlCustomerSimCards\$ctl\d+\$lnSimDetails$/.test(form.get('__EVENTTARGET') || '');
    if (!filter && !details) throw new Error('Only search and Details postbacks are permitted');
  }
  const payload = { method, url, data: options.data };
  const response = await evaluate(`(async (input) => {
    if (location.origin !== ${JSON.stringify(origin)}) throw new Error('Origin changed');
    const res = await fetch(input.url, {
      method: input.method, credentials: 'include', cache: 'no-store',
      signal: AbortSignal.timeout(60000),
      ...(input.data === undefined ? {} : { body: input.data, headers: {'content-type':'application/x-www-form-urlencoded'} }),
    });
    const html = await res.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const fields = [...doc.querySelectorAll('input,select,span')]
      .filter(el => /lbl(?:ActivationRequestDate|ActivateDate|ActivationDate|SimCardNumber|PurchaseDate)|txt(?:ActivationDate|SimCardNumber)|(?:Sim|Activation|Request).*Status|Status.*(?:Sim|Activation|Request)/i.test(el.id || el.name || ''))
      .filter(el => !/password|hidden/i.test(el.type || ''))
      .map(el => ({id:el.id, value:el.tagName === 'SELECT' ? el.selectedOptions?.[0]?.textContent?.trim() : (el.value ?? el.textContent?.trim() ?? '')}));
    const body = doc.body?.textContent || '';
    return {status:res.status, url:res.url, text:html, evidence:{
      showAll:/Show\\s+All/i.test(body), passwordInput:!!doc.querySelector('input[type=password]'),
      searchValue:doc.querySelector('input[id$=txtSearchCriteria]')?.value || '',
      total:doc.querySelector('input[id$=hfTotal]')?.value ?? null,
      emptyMessage:/no (?:records|results|data)(?: found| available)?/i.test(body), fields,
    }};
  })(${JSON.stringify(payload)})`);
  receipt.requests.push({method, queryAt:new Date().toISOString(), status:response.status, url:response.url, ...response.evidence});
  return {status:() => response.status, url:() => response.url, text:async () => response.text};
}

try {
  await connect();
  const api = {get:(url, options) => request('GET',url,options), post:(url,options) => request('POST',url,options)};
  let reusableHtml = '';
  for (let index = 0; index < iccids.length; index++) {
    const originalIccid = iccids[index];
    const item = {originalIccid, queryIccid:originalIccid.replace(/F$/i, ''), sampleIndex:index+1};
    const result = await queryOne(api, item, 'existing_430_serial', reusableHtml);
    reusableHtml = result.reusableHtml;
    const row = result.row;
    row.statusChinese = row.status === 'not_found' ? '不存在' : row.activationDate ? '激活成功' : row.actRequestDate ? '激活中' : '未激活';
    row.classificationBasis = row.status === 'not_found' ? 'authenticated_exact_query_no_match'
      : row.activationDate ? 'activation_date_present' : row.actRequestDate ? 'request_date_present_activation_date_empty' : 'request_and_activation_dates_empty';
    receipt.rows.push(row);
    await checkpoint();
    console.log(JSON.stringify({index:index+1, iccid:originalIccid, status:row.statusChinese, activationDate:row.activationDate, actRequestDate:row.actRequestDate}));
    if(index+1 < iccids.length) await new Promise(resolve => setTimeout(resolve,1000));
  }
  receipt.status = 'complete';
} catch(error) {
  receipt.status = 'failed';
  receipt.errors.push({at:new Date().toISOString(), message:String(error.message || error)});
  process.exitCode = 1;
} finally {
  receipt.finishedAt = new Date().toISOString();
  await checkpoint();
  socket?.close();
  console.log(JSON.stringify({status:receipt.status, completed:receipt.rows.length, receiptPath, errors:receipt.errors}));
}
