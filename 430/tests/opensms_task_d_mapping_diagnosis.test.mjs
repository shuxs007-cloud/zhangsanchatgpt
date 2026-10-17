import test from 'node:test';
import assert from 'node:assert/strict';
import {
  diagnoseTaskDMapping,
  parseDiagnosticArgs,
  queryRawPhoneNumbers,
  validateExact430Endpoint,
} from '../scripts/opensms_diagnose_task_d_mapping.mjs';

test('mapping diagnosis compares raw country-code forms and samples only three missing cards', async () => {
  const cards = Array.from({ length: 5 }, (_, i) => String(2000000000 + i));
  const calls = [];
  const report = await diagnoseTaskDMapping(async (method, endpoint, data, query) => {
    calls.push({ method, endpoint, data, query });
    if (endpoint === '/phone/cardRetain/list') {
      return { code: 200, total: cards.length, rows: cards.map(cardNo => ({ cardNo, rawField: 'preserved' })) };
    }
    if (endpoint === '/carrier/config/listNotDeleted') return { code: 200, total: 0, rows: [] };
    assert.equal(method, 'POST');
    assert.equal(endpoint, '/phone/config/list');
    assert.deepEqual(query, { pageNum: data.pageNum, pageSize: 500 });
    let rows = [];
    if (data.cardNo.includes('\n')) rows = [{ id: 'a', cardNo: `1${cards[0]}` }];
    if (data.cardNo === cards[1]) rows = [{ id: 'b', cardNo: cards[1] }];
    if (data.cardNo === `1${cards[2]}`) rows = [{ id: 'c', cardNo: `1${cards[2]}` }];
    return { code: 200, total: rows.length, rows };
  });
  assert.deepEqual(report.perCard.map(row => row.reason), [
    'matched_backend',
    'found_with_single_10_digit',
    'found_only_with_country_code',
    'missing_backend_both_forms',
    'missing_backend_unprobed',
  ]);
  assert.equal(report.summary.singleQueryCardCount, 3);
  assert.equal(report.summary.totalPhoneQueryCount, 7);
  assert.equal(report.rawRetentionPages[0].response.rows[0].rawField, 'preserved');
  assert.equal(calls.filter(call => call.endpoint === '/phone/config/list').length, 7);
  assert.ok(calls.some(call => call.data?.cardNo === `1${cards[2]}`));
});

test('all-missing diagnosis probes every missing card without business write APIs', async () => {
  const cards = Array.from({ length: 4 }, (_, i) => String(2000000010 + i));
  const allowed = new Set(['GET /phone/cardRetain/list', 'GET /carrier/config/listNotDeleted', 'POST /phone/config/list']);
  const report = await diagnoseTaskDMapping(async (method, endpoint) => {
    assert.ok(allowed.has(`${method} ${endpoint}`));
    return endpoint === '/phone/cardRetain/list'
      ? { code: 200, total: cards.length, rows: cards.map(cardNo => ({ cardNo })) }
      : { code: 200, total: 0, rows: [] };
  }, { allMissing: true });
  assert.equal(report.summary.singleQueryCardCount, 4);
  assert.equal(report.summary.totalPhoneQueryCount, 9);
  assert.equal(report.summary.reasonCounts.missing_backend_both_forms, 4);
});

test('raw phone query retains leading country code and detects initial truncation', async () => {
  const queries = [];
  const number = '12000000000';
  const result = await queryRawPhoneNumbers(async (method, endpoint, data, query) => {
    assert.equal(data.cardNo, number);
    assert.equal(query.pageNum, data.pageNum);
    const count = data.pageNum === 1 ? 500 : 1;
    return {
      code: 200, total: 501,
      rows: Array.from({ length: count }, (_, i) => ({ id: String((data.pageNum - 1) * 500 + i), cardNo: number })),
    };
  }, [number], 'test', queries);
  assert.equal(result.firstPageTruncated, true);
  assert.equal(result.pages.length, 2);
  assert.equal(result.rows.length, 501);
  assert.equal(result.complete, true);
  assert.equal(result.pages[1].request.data.cardNo, number);
});

test('diagnosis preserves unexpected rows and pagination anomalies', async () => {
  const result = await queryRawPhoneNumbers(async () => ({
    code: 200, total: 2, rows: [{ id: 'a', cardNo: '2999999999' }],
  }), ['2000000000'], 'test', []);
  assert.equal(result.complete, false);
  assert.equal(result.firstPageTruncated, true);
  assert.equal(result.unexpectedRows.length, 1);
  assert.equal(result.anomalies[0].reason, 'page_row_count_mismatch');
});

test('430 endpoint validation rejects a neighboring profile and malformed URLs', () => {
  assert.equal(validateExact430Endpoint('12345\nopaque'), 'http://127.0.0.1:12345');
  assert.equal(validateExact430Endpoint('12345', 'http://localhost:12345'), 'http://127.0.0.1:12345');
  assert.throws(() => validateExact430Endpoint('12345', 'http://127.0.0.1:12346'), /不一致/);
  assert.throws(() => validateExact430Endpoint('12345', 'http://example.com:12345'), /不一致/);
  assert.throws(() => validateExact430Endpoint('not-a-port'), /无效/);
  assert.equal(parseDiagnosticArgs(['/tmp/new.json', '--all-missing']).allMissing, true);
  assert.throws(() => parseDiagnosticArgs(['/tmp/new.json', '--write']), /usage/);
});
