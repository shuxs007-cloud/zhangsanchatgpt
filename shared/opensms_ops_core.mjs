export const split = value => [...new Set(String(value || '').split(/[,，、;；]/).map(x => x.trim()).filter(Boolean))];
const norm = x => String(x).trim().toLowerCase();
export function carrier(rows, code) {
  const exact = rows.filter(r => norm(r.name) === norm(code));
  const matches = exact.length ? exact : rows.filter(r => norm(r.name).startsWith(`${norm(code)}-`));
  if (matches.length !== 1 || !matches[0].identif) throw new Error(`Carrier not unique: ${code}`);
  return matches[0];
}
export function exactUser(rows, name) {
  const matches = rows.filter(r => norm(r.proxyUserName) === norm(name));
  if (matches.length !== 1) throw new Error(`Account not exact/unique: ${name}`);
  return matches[0];
}
export function checkAccount(before, after, desired, only = false) {
  const ids = new Set(after.map(r => String(r.carrireIdentif)));
  const target = new Set(desired.map(String));
  return {
    missing: [...target].filter(id => !ids.has(id)),
    extra: only ? [...ids].filter(id => !target.has(id)) : [],
    lost: only ? [] : before.filter(r => !after.some(a => String(a.id) === String(r.id))),
    duplicates: only ? after.length - ids.size : 0,
  };
}
export function checkSets(users, rows) {
  const bound = new Set(rows.map(r => String(r.proxyUserId)));
  return [...new Set(users.map(String))].filter(id => !bound.has(id));
}
export function validateBatch(record) {
  if (!Array.isArray(record.bindings) || !record.bindings.length) throw new Error('Empty batch');
  const ids = new Set();
  for (const r of record.bindings) {
    for (const k of ['id', 'proxyUserId', 'carrireIdentif']) {
      if (typeof r[k] !== 'string' || !/^\d+$/.test(r[k])) throw new Error(`Unsafe batch field: ${k}`);
    }
    if (ids.has(r.id)) throw new Error('Duplicate binding ID');
    ids.add(r.id);
  }
  return record;
}
export function exactCreatedBindings(beforeRows, afterRows, sourceUserIds, rawResults, targetIdentif) {
  const beforeIds = new Set(beforeRows.map(row => String(row.id)));
  const afterById = new Map(afterRows.map(row => [String(row.id), row]));
  const users = new Set([...sourceUserIds].map(String));
  const declared = (rawResults || []).flatMap(row => row.createdBindings || []);
  const declaredIds = new Set(declared.map(row => String(row.id)));
  if (declaredIds.size !== declared.length || afterById.size !== afterRows.length) throw new Error('Duplicate binding IDs in exact B12 readback');
  const bindings = declared.map(row => {
    const id = String(row.id);
    const actual = afterById.get(id);
    if (!actual || beforeIds.has(id) || !users.has(String(actual.proxyUserId)) ||
        String(actual.carrireIdentif) !== String(targetIdentif) ||
        String(row.proxyUserId) !== String(actual.proxyUserId) ||
        String(row.carrireIdentif) !== String(targetIdentif)) {
      throw new Error(`Unverified B12 created binding: ${id}`);
    }
    return {id, proxyUserId:String(actual.proxyUserId), proxyUserName:String(actual.proxyUserName || ''),
      carrireIdentif:String(actual.carrireIdentif), carrierName:String(actual.carrierName || '')};
  });
  const unexplainedNewTargetIds = afterRows.filter(row => !beforeIds.has(String(row.id)) && !declaredIds.has(String(row.id)))
    .map(row => String(row.id));
  return {bindings, unexplainedNewTargetIds};
}
export function beijing(date = new Date()) {
  return new Intl.DateTimeFormat('sv-SE', {timeZone: 'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false}).format(date);
}
export function assertCancelTime(stamp, now = beijing()) {
  if (!/^\d{8}$/.test(stamp) || now.slice(0,10).replaceAll('-', '') !== stamp || now.slice(11) < '13:00:00') throw new Error('Cancellation requires same day at/after 13:00 Beijing');
}
export function validCheck(c) { return !c.missing.length && !c.extra.length && !c.lost.length && !c.duplicates; }
