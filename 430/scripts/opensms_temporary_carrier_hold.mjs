// User explicitly resumed B11 early; retain the timestamp for historical audits.
export const B11_RESUME_AT = '2026-09-11T23:01:08+08:00';
export function temporaryCarrierHeld(name, now = Date.now()) {
  if (!Number.isFinite(now)) throw new Error('Invalid temporary carrier hold time');
  return now < Date.parse(B11_RESUME_AT) && /^B11(?:-|\s|$)/i.test(String(name || '').trim());
}
export function assertTemporaryCarrierAllowed(name) {
  if (String(name || '').trim() === '三方隔离') throw new Error('三方隔离保护：禁止修改该区号码运营商');
  if (temporaryCarrierHeld(name)) throw new Error(`B11临时保护：${B11_RESUME_AT}前禁止修改来源B11运营商`);
}
