import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import test from 'node:test';
import {
  isAtRelatedRecord,
  isExecutedSuccessfully,
  matchesAction,
  parseBeijingTimestamp,
  selectFreshAtRecords,
} from '../scripts/opensms_device_command_record.mjs';

const submittedAt = '2026-10-15T13:00:00.000Z';

test('command record parser uses Beijing timestamps', () => {
  assert.equal(parseBeijingTimestamp('2026-10-15 21:00:00'), Date.parse(submittedAt));
  assert.ok(Number.isNaN(parseBeijingTimestamp('not-a-time')));
});

test('sendTo and SIM-writing rows are not accepted as AT action evidence', () => {
  assert.equal(isAtRelatedRecord({ commandType: 'sendTo', commandDetail: 'hello' }), false);
  assert.equal(isAtRelatedRecord({ commandType: 'WRITESIMAT', commandDetail: '12498690683' }), false);
  assert.equal(isAtRelatedRecord({ commandType: 'at', commandDetail: 'AT+QICSGP=1,1' }), true);
  assert.equal(isAtRelatedRecord({ commandType: 'other', commandDetail: 'AT+CFUN=1,1' }), true);
});

test('failed AT execution is rejected even when status says executed', () => {
  assert.equal(isExecutedSuccessfully({
    executionStatus: '已执行',
    executionResult: 'FAILED: max retry exceeded',
  }), false);
  assert.equal(isExecutedSuccessfully({
    executionStatus: '已执行',
    executionResult: '{"code":0,"reason":"OK"}',
  }), true);
});

test('action matching keeps MMS and restart proofs specific', () => {
  assert.equal(matchesAction({ commandDetail: 'AT+QICSGP=1,1' }, 'mms'), true);
  assert.equal(matchesAction({ commandDetail: 'at+cfun=1,1' }, 'restart'), true);
  assert.equal(matchesAction({ commandDetail: 'AT+CSQ' }, 'restart'), false);
  assert.equal(matchesAction({ commandDetail: 'AT+CSQ' }, 'maintenance'), true);
});

test('fresh proof must be after submission and belong to a selected device', () => {
  const records = [
    {
      machineName: '180', commandType: 'at', commandDetail: 'AT+QICSGP=1,1',
      executionStatus: '已执行', executionResult: 'OK', createdAt: '2026-10-15 21:00:01',
    },
    {
      machineName: '999', commandType: 'at', commandDetail: 'AT+QICSGP=1,1',
      executionStatus: '已执行', executionResult: 'OK', createdAt: '2026-10-15 21:00:02',
    },
    {
      machineName: '180', commandType: 'sendTo', commandDetail: 'hello',
      executionStatus: '已执行', executionResult: 'OK', createdAt: '2026-10-15 21:00:03',
    },
  ];
  const matched = selectFreshAtRecords(records, {
    action: 'mms', submittedAt, selectedDeviceNames: ['180'], clockToleranceMs: 0,
  });
  assert.equal(matched.length, 1);
  assert.equal(matched[0].machineName, '180');
});

test('stage 2 is one all-device restart with no batching delay', async () => {
  const source = await fs.readFile(new URL('../scripts/opensms_device_followup.mjs', import.meta.url), 'utf8');
  assert.match(source, /return \[\{ label: '全部设备', targetNames: \[\.\.\.deviceNames\] \}\]/);
  assert.match(source, /阶段 2：设备监控全选一次重启/);
  assert.doesNotMatch(source, /label: '设备201-210'/);
  const stage2Start = source.indexOf('async function executeStage2');
  const stage2End = source.indexOf('async function mainWithSharedBrowser', stage2Start);
  const stage2Source = source.slice(stage2Start, stage2End);
  assert.doesNotMatch(stage2Source, /120000/);
  assert.match(stage2Source, /action: 'restart'/);
  assert.match(stage2Source, /commandRecordVerification/);
});
