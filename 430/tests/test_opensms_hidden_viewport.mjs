import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {configureOwnedHiddenViewport} from '../opensms_hidden_viewport.mjs';

test('new browser configures and verifies only the owned hidden renderer', async () => {
  const calls=[];
  const receipt=await configureOwnedHiddenViewport({
    send:async (...args)=>calls.push(args),
    evaluate:async (...args)=>{calls.push(args); return {width:1440,height:900};},
  }, 'cloakbrowser');
  assert.deepEqual(calls[0], ['Emulation.setDeviceMetricsOverride',
    {width:1440,height:900,deviceScaleFactor:1,mobile:false},15000]);
  assert.equal(calls.length,2);
  assert.equal(receipt.scope,'owned-hidden-target-only');
  assert.doesNotMatch(JSON.stringify(calls),/screenWidth|screenHeight|activateTarget|setWindowBounds|bringToFront/);
});

test('legacy browser and unspecified kind are not reconfigured', async () => {
  for (const kind of ['bitbrowser','',null]) {
    assert.equal(await configureOwnedHiddenViewport({
      send:()=>{throw new Error('unexpected CDP');},evaluate:()=>{throw new Error('unexpected CDP');},
    },kind),null);
  }
});

test('zero, missing, nonfinite and unexpected viewports fail closed', async () => {
  for (const size of [null,{}, {width:0,height:0}, {width:1440,height:0},
    {width:'1440',height:900}, {width:NaN,height:900}, {width:800,height:600}]) {
    await assert.rejects(configureOwnedHiddenViewport({send:async()=>{},evaluate:async()=>size},'cloakbrowser'),
      {code:'OPENSMS_HIDDEN_VIEWPORT_UNCONFIRMED'});
  }
});

test('unsupported emulation is not ignored or converted into a visible tab', async () => {
  let evaluated=false;
  await assert.rejects(configureOwnedHiddenViewport({
    send:async()=>{throw new Error('unsupported CDP');},evaluate:async()=>{evaluated=true;},
  },'cloakbrowser'),/unsupported CDP/);
  assert.equal(evaluated,false);
});

test('viewport configuration is after owned-target connection and before readiness', () => {
  const source=fs.readFileSync(new URL('../opensms_protocol_client.mjs',import.meta.url),'utf8');
  const start=source.indexOf('async function createRawBackgroundPageAttempt');
  const body=source.slice(start,source.indexOf('export function encryptPayload',start));
  assert.ok(body.indexOf('targetId = created.targetId') < body.indexOf('configureOwnedHiddenViewport(client)'));
  assert.ok(body.indexOf('await client.connect()') < body.indexOf('configureOwnedHiddenViewport(client)'));
  assert.ok(body.indexOf('configureOwnedHiddenViewport(client)') < body.indexOf('ensureBackgroundPageNavigation(client'));
  assert.match(body,/viewportReceipt,/);
  assert.match(body,/backgroundCleanup/);
});

test('every server-clock reader keeps the one-pixel tolerance and strict header limit', () => {
  for (const file of ['scripts/opensms_export_fast_raw.mjs','scripts/opensms_read_server_dates_fast_raw.mjs',
    'scripts/opensms_read_server_date_raw.mjs','scripts/opensms_read_server_date.mjs','opensms_cdp_export_once.mjs']) {
    const source=fs.readFileSync(new URL(`../${file}`,import.meta.url),'utf8');
    assert.match(source,/rect\.top < -1 \|\| rect\.top > 180/);
    assert.doesNotMatch(source,/rect\.top < 0 \|\| rect\.top > 180/);
  }
});
