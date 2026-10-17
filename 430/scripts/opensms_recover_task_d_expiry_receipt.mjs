#!/usr/bin/env node
// Read-only recovery for the 2026-09-01 legacy Task D breakpoint.  The legacy
// executor wrote a successful transactional response to its progress file and
// then overwrote that pending payload when the invalid cardRetain readback
// failed.  This tool never calls batchSetChatExpire.  It reconstructs only the
// already-gated counts encoded by that failure path and revalidates every card
// against the current phone backend before emitting a recovery receipt.

import fs from 'node:fs/promises';
import path from 'node:path';
import {
  createRawOpenSmsApiSession,
  getCarrierConfigs,
  listPhonesByNumbers,
  shortCardNo,
} from '../opensms_protocol_client.mjs';

const PHONE_BATCH_SIZE = 500;
const SWAP_CARRIERS = new Set(['换号区C', '换号区T']);

function carrierName(row) {
  return String(row.name || row.carrierName || row.carrireName || row.carrier || '').trim();
}

function carrierIdentif(row) {
  return String(row.carrireIdentif || row.carrierIdentif || row.identif || row.id || '').trim();
}

async function writeAtomic(file, payload) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  const temp = `${file}.tmp-${process.pid}`;
  await fs.writeFile(temp, `${JSON.stringify(payload, null, 2)}\n`);
  await fs.rename(temp, file);
}

async function listPhonesBatched(api, cards) {
  const chunks = [];
  for (let index = 0; index < cards.length; index += PHONE_BATCH_SIZE) {
    chunks.push(cards.slice(index, index + PHONE_BATCH_SIZE));
  }
  const results = await Promise.all(chunks.map(chunk => listPhonesByNumbers(api, chunk, PHONE_BATCH_SIZE)));
  return results.flatMap(item => item.rows || []);
}

function uniqueCards(manifest) {
  const cards = (manifest.cardNos || []).map(shortCardNo).filter(Boolean);
  if (new Set(cards).size !== cards.length || Number(manifest.count) !== cards.length) {
    throw new Error('恢复清单卡号重复或计数不一致');
  }
  return cards;
}

async function main() {
  const [manifestArg, progressArg, outputArg] = process.argv.slice(2);
  if (!manifestArg || !progressArg || !outputArg) {
    throw new Error('usage: opensms_recover_task_d_expiry_receipt.mjs manifest progress output');
  }
  if (process.env.OPENSMS_FORCE_TASK_D !== '1' || process.env.OPENSMS_ENABLE_TASK_D !== '1') {
    throw new Error('任务D当前用户授权门禁未开启，拒绝恢复回执');
  }
  const manifestPath = path.resolve(manifestArg);
  const progressPath = path.resolve(progressArg);
  const outputPath = path.resolve(outputArg);
  const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
  const progress = JSON.parse(await fs.readFile(progressPath, 'utf8'));
  if (
    manifest.task !== 'D'
    || manifest.phase !== 'swap_zero'
    || manifest.forceGate !== true
    || manifest.automationGate !== true
  ) {
    throw new Error('仅允许恢复已授权的任务D换号区断点');
  }
  if (progress.status !== 'failed') throw new Error(`断点状态不是failed: ${progress.status}`);
  const match = /missingCards=(\d+), newLogs=(\d+), updatedChats=(\d+)/.exec(String(progress.error || ''));
  if (!match) throw new Error('断点没有旧版无效cardRetain回读错误证据');
  const cards = uniqueCards(manifest);
  const missingCards = Number(match[1]);
  const newLogs = Number(match[2]);
  const updatedChatCount = Number(match[3]);
  if (missingCards !== cards.length || newLogs !== 0 || updatedChatCount < cards.length) {
    throw new Error('断点计数不符合可恢复的旧版控制流');
  }

  const session = await createRawOpenSmsApiSession({ cdp: process.env.BITBROWSER_CDP, pathName: '/phoneConfig' });
  try {
    const [carriers, rows] = await Promise.all([
      getCarrierConfigs(session.api),
      listPhonesBatched(session.api, cards),
    ]);
    const nameByIdentif = new Map(carriers.map(row => [carrierIdentif(row), carrierName(row)]));
    const grouped = new Map();
    for (const row of rows) {
      const card = shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
      if (!grouped.has(card)) grouped.set(card, []);
      grouped.get(card).push(row);
    }
    const invalid = [];
    for (const card of cards) {
      const hits = grouped.get(card) || [];
      const liveCarrier = hits.length === 1
        ? (nameByIdentif.get(carrierIdentif(hits[0])) || carrierName(hits[0]))
        : '';
      if (hits.length !== 1 || !SWAP_CARRIERS.has(liveCarrier)) {
        invalid.push({ card, backendCount: hits.length, liveCarrier });
      }
    }
    if (invalid.length) {
      throw new Error(`恢复前后台候选复核失败${invalid.length}条: ${JSON.stringify(invalid.slice(0, 20))}`);
    }

    const batch = {
      batchNumber: 1,
      inputCount: cards.length,
      matchedCardCount: cards.length,
      updatedChatCount,
      notFound: [],
      businessReceiptPassed: true,
      transactionalVerificationPassed: true,
      independentReadbackPassed: false,
      recoveredFromLegacyControlFlow: true,
    };
    const receipt = {
      createdAt: new Date().toISOString(),
      task: 'D',
      phase: manifest.phase,
      route: manifest.route,
      manifestPath,
      legacyProgressPath: progressPath,
      serverDate: manifest.serverDate,
      expireDate: manifest.expireDate,
      candidateCount: cards.length,
      markerBatchSize: 1500,
      status: 'recovered_transactional_receipt',
      verificationMode: 'synchronous_transactional_business_receipt',
      independentReadbackAvailable: false,
      independentReadbackPassed: false,
      independentReadbackUnavailableReason: 'batchSetChatExpire未提供查询接口；旧程序错误使用cardRetain来源日志并覆盖了pending响应',
      recoveryEvidence: {
        legacyError: progress.error,
        controlFlowProof: '旧程序只有在inputCount、matchedCardCount、updatedChatCount和notFound全部通过后才进入该错误分支',
        currentBackendCandidateCount: cards.length,
        currentBackendAllStillInSwapZone: true,
        writeEndpointReplayed: false,
      },
      batches: [batch],
      inputCount: cards.length,
      matchedCardCount: cards.length,
      updatedChatCount,
      businessReceiptPassed: true,
      transactionalVerificationPassed: true,
    };
    await writeAtomic(outputPath, receipt);
    console.log(JSON.stringify({ ...receipt, path: outputPath }, null, 2));
  } finally {
    session.client.close();
  }
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
