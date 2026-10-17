import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { mapReadOnlyBounded } from './opensms_readonly_concurrency.mjs';
import { getCarrierConfigsReadOnly } from './opensms_carriers_readonly.mjs';

import {
  compactPhoneRow,
  createOpenSmsApiSession,
  listPhonesByNumbers,
  shortCardNo,
} from '../opensms_protocol_client.mjs';

const BATCH_SIZE = 500;

const text = value => String(value ?? '').trim();
const carrierName = row => text(row.name || row.carrierName || row.carrireName || row.carrier);
const carrierConfigIdentif = row => text(row.carrireIdentif || row.carrierIdentif || row.identif || row.id);
// Phone-row `id` is the phone record id, not a carrier identifier.  Falling
// back to it makes an explicitly unbound phone look like an unknown carrier.
const phoneCarrierIdentif = row => text(row.carrireIdentif || row.carrierIdentif || row.identif);
const backendPhone = row => shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
const isTaskBPriorityRoute = route => /^task_b_(?:expired_to_offline|tomorrow_fast_to_isolation)(?:_|$)/.test(text(route));
const isTaskCSwapRoute = route => /^task_c_.*_to_swap$/.test(text(route));

async function readCandidate(candidatePath) {
  const payload = JSON.parse(await fs.readFile(candidatePath, 'utf8'));
  return {
    payload,
    rows: payload.rows || payload.candidates || [],
  };
}

export async function listPhonesBatched(api, phones, { concurrency = 4 } = {}) {
  const batches = [];
  for (let index = 0; index < phones.length; index += BATCH_SIZE) {
    batches.push(phones.slice(index, index + BATCH_SIZE));
  }
  const results = await mapReadOnlyBounded(batches,
    batch => listPhonesByNumbers(api, batch, BATCH_SIZE), { concurrency });
  return results.flatMap(result => result.rows);
}

async function main() {
  const [specPath, outputPath] = process.argv.slice(2);
  if (!specPath || !outputPath) {
    throw new Error('usage: node opensms_collect_final_carrier_state.mjs SPEC.json RECEIPT.json');
  }
  const spec = JSON.parse(await fs.readFile(specPath, 'utf8'));
  const expected = new Map();
  let supersededExpectedRuleCount = 0;

  for (const [groupIndex, group] of (spec.groups || []).entries()) {
    const mode = text(group.mode || 'expected');
    if (!['expected', 'live'].includes(mode)) {
      throw new Error(`最终状态组模式无效: index=${groupIndex}, mode=${mode}`);
    }
    let candidatePayload = {};
    let candidateRows = [];
    if (group.candidate) {
      const loaded = await readCandidate(path.resolve(group.candidate));
      candidatePayload = loaded.payload;
      candidateRows = loaded.rows;
    } else if (Array.isArray(group.phones)) {
      candidateRows = group.phones.map(phone => ({ phone }));
    } else {
      throw new Error(`最终状态组缺少candidate或phones: index=${groupIndex}`);
    }
    const target = text(group.target || candidatePayload.targetCarrierName);
    if (mode === 'expected' && !target) {
      throw new Error(`expected模式缺少目标运营商: index=${groupIndex}`);
    }
    for (const row of candidateRows) {
      const phone = text(row.phone || row['电话']);
      if (!phone) continue;
      const key = shortCardNo(phone);
      const prior = expected.get(key);
      const item = {
        phone,
        stateMode: mode,
        expectedTarget: mode === 'expected' ? target : '',
        sourceCandidate: group.candidate ? path.resolve(group.candidate) : '',
        sourceReceipt: group.sourceReceipt ? path.resolve(group.sourceReceipt) : '',
        route: text(candidatePayload.route || row.route || group.route),
      };
      if (prior) {
        const sameRule = prior.stateMode === item.stateMode
          && prior.expectedTarget === item.expectedTarget;
        if (sameRule) continue;
        // A phone can be an expected, independently verified write in one
        // route and a non-source/live-only observation in a later route.  The
        // expected target is authoritative; the live-only duplicate must not
        // create a false conflict.  In a declared sequential stage pipeline,
        // a later independently verified write intentionally supersedes the
        // earlier target.  Without that explicit declaration, conflicting
        // expected targets remain a hard failure.
        if (prior.stateMode === 'expected' && item.stateMode === 'live') continue;
        if (prior.stateMode === 'live' && item.stateMode === 'expected') {
          expected.set(key, item);
          continue;
        }
        if (prior.stateMode === 'live' && item.stateMode === 'live') continue;
        if (
          prior.stateMode === 'expected'
          && item.stateMode === 'expected'
          && spec.orderedStageSupersession === true
        ) {
          if (isTaskBPriorityRoute(prior.route) && isTaskCSwapRoute(item.route)) {
            throw new Error(`任务B过期/明日到期结果禁止被任务C覆盖: ${phone}`);
          }
          item.supersededRules = [
            ...(prior.supersededRules || []),
            {
              expectedTarget: prior.expectedTarget,
              route: prior.route,
              sourceCandidate: prior.sourceCandidate,
            },
          ];
          expected.set(key, item);
          supersededExpectedRuleCount += 1;
          continue;
        }
        throw new Error(`同一号码出现冲突最终状态规则: ${phone}`);
      }
      expected.set(key, item);
    }
  }

  const session = await createOpenSmsApiSession({
    cdp: process.env.BITBROWSER_CDP,
    pathName: '/phoneConfig',
  });
  let receipt;
  try {
    const readStartedMs = Date.now();
    // No write runs in this stage. Both independent reads finish before any
    // workbook is refreshed; each phone batch remains at most 500 numbers.
    const reads = await Promise.allSettled([
      getCarrierConfigsReadOnly(session.api, { concurrency: 4 }),
      listPhonesBatched(session.api, [...expected.values()].map(item => item.phone)),
    ]);
    const failed = reads.find(item => item.status === 'rejected');
    if (failed) throw failed.reason;
    const [carriers, queried] = reads.map(item => item.value);
    const carrierByIdentif = new Map(carriers.map(row => [carrierConfigIdentif(row), carrierName(row)]));
    const grouped = new Map();
    for (const row of queried) {
      const key = backendPhone(row);
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(row);
    }
    const rows = [];
    for (const [key, item] of expected) {
      const hits = grouped.get(key) || [];
      if (hits.length !== 1) {
        rows.push({
          ...item,
          backendMatchCount: hits.length,
          backendId: null,
          liveCarrierName: '',
          liveCarrierIdentif: '',
          passed: false,
          backendRows: hits.map(compactPhoneRow),
        });
        continue;
      }
      const liveCarrierIdentif = phoneCarrierIdentif(hits[0]);
      const liveCarrierName = carrierByIdentif.get(liveCarrierIdentif) || carrierName(hits[0]);
      const liveCarrierUnassigned = !liveCarrierIdentif && !liveCarrierName;
      const passed = item.stateMode === 'live'
        ? Boolean(liveCarrierName) || liveCarrierUnassigned
        : Boolean(liveCarrierName) && liveCarrierName === item.expectedTarget;
      rows.push({
        ...item,
        backendMatchCount: 1,
        backendId: hits[0].id,
        liveCarrierName,
        liveCarrierIdentif,
        liveCarrierUnassigned,
        passed,
        backendRow: compactPhoneRow(hits[0]),
      });
    }
    receipt = {
      checkedAt: new Date().toISOString(),
      mode: 'protocol-api-readonly-final-carrier-state',
      apiTransport: session.apiTransport,
      silentBrowserMode: true,
      browserForegroundActivationAttempted: false,
      sourceSpec: path.resolve(specPath),
      candidateGroupCount: (spec.groups || []).length,
      candidateCount: expected.size,
      backendValidCount: rows.filter(row => row.backendMatchCount === 1).length,
      targetMatchCount: rows.filter(row => row.passed).length,
      unassignedCarrierCount: rows.filter(row => row.liveCarrierUnassigned).length,
      failureCount: rows.filter(row => !row.passed).length,
      orderedStageSupersession: spec.orderedStageSupersession === true,
      supersededExpectedRuleCount,
      readOnlyConcurrency: { carrierPages: 4, phoneBatches: 4, maximumCombinedRequests: 8 },
      readDurationMs: Date.now() - readStartedMs,
      passed: rows.every(row => row.passed),
      rows,
    };
  } finally {
    await session.browser.close().catch(() => {});
  }
  await fs.mkdir(path.dirname(path.resolve(outputPath)), { recursive: true });
  await fs.writeFile(outputPath, `${JSON.stringify(receipt, null, 2)}\n`);
  console.log(JSON.stringify({ path: path.resolve(outputPath), ...receipt }, null, 2));
  if (!receipt.passed) process.exitCode = 2;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => {
    console.error(error.stack || String(error));
    process.exitCode = 1;
  });
}
