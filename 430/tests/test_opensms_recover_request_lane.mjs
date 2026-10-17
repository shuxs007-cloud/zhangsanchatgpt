import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { recoverRequestLane } from '../scripts/opensms_recover_request_lane.mjs';

const hash = bytes => createHash('sha256').update(bytes).digest('hex');
async function fixture(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'opensms-request-recovery-test-'));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const lane = path.join(dir, 'request.lock');
  await fs.mkdir(lane);
  const runId = '20260930T233055+0800', targetId = 'A1234';
  const owner = '100003-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
  await fs.writeFile(path.join(lane, 'owner'), owner);
  await fs.writeFile(path.join(dir, 'next.json'), '{"nextAllowedAt":12345}');
  const statePath = path.join(dir, 'state.json'), receiptPath = path.join(dir, 'receipt.json');
  const receipt = { ownedBy: 'opensms-patrol-api-target', runId, targetId, hidden: true,
    endpoint: 'http://127.0.0.1:49343', marker: `opensms-patrol-api:${runId}:nonce`,
    ownerPid: 100001, keeperPid: 100002 };
  const state = { runId, runnerPid: 100001, apiTarget: { runId, targetId,
    receipt: receiptPath, endpoint: receipt.endpoint } };
  await fs.writeFile(receiptPath, JSON.stringify(receipt));
  await fs.writeFile(statePath, JSON.stringify(state));
  const options = { authorizationSource: 'current-user', runId, statePath, receiptPath,
    stateSha256: hash(await fs.readFile(statePath)), receiptSha256: hash(await fs.readFile(receiptPath)),
    ownerSha256: hash(owner) };
  const effects = { acquired: 0, released: 0, closed: 0, listed: 0 };
  const dependencies = { requestLock: lane, evidenceBase: path.join(dir, 'evidence'),
    processAlive: () => false,
    acquire: async () => { effects.acquired++; return { token: 'test-lease', inherited: false,
      release() { effects.released++; } }; },
    closeOwnedPage: async () => { effects.closed++; return { success: true, closed: true, alreadyClosed: true, targetId }; },
    listTargets: async () => { effects.listed++; return []; },
  };
  return { dir, lane, options, dependencies, effects, receipt, state, targetId };
}
test('explicit cleanup archives the lane, preserves evidence and cooldown, never replays business', async t => {
  const f = await fixture(t);
  const result = await recoverRequestLane(f.options, f.dependencies);
  assert.equal(result.status, 'recovered');
  assert.equal(result.businessRequests, 0);
  assert.equal(result.businessReplay, false);
  assert.equal(result.originalStateUnchanged, true);
  assert.equal(result.originalReceiptUnchanged, true);
  assert.equal(await fs.readFile(path.join(f.dir, 'next.json'), 'utf8'), '{"nextAllowedAt":12345}');
  assert.equal(hash(await fs.readFile(path.join(result.archivedLock, 'owner'))), f.options.ownerSha256);
  await assert.rejects(fs.stat(f.lane), { code: 'ENOENT' });
  assert.deepEqual(f.effects, { acquired: 1, released: 1, closed: 1, listed: 1 });
});
test('history or timer cannot authorize cleanup', async t => {
  const f = await fixture(t);
  for (const authorizationSource of ['history', 'timer', undefined])
    await assert.rejects(recoverRequestLane({ ...f.options, authorizationSource }, f.dependencies), /current-user/);
  assert.equal(f.effects.acquired, 0);
  await fs.access(f.lane);
});
test('owner mismatch fails before any CDP call', async t => {
  const f = await fixture(t);
  await fs.writeFile(path.join(f.lane, 'owner'), '100004-aaaaaaaa');
  await assert.rejects(recoverRequestLane(f.options, f.dependencies), /owner changed/);
  assert.equal(f.effects.closed, 0);
});
test('changed original state or receipt fails before CDP', async t => {
  for (const which of ['statePath', 'receiptPath']) {
    const f = await fixture(t);
    await fs.appendFile(f.options[which], ' ');
    await assert.rejects(recoverRequestLane(f.options, f.dependencies), /changed/);
    assert.equal(f.effects.closed, 0);
  }
});
test('wrong original page binding cannot be cleaned', async t => {
  const f = await fixture(t);
  f.state.apiTarget.targetId = 'B1234';
  await fs.writeFile(f.options.statePath, JSON.stringify(f.state));
  f.options.stateSha256 = hash(await fs.readFile(f.options.statePath));
  await assert.rejects(recoverRequestLane(f.options, f.dependencies), /binding mismatch/);
  assert.equal(f.effects.closed, 0);
});
test('any live original process blocks cleanup', async t => {
  for (const livePid of [100001, 100002, 100003]) {
    const f = await fixture(t);
    f.dependencies.processAlive = pid => pid === livePid;
    await assert.rejects(recoverRequestLane(f.options, f.dependencies), /still alive/);
    assert.equal(f.effects.closed, 0);
  }
});
test('failed or ambiguous owned-page cleanup retains the lane', async t => {
  const f = await fixture(t);
  f.dependencies.closeOwnedPage = async () => { throw new Error('cleanup failed'); };
  await assert.rejects(recoverRequestLane(f.options, f.dependencies), /cleanup failed/);
  await fs.access(f.lane);
  assert.equal(f.effects.released, 1);
});
test('wrong cleanup target or surviving target retains the lane', async t => {
  for (const mode of ['wrong-target', 'surviving']) {
    const f = await fixture(t);
    if (mode === 'wrong-target') f.dependencies.closeOwnedPage = async () => ({ success: true, closed: true, targetId: 'OTHER' });
    else f.dependencies.listTargets = async () => [{ id: f.targetId }];
    await assert.rejects(recoverRequestLane(f.options, f.dependencies), /target identity|target still present/);
    await fs.access(f.lane);
  }
});
test('lock change during confirmation is not archived', async t => {
  const f = await fixture(t);
  f.dependencies.listTargets = async () => {
    await fs.writeFile(path.join(f.lane, 'owner'), '100004-other'); return [];
  };
  await assert.rejects(recoverRequestLane(f.options, f.dependencies), /evidence changed/);
  assert.equal(await fs.readFile(path.join(f.lane, 'owner'), 'utf8'), '100004-other');
});
test('unknown lane files and symlink evidence are rejected', async t => {
  for (const mode of ['extra-file', 'symlink']) {
    const f = await fixture(t);
    if (mode === 'extra-file') await fs.writeFile(path.join(f.lane, 'unowned'), 'x');
    else { await fs.rename(path.join(f.lane, 'owner'), path.join(f.dir, 'old-owner'));
      await fs.symlink(path.join(f.dir, 'old-owner'), path.join(f.lane, 'owner')); }
    await assert.rejects(recoverRequestLane(f.options, f.dependencies), /Unexpected/);
    assert.equal(f.effects.closed, 0);
  }
});
test('an inherited site lease cannot authorize maintenance', async t => {
  const f = await fixture(t);
  f.dependencies.acquire = async () => ({ inherited: true, release() {} });
  await assert.rejects(recoverRequestLane(f.options, f.dependencies), /own website lease/);
  assert.equal(f.effects.closed, 0);
});
