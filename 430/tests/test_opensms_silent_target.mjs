import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {RawCdpClient, selectPinnedOpenSmsTarget, selectExistingOpenSmsTarget} from '../opensms_protocol_client.mjs';

test('existing chat route is supported without navigation; exact origin required', () => {
  const chat = {type:'page', url:'https://adminsms.smsoug.com/message/chatMessage', webSocketDebuggerUrl:'ws://offline'};
  assert.equal(selectExistingOpenSmsTarget([chat], '/day'), chat);
  const day = {...chat,url:'https://adminsms.smsoug.com/day'};
  assert.equal(selectExistingOpenSmsTarget([chat,day], '/day'), day);
  for (const change of [{type:'other'},{type:'service_worker'},{webSocketDebuggerUrl:null},
    {url:'http://adminsms.smsoug.com/day'},{url:'https://adminsms.smsoug.com:444/day'},
    {url:'https://adminsms.smsoug.com.evil.invalid/day'}]) {
    assert.equal(selectExistingOpenSmsTarget([{...chat,...change}], '/day'), null);
  }
});

test('run-owned hidden target connects despite Chromium other type', () => {
  for (const type of ['page','other']) {
    const target={id:'owned',type,url:'https://adminsms.smsoug.com/day',webSocketDebuggerUrl:'ws://offline'};
    assert.equal(selectPinnedOpenSmsTarget([target],'owned'),target);
  }
});
test('pinned target cannot fall back or cross identity, origin or transport', () => {
  const good={id:'owned',type:'other',url:'https://adminsms.smsoug.com/day',webSocketDebuggerUrl:'ws://offline'};
  for (const change of [{id:'not-owned'},{type:'service_worker'},{url:'http://evil.invalid/day'},{url:'bad'},{webSocketDebuggerUrl:null}]) {
    assert.throws(()=>selectPinnedOpenSmsTarget([{...good,...change}],'owned'),/专属API页面/);
  }
  assert.throws(()=>selectPinnedOpenSmsTarget([good],'missing'),/专属API页面/);
});

test('raw transport rejects foreground and window mutation commands', () => {
  const client = new RawCdpClient('offline');
  for (const method of ['Page.bringToFront', 'Target.activateTarget', 'Browser.setWindowBounds']) {
    assert.throws(() => client.send(method), /静默模式禁止/);
  }
});
test('evaluation never claims a user gesture', async () => {
  const client = new RawCdpClient('offline');
  client.send = async (method, params) => {
    assert.equal(method, 'Runtime.evaluate');
    assert.equal(params.userGesture, false);
    return {result:{value:42}};
  };
  assert.equal(await client.evaluate('42'),42);
});
test('creation requires hidden support without visible fallback; keeper retains session', () => {
  const protocol = fs.readFileSync(new URL('../opensms_protocol_client.mjs',import.meta.url),'utf8');
  const body = protocol.split('export async function createRawBackgroundPage')[1].split('export function encryptPayload')[0];
  assert.match(body,/hidden: true/);
  assert.match(body,/forTab: false/);
  assert.doesNotMatch(body,/newWindow: false,/);
  assert.match(body,/p.name === 'hidden'/);
  assert.equal((body.match(/send\('Target.createTarget'/g)||[]).length,1);
  const keeper = fs.readFileSync(new URL('../scripts/opensms_api_target_guard_cli.mjs',import.meta.url),'utf8');
  assert.match(keeper,/mode === 'hold'/);
  assert.match(keeper,/process.kill\(owner, 0\)/);
  assert.doesNotMatch(keeper,/page.browserClient.close\(\)/);
  assert.doesNotMatch(protocol,/send\('(?:Runtime|Page).enable'/);
  assert.match(body,/ensureBackgroundPageNavigation\(client, requestedUrl/);
  assert.match(body,/withBackgroundStartupRecovery/);
  assert.match(keeper,/120000/);
});
