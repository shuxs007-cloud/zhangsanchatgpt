import test from 'node:test';
import assert from 'node:assert/strict';
import { validateSource, buildPlan, verifyGroup } from '../scripts/opensms_manual_retired_b14_swap.mjs';

const c = ['2043969351', '9423882118', '4378339823', '4162197657', '4167237368', '4168168608',
  '4378290065', '4375520311', '4376769439', '6043634795'];
const t = ['7052746715', '8733782833'];
function fixture() {
  const rows = [...c, ...t].map((cardNo, index) => ({ id: 100 + index, cardNo, slotIdentif: null,
    iccid: null, opr: c.includes(cardNo) ? '302720 chatr chatr' : '0 Koodo',
    carrireIdentif: c.includes(cardNo) ? '2093378431675928577' : '2088852494502465536' }));
  const source = { runId: '20261007T200519+0800', task: 'D', errorCount: 12,
    errors: rows.map(row => ({ cardNo: row.cardNo, reason: 'unresolved_carrier', carrireIdentif: row.carrireIdentif })),
    filteredQueryProof: { queries: [{ pages: [{ rows }] }] } };
  const carriers = [{ identif: 'swap-c', name: '换号区C' }, { identif: 'swap-t', name: '换号区T' }];
  return { source, baseline: validateSource(source), carriers, queried: { total: 12, rows: structuredClone(rows) } };
}
test('exact manual 10 C and 2 T; independent backend identity readback', () => {
  const f = fixture(), plan = buildPlan(f.baseline, f.carriers, f.queried);
  assert.deepEqual(plan.map(group => [group.target.name, group.rows.length]), [['换号区C', 10], ['换号区T', 2]]);
  for (const group of plan) {
    const rows = group.rows.map(row => ({ ...row, carrireIdentif: group.target.identif }));
    assert.doesNotThrow(() => verifyGroup(group, { total: rows.length, rows }));
    rows[0].id = 'different';
    assert.throws(() => verifyGroup(group, { total: rows.length, rows }));
  }
});
test('changed source, unknown target, missing and duplicate phones fail before write', () => {
  for (const change of [f => { f.queried.rows[0].id = 999; },
    f => { f.queried.rows[0].carrireIdentif = 'another'; },
    f => { f.queried.rows[0].opr = 'Koodo'; }, f => { f.queried.rows.pop(); },
    f => { f.queried.rows[1] = f.queried.rows[0]; }, f => { f.carriers.pop(); },
    f => { f.carriers.push({ identif: '2093378431675928577', name: 'B14-C快下保留卡' }); },
    f => { f.queried.rows[0].carrierIdentif = 'conflict'; }]) {
    const f = fixture(); change(f);
    assert.throws(() => buildPlan(f.baseline, f.carriers, f.queried));
  }
});
test('wrong historical scope fails and already target is never submitted again', () => {
  const f = fixture(); f.source.errors[0].cardNo = '0000000000';
  assert.throws(() => validateSource(f.source));
  const g = fixture(); g.queried.rows[0].carrireIdentif = 'swap-c';
  assert.equal(buildPlan(g.baseline, g.carriers, g.queried)[0].rows[0].alreadyTarget, true);
});
