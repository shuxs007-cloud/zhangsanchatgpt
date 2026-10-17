import test from 'node:test';
import assert from 'node:assert/strict';
import {withBackgroundStartupRecovery} from '../opensms_background_startup_recovery.mjs';

const timeout = () => Object.assign(new Error('context probe failed'), {
  code: 'BACKGROUND_PAGE_READ_FAILED',
  cause: {code: 'CDP_COMMAND_TIMEOUT', cdpMethod: 'Runtime.evaluate'},
  backgroundCleanup: {targetId: 'first-owned', closed: true, status: 'confirmed_closed'},
});
const quiet = {onAttempt: () => {}};
test('ready startup does no retry, and records exact owned target', async () => {
  let calls = 0;
  const result = await withBackgroundStartupRecovery(async () => {calls++; return {targetId: 'owned'};}, quiet);
  assert.equal(calls, 1);
  assert.equal(result.startupRecovery.status, 'ready');
  assert.equal(result.startupRecovery.attempts[0].targetId, 'owned');
});
test('cleaned-up startup timeout gets exactly one new hidden page', async () => {
  let calls = 0;
  const events = [];
  const result = await withBackgroundStartupRecovery(async () => {
    if (++calls === 1) throw timeout();
    return {targetId: 'second-owned'};
  }, {onAttempt: e => events.push(e)});
  assert.equal(calls, 2);
  assert.equal(result.startupRecovery.status, 'recovered');
  assert.deepEqual(events.map(e => e.retry), [true, false]);
});
test('persistent startup failure stops at two and keeps both attempts', async () => {
  let calls = 0;
  await assert.rejects(withBackgroundStartupRecovery(async () => {calls++; throw timeout();}, quiet), e => {
    assert.equal(e.startupRecovery.status, 'failed');
    assert.equal(e.startupRecovery.attempts.length, 2);
    assert.equal(e.startupRecovery.attempts[1].retry, false);
    return true;
  });
  assert.equal(calls, 2);
});
test('unknown ownership or unconfirmed cleanup cannot create a replacement', async () => {
  for (const cleanup of [null, {}, {closed:true}, {targetId:'owned',closed:false},
    {targetId:'owned',closed:true,status:'cleanup_unconfirmed'}]) {
    let calls = 0;
    await assert.rejects(withBackgroundStartupRecovery(async () => {
      calls++; throw Object.assign(timeout(), {backgroundCleanup:cleanup});
    }, quiet));
    assert.equal(calls, 1);
  }
});
test('authorization, origin, protocol, unsupported hidden, unknown creation and submission errors never retry', async () => {
  for (const code of ['UNEXPECTED_BACKGROUND_ORIGIN', 'OPENSMS_PROTOCOL_ERROR', 'AUTH_REQUIRED',
    'UNSUPPORTED_HIDDEN', 'BACKGROUND_NAVIGATION_FAILED', 'SUBMISSION_OUTCOME_UNCERTAIN', undefined]) {
    let calls = 0;
    await assert.rejects(withBackgroundStartupRecovery(async () => {
      calls++; throw Object.assign(timeout(), {code});
    }, quiet));
    assert.equal(calls, 1);
  }
});
test('startup diagnostics never serialize exception text, URLs or cookies', async () => {
  const events=[];
  await assert.rejects(withBackgroundStartupRecovery(async () => {
    throw Object.assign(timeout(), {message:'secret token', code:'AUTH_REQUIRED', url:'?cookie=secret'});
  }, {onAttempt: e => events.push(e)}));
  assert.ok(!JSON.stringify(events).includes('secret'));
});
