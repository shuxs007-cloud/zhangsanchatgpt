import fs from 'node:fs/promises';
import path from 'node:path';
import {
  compactPhoneRow,
  createOpenSmsApiSession,
  listPhonesByNumbers,
  shortCardNo,
  targetPhonesFromCandidate,
} from './opensms_protocol_client.mjs';
import { SNAPSHOT_DIR, ensureArtifactDirs } from './opensms_artifact_paths.mjs';

const QUERY_BATCH = 500;
const WRITE_BATCH = 500;


function stamp() {
  return new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
}

function backendPhone(row) {
  return shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
}

async function queryUnique(api, phones) {
  if (!phones.length) return { rows: [], missingPhones: [], duplicateBackend: [] };
  const backendRows = [];
  for (let index = 0; index < phones.length; index += QUERY_BATCH) {
    const chunk = phones.slice(index, index + QUERY_BATCH);
    const result = await listPhonesByNumbers(api, chunk, QUERY_BATCH);
    backendRows.push(...result.rows);
  }
  const grouped = new Map();
  for (const row of backendRows) {
    const key = backendPhone(row);
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(row);
  }
  const rows = [];
  const missingPhones = [];
  const duplicateBackend = [];
  for (const phone of phones) {
    const hits = grouped.get(shortCardNo(phone)) || [];
    if (!hits.length) missingPhones.push(phone);
    else if (hits.length !== 1) duplicateBackend.push({ phone, rows: hits.map(compactPhoneRow) });
    else rows.push(hits[0]);
  }
  return { rows, missingPhones, duplicateBackend };
}

async function main() {
  await ensureArtifactDirs();
  const [mode, candidatePath, label = 'offline_recover'] = process.argv.slice(2);
  if (!['write', 'verify'].includes(mode) || !candidatePath) {
    throw new Error('usage: node opensms_offline_recover_candidates.mjs write|verify candidates.json label');
  }
  const candidate = JSON.parse(await fs.readFile(candidatePath, 'utf8'));
  const phones = targetPhonesFromCandidate(candidate);
  const normalizedPhones = phones.map(shortCardNo);
  if (new Set(normalizedPhones).size !== normalizedPhones.length) {
    throw new Error('候选文件存在重复号码，停止操作');
  }
  const receipt = {
    createdAt: new Date().toISOString(),
    mode: mode === 'write' ? 'protocol-api' : 'protocol-api-readonly-verify',
    candidatePath: path.resolve(candidatePath),
    candidateCount: phones.length,
    status: 'started',
  };
  if (mode === 'write') {
    receipt.status = 'disabled';
    receipt.passed = true;
    receipt.reason = '下架恢复功能已取消；未连接后台、未提交恢复、未重试';
    receipt.backendValidCount = 0;
    receipt.inactiveCount = 0;
    receipt.alreadyActiveCount = 0;
    receipt.afterActiveCount = 0;
    receipt.afterInactiveCount = 0;
    const outPath = path.join(SNAPSHOT_DIR, `opensms_offline_recover_disabled_${label}_${stamp()}.json`);
    await fs.writeFile(outPath, JSON.stringify(receipt, null, 2));
    console.log(JSON.stringify({
      path: outPath,
      status: receipt.status,
      candidateCount: receipt.candidateCount,
      actualWriteCount: 0,
      passed: true,
      reason: receipt.reason,
    }, null, 2));
    return;
  }
  let session;
  try {
    session = await createOpenSmsApiSession({ pathName: '/phoneConfig' });
    receipt.apiTransport = session.apiTransport;
    const queried = await queryUnique(session.api, phones);
    receipt.missingPhones = queried.missingPhones;
    receipt.duplicateBackend = queried.duplicateBackend;
    receipt.backendValidCount = queried.rows.length;
    if (queried.missingPhones.length || queried.duplicateBackend.length) {
      throw new Error(`后台号码核验未完整通过，停止操作: 缺失${queried.missingPhones.length}，重复${queried.duplicateBackend.length}`);
    }
    receipt.before = queried.rows.map(compactPhoneRow);
    const inactive = queried.rows.filter(row => row.userAble === false);
    receipt.inactiveCount = inactive.length;
    receipt.alreadyActiveCount = queried.rows.length - inactive.length;
    if (mode === 'write') {
      receipt.updateResponses = [];
      const ids = inactive.map(row => row.id);
      for (let index = 0; index < ids.length; index += WRITE_BATCH) {
        const response = await session.api('POST', '/phone/config/offlineRecover', {
          phoneIds: ids.slice(index, index + WRITE_BATCH),
        }, null, { timeoutMs: 300000 });
        if (!response || response.code !== 200) {
          throw new Error(`批量上架失败: ${JSON.stringify(response)}`);
        }
        receipt.updateResponses.push(response);
      }
      if (!ids.length) receipt.updateResponses.push({ code: 200, msg: 'no-inactive', skipped: true });
    }
    const after = mode === 'write' ? await queryUnique(session.api, phones) : queried;
    receipt.afterActiveCount = after.rows.filter(row => row.userAble !== false).length;
    receipt.afterInactiveCount = after.rows.filter(row => row.userAble === false).length;
    receipt.passed = receipt.afterActiveCount === phones.length
      && receipt.afterInactiveCount === 0
      && !after.missingPhones.length
      && !after.duplicateBackend.length;
    if (!receipt.passed) throw new Error(`上架回查未通过: 已上架${receipt.afterActiveCount}，仍下架${receipt.afterInactiveCount}`);
    receipt.status = 'success';
  } catch (error) {
    receipt.status = 'failed';
    receipt.error = error.stack || String(error);
  } finally {
    await session?.browser.close().catch(() => {});
  }
  const outPath = path.join(SNAPSHOT_DIR, `opensms_${mode === 'write' ? 'offline_recover' : 'verify_offline_recover'}_${label}_${stamp()}.json`);
  await fs.writeFile(outPath, JSON.stringify(receipt, null, 2));
  console.log(JSON.stringify({
    path: outPath,
    status: receipt.status,
    candidateCount: receipt.candidateCount,
    backendValidCount: receipt.backendValidCount || 0,
    inactiveCount: receipt.inactiveCount || 0,
    alreadyActiveCount: receipt.alreadyActiveCount || 0,
    afterActiveCount: receipt.afterActiveCount || 0,
    afterInactiveCount: receipt.afterInactiveCount || 0,
    missingCount: receipt.missingPhones?.length || 0,
    duplicateBackendCount: receipt.duplicateBackend?.length || 0,
    updateResponses: receipt.updateResponses || null,
    passed: receipt.passed || false,
    error: receipt.error || null,
  }, null, 2));
  if (receipt.status !== 'success') process.exitCode = 1;
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
