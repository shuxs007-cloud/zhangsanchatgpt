import assert from 'node:assert/strict';
import test from 'node:test';
import {carrierBalances, checkpointFor, checkpointQueueRemainingMs, monitoredGroups, readFailureDetails, reportText, waitForSharedSession, weightedBalance} from './opensms_weighted_balance_monitor.mjs';

const date = text => new Date(text);

test('北京时间检查点只允许 01:00 到 11:00，迟到五分钟内开始', () => {
  assert.equal(checkpointFor(date('2026-10-02T17:00:00Z'))?.time, '01:00');
  assert.equal(checkpointFor(date('2026-10-02T17:05:00Z'))?.time, '01:00');
  assert.equal(checkpointFor(date('2026-10-02T17:05:01Z')), null);
  assert.equal(checkpointFor(date('2026-10-03T02:30:00Z'))?.time, '10:30');
  assert.equal(checkpointFor(date('2026-10-03T03:00:00Z'))?.time, '11:00');
  assert.equal(checkpointFor(date('2026-10-03T03:30:00Z')), null);
  assert.equal(checkpointFor(date('2026-10-02T15:30:00Z')), null);
});

test('访问锁排队只使用本检查点剩余窗口，过期不再读取', async () => {
  const checkpoint = checkpointFor(date('2026-10-02T17:00:00Z'));
  assert.equal(checkpointQueueRemainingMs(checkpoint, date('2026-10-02T17:02:00Z')), 181000);
  assert.equal(checkpointQueueRemainingMs(checkpoint, date('2026-10-02T17:05:01Z')), 0);
  let timeoutMs;
  const lease = {release() {}};
  assert.equal(await waitForSharedSession(checkpoint, {
    acquire: async options => {timeoutMs = options.timeoutMs; return lease;},
    now: () => date('2026-10-02T17:02:00Z'),
  }), lease);
  assert.equal(timeoutMs, 181000);
  let released = false;
  let call = 0;
  await assert.rejects(waitForSharedSession(checkpoint, {
    acquire: async () => ({release() {released = true;}}),
    now: () => date(call++ ? '2026-10-02T17:05:01Z' : '2026-10-02T17:04:59Z'),
  }), /OPENSMS_CHECKPOINT_QUEUE_EXPIRED/);
  assert.equal(released, true);
  await assert.rejects(waitForSharedSession(checkpoint, {
    acquire: async () => {throw new Error('should not acquire');},
    now: () => date('2026-10-02T17:05:01Z'),
  }), /OPENSMS_CHECKPOINT_QUEUE_EXPIRED/);
});

test('四组按总量加权，不把近似组混入', () => {
  const rows = [
    {name: 'A3-运营商', dailyRemaining: 1, dailyTotal: 10},
    {name: 'B1 精选', dailyRemaining: 1, dailyTotal: 100},
    {name: 'b11-运营商', dailyRemaining: 1, dailyTotal: 100},
    {name: 'B3', dailyRemaining: 1, dailyTotal: 100},
    {name: 'B1V', dailyRemaining: 0, dailyTotal: 100},
    {name: 'B31', dailyRemaining: 0, dailyTotal: 100},
  ];
  const result = weightedBalance(rows);
  assert.equal(result.dailyRemaining, 4);
  assert.equal(result.dailyTotal, 310);
  assert.equal(result.alert, true);
  assert.equal(weightedBalance(rows.map(row => ({...row, dailyRemaining: row.name === 'B1 精选' ? 28 : row.dailyRemaining}))).alert, false);
});

test('重复、缺失和无效数据均不可触发', () => {
  const rows = ['A3', 'B1', 'B11', 'B3'].map(name => ({name, dailyRemaining: 1, dailyTotal: 10}));
  assert.throws(() => weightedBalance([...rows, {...rows[0]}]), /精确匹配数量/);
  assert.throws(() => weightedBalance(rows.slice(1)), /精确匹配数量/);
  assert.throws(() => weightedBalance(rows.map((row, index) => index ? row : {...row, dailyTotal: 0})), /余量数据无效/);
  assert.throws(() => weightedBalance(rows.map((row, index) => index ? row : {...row, dailyTotal: ' '})), /余量数据无效/);
});

test('单组 0/0 不可计算，但四组总量有效时可纳入加权合计', () => {
  const result = weightedBalance([
    {name: 'A3', dailyRemaining: 0, dailyTotal: 0},
    {name: 'B1', dailyRemaining: 5, dailyTotal: 100},
    {name: 'B11', dailyRemaining: 0, dailyTotal: 0},
    {name: 'B3', dailyRemaining: 0, dailyTotal: 0},
  ]);
  assert.equal(result.ratio, 0.05);
  assert.equal(result.alert, true);
  assert.throws(() => weightedBalance(result.records.map(row => ({...row, dailyTotal: 0, dailyRemaining: 0}))), /四组合计数据无效/);
});

test('按指定顺序统计十四组，V组和相似前缀不串组', () => {
  assert.equal(monitoredGroups.length, 14);
  const rows = monitoredGroups.map((group, index) => ({
    name: `${group.toLowerCase()}-运营商`, dailyRemaining: index, dailyTotal: 100,
  }));
  rows.push({name: 'B1VX-近似', dailyRemaining: 0, dailyTotal: 100});
  rows.push({name: 'A211-近似', dailyRemaining: 0, dailyTotal: 100});
  const balances = carrierBalances(rows);
  assert.deepEqual(balances.map(row => row.group), monitoredGroups);
  assert.equal(balances.find(row => row.group === 'B1')?.dailyRemaining, 3);
  assert.equal(balances.find(row => row.group === 'B1V')?.dailyRemaining, 6);
  assert.equal(balances.find(row => row.group === 'A2')?.dailyRemaining, 9);
  assert.equal(balances.find(row => row.group === 'A21')?.dailyRemaining, 10);
  assert.ok(balances.every(row => row.status === 'ok'));
});

test('非组合组异常单独标不可用，不污染四组加权结果', () => {
  const rows = monitoredGroups.map(group => ({name: group, dailyRemaining: 1, dailyTotal: 10}));
  rows.push({name: 'B12-重复', dailyRemaining: 1, dailyTotal: 10});
  const balances = carrierBalances(rows);
  assert.equal(balances.find(row => row.group === 'B12')?.status, 'unavailable');
  assert.equal(weightedBalance(rows).dailyRemaining, 4);
});

test('报告包含十四组原顺序，0/0 单组不伪造比例', () => {
  const rows = monitoredGroups.map(group => ({name: group, dailyRemaining: 1, dailyTotal: 10}));
  rows.find(row => row.name === 'A3').dailyRemaining = 0;
  rows.find(row => row.name === 'A3').dailyTotal = 0;
  const balances = carrierBalances(rows);
  const report = reportText({checkpoint: {label: '2026-10-03 07:00'},
    status: 'ok', silentBrowserMode: true, browserForegroundActivationAttempted: false,
    balances, balance: weightedBalance(rows), receipt: '/tmp/receipt.json'});
  const dataLines = report.split('\n').filter(line => /^\| (A1|A11|A12|B1|B11|B12|B1V|B11V|B12V|A2|A21|A3|B3|C2) \|/.test(line));
  assert.deepEqual(dataLines.map(line => line.split('|')[1].trim()), monitoredGroups);
  assert.match(dataLines.find(line => line.startsWith('| A3 |')), /\| 0 \| 0 \| 不可计算 \| 0\/0 \|/);
  assert.match(report, /四组加权合计/);
  assert.match(report, /\[本轮回执\]\(\/tmp\/receipt.json\)/);
});

test('访问锁超时保留可诊断原因但不泄露锁路径', () => {
  const result = readFailureDetails(new Error('Timed out waiting for site lock: /private/secret.lock'), 'shared_session');
  assert.deepEqual(result, {category: 'site_lock_timeout', phase: 'shared_session', reason: '共用浏览器访问锁排队超出检查点五分钟窗口'});
  assert.deepEqual(readFailureDetails(new Error('OPENSMS_CHECKPOINT_QUEUE_EXPIRED'), 'shared_session'), result);
});

test('只读接口失败保留页码、类别和重试次数，不写入目标或响应内容', () => {
  const error = new Error('sensitive response');
  error.readDiagnostic = {category: 'http_502', pageNum: 2, targetId: 'secret'};
  error.readAttempts = [{}, {}];
  const result = readFailureDetails(error, 'carrier_config');
  assert.deepEqual(result, {category: 'http_502', phase: 'carrier_config', attempts: 2,
    pageNum: 2, reason: '运营商配置第2页只读接口失败（http_502，尝试2次）'});
  assert.doesNotMatch(JSON.stringify(result), /secret|sensitive/);
});
