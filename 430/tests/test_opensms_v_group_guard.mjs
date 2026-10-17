import test from 'node:test';
import assert from 'node:assert/strict';
import { enforceVGroupBoundary, vGroup } from '../scripts/opensms_v_group_guard.mjs';

test('V-group exact parsing and internal routing', () => {
  assert.equal(vGroup('b11v-精-Te-120'), 'B11V');
  for (const name of ['B1', 'B11', 'B12', 'B1VX', 'AB1V']) assert.equal(vGroup(name), null);
  for (const source of ['B1V', 'B11V', 'B12V']) for (const target of ['B1V', 'B11V', 'B12V']) {
    assert.doesNotThrow(() => enforceVGroupBoundary({rows:[{'运营商':source+'-名称'}]}, target+'-名称'));
  }
});
test('V sources cannot escape through ordinary routes or reopened rest switch', () => {
  for (const target of ['A1', 'A11', 'B1', 'B11', 'B12', 'E-T休息区', 'B14-T快下保留卡', '快过期隔离区T', '换号区T']) {
    assert.throws(() => enforceVGroupBoundary({rows:[{'运营商':'B12V-精'}]}, target), /V组边界保护/);
  }
  assert.throws(() => enforceVGroupBoundary({sourceCarrierNames:['B1V-名称']}, 'B12'), /V组边界保护/);
});
test('only named authorized exception route/target combinations pass', () => {
  for (const [route, task, target] of [
    ['task_b_expired_to_offline_t','B','过期下架卡T'],
    ['task_b_tomorrow_fast_to_isolation_c','B','快过期隔离区C'],
    ['task_c_kooda_4day_reply_rate_lt5_to_swap','C','换号区T'],
    ['task_c_chatr_unchanged_4day_reply_rate_lt4_to_swap','C','换号区C'],
  ]) {
    const candidate = {route, task, forceGate:true, rows:[{'运营商':'B11V'}]};
    assert.doesNotThrow(() => enforceVGroupBoundary(candidate, target));
    assert.throws(() => enforceVGroupBoundary({...candidate,forceGate:false}, target), /V组边界保护/);
    assert.throws(() => enforceVGroupBoundary(candidate, 'A11'), /V组边界保护/);
  }
});
test('live V source blocks stale non-V candidate', () => {
  assert.throws(() => enforceVGroupBoundary({rows:[{'运营商':'B1'}]}, 'B12', ['B1V-实时']), /V组边界保护/);
  assert.doesNotThrow(() => enforceVGroupBoundary({rows:[{'运营商':'B1'}]}, 'B12', ['B1-实时']));
});
