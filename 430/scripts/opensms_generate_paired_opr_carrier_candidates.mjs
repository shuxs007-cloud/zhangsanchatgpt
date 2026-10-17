import fs from 'node:fs/promises';
import path from 'node:path';

import {
  createOpenSmsApiSession,
  getCarrierConfigs,
  shortCardNo,
} from '../opensms_protocol_client.mjs';
import {
  SNAPSHOT_DIR,
  ensureArtifactDirs,
} from '../opensms_artifact_paths.mjs';

const PAGE_SIZE = 500;
const PAIRS = [
  {
    key: 'expired',
    label: '过期下架卡',
    c: '过期下架卡C',
    t: '过期下架卡T',
  },
  {
    key: 'swap',
    label: '换号区',
    c: '换号区C',
    t: '换号区T',
  },
  {
    key: 'fast_expiry',
    label: '快过期隔离区',
    c: '快过期隔离区C',
    t: '快过期隔离区T',
  },
];

const CHATR_PATTERN = /chatr/i;
const T_FAMILY_PATTERN = /kooda|koodo|kini|telus1/i;

function stamp() {
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date()).replace(/[-: ]/g, '');
}

function carrierName(row) {
  return String(row.name || row.carrierName || row.carrireName || row.carrier || '').trim();
}

function carrierIdentif(row) {
  return String(row.identif || row.carrireIdentif || row.carrierIdentif || row.id || '').trim();
}

function exactCarrier(carriers, requestedName) {
  const matches = carriers.filter(row => carrierName(row) === requestedName);
  if (matches.length !== 1) {
    throw new Error(`${requestedName}唯一精确匹配数量不是1: ${matches.length}`);
  }
  const row = matches[0];
  if (row.delData === true) throw new Error(`${requestedName}已停用或删除`);
  const identif = carrierIdentif(row);
  if (!identif) throw new Error(`${requestedName}缺少运营商ID`);
  return { name: requestedName, identif, id: row.id ?? null };
}

function displayPhone(row) {
  const value = shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
  return value.length === 10 ? `1${value}` : value;
}

function classifyOpr(opr) {
  const value = String(opr || '').trim();
  const chatr = CHATR_PATTERN.test(value);
  const tFamily = T_FAMILY_PATTERN.test(value);
  if (chatr && tFamily) return 'ambiguous';
  if (chatr) return 'C';
  if (tFamily) return 'T';
  return 'unclassified';
}

async function listPhonesByCarrier(api, carrier) {
  const rows = [];
  let reportedTotal = null;
  for (let pageNum = 1; pageNum <= 100; pageNum += 1) {
    const query = {
      carrireIdentif: carrier.identif,
      pageNum,
      pageSize: PAGE_SIZE,
    };
    const response = await api('POST', '/phone/config/list', query, {
      pageNum,
      pageSize: PAGE_SIZE,
    });
    if (Number(response.code) !== 200) {
      throw new Error(`查询${carrier.name}号码失败: ${JSON.stringify(response)}`);
    }
    const pageRows = response.rows || [];
    const mismatched = pageRows.filter(row => carrierIdentif(row) !== carrier.identif);
    if (mismatched.length) {
      throw new Error(`${carrier.name}查询结果中有${mismatched.length}条运营商ID不一致`);
    }
    if (reportedTotal === null) reportedTotal = Number(response.total || 0);
    rows.push(...pageRows.map(row => ({ ...row, __sourceCarrierName: carrier.name })));
    if (!pageRows.length || rows.length >= reportedTotal || pageRows.length < PAGE_SIZE) break;
  }
  if (rows.length !== reportedTotal) {
    throw new Error(`${carrier.name}分页结果不完整: expected=${reportedTotal}, received=${rows.length}`);
  }
  return { rows, reportedTotal: reportedTotal ?? rows.length };
}

function candidatePayload(pair, target, sources, rows, runStamp) {
  const targetSide = target.name === pair.c ? 'C' : 'T';
  return {
    createdAt: new Date().toISOString(),
    runStamp,
    mode: 'protocol-api-read-only-candidate-generation',
    apiEndpoint: '/api/phone/config/list',
    ruleVersion: 'paired-opr-carrier-normalization-v1',
    route: `paired_opr_${pair.key}_to_${targetSide.toLowerCase()}`,
    rule: `${pair.c}/${pair.t}内Chatr归${pair.c}，Kooda/T系归${pair.t}`,
    oprClassification: {
      chatrPattern: 'chatr (case-insensitive)',
      tFamilyPattern: 'kooda|koodo|kini|telus1 (case-insensitive)',
      ambiguousPolicy: 'hold_no_write',
      unclassifiedPolicy: 'hold_no_write',
    },
    targetCarrierName: target.name,
    targetCarrierIdentif: target.identif,
    sourceCarrierNames: sources.map(item => item.name),
    sourceCarrierIdentifs: sources.map(item => item.identif),
    count: rows.length,
    rows,
    candidates: rows,
  };
}

async function main() {
  await ensureArtifactDirs();
  const runStamp = `${stamp()}+0800`;
  const session = await createOpenSmsApiSession({ pathName: '/phoneConfig' });
  try {
    const carrierRows = await getCarrierConfigs(session.api);
    const resolved = new Map();
    for (const pair of PAIRS) {
      for (const requestedName of [pair.c, pair.t]) {
        resolved.set(requestedName, exactCarrier(carrierRows, requestedName));
      }
    }

    const manifestPairs = [];
    const outputPaths = [];
    for (const pair of PAIRS) {
      const cCarrier = resolved.get(pair.c);
      const tCarrier = resolved.get(pair.t);
      const sources = [cCarrier, tCarrier];
      const sourceResults = [];
      for (const source of sources) {
        sourceResults.push({ source, ...(await listPhonesByCarrier(session.api, source)) });
      }

      const grouped = new Map();
      const missingPhoneRows = [];
      for (const result of sourceResults) {
        for (const row of result.rows) {
          const phone = displayPhone(row);
          if (!phone) {
            missingPhoneRows.push({
              backendId: row.id ?? null,
              sourceCarrierName: row.__sourceCarrierName,
              opr: String(row.opr || ''),
              slot: String(row.slot || row.cardSlot || row.slotIdentif || ''),
            });
            continue;
          }
          if (!grouped.has(phone)) grouped.set(phone, []);
          grouped.get(phone).push(row);
        }
      }

      const toC = [];
      const toT = [];
      const alreadyCorrect = { C: 0, T: 0 };
      const held = {
        missingPhone: missingPhoneRows,
        duplicate: [],
        ambiguous: [],
        unclassified: [],
      };
      for (const [phone, hits] of grouped.entries()) {
        if (hits.length !== 1) {
          held.duplicate.push({ phone, count: hits.length });
          continue;
        }
        const row = hits[0];
        const sourceCarrierName = row.__sourceCarrierName;
        const family = classifyOpr(row.opr);
        if (family === 'ambiguous') {
          held.ambiguous.push({ phone, sourceCarrierName, opr: row.opr || '' });
          continue;
        }
        if (family === 'unclassified') {
          held.unclassified.push({ phone, sourceCarrierName, opr: row.opr || '' });
          continue;
        }
        const targetCarrierName = family === 'C' ? pair.c : pair.t;
        if (sourceCarrierName === targetCarrierName) {
          alreadyCorrect[family] += 1;
          continue;
        }
        const candidateRow = {
          电话: phone,
          运营商: sourceCarrierName,
          原运营商: sourceCarrierName,
          opr: String(row.opr || ''),
          卡槽: String(row.slot || row.cardSlot || row.slotIdentif || ''),
          后台ID: row.id ?? null,
          OPR分类: family === 'C' ? 'Chatr' : 'Kooda/T系',
          targetCarrierName,
          route: `paired_opr_${pair.key}_to_${family.toLowerCase()}`,
        };
        (family === 'C' ? toC : toT).push(candidateRow);
      }

      toC.sort((a, b) => a.电话.localeCompare(b.电话));
      toT.sort((a, b) => a.电话.localeCompare(b.电话));
      const cPayload = candidatePayload(pair, cCarrier, sources, toC, runStamp);
      const tPayload = candidatePayload(pair, tCarrier, sources, toT, runStamp);
      const cPath = path.join(SNAPSHOT_DIR, `opensms_paired_opr_${pair.key}_to_c_${runStamp}_candidates.json`);
      const tPath = path.join(SNAPSHOT_DIR, `opensms_paired_opr_${pair.key}_to_t_${runStamp}_candidates.json`);
      await fs.writeFile(cPath, JSON.stringify(cPayload, null, 2));
      await fs.writeFile(tPath, JSON.stringify(tPayload, null, 2));
      outputPaths.push(cPath, tPath);
      manifestPairs.push({
        key: pair.key,
        label: pair.label,
        carriers: { C: cCarrier, T: tCarrier },
        sourceQueries: sourceResults.map(item => ({
          carrierName: item.source.name,
          carrierIdentif: item.source.identif,
          reportedTotal: item.reportedTotal,
          receivedRows: item.rows.length,
        })),
        uniquePhoneCount: grouped.size,
        alreadyCorrect,
        heldCounts: {
          missingPhone: held.missingPhone.length,
          duplicate: held.duplicate.length,
          ambiguous: held.ambiguous.length,
          unclassified: held.unclassified.length,
        },
        held,
        toC: { path: cPath, count: toC.length },
        toT: { path: tPath, count: toT.length },
      });
    }

    const manifest = {
      createdAt: new Date().toISOString(),
      runStamp,
      mode: 'protocol-api-read-only-candidate-generation',
      apiTransport: session.apiTransport,
      silentBrowserMode: true,
      browserForegroundActivationAttempted: false,
      ruleVersion: 'paired-opr-carrier-normalization-v1',
      pairs: manifestPairs,
      candidatePaths: outputPaths,
      totalCandidates: manifestPairs.reduce(
        (sum, item) => sum + item.toC.count + item.toT.count,
        0,
      ),
    };
    const manifestPath = path.join(
      SNAPSHOT_DIR,
      `opensms_paired_opr_normalization_${runStamp}_manifest.json`,
    );
    await fs.writeFile(manifestPath, JSON.stringify(manifest, null, 2));
    console.log(JSON.stringify({ path: manifestPath, ...manifest }, null, 2));
  } finally {
    await session.browser.close().catch(() => {});
  }
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
