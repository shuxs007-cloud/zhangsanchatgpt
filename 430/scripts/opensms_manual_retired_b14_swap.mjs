#!/usr/bin/env node
// One explicitly authorized repair, not an automatic C/D route or allocation rule.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { acquireSharedOpenSmsSession } from './opensms_shared_browser.mjs';
import { getCarrierConfigsReadOnly } from './opensms_carriers_readonly.mjs';
import { retentionOprClass } from './opensms_task_d_filtered_retention.mjs';
import { createOpenSmsApiSession, listPhonesByNumbers, updatePhoneCarrier } from '../opensms_protocol_client.mjs';

const ROOT = '/Users/zhangsan/Downloads/430';
const RUN_ID = '20261007T200519+0800';
const SOURCE = `/Volumes/zs01/chatgpt/OpenSMS/20261007/${RUN_ID}/receipts/opensms_task_d_mapping_${RUN_ID}_receipt.json`;
const DIRECTORY = `${ROOT}/opensms_artifacts/state/manual_retired_b14_swap/${RUN_ID}`;
const GROUPS = [
  { source: '2093378431675928577', name: 'B14-C快下保留卡', target: '换号区C', oprClass: 'chatr',
    phones: ['2043969351', '9423882118', '4378339823', '4162197657', '4167237368', '4168168608',
      '4378290065', '4375520311', '4376769439', '6043634795'] },
  { source: '2088852494502465536', name: 'B14-T快下保留卡', target: '换号区T', oprClass: 'kooda',
    phones: ['7052746715', '8733782833'] },
];
const text = value => value == null ? '' : String(value).trim();
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const carrierId = row => text(row.carrireIdentif || row.carrierIdentif || row.identif || row.id);
const carrierName = row => text(row.name || row.carrierName || row.carrireName || row.carrier);
const identity = row => ({ id: text(row.id), cardNo: text(row.cardNo),
  slotIdentif: text(row.slotIdentif), iccid: text(row.iccid) });

export function validateSource(source) {
  const expected = new Map(GROUPS.flatMap(group => group.phones.map(phone => [phone, group.source])));
  if (source.runId !== RUN_ID || source.task !== 'D' || source.errorCount !== 12
      || !Array.isArray(source.errors) || source.errors.length !== 12
      || new Set(source.errors.map(row => row.cardNo)).size !== 12
      || source.errors.some(row => row.reason !== 'unresolved_carrier'
        || expected.get(row.cardNo) !== row.carrireIdentif)) throw new Error('原失败回执与指定12号不一致');
  const rawRows = source.filteredQueryProof?.queries?.flatMap(query => query.pages.flatMap(page => page.rows));
  if (!Array.isArray(rawRows)) throw new Error('原回执缺少原始后台身份');
  const baseline = new Map();
  for (const phone of expected.keys()) {
    const rows = rawRows.filter(row => text(row.cardNo) === phone);
    if (!rows.length || rows.some(row => !identity(row).id || carrierId(row) !== expected.get(phone)
        || JSON.stringify(identity(row)) !== JSON.stringify(identity(rows[0]))
        || text(row.opr) !== text(rows[0].opr))) throw new Error(`原回执后台身份冲突: ${phone}`);
    baseline.set(phone, rows[0]);
  }
  return baseline;
}

export function buildPlan(baseline, carriers, queried) {
  const phones = GROUPS.flatMap(group => group.phones);
  if (queried.total !== 12 || !Array.isArray(queried.rows) || queried.rows.length !== 12
      || queried.rows.some(row => !phones.includes(text(row.cardNo)))
      || new Set(queried.rows.map(row => text(row.cardNo))).size !== 12
      || new Set(queried.rows.map(row => text(row.id))).size !== 12) throw new Error('实时查询缺号、重复或范围超出12号');
  return GROUPS.map(group => {
    if (carriers.some(row => carrierId(row) === group.source)) throw new Error('旧B14 ID重新出现，停止人工修复');
    const matches = carriers.filter(row => carrierName(row) === group.target && row.delData !== true);
    if (matches.length !== 1 || !carrierId(matches[0])) throw new Error(`目标运营商不唯一: ${group.target}`);
    const target = { name: group.target, identif: carrierId(matches[0]) };
    const rows = group.phones.map(phone => {
      const row = queried.rows.find(item => text(item.cardNo) === phone);
      if (JSON.stringify(identity(row)) !== JSON.stringify(identity(baseline.get(phone)))
          || retentionOprClass(row.opr) !== group.oprClass) throw new Error(`号码后台身份或OPR变化: ${phone}`);
      const source = carrierId(row);
      if (![group.source, target.identif].includes(source)
          || ['carrireIdentif', 'carrierIdentif'].some(key => text(row[key]) && text(row[key]) !== source)
          || ['carrierName', 'carrireName', 'carrier'].some(key => text(row[key])
            && text(row[key]) !== (source === group.source ? group.name : target.name))) {
        throw new Error(`号码归属已变化或字段冲突，不覆盖人工调整: ${phone}`);
      }
      return { ...identity(row), opr: text(row.opr), originalCarrierIdentif: source,
        alreadyTarget: source === target.identif };
    });
    return { originalCarrierName: group.name, originalCarrierIdentif: group.source, target, rows };
  });
}

export function verifyGroup(group, queried) {
  if (queried.total !== group.rows.length || queried.rows.length !== group.rows.length
      || new Set(queried.rows.map(row => text(row.cardNo))).size !== group.rows.length) throw new Error('独立回读缺号或重复');
  for (const expected of group.rows) {
    const row = queried.rows.find(item => text(item.cardNo) === expected.cardNo);
    if (!row || JSON.stringify(identity(row)) !== JSON.stringify(identity(expected))
        || text(row.opr) !== expected.opr || carrierId(row) !== group.target.identif
        || ['carrireIdentif', 'carrierIdentif'].some(key => text(row[key]) && text(row[key]) !== group.target.identif)
        || ['carrierName', 'carrireName', 'carrier'].some(key => text(row[key]) && text(row[key]) !== group.target.name)) {
      throw new Error(`独立回读身份或目标不一致: ${expected.cardNo}`);
    }
  }
}

async function saveDurable(file, value, exclusive = false) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  const target = exclusive ? file : `${file}.tmp-${process.pid}`;
  const handle = await fs.open(target, 'wx', 0o600);
  try { await handle.writeFile(`${JSON.stringify(value, null, 2)}\n`); await handle.sync(); }
  finally { await handle.close(); }
  if (!exclusive) await fs.rename(target, file);
  const directory = await fs.open(path.dirname(file), 'r');
  try { await directory.sync(); } finally { await directory.close(); }
}

async function main() {
  const mode = process.argv[2];
  if (!['--preview', '--apply'].includes(mode)
      || (mode === '--apply' && process.argv[3] !== '--current-user-authorized-12-only')) {
    throw new Error('仅支持指定12号预检，或本轮人工授权修改；不接入定时任务');
  }
  const bytes = await fs.readFile(SOURCE);
  const baseline = validateSource(JSON.parse(bytes));
  const sourceSha256 = hash(bytes);
  let preview;
  if (mode === '--apply') {
    preview = JSON.parse(await fs.readFile(`${DIRECTORY}/preview.json`, 'utf8'));
    if (preview.sourceSha256 !== sourceSha256 || preview.runId !== RUN_ID
        || preview.mode !== 'readonly-preview' || !Number.isFinite(Date.parse(preview.checkedAt))
        || Date.parse(preview.checkedAt) > Date.now()
        || Date.now() - Date.parse(preview.checkedAt) > 15 * 60 * 1000) {
      throw new Error('人工修复预检过期或来源变化');
    }
  }
  const lease = await acquireSharedOpenSmsSession({ timeoutMs: 301000 });
  let session, state, claimed = false;
  const statePath = `${DIRECTORY}/execution.json`;
  try {
    session = await createOpenSmsApiSession({ cdp: lease.session.cdp, pathName: '/phoneConfig' });
    // Manual carrier changes retain the binding workflow's >=2s post-response gap.
    let responseAt = 0;
    const api = async (...args) => {
      const delay = Math.max(0, responseAt + 2000 - Date.now());
      if (delay) await new Promise(resolve => setTimeout(resolve, delay));
      try { return await session.api(...args); } finally { responseAt = Date.now(); }
    };
    const carriers = await getCarrierConfigsReadOnly(api, { concurrency: 1 });
    const phones = GROUPS.flatMap(group => group.phones);
    const queried = await listPhonesByNumbers(api, phones, 500);
    const groups = buildPlan(baseline, carriers, queried);
    if (mode === '--preview') {
      const result = { runId: RUN_ID, mode: 'readonly-preview', checkedAt: new Date().toISOString(),
        source: SOURCE, sourceSha256, groups, carriers, before: queried, businessWrites: 0 };
      await saveDurable(`${DIRECTORY}/preview.json`, result, true);
      console.log(JSON.stringify({ path: `${DIRECTORY}/preview.json`, total: 12, businessWrites: 0,
        groups: groups.map(group => ({ target: group.target.name, count: group.rows.length,
          alreadyTarget: group.rows.filter(row => row.alreadyTarget).length })) }));
      return;
    }
    if (JSON.stringify(groups) !== JSON.stringify(preview.groups)) throw new Error('预检后身份或运营商变化，未提交');
    state = { runId: RUN_ID, operation: 'manual_retired_b14_swap_12_only',
      authorized: true, startedAt: new Date().toISOString(), sourceSha256,
      status: 'preflight_passed', before: queried, groups: groups.map(group => ({ ...group, status: 'pending' })) };
    // Exclusive claim is permanent: an unknown submission can never be replayed.
    await saveDurable(statePath, state, true);
    claimed = true;
    for (const group of state.groups) {
      const eligible = group.rows.filter(row => !row.alreadyTarget);
      if (eligible.length) {
        const current = await listPhonesByNumbers(api, group.rows.map(row => row.cardNo), 500);
        if (current.total !== group.rows.length || current.rows.length !== group.rows.length) {
          throw new Error('提交前精确号码查询不完整');
        }
        const refreshed = buildPlan(baseline, carriers, { total: 12,
          rows: [...current.rows, ...queried.rows.filter(row => !group.rows.some(item => item.cardNo === row.cardNo))] });
        const refreshedGroup = refreshed.find(item => item.target.name === group.target.name);
        if (JSON.stringify(refreshedGroup.rows) !== JSON.stringify(group.rows)) throw new Error('提交前来源变化，未提交该组');
        group.status = 'submission_intent';
        group.intentAt = new Date().toISOString();
        group.submitIds = eligible.map(row => baseline.get(row.cardNo).id);
        await saveDurable(statePath, state);
        group.response = await updatePhoneCarrier(api, group.submitIds, group.target.identif);
        group.status = 'submitted_pending_readback';
        await saveDurable(statePath, state);
      }
      group.readback = await listPhonesByNumbers(api, group.rows.map(row => row.cardNo), 500);
      verifyGroup(group, group.readback);
      group.actualWriteCount = eligible.length;
      group.status = 'independently_verified';
      group.verifiedAt = new Date().toISOString();
      await saveDurable(statePath, state);
    }
    const finalRows = await listPhonesByNumbers(api, phones, 500);
    for (const group of state.groups) verifyGroup(group, { total: group.rows.length,
      rows: finalRows.rows.filter(row => group.rows.some(item => item.cardNo === row.cardNo)) });
    if (finalRows.total !== 12 || finalRows.rows.length !== 12) throw new Error('最终12号全量回读不完整');
    state.finalReadback = finalRows;
    state.status = 'passed';
    state.completedAt = new Date().toISOString();
    await saveDurable(statePath, state);
    console.log(JSON.stringify({ path: statePath, status: state.status, total: 12,
      actualWriteCount: state.groups.reduce((sum, group) => sum + group.actualWriteCount, 0),
      independentlyVerifiedCount: 12,
      groups: state.groups.map(group => ({ target: group.target.name, count: group.rows.length,
        actualWriteCount: group.actualWriteCount, status: group.status })) }));
  } catch (error) {
    if (state && claimed) {
      state.status = 'stopped_no_retry';
      state.error = String(error.message);
      await saveDurable(statePath, state).catch(() => {});
    }
    throw error;
  } finally {
    await session?.browser.close().catch(() => {});
    lease.release();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
