// Pure validation; invoked before connecting and again against live sources.
export function vGroup(name) {
  return String(name || '').trim().match(/^(B1V|B11V|B12V)(?:-|\s|$)/i)?.[1].toUpperCase() || null;
}

const EXITS = new Map([
  ['task_b_expired_to_offline_c', ['B', '过期下架卡C']],
  ['task_b_expired_to_offline_t', ['B', '过期下架卡T']],
  ['task_b_tomorrow_fast_to_isolation_c', ['B', '快过期隔离区C']],
  ['task_b_tomorrow_fast_to_isolation_t', ['B', '快过期隔离区T']],
  ['task_c_kooda_4day_reply_rate_lt5_to_swap', ['C', '换号区T']],
  ['task_c_chatr_unchanged_4day_reply_rate_lt4_to_swap', ['C', '换号区C']],
]);

export function enforceVGroupBoundary(candidate, target, sources) {
  const rows = candidate.rows || candidate.candidates || [];
  const names = sources ?? [...(candidate.sourceCarrierNames || []), ...rows.flatMap(row =>
    ['原运营商', '运营商', '精确原运营商', 'sourceCarrierName', 'sourceCarrier'].map(key => row[key]))];
  if (!names.some(vGroup) || vGroup(target)) return;
  const allowed = EXITS.get(candidate.route);
  if (allowed && candidate.task === allowed[0] && candidate.forceGate === true && target === allowed[1]) return;
  throw new Error('V组边界保护：仅允许B1V/B11V/B12V内部流转，或经授权且条件核验通过的任务B/C退出；禁止转入休息区或标准B组');
}
