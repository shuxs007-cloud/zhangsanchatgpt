import test from 'node:test';
import assert from 'node:assert/strict';
import { setImmediate as nextTurn } from 'node:timers/promises';
import { collectCardRetention } from '../scripts/opensms_card_retention_readonly_lib.mjs';

function rows(count) {
  return Array.from({ length: count }, (_, index) => ({
    id: `retain-${index}`,
    cardNo: String(2000000000 + index),
    expireBefore: '2026/8/31',
    expireAfter: '2026年9月7日',
    chatId: `chat-${index}`,
    createTime: '2026-09-01 01:02:03',
  }));
}

function fixture(records, mutate = response => response) {
  const called = [];
  return {
    called,
    async api(method, endpoint, data, { pageNum, pageSize }) {
      assert.equal(method, 'GET');
      assert.equal(endpoint, '/phone/cardRetain/list');
      assert.equal(data, null);
      assert.equal(pageSize, 200);
      called.push(pageNum);
      return mutate({
        code: 200,
        total: records.length,
        rows: records.slice((pageNum - 1) * pageSize, pageNum * pageSize),
      }, pageNum);
    },
  };
}

test('retention keeps ordered records, repeated phones and legacy records without ids', async () => {
  const records = rows(201);
  records[200].cardNo = `1${records[0].cardNo}`;
  delete records[0].id;
  delete records[200].id;
  const result = await collectCardRetention(fixture(records).api);
  assert.equal(result.totalRecords, 201);
  assert.equal(result.fetchedRecords, 201);
  assert.equal(result.uniqueCardCount, 200);
  assert.equal(result.duplicateCardRecordCount, 1);
  assert.equal(result.invalidCardRecordCount, 0);
  assert.equal(result.invalidExpireAfterRecordCount, 0);
  assert.deepEqual(result.pages, [{ pageNum: 1, rowCount: 200 }, { pageNum: 2, rowCount: 1 }]);
  assert.deepEqual(result.records[200], {
    cardNo: records[0].cardNo,
    expireBefore: '2026-08-31',
    expireAfter: '2026-09-07',
    chatId: 'chat-200',
    createTime: '2026-09-01 01:02:03',
  });
});

test('retention accepts a complete empty page and rejects malformed first-page rows/total', async () => {
  const empty = fixture([]);
  const result = await collectCardRetention(empty.api);
  assert.deepEqual(empty.called, [1]);
  assert.equal(result.totalRecords, 0);
  assert.equal(result.pageCount, 1);
  assert.deepEqual(result.records, []);
  for (const response of [
    null,
    { code: 200, rows: [] },
    { code: 200, rows: null, total: 0 },
    ...[null, '', ' ', true, -1, 0.5, Number.MAX_SAFE_INTEGER + 1, 'invalid'].map(total => ({ code: 200, rows: [], total })),
  ]) {
    await assert.rejects(collectCardRetention(async () => response), /rows\/total|total无效/);
  }
});

test('retention validates each later page and rejects total drift or business errors', async () => {
  for (const mutate of [
    response => ({ ...response, total: response.total + 1 }),
    response => ({ ...response, total: undefined }),
    response => ({ ...response, total: 'invalid' }),
    response => ({ ...response, rows: null }),
    response => ({ ...response, code: 500 }),
  ]) {
    const source = fixture(rows(201), (response, pageNum) => pageNum === 2 ? mutate(response) : response);
    await assert.rejects(collectCardRetention(source.api), /total变化|rows\/total|total无效|失败/);
  }
});

test('retention rejects short or extra rows on the first and last pages, including zero total', async () => {
  for (const pageNum of [1, 2]) {
    for (const delta of [-1, 1]) {
      const source = fixture(rows(201), (response, page) => page !== pageNum ? response : {
        ...response,
        rows: delta < 0 ? response.rows.slice(1) : [...response.rows, rows(1)[0]],
      });
      await assert.rejects(collectCardRetention(source.api), /行数不符/);
    }
  }
  await assert.rejects(collectCardRetention(fixture([], response => ({ ...response, rows: rows(1) })).api), /行数不符/);
});

test('retention rejects repeated nonempty raw record ids across pages', async () => {
  const records = rows(201);
  records[200].id = records[0].id;
  await assert.rejects(collectCardRetention(fixture(records).api), /重复后台ID/);
});

test('retention rejects invalid normalized phones while preserving invalid-date accounting', async () => {
  for (const cardNo of [null, '', 'abc', '123456789']) {
    await assert.rejects(collectCardRetention(fixture([{ ...rows(1)[0], cardNo }]).api), /无效卡号/);
  }
  const result = await collectCardRetention(fixture([{ ...rows(1)[0], expireAfter: '2026-02-30' }]).api);
  assert.equal(result.records[0].expireAfter, '');
  assert.equal(result.invalidExpireAfterRecordCount, 1);
});

test('retention bounds reads to eight and drains in-flight pages before rejecting', async () => {
  const source = fixture(rows(2201));
  let active = 0;
  let maxActive = 0;
  const result = await collectCardRetention(async (...args) => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    try {
      await nextTurn();
      return await source.api(...args);
    } finally { active -= 1; }
  });
  assert.equal(result.fetchedRecords, 2201);
  assert.equal(maxActive, 8);
  assert.equal(active, 0);
  assert.deepEqual(result.pages.map(page => page.pageNum), Array.from({ length: 12 }, (_, index) => index + 1));

  const failedSource = fixture(rows(2201));
  const started = [];
  const finished = [];
  let release;
  const heldReads = new Promise(resolve => { release = resolve; });
  let settled = false;
  const collection = collectCardRetention(async (...args) => {
    const { pageNum } = args[3];
    started.push(pageNum);
    if (pageNum === 1) return failedSource.api(...args);
    if (pageNum === 2) {
      await nextTurn();
      throw new Error('page-two-read-failed');
    }
    await heldReads;
    finished.push(pageNum);
    return failedSource.api(...args);
  });
  const rejected = assert.rejects(collection, /page-two-read-failed/).then(() => { settled = true; });
  await nextTurn();
  await nextTurn();
  assert.equal(settled, false);
  assert.deepEqual(started, [1, 2, 3, 4, 5, 6, 7, 8, 9]);
  release();
  await rejected;
  assert.deepEqual(finished, [3, 4, 5, 6, 7, 8, 9]);
  assert.deepEqual(started, [1, 2, 3, 4, 5, 6, 7, 8, 9]);
});
