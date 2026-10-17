import fs from 'node:fs/promises';
import path from 'node:path';
import {
  compactPhoneRow,
  createOpenSmsApiSession,
  getCarrierConfigs,
  listPhonesByNumbers,
  shortCardNo,
  targetPhonesFromCandidate,
} from './opensms_protocol_client.mjs';
import { SNAPSHOT_DIR, ensureArtifactDirs } from './opensms_artifact_paths.mjs';

const CDP = process.env.BITBROWSER_CDP;
const BATCH_SIZE = 500;

function stamp() {
  return new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
}

function carrierName(row) {
  return String(row.name || row.carrierName || row.carrireName || row.carrier || '');
}

function carrierIdentif(row) {
  return String(row.carrireIdentif || row.carrierIdentif || row.identif || row.id || '');
}

function exactCarrier(carriers, name) {
  const matches = carriers.filter(row => carrierName(row) === name);
  if (matches.length !== 1) throw new Error(`运营商精确匹配异常: ${name}, count=${matches.length}`);
  return {
    name: carrierName(matches[0]),
    identif: carrierIdentif(matches[0]),
    id: matches[0].id,
  };
}

function backendPhone(row) {
  return shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
}

function validateCandidateStructure(candidate) {
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate) || !Array.isArray(candidate.rows)) {
    throw new Error('候选清单结构无效：必须为包含rows数组的对象');
  }
  for (const row of candidate.rows) {
    if (!row || typeof row !== 'object' || Array.isArray(row) || !shortCardNo(row['电话'] || row.phone)) {
      throw new Error('候选清单包含无效号码行，拒绝作为空清单跳过');
    }
  }
  if (candidate.count !== undefined && (!Number.isInteger(candidate.count) || candidate.count !== candidate.rows.length)) {
    throw new Error('候选清单count与rows数量不一致');
  }
  if (candidate.candidates !== undefined && (
    !Array.isArray(candidate.candidates)
    || candidate.candidates.length !== candidate.rows.length
    || candidate.candidates.some((row, index) => shortCardNo(row?.['电话'] || row?.phone)
      !== shortCardNo(candidate.rows[index]['电话'] || candidate.rows[index].phone))
  )) {
    throw new Error('候选清单candidates与rows号码不一致');
  }
}

async function saveReceipt(receipt, label) {
  const outPath = path.join(SNAPSHOT_DIR, `opensms_verify_carrier_${label}_${stamp()}.json`);
  await fs.writeFile(outPath, JSON.stringify(receipt, null, 2));
  console.log(JSON.stringify({ path: outPath, ...receipt }, null, 2));
}

async function listPhonesBatched(api, phones) {
  const rows = [];
  for (let index = 0; index < phones.length; index += BATCH_SIZE) {
    const chunk = phones.slice(index, index + BATCH_SIZE);
    const result = await listPhonesByNumbers(api, chunk, BATCH_SIZE);
    rows.push(...result.rows);
  }
  return { rows };
}

async function main() {
  await ensureArtifactDirs();
  const [candidatePath, targetName, label = 'carrier'] = process.argv.slice(2);
  if (!candidatePath || !targetName) {
    throw new Error('usage: node opensms_verify_carrier_candidates_readonly.mjs candidates.json targetCarrier label');
  }

  const candidate = JSON.parse(await fs.readFile(candidatePath, 'utf8'));
  validateCandidateStructure(candidate);
  const phones = targetPhonesFromCandidate(candidate);
  if (!phones.length) {
    await saveReceipt({
      checkedAt: new Date().toISOString(), mode: 'protocol-api-readonly-verify',
      apiTransport: null, source: path.resolve(candidatePath),
      target: { name: targetName, identif: null, id: null, resolved: false },
      status: 'no_candidate', verificationStatus: 'not_required',
      independentReadbackRequired: false, independentReadbackPerformed: false,
      total: 0, targetCount: 0, stillNotTargetCount: 0, missingCount: 0, duplicateBackendCount: 0,
      stillNotTarget: [], missingPhones: [], duplicateBackend: [], passed: null,
    }, label);
    return;
  }
  const session = await createOpenSmsApiSession({ cdp: CDP, pathName: '/phoneConfig' });
  let receipt;
  try {
    const carriers = await getCarrierConfigs(session.api);
    const target = exactCarrier(carriers, targetName);
    const queried = phones.length
      ? await listPhonesBatched(session.api, phones)
      : { rows: [] };
    const grouped = new Map();
    for (const row of queried.rows) {
      const key = backendPhone(row);
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(row);
    }

    const atTarget = [];
    const stillNotTarget = [];
    const missingPhones = [];
    const duplicateBackend = [];
    for (const phone of phones) {
      const hits = grouped.get(shortCardNo(phone)) || [];
      if (!hits.length) {
        missingPhones.push(phone);
      } else if (hits.length !== 1) {
        duplicateBackend.push({ phone, rows: hits.map(compactPhoneRow) });
      } else if (carrierIdentif(hits[0]) === target.identif) {
        atTarget.push(phone);
      } else {
        stillNotTarget.push({ phone, row: compactPhoneRow(hits[0]) });
      }
    }

    receipt = {
      checkedAt: new Date().toISOString(),
      mode: 'protocol-api-readonly-verify',
      apiTransport: session.apiTransport,
      source: path.resolve(candidatePath),
      target,
      total: phones.length,
      targetCount: atTarget.length,
      stillNotTargetCount: stillNotTarget.length,
      missingCount: missingPhones.length,
      duplicateBackendCount: duplicateBackend.length,
      stillNotTarget,
      missingPhones,
      duplicateBackend,
      status: 'checked',
      verificationStatus: 'performed',
      independentReadbackRequired: true,
      independentReadbackPerformed: true,
      passed: atTarget.length === phones.length
        && !stillNotTarget.length
        && !missingPhones.length
        && !duplicateBackend.length,
    };
  } finally {
    await session.browser.close().catch(() => {});
  }

  await saveReceipt(receipt, label);
  if (!receipt.passed) process.exitCode = 1;
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
