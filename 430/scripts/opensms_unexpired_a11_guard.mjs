// Explicit correction requires fresh document proof AND unchanged live SIM identity.
import fs from 'node:fs/promises';
import crypto from 'node:crypto';

export const RESTORE_ROUTE = 'restore_unexpired_to_a11';
const SOURCES = new Set(['快过期隔离区C', '快过期隔离区T', '过期下架卡C', '过期下架卡T']);
const text = value => String(value ?? '').trim();
const phone = value => text(value).replace(/\D/g, '').slice(-10);
const iccid = value => text(value).replace(/\s/g, '').replace(/[Ff]$/, '');
const carrierId = row => text(row.carrireIdentif || row.carrierIdentif || row.identif);
const fail = message => { throw new Error(`未到期恢复A11安全门禁: ${message}`); };
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
export function isUnexpiredRestore(candidate) {
  return candidate.task === 'RESTORE_UNEXPIRED_A11' || candidate.route === RESTORE_ROUTE;
}
function isoDay(value) {
  const v = text(value);
  if (!/^20\d{2}-\d{2}-\d{2}$/.test(v)) fail('日期不是有效ISO日期');
  const ms = Date.parse(v + 'T00:00:00Z');
  if (!Number.isFinite(ms) || new Date(ms).toISOString().slice(0, 10) !== v) fail('日期无效');
  return ms;
}
export async function enforceUnexpiredRestore(candidate, target, now = Date.now()) {
  if (!isUnexpiredRestore(candidate)) return;
  if (process.env.OPENSMS_ALLOW_UNEXPIRED_A11_RESTORE !== '1'
      || candidate.task !== 'RESTORE_UNEXPIRED_A11' || candidate.route !== RESTORE_ROUTE
      || candidate.authorizationSource !== 'current-user' || candidate.forceGate !== true
      || !candidate.runId || candidate.runId !== process.env.OPENSMS_RUN_ID
      || candidate.targetCarrierName !== target || !/^A11(?:-|\s|$)/i.test(target)) fail('授权、路由、运行编号或目标不一致');
  const age = now - Date.parse(candidate.createdAt);
  if (!Number.isFinite(age) || age < -5000 || age > 15 * 60000) fail('候选不是当前新鲜批次');
  if (!Array.isArray(candidate.sourceCarrierNames) || candidate.sourceCarrierNames.length !== SOURCES.size
      || new Set(candidate.sourceCarrierNames).size !== SOURCES.size
      || candidate.sourceCarrierNames.some(name => !SOURCES.has(name))) fail('来源范围越界');
  for (const [file, sha] of [[candidate.sourceWorkbook, candidate.sourceWorkbookSha256],
    [candidate.sourceArchive, candidate.sourceSha256], [candidate.documentReceipt, candidate.documentReceiptSha256]]) {
    if (!file || !file.includes(candidate.runId) || !sha || digest(await fs.readFile(file)) !== sha) fail('当前来源文件身份不一致');
  }
  const doc = JSON.parse(await fs.readFile(candidate.documentReceipt, 'utf8'));
  const review = doc.verification?.date_resolution_review;
  if (doc.result !== '成功' || doc.failed_checks?.length || review?.matches !== true
      || review.source_export?.sha256 !== candidate.sourceSha256) fail('文档日期独立验证未通过');
  const expectedDay = isoDay(candidate.serverDate);
  const seenPaths = new Set();
  for (const proof of candidate.serverDateReceipts || []) {
    const bytes = await fs.readFile(proof.path);
    if (digest(bytes) !== proof.sha256) fail('服务器日期回执SHA不符');
    const date = JSON.parse(bytes);
    const dateAge = now - Date.parse(date.checkedAt);
    if (!Number.isFinite(dateAge) || dateAge < -5000 || dateAge > 120000
        || date.serverDate !== candidate.serverDate || date.silentBrowserMode !== true
        || date.browserForegroundActivationAttempted !== false
        || !['/day', '/phoneConfig'].includes(date.requestedPath)) fail('写入前服务器日期未确认或过期');
    seenPaths.add(date.requestedPath);
  }
  if (seenPaths.size !== 2 || candidate.serverDateReceipts.length !== 2) fail('缺少两个页面的独立服务器日期回执');
  const seen = new Set();
  if (!Array.isArray(candidate.rows) || candidate.rows.length !== candidate.count) fail('候选计数不符');
  for (const row of candidate.rows) {
    const key = phone(row.电话);
    const days = (isoDay(row.截止日) - expectedDay) / 86400000;
    if (!/^\d{10}$/.test(key) || seen.has(key) || !SOURCES.has(row.原运营商)
        || !/^\d{18,22}$/.test(iccid(row.ICCID)) || !text(row.卡槽)
        || days <= 0 || days !== row.距截止日天数) fail('候选身份、截止日或来源不符合恢复条件');
    seen.add(key);
  }
}

export function validateUnexpiredLiveIdentity(candidate, liveRows, carriers, target) {
  if (!isUnexpiredRestore(candidate)) return;
  if (target.delData === true || target.name !== candidate.targetCarrierName) fail('目标已删除或不一致');
  const names = new Map(carriers.map(row => [carrierId(row), text(row.name || row.carrierName || row.carrireName)]));
  const grouped = new Map();
  for (const row of liveRows) {
    const key = phone(row.cardNo || row.phone || row.cardPhone || row.tel);
    grouped.set(key, [...(grouped.get(key) || []), row]);
  }
  const ids = new Set();
  for (const expected of candidate.rows) {
    const rows = grouped.get(phone(expected.电话)) || [];
    if (rows.length !== 1 || !rows[0].id || ids.has(text(rows[0].id))) fail('实时号码缺失、重复或ID冲突；整批不写入');
    const row = rows[0];
    const sim = row.cardStore || row.cardStorage || row.cardSave || row.iccid || row.simCardNumber || row.simCardNo;
    const slot = text(row.slot || row.cardSlot || row.slotIdentif);
    const name = names.get(carrierId(row)) || text(row.carrierName || row.carrireName || row.carrier);
    if (iccid(sim) !== iccid(expected.ICCID) || slot !== text(expected.卡槽)) fail(`号码${expected.电话}的ICCID或卡槽已变化；整批不写入`);
    if (name !== expected.原运营商 && name !== target.name) fail(`号码${expected.电话}已离开指定来源；整批不写入`);
    ids.add(text(row.id));
  }
}
