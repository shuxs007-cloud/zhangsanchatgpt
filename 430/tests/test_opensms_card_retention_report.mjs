import test from 'node:test';
import assert from 'node:assert/strict';
import { setImmediate } from 'node:timers/promises';
import { collectCardRetentionSnapshot, main } from '../scripts/opensms_collect_card_retention_report.mjs';

const makeRows = count => Array.from({length: count}, (_, i) => ({id: `record-${i}`, cardNo: String(2000000000+i),
  expireBefore: '2026-09-10', expireAfter: '2026-09-23', createTime: '2026-09-16 10:00:00', chatId: `chat-${i}`}));
function fixture(rows, mutate = response => response) {
  let pass = 0, active = 0, maxActive = 0;
  const calls = [];
  return { calls, get active() {return active;}, get maxActive() {return maxActive;},
    async api(method, endpoint, body, query) {
      assert.equal(method, 'GET'); assert.equal(endpoint, '/phone/cardRetain/list'); assert.equal(body, null);
      assert.deepEqual(Object.keys(query).sort(), ['pageNum', 'pageSize']);
      assert.equal(query.pageSize, 200);
      if (query.pageNum === 1) pass++;
      const currentPass = pass;
      calls.push([pass, query.pageNum]); active++; maxActive = Math.max(maxActive, active);
      try {
        await setImmediate();
        return mutate({code:200, total:rows.length, rows:structuredClone(rows.slice((query.pageNum-1)*200, query.pageNum*200))}, currentPass, query.pageNum);
      } finally {active--;}
    }};
}

test('A retention reads all pages twice, preserves duplicates, raw metadata and page order', async () => {
  const rows = makeRows(401); rows[400].cardNo = `1${rows[0].cardNo}`; rows[0].memo = '=not a formula';
  delete rows[2].id;
  const source = fixture(rows);
  const result = await collectCardRetentionSnapshot(source.api);
  assert.equal(result.retention.totalRecords, 401); assert.equal(result.retention.uniqueCardCount, 400);
  assert.equal(result.passes.length, 2); assert.equal(source.calls.length, 6);
  assert.deepEqual(result.passes[0].pages.flatMap(p=>p.response.rows), rows);
  assert.equal(result.independentSourceReadbackPassed, true); assert.equal(source.active, 0);
});
test('A retention handles an empty source without creating phantom rows', async () => {
  const result = await collectCardRetentionSnapshot(fixture([]).api);
  assert.equal(result.retention.totalRecords, 0); assert.equal(result.retention.pageCount, 1);
});
test('A retention rejects cross-pass drift and same-count changed rows', async () => {
  for (const mutate of [
    (r, pass) => pass === 2 ? {...r, total:r.total+1} : r,
    (r, pass) => {if(pass % 2 === 0) r.rows[0].expireAfter='2026-10-01'; return r;},
  ]) await assert.rejects(collectCardRetentionSnapshot(fixture(makeRows(1), mutate).api, {wait: async()=>{}}));
});
test('total drift restarts from page one and requires two fresh matching passes', async () => {
  const source = fixture(makeRows(401), (r, pass, page) => pass === 1 && page === 2 ? {...r,total:402} : r);
  const waits = [];
  const result = await collectCardRetentionSnapshot(source.api, {wait:async ms=>{assert.equal(source.active,0);waits.push(ms);}});
  assert.deepEqual(waits,[2000]);
  assert.equal(result.snapshotAttempts.length,2);
  assert.equal(result.passes.length,2);
  assert.equal(result.retention.totalRecords,401);
  assert.equal(source.calls.filter(([,page])=>page===1).length,3);
});
test('same-count transient content drift recovers but authentication never retries', async () => {
  const source=fixture(makeRows(1),(r,pass)=>{if(pass===2) r.rows[0].expireAfter='2026-10-01';return r;});
  const result=await collectCardRetentionSnapshot(source.api,{wait:async()=>{}});
  assert.equal(result.snapshotAttempts.length,2);
  let calls=0;
  await assert.rejects(collectCardRetentionSnapshot(async()=>{calls++;throw new Error('HTTP 401');},{wait:async()=>assert.fail('must not retry')}),/401/);
  assert.equal(calls,1);
});
test('persistent total drift stops after three attempts with evidence', async () => {
  const source=fixture(makeRows(401),(r,pass,page)=>page===2?{...r,total:402}:r);
  const waits=[];
  await assert.rejects(collectCardRetentionSnapshot(source.api,{wait:async ms=>waits.push(ms)}),error=>{
    assert.equal(error.snapshotAttempts.length,3); return /total变化/.test(error.message);
  });
  assert.deepEqual(waits,[2000,4000]);
  assert.equal(source.calls.filter(([,page])=>page===1).length,3);
  assert.equal(source.active,0);
});
test('A retention rejects missing business code, missing rows, duplicate IDs and invalid phones', async () => {
  for (const mutate of [
    r => ({rows:r.rows,total:r.total}), r=>({...r,rows:[]}),
    r=>{r.rows[1].id=r.rows[0].id;return r;}, r=>{r.rows[0].cardNo='';return r;},
  ]) await assert.rejects(collectCardRetentionSnapshot(fixture(makeRows(2),mutate).api));
});
test('A retention serializes readers and drains pending reads before error', async () => {
  const source=fixture(makeRows(2201)); await collectCardRetentionSnapshot(source.api);
  assert.equal(source.maxActive,1); assert.equal(source.active,0);
  const bad=fixture(makeRows(2201),(r,pass,page)=>{if(page===3) throw new Error('fixture failure');return r;});
  await assert.rejects(collectCardRetentionSnapshot(bad.api)); assert.equal(bad.active,0);
});
test('A retention denies missing authorization before touching files or browser', async () => {
  await assert.rejects(main([]),/授权/);
  await assert.rejects(main(['20260916T230000+0800','/not-created.json','timer']),/授权/);
});
