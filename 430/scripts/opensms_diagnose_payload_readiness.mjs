// Read-only diagnosis: exact 430, no business API, no cookies/key/IV output.
import fs from 'node:fs/promises';
import {createRawBackgroundPage, connectRawOpenSmsPage} from '../opensms_protocol_client.mjs';
import {readLoadedPayloadConfig, createPayloadCodec} from '../opensms_payload_codec.mjs';

const output = process.argv[2];
if (!output) throw new Error('需要本轮独立诊断JSON输出路径');
const port = (await fs.readFile('/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache/a53ce74e6c9e4fa08aec35f81ce1e2a3/DevToolsActivePort', 'utf8')).split(/\r?\n/)[0];
if (!/^\d+$/.test(port)) throw new Error('430端口无效');
const cdp = `http://127.0.0.1:${port}`;
process.env.OPENSMS_EXACT_CDP_ONLY = '1';
delete process.env.OPENSMS_RAW_API_TARGET_ID;
const result = {startedAt: new Date().toISOString(), silentBrowserMode:true, businessApiCalls:0, observations:[]};
async function observe(client, label) {
  const state = await client.evaluate(`(() => ({origin:location.origin, pathname:location.pathname,
    readyState:document.readyState, appScripts:[...document.scripts].map(s=>s.src).filter(s=>/\\/static\\/js\\/app\\.[\\w-]+\\.js$/.test(s))}))()`);
  let codec;
  try { codec = createPayloadCodec(await client.evaluate(`(${readLoadedPayloadConfig.toString()})()`,12000)).metadata; }
  catch (error) {
    const first = String(error.message).split('\n')[0];
    codec = {failed:true, reason:/当前页面应用脚本|当前应用AES|读取当前应用|AES参数页面|Failed to fetch|fetch failed|TimeoutError|PAYLOAD_/.test(first) ? first : 'unclassified_read_failure'};
  }
  result.observations.push({label, at:new Date().toISOString(), ...state, codec});
}
try {
  const existing = await connectRawOpenSmsPage({cdp,pathName:'/day'});
  try { await observe(existing.client,'existing_page'); } finally { existing.client.close(); }
  const hidden = await createRawBackgroundPage({cdp,url:'https://adminsms.smsoug.com/day'});
  result.navigation = hidden.navigationReceipt;
  try {
    await observe(hidden.client,'new_hidden_immediate');
    await new Promise(resolve=>setTimeout(resolve,1000));
    await observe(hidden.client,'new_hidden_after_1s');
  } finally { await hidden.close(); result.hiddenClosed=true; }
} catch (error) {
  result.failed=true;
  result.error=String(error.code || 'DIAGNOSTIC_FAILED');
} finally {
  await fs.writeFile(output,JSON.stringify(result,null,2)+'\n',{flag:'wx'});
  console.log(JSON.stringify(result));
}
