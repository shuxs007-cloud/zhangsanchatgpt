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
const WRITE_BATCH = 2000;

function stamp() {
  return new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
}

function safeLabel(value) {
  return String(value || 'downlist').replace(/[^A-Za-z0-9_.-]+/g, '_') || 'downlist';
}

function backendPhone(row) {
  return shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
}

function uniquePhones(candidate) {
  const values = targetPhonesFromCandidate(candidate);
  const seen = new Set();
  const duplicates = [];
  const result = [];
  for (const phone of values) {
    const key = shortCardNo(phone);
    if (!key) continue;
    if (seen.has(key)) duplicates.push(phone);
    else {
      seen.add(key);
      result.push(phone);
    }
  }
  if (duplicates.length) throw new Error(`候选文件存在重复号码: ${duplicates.slice(0, 20).join(',')}`);
  return result;
}

async function listPhonesBatched(api, phones) {
  const rows = [];
  for (let index = 0; index < phones.length; index += QUERY_BATCH) {
    const chunk = phones.slice(index, index + QUERY_BATCH);
    const result = await listPhonesByNumbers(api, chunk, QUERY_BATCH);
    rows.push(...result.rows);
  }
  return rows;
}

function classify(phones, backendRows) {
  const grouped = new Map();
  for (const row of backendRows) {
    const key = backendPhone(row);
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(row);
  }
  const active = [];
  const alreadyDown = [];
  const missing = [];
  const duplicate = [];
  for (const phone of phones) {
    const hits = grouped.get(shortCardNo(phone)) || [];
    if (!hits.length) missing.push(phone);
    else if (hits.length !== 1) duplicate.push({ phone, rows: hits.map(compactPhoneRow) });
    else if (hits[0].userAble === false) alreadyDown.push({ phone, row: compactPhoneRow(hits[0]) });
    else if (!hits[0].id) missing.push(phone);
    else active.push({ phone, row: hits[0] });
  }
  return { active, alreadyDown, missing, duplicate };
}

async function main() {
  const [mode, candidatePath, rawLabel = 'room_2_downlist'] = process.argv.slice(2);
  if (!['write', 'verify'].includes(mode) || !candidatePath) {
    throw new Error('usage: node scripts/opensms_downlist_candidates.mjs write|verify candidates.json [label]');
  }
  await ensureArtifactDirs();
  const label = safeLabel(rawLabel);
  const candidate = JSON.parse(await fs.readFile(candidatePath, 'utf8'));
  const phones = uniquePhones(candidate);
  const session = await createOpenSmsApiSession({ cdp: process.env.BITBROWSER_CDP, pathName: '/phoneConfig' });
  try {
    const beforeRows = phones.length ? await listPhonesBatched(session.api, phones) : [];
    const before = classify(phones, beforeRows);
    if (mode === 'verify') {
      const receipt = {
        checkedAt: new Date().toISOString(),
        mode: 'protocol-api-readonly-verify',
        apiTransport: session.apiTransport,
        source: path.resolve(candidatePath),
        candidateCount: phones.length,
        backendValidCount: phones.length - before.missing.length - before.duplicate.length,
        downCount: before.alreadyDown.length,
        stillActiveCount: before.active.length,
        missingCount: before.missing.length,
        duplicateBackendCount: before.duplicate.length,
        stillActive: before.active.map(item => ({ phone: item.phone, row: compactPhoneRow(item.row) })),
        missingPhones: before.missing,
        duplicateBackend: before.duplicate,
        passed: before.alreadyDown.length === phones.length
          && !before.active.length
          && !before.missing.length
          && !before.duplicate.length,
      };
      const outPath = path.join(SNAPSHOT_DIR, `opensms_verify_downlist_${label}_${stamp()}.json`);
      await fs.writeFile(outPath, `${JSON.stringify(receipt, null, 2)}\n`, { flag: 'wx' });
      console.log(JSON.stringify({ path: outPath, ...receipt }, null, 2));
      if (!receipt.passed) process.exitCode = 1;
      return;
    }

    const sourceRows = new Map((candidate.rows || candidate.candidates || []).map(row => [shortCardNo(row['电话'] || row.phone), row]));
    const validatedRows = before.active.map(({ phone, row }) => ({
      ...(sourceRows.get(shortCardNo(phone)) || { 电话: phone }),
      后台ID: row.id,
      写入前userAble: row.userAble,
      下架目标: true,
    }));
    const validatedPath = path.join(SNAPSHOT_DIR, `opensms_validated_${label}_${stamp()}_candidates.json`);
    await fs.writeFile(validatedPath, `${JSON.stringify({
      ...candidate,
      sourceCandidate: path.resolve(candidatePath),
      validatedAt: new Date().toISOString(),
      originalCount: phones.length,
      count: validatedRows.length,
      excluded: {
        alreadyDownCount: before.alreadyDown.length,
        missingCount: before.missing.length,
        duplicateBackendCount: before.duplicate.length,
      },
      rows: validatedRows,
      candidates: validatedRows,
    }, null, 2)}\n`, { flag: 'wx' });

    const ids = before.active.map(item => item.row.id);
    const updateResponses = [];
    for (let index = 0; index < ids.length; index += WRITE_BATCH) {
      const response = await session.api('POST', '/phone/config/batchOffline', {
        ids: ids.slice(index, index + WRITE_BATCH),
      }, null, { timeoutMs: 300000 });
      if (!response || response.code !== 200) throw new Error(`批量下架失败: ${JSON.stringify(response)}`);
      updateResponses.push(response);
    }
    if (!ids.length) updateResponses.push({ code: 200, msg: 'no-eligible-records', skipped: true });

    const afterRows = phones.length ? await listPhonesBatched(session.api, phones) : [];
    const after = classify(phones, afterRows);
    const receipt = {
      ts: new Date().toISOString(),
      mode: 'protocol-api',
      apiTransport: session.apiTransport,
      candidatePath: path.resolve(candidatePath),
      validatedCandidatePath: validatedPath,
      before: {
        candidateCount: phones.length,
        backendValidCount: phones.length - before.missing.length - before.duplicate.length,
        eligibleCount: before.active.length,
        alreadyDownCount: before.alreadyDown.length,
        missingCount: before.missing.length,
        duplicateBackendCount: before.duplicate.length,
        missingPhones: before.missing,
        duplicateBackend: before.duplicate,
      },
      updateResponses,
      after: {
        total: phones.length,
        downCount: after.alreadyDown.length,
        stillActiveCount: after.active.length,
        missingCount: after.missing.length,
        duplicateBackendCount: after.duplicate.length,
        stillActive: after.active.map(item => ({ phone: item.phone, row: compactPhoneRow(item.row) })),
        missingPhones: after.missing,
        duplicateBackend: after.duplicate,
      },
    };
    const receiptPath = path.join(SNAPSHOT_DIR, `opensms_downlist_${label}_${stamp()}.json`);
    await fs.writeFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, { flag: 'wx' });
    console.log(JSON.stringify({ path: receiptPath, ...receipt }, null, 2));
  } finally {
    await session.browser.close().catch(() => {});
  }
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
