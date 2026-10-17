import fs from 'node:fs/promises';
import path from 'node:path';

import {
  createOpenSmsApiSession,
  getCarrierConfigs,
} from '../opensms_protocol_client.mjs';
import {
  SNAPSHOT_DIR,
  ensureArtifactDirs,
} from '../opensms_artifact_paths.mjs';

const REQUIRED_GROUPS = [
  'A1', 'A11', 'A12', 'A3', 'A21',
  'B1', 'B11', 'B12', 'B1V', 'B11V', 'B12V',
  'C2', 'E-T', 'E-C',
];

function carrierName(row) {
  return String(row.name || row.carrierName || row.carrireName || row.carrier || '').trim();
}

function carrierIdentif(row) {
  return String(row.carrireIdentif || row.carrierIdentif || row.identif || row.id || '').trim();
}

function groupMatches(name, group) {
  return name === group
    || name.startsWith(`${group}-`)
    || name.startsWith(`${group} `)
    || ((group === 'E-T' || group === 'E-C') && name === `${group}休息区`);
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
  await ensureArtifactDirs();
  const session = await createOpenSmsApiSession({ pathName: '/phoneConfig' });
  try {
    const carriers = await getCarrierConfigs(session.api);
    const resolved = {};
    const errors = [];
    for (const group of REQUIRED_GROUPS) {
      const matches = carriers.filter(row => groupMatches(carrierName(row), group));
      if (matches.length !== 1) {
        errors.push({ group, matchCount: matches.length, matches: matches.map(carrierName) });
        continue;
      }
      const row = matches[0];
      const identif = carrierIdentif(row);
      if (!identif) {
        errors.push({ group, matchCount: 1, error: 'missing_identif', matches: [carrierName(row)] });
        continue;
      }
      resolved[group] = { name: carrierName(row), identif, id: row.id ?? null };
    }
    const receipt = {
      createdAt: new Date().toISOString(),
      mode: 'protocol-api-read-only',
      apiTransport: session.apiTransport,
      requiredGroups: REQUIRED_GROUPS,
      resolved,
      errors,
      success: errors.length === 0 && Object.keys(resolved).length === REQUIRED_GROUPS.length,
    };
    const receiptPath = path.join(SNAPSHOT_DIR, `opensms_resolved_active_carriers_${stamp()}+0800.json`);
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
