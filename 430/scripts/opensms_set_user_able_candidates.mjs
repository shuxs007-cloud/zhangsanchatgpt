import fs from 'node:fs/promises';
import path from 'node:path';

import {
  compactPhoneRow,
  createOpenSmsApiSession,
  listPhonesByNumbers,
  shortCardNo,
  targetPhonesFromCandidate,
} from '../opensms_protocol_client.mjs';
import { SNAPSHOT_DIR, ensureArtifactDirs } from '../opensms_artifact_paths.mjs';

const QUERY_BATCH = 500;

function stamp() {
  return new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
}

function safeLabel(value) {
  return String(value || 'set_user_able').replace(/[^A-Za-z0-9_.-]+/g, '_') || 'set_user_able';
}

function backendPhone(row) {
  return shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
}

function uniquePhones(candidate) {
  const phones = targetPhonesFromCandidate(candidate).map(shortCardNo).filter(Boolean);
  if (new Set(phones).size !== phones.length) throw new Error('候选文件存在重复号码，停止操作');
  return phones;
}

async function queryUnique(api, phones) {
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
  const missing = [];
  const duplicate = [];
  for (const phone of phones) {
    const hits = grouped.get(phone) || [];
    if (!hits.length) missing.push(phone);
    else if (hits.length !== 1) duplicate.push({ phone, rows: hits.map(compactPhoneRow) });
    else rows.push(hits[0]);
  }
  return { rows, missing, duplicate };
}

async function main() {
  const [mode, candidatePath, rawLabel = 'set_user_able'] = process.argv.slice(2);
  if (!['write', 'verify'].includes(mode) || !candidatePath) {
    throw new Error('usage: node scripts/opensms_set_user_able_candidates.mjs write|verify candidates.json [label]');
  }
  await ensureArtifactDirs();
  const label = safeLabel(rawLabel);
  const candidate = JSON.parse(await fs.readFile(candidatePath, 'utf8'));
  const phones = uniquePhones(candidate);
  const receipt = {
    createdAt: new Date().toISOString(),
    mode: mode === 'write' ? 'protocol-api-per-record-setUserAble' : 'protocol-api-readonly-verify',
    candidatePath: path.resolve(candidatePath),
    candidateCount: phones.length,
    status: 'started',
  };
  let session;
  try {
    session = await createOpenSmsApiSession({ cdp: process.env.BITBROWSER_CDP, pathName: '/phoneConfig' });
    receipt.apiTransport = session.apiTransport;
    const before = await queryUnique(session.api, phones);
    receipt.backendValidCount = before.rows.length;
    receipt.missingPhones = before.missing;
    receipt.duplicateBackend = before.duplicate;
    if (before.missing.length || before.duplicate.length) {
      throw new Error(`后台号码核验未完整通过，停止操作: 缺失${before.missing.length}，重复${before.duplicate.length}`);
    }
    const inactive = before.rows.filter(row => row.userAble === false);
    receipt.beforeActiveCount = before.rows.length - inactive.length;
    receipt.beforeInactiveCount = inactive.length;
    receipt.eligible = inactive.map(compactPhoneRow);
    receipt.writeResults = [];
    if (mode === 'write') {
      for (const row of inactive) {
        try {
          const response = await session.api('PUT', `/phone/config/setUserAble/${row.id}`, { userAble: true });
          receipt.writeResults.push({ id: row.id, phone: backendPhone(row), success: response?.code === 200, response });
        } catch (error) {
          receipt.writeResults.push({ id: row.id, phone: backendPhone(row), success: false, error: error.message || String(error) });
        }
      }
    }
    receipt.actualWriteCount = mode === 'write' ? inactive.length : 0;
    receipt.successCount = receipt.writeResults.filter(row => row.success).length;
    receipt.failureCount = receipt.writeResults.filter(row => !row.success).length;

    const after = mode === 'write' ? await queryUnique(session.api, phones) : before;
    receipt.afterActiveCount = after.rows.filter(row => row.userAble !== false).length;
    receipt.afterInactiveCount = after.rows.filter(row => row.userAble === false).length;
    receipt.afterInactive = after.rows.filter(row => row.userAble === false).map(compactPhoneRow);
    receipt.passed = receipt.afterActiveCount === phones.length
      && receipt.afterInactiveCount === 0
      && !after.missing.length
      && !after.duplicate.length;
    receipt.status = receipt.passed ? 'success' : 'failed';
    if (!receipt.passed) receipt.error = `逐号上架回查未通过: 已上架${receipt.afterActiveCount}，仍下架${receipt.afterInactiveCount}`;
  } catch (error) {
    receipt.status = 'failed';
    receipt.error = error.stack || String(error);
  } finally {
    await session?.browser.close().catch(() => {});
  }

  const prefix = mode === 'write' ? 'opensms_set_user_able' : 'opensms_verify_set_user_able';
  const outPath = path.join(SNAPSHOT_DIR, `${prefix}_${label}_${stamp()}.json`);
  await fs.writeFile(outPath, `${JSON.stringify(receipt, null, 2)}\n`, { flag: 'wx' });
  console.log(JSON.stringify({
    path: outPath,
    status: receipt.status,
    candidateCount: receipt.candidateCount,
    backendValidCount: receipt.backendValidCount || 0,
    beforeActiveCount: receipt.beforeActiveCount || 0,
    beforeInactiveCount: receipt.beforeInactiveCount || 0,
    actualWriteCount: receipt.actualWriteCount || 0,
    successCount: receipt.successCount || 0,
    failureCount: receipt.failureCount || 0,
    afterActiveCount: receipt.afterActiveCount || 0,
    afterInactiveCount: receipt.afterInactiveCount || 0,
    passed: receipt.passed || false,
    error: receipt.error || null,
  }, null, 2));
  if (!receipt.passed) process.exitCode = 1;
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
