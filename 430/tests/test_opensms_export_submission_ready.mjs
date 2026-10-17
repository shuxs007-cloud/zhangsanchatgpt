import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { submissionReadyConfig, publishSubmissionReady } from '../scripts/opensms_export_submission_ready.mjs';
const token = 'a'.repeat(32), runId = '20261005T000302+0800';
const facts = {taskId: 'b'.repeat(32), serverDate: '2026-10-04', exportPageClosed: true};

test('standalone export has no scheduling signal or side effects', async () => {
  assert.equal(submissionReadyConfig({}), null);
  assert.equal(await publishSubmissionReady(null, {}), null);
});
test('partial or invalid config fails before export begins', () => {
  const valid = {OPENSMS_EXPORT_SUBMISSION_READY_PATH: '/private/ready.json',
    OPENSMS_EXPORT_SUBMISSION_READY_TOKEN: token, OPENSMS_RUN_ID: runId};
  assert.deepEqual(submissionReadyConfig(valid), {output: '/private/ready.json', token, runId});
  for (const changes of [{OPENSMS_EXPORT_SUBMISSION_READY_TOKEN: ''},
    {OPENSMS_EXPORT_SUBMISSION_READY_PATH: 'relative.json'},
    {OPENSMS_EXPORT_SUBMISSION_READY_PATH: ''}, {OPENSMS_RUN_ID: 'old-run'},
    {OPENSMS_EXPORT_SUBMISSION_READY_TOKEN: 'bad'}]) {
    assert.throws(() => submissionReadyConfig({...valid, ...changes}));
  }
});
test('atomic signal requires confirmed identity and closed page and cannot overwrite', async t => {
  const folder = await fs.mkdtemp(path.join(os.tmpdir(), 'export-ready-'));
  t.after(() => fs.rm(folder, {recursive: true, force: true}));
  const config = {output: path.join(folder, 'ready.json'), token, runId};
  for (const changes of [{exportPageClosed: false}, {exportPageClosed: 1},
    {taskId: 'old-task'}, {serverDate: '2026-02-30'}, {serverDate: '20261004'}]) {
    await assert.rejects(publishSubmissionReady(config, {...facts, ...changes}));
    await assert.rejects(fs.stat(config.output), {code: 'ENOENT'});
  }
  const receipt = await publishSubmissionReady(config, facts);
  assert.deepEqual(JSON.parse(await fs.readFile(config.output, 'utf8')), receipt);
  assert.equal(receipt.requestLaneReleased, true);
  assert.equal(receipt.runId, runId);
  assert.equal(receipt.token, token);
  assert.equal((await fs.stat(config.output)).mode & 0o777, 0o600);
  await assert.rejects(publishSubmissionReady(config, {...facts, taskId: 'c'.repeat(32)}), {code: 'EEXIST'});
  assert.deepEqual(await fs.readdir(folder), ['ready.json']);
  assert.equal(JSON.parse(await fs.readFile(config.output, 'utf8')).taskId, facts.taskId);
});
test('production signal follows paced submit, persisted checkpoint and owned-page close', async () => {
  const text = await fs.readFile(new URL('../scripts/opensms_export_fast_raw.mjs', import.meta.url), 'utf8');
  const start = text.indexOf('const submitted = await withOpenSmsRequestPacing');
  const checkpoint = text.indexOf('await fs.writeFile(exportProgressPath', start);
  const closed = text.indexOf('await backgroundPage.close()', checkpoint);
  const signal = text.indexOf('await publishSubmissionReady(', closed);
  const poll = text.indexOf("'/adminExport/export4day/async/status'", signal);
  assert.ok(start < checkpoint && checkpoint < closed && closed < signal && signal < poll);
  assert.ok(text.indexOf('const scheduling = submissionReadyConfig()') < text.indexOf('await ensureArtifactDirs()'));
  assert.match(text, /setTimeout\(resolve, 60000\)/);
  assert.match(text, /setTimeout\(resolve, 15000\)/);
});
