import { test } from 'node:test';
import assert from 'node:assert/strict';
import { listPhonesBatched } from '../scripts/opensms_collect_final_carrier_state.mjs';

test('final reads are bounded, read-only, <=500 and retain complete ordered coverage', async () => {
  const phones = Array.from({length: 2301}, (_, i) => String(17000000000 + i));
  let active = 0, maxActive = 0, requests = 0;
  const rows = await listPhonesBatched(async (method, endpoint, body) => {
    assert.equal(method, 'POST');
    assert.equal(endpoint, '/phone/config/list');
    const batch = body.cardNo.split('\n');
    assert.ok(batch.length <= 500);
    active++; maxActive = Math.max(maxActive, active); requests++;
    await new Promise(resolve => setTimeout(resolve, batch.length === 500 ? 10 : 1));
    active--;
    return {code: 200, total: batch.length, rows: batch.map(cardNo => ({cardNo}))};
  }, phones);
  assert.equal(requests, 5);
  assert.equal(maxActive, 4);
  assert.deepEqual(rows.map(row => row.cardNo), phones.map(phone => phone.slice(-10)));
  assert.equal(active, 0);
});

test('no candidate does not query all numbers', async () => {
  assert.deepEqual(await listPhonesBatched(() => {throw new Error('unexpected request');}, []), []);
});

test('a failed final read stops dispatch and drains active requests', async () => {
  const phones = Array.from({length: 4000}, (_, i) => String(17000000000 + i));
  let active = 0, requests = 0;
  await assert.rejects(listPhonesBatched(async () => {
    const index = requests++;
    active++;
    try {
      await new Promise(resolve => setTimeout(resolve, index === 0 ? 1 : 10));
      if (index === 0) throw new Error('read failed');
      return {code: 200, total: 0, rows: []};
    } finally { active--; }
  }, phones), /read failed/);
  assert.equal(active, 0);
  assert.equal(requests, 4);
});
