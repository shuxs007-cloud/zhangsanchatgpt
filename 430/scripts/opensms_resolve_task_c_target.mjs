import fs from 'node:fs/promises';
import path from 'node:path';

import {
  createOpenSmsApiSession,
} from '../opensms_protocol_client.mjs';
import { getCarrierConfigsReadOnly } from './opensms_carriers_readonly.mjs';
import {
  SNAPSHOT_DIR,
  ensureArtifactDirs,
} from '../opensms_artifact_paths.mjs';

const TASK_C_TARGETS = {
  C: '换号区C',
  T: '换号区T',
};

function carrierName(row) {
  return String(row.name || row.carrierName || row.carrireName || row.carrier || '').trim();
}

function carrierIdentif(row) {
  return String(row.carrireIdentif || row.carrierIdentif || row.identif || row.id || '').trim();
}

function stamp() {
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Hong_Kong',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date()).replace(/[-: ]/g, '');
}

async function main() {
  if (process.env.OPENSMS_FORCE_TASK_C !== '1') {
    throw new Error('OPENSMS_FORCE_TASK_C 未严格等于 1，拒绝解析任务C目标');
  }
  await ensureArtifactDirs();
  const session = await createOpenSmsApiSession({ pathName: '/phoneConfig' });
  try {
    const carriers = await getCarrierConfigsReadOnly(session.api, { concurrency: 4 });
    const resolved = {};
    const matchCounts = {};
    const errors = [];
    for (const [group, requestedName] of Object.entries(TASK_C_TARGETS)) {
      const matches = carriers.filter(row => carrierName(row) === requestedName);
      matchCounts[group] = matches.length;
      if (matches.length !== 1) {
        errors.push(`${requestedName}精确匹配数量不是1: ${matches.length}`);
        continue;
      }
      const row = matches[0];
      const identif = carrierIdentif(row);
      if (!identif) {
        errors.push(`${requestedName}缺少运营商ID`);
        continue;
      }
      if (row.delData === true) {
        errors.push(`${requestedName}已停用或删除`);
        continue;
      }
      resolved[group] = { name: carrierName(row), identif, id: row.id ?? null };
    }
    const relatedTargets = carriers
      .filter(row => carrierName(row).startsWith('换号区'))
      .map(row => ({
        name: carrierName(row),
        identif: carrierIdentif(row),
        id: row.id ?? null,
        deleted: row.delData === true,
      }));
    const success = errors.length === 0 && Object.keys(resolved).length === 2;
    const receipt = {
      createdAt: new Date().toISOString(),
      task: 'C',
      forceGate: true,
      mode: 'protocol-api-read-only',
      apiTransport: session.apiTransport,
      readOnlyPageConcurrency: 4,
      requestedExactNames: TASK_C_TARGETS,
      matchCounts,
      relatedTargets,
      targets: success ? resolved : null,
      errors,
      success,
    };
    const receiptPath = path.join(
      SNAPSHOT_DIR,
      `opensms_task_c_resolved_target_${stamp()}+0800.json`,
    );
    await fs.writeFile(receiptPath, JSON.stringify(receipt, null, 2));
    console.log(JSON.stringify({ path: receiptPath, ...receipt }, null, 2));
    if (!receipt.success) process.exitCode = 2;
  } finally {
    await session.browser.close().catch(() => {});
  }
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
