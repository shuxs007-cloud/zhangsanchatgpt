#!/usr/bin/env node
// Task E batches workbook-derived expiry markers by exact deadline and keeps a
// persistent phone-only ledger. Live writes remain serial and receipt-gated.

import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { validateExpiryReceipt } from './opensms_expiry_receipt.mjs';
import { createExpirySubmitter, expiryIdentitySnapshot, verifyExpiryRetryIdentity, writeExpiryEvidence } from './opensms_expiry_submission.mjs';
import { readPlatformDate } from './opensms_expired_card_probe.mjs';
import {
  createRawOpenSmsApiSession,
  getCarrierConfigs,
  listPhonesByNumbers,
  shortCardNo,
} from '../opensms_protocol_client.mjs';

// A single phone can fan out to many chat rows. A 499-phone date group caused
// nginx 504 after the request was accepted, leaving the outcome unknowable.
// Keep writes small enough to finish inside the gateway window.
const MARKER_BATCH_SIZE = 20;
const PHONE_BATCH_SIZE = 500;
const LEDGER_SCHEMA_VERSION = 1;
const ELIGIBLE_STATUSES = new Set(['6天', '快过期', '已过期']);
const NEVER_PROCESS_CARRIERS = new Set(['快过期续费C', '快过期续费T']);

function carrierName(row) {
  return String(row.name || row.carrierName || row.carrireName || row.carrier || '').trim();
}

function carrierIdentif(row) {
  return String(row.carrireIdentif || row.carrierIdentif || row.identif || row.id || '').trim();
}

function assertApi(label, response) {
  const code = Number(response?.code ?? 200);
  if (code !== 200) throw new Error(`${label}失败: ${JSON.stringify(response)}`);
}

function validDate(value) {
  const text = String(value || '');
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (!match) return false;
  const parsed = new Date(`${text}T00:00:00Z`);
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === text;
}

async function sha256(file) {
  const data = await fs.readFile(file);
  return crypto.createHash('sha256').update(data).digest('hex');
}

async function writeAtomic(file, payload) {
  await writeExpiryEvidence(file, payload);
}

async function readLedger(file) {
  try {
    const payload = JSON.parse(await fs.readFile(file, 'utf8'));
    if (payload.schemaVersion !== LEDGER_SCHEMA_VERSION || payload.dedupeKey !== 'phone') {
      throw new Error('任务E去重台账版本或去重键无效');
    }
    if (!payload.submissions || typeof payload.submissions !== 'object' || Array.isArray(payload.submissions)) {
      throw new Error('任务E去重台账submissions必须为对象');
    }
    for (const [key, value] of Object.entries(payload.submissions)) {
      const phone = shortCardNo(key);
      if (!phone || phone !== shortCardNo(value?.phone)) {
        throw new Error(`任务E去重台账号码键无效: ${key}`);
      }
    }
    return payload;
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
    return { schemaVersion: LEDGER_SCHEMA_VERSION, dedupeKey: 'phone', submissions: {} };
  }
}

async function loadRecoveryProof(file, manifestGroups) {
  if (!file) return { path: null, cardDates: new Map(), sourceRunId: null };
  const recoveryPath = path.resolve(file);
  const payload = JSON.parse(await fs.readFile(recoveryPath, 'utf8'));
  const pending = payload.pendingBatch || {};
  const errorText = String(payload.error || '');
  const pendingCards = Array.isArray(pending.cardNos) ? pending.cardNos.map(shortCardNo).filter(Boolean) : [];
  const responseData = pending.response?.data || {};
  const responseNotFound = Array.isArray(responseData.notFound)
    ? responseData.notFound.map(shortCardNo).filter(Boolean)
    : [];
  const isUncertain = payload.uncertainSubmission === true
    || (payload.status === 'failed' && /HTTP\s+5\d\d|gateway|time[- ]?out|timeout|ECONNRESET|socket hang up/i.test(errorText));
  const isReconciledPartialReceipt = payload.status === 'failed'
    && /业务回执不完整/.test(errorText)
    && Number(responseData.inputCount) === pendingCards.length
    && Number(responseData.matchedCardCount) + responseNotFound.length === pendingCards.length
    && responseNotFound.every(card => pendingCards.includes(card))
    && Number(responseData.updatedChatCount) >= 0;
  if ((!isUncertain && !isReconciledPartialReceipt) || !validDate(pending.expireDate) || !pendingCards.length) {
    throw new Error('任务E恢复断点不是有效的不确定提交或已核对部分业务回执');
  }
  const manifestDateByCard = new Map();
  for (const group of manifestGroups) {
    for (const card of group.cards) manifestDateByCard.set(card, group.expireDate);
  }
  if (new Set(pendingCards).size !== pendingCards.length) throw new Error('任务E恢复断点号码重复');
  for (const card of pendingCards) {
    if (manifestDateByCard.get(card) !== pending.expireDate) {
      throw new Error(`任务E恢复断点号码不在本轮同截止日新鲜候选中: ${card}`);
    }
  }
  const cardDates = new Map();
  const recoverableCards = isReconciledPartialReceipt
    ? pendingCards.filter(card => !responseNotFound.includes(card))
    : pendingCards;
  for (const card of recoverableCards) {
    cardDates.set(card, pending.expireDate);
  }
  return {
    path: recoveryPath,
    cardDates,
    sourceRunId: String(payload.runId || payload.label || ''),
    sha256: await sha256(recoveryPath),
  };
}

async function loadResumeProgress(file, manifestPath, sourceSha256, ledgerPath, groups) {
  if (!file) return { path: null, batches: [], cardDates: new Map(), ledgerCardDates: new Map() };
  const resumePath = path.resolve(file);
  const payload = JSON.parse(await fs.readFile(resumePath, 'utf8'));
  if (
    payload.status !== 'failed'
    || payload.uncertainSubmission === true
    || payload.automaticRetryBlocked === true
    || path.resolve(payload.manifestPath || '') !== manifestPath
    || payload.sourceSha256 !== sourceSha256
    || path.resolve(payload.ledgerPath || '') !== ledgerPath
    || !Array.isArray(payload.batches)
  ) {
    throw new Error('任务E续跑断点结构或安全状态无效');
  }
  const manifestDateByCard = new Map();
  for (const group of groups) {
    for (const card of group.cards) manifestDateByCard.set(card, group.expireDate);
  }
  const cardDates = new Map();
  const ledgerCardDates = new Map();
  for (const batch of payload.batches) {
    const cards = (batch.cardNos || []).map(shortCardNo).filter(Boolean);
    const notFound = new Set((batch.notFound || []).map(shortCardNo).filter(Boolean));
    const matchedCards = cards.filter(card => !notFound.has(card));
    if (
      !cards.length
      || Number(batch.inputCount) !== cards.length
      || Number(batch.matchedCardCount) !== matchedCards.length
      || [...notFound].some(card => !cards.includes(card))
      || batch.businessReceiptPassed !== true
      || batch.transactionalVerificationPassed !== true
    ) {
      throw new Error('任务E续跑断点含未确认成功批次');
    }
    for (const card of cards) {
      if (cardDates.has(card) || manifestDateByCard.get(card) !== batch.expireDate) {
        throw new Error(`任务E续跑断点号码重复或截止日不一致: ${card}`);
      }
      cardDates.set(card, batch.expireDate);
      if (!notFound.has(card)) ledgerCardDates.set(card, batch.expireDate);
    }
  }
  return { path: resumePath, batches: payload.batches, cardDates, ledgerCardDates };
}

function validateManifestShape(manifest) {
  if (manifest.task !== 'E' || manifest.forceGate !== true || manifest.automationGate !== true) {
    throw new Error('任务E清单缺少当前用户授权门禁或程序门禁');
  }
  const statuses = new Set(manifest.eligibleExpiryStatuses || []);
  if (statuses.size !== ELIGIBLE_STATUSES.size || [...ELIGIBLE_STATUSES].some(item => !statuses.has(item))) {
    throw new Error('任务E清单过期状态范围不是6天、快过期、已过期');
  }
  if (manifest.ledgerDedupeKey !== 'phone') throw new Error('任务E清单必须按手机号永久去重');
  const owners = new Map();
  const groups = [];
  for (const group of manifest.groups || []) {
    const expireDate = String(group.expireDate || '');
    if (!validDate(expireDate)) throw new Error(`任务E截止日无效: ${expireDate}`);
    const rows = Array.isArray(group.rows) ? group.rows : [];
    const cards = (group.cardNos || []).map(shortCardNo).filter(Boolean);
    if (cards.length !== Number(group.count ?? cards.length) || rows.length !== cards.length) {
      throw new Error(`任务E日期组计数不一致: ${expireDate}`);
    }
    const rowMap = new Map(rows.map(row => [shortCardNo(row['电话']), row]));
    for (const card of cards) {
      if (owners.has(card)) throw new Error(`任务E号码跨日期组重复: ${card}`);
      const row = rowMap.get(card);
      if (!row || row['截止日'] !== expireDate || !ELIGIBLE_STATUSES.has(String(row['过期状态'] || ''))) {
        throw new Error(`任务E号码缺少有效状态或截止日证明: ${card}`);
      }
      if (NEVER_PROCESS_CARRIERS.has(String(row['运营商'] || '').trim())) {
        throw new Error(`任务E候选包含快过期续费C/T保护号码: ${card}`);
      }
      if (!String(row['运营商'] || '').trim()) {
        throw new Error(`任务E候选运营商为空，禁止通知未配置号码: ${card}`);
      }
      owners.set(card, expireDate);
    }
    groups.push({ expireDate, cards });
  }
  if (owners.size !== Number(manifest.candidateCount ?? owners.size)) {
    throw new Error(`任务E候选总数不一致: manifest=${manifest.candidateCount}, cards=${owners.size}`);
  }
  if (groups.length !== Number(manifest.dateGroupCount ?? groups.length)) {
    throw new Error('任务E日期组数量不一致');
  }
  return { groups, cards: [...owners.keys()] };
}

async function listPhonesBatched(api, cards) {
  const chunks = [];
  for (let index = 0; index < cards.length; index += PHONE_BATCH_SIZE) {
    chunks.push(cards.slice(index, index + PHONE_BATCH_SIZE));
  }
  const results = await Promise.all(chunks.map(chunk => listPhonesByNumbers(api, chunk, PHONE_BATCH_SIZE)));
  return results.flatMap(item => item.rows || []);
}

function groupPhoneRows(rows) {
  const grouped = new Map();
  for (const row of rows) {
    const card = shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
    if (!grouped.has(card)) grouped.set(card, []);
    grouped.get(card).push(row);
  }
  return grouped;
}

async function validateLivePhones(api, cards) {
  const [rows, carriers] = await Promise.all([
    listPhonesBatched(api, cards),
    getCarrierConfigs(api),
  ]);
  const nameByIdentif = new Map(carriers.map(row => [carrierIdentif(row), carrierName(row)]));
  const grouped = groupPhoneRows(rows);
  const invalid = [];
  for (const card of cards) {
    const hits = grouped.get(card) || [];
    if (hits.length !== 1) {
      invalid.push({ card, reason: hits.length ? 'duplicate_backend' : 'missing_backend' });
      continue;
    }
    const row = hits[0];
    const liveCarrier = nameByIdentif.get(carrierIdentif(row)) || carrierName(row);
    if (!liveCarrier) {
      invalid.push({ card, reason: 'unconfigured_carrier' });
    }
    if (NEVER_PROCESS_CARRIERS.has(liveCarrier)) {
      invalid.push({ card, reason: 'quick_renew_protected', liveCarrier });
    }
  }
  if (invalid.length) {
    throw new Error(`任务E写前后台独立验证失败${invalid.length}条: ${JSON.stringify(invalid.slice(0, 20))}`);
  }
  return { backendVerifiedCount: cards.length, quickRenewProtectedCount: 0, retryIdentitySnapshot: expiryIdentitySnapshot(rows, cards, carriers) };
}

export async function main({ args = process.argv.slice(2), createSession = createRawOpenSmsApiSession,
  submissionRuntime = {}, readDate = readPlatformDate } = {}) {
  const validateOnly = args.includes('--validate-only');
  const recoveryIndex = args.indexOf('--recovery-progress');
  const recoveryProgressArg = recoveryIndex >= 0 ? args[recoveryIndex + 1] : null;
  if (recoveryIndex >= 0 && !recoveryProgressArg) throw new Error('--recovery-progress缺少路径');
  const resumeIndex = args.indexOf('--resume-progress');
  const resumeProgressArg = resumeIndex >= 0 ? args[resumeIndex + 1] : null;
  if (resumeIndex >= 0 && !resumeProgressArg) throw new Error('--resume-progress缺少路径');
  const positional = args.filter((arg, index) => {
    if (arg === '--validate-only') return false;
    if (recoveryIndex >= 0 && (index === recoveryIndex || index === recoveryIndex + 1)) return false;
    if (resumeIndex >= 0 && (index === resumeIndex || index === resumeIndex + 1)) return false;
    return true;
  });
  const [manifestArg, label = 'task_e_expiry', outputDirArg = '.', ledgerArg] = positional;
  if (!manifestArg || !ledgerArg) {
    throw new Error('usage: opensms_execute_task_e_expiry.mjs manifest [label] [outputDir] ledger [--validate-only]');
  }
  if (process.env.OPENSMS_ENABLE_TASK_E !== '1') {
    throw new Error('OPENSMS_ENABLE_TASK_E 未严格等于 1，拒绝处理任务E');
  }
  if (process.env.OPENSMS_FORCE_TASK_E !== '1') {
    throw new Error('OPENSMS_FORCE_TASK_E 未严格等于 1，缺少当前真实用户授权，拒绝处理任务E');
  }
  if (!validateOnly && process.env.OPENSMS_EXECUTE_TASK_E !== '1') {
    throw new Error('OPENSMS_EXECUTE_TASK_E 未严格等于 1，拒绝写入任务E到期标记');
  }

  const manifestPath = path.resolve(manifestArg);
  const outputDir = path.resolve(outputDirArg);
  const ledgerPath = path.resolve(ledgerArg);
  const receiptPath = path.join(outputDir, `opensms_task_e_${label}_receipt.json`);
  const progressPath = path.join(outputDir, `opensms_task_e_${label}_progress.json`);
  const lockPath = `${ledgerPath}.lock`;
  if (!resumeProgressArg) {
    for (const file of [progressPath, receiptPath]) {
      try { await fs.access(file); } catch (error) {
        if (error.code === 'ENOENT') continue;
        throw error;
      }
      throw new Error('任务E已有断点或回执，禁止自动重复提交；需核对证据和显式恢复授权');
    }
  }
  const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
  const { groups, cards } = validateManifestShape(manifest);
  const recovery = await loadRecoveryProof(recoveryProgressArg, groups);
  const resume = await loadResumeProgress(resumeProgressArg, manifestPath, manifest.sourceSha256, ledgerPath, groups);
  const sourcePath = path.resolve(manifest.sourcePath || '');
  if (!sourcePath || await sha256(sourcePath) !== manifest.sourceSha256) {
    throw new Error('任务E源工作簿SHA-256不一致');
  }
  if (path.resolve(manifest.ledgerPath || '') !== ledgerPath) {
    throw new Error('任务E清单与执行器去重台账路径不一致');
  }
  const initialLedger = await readLedger(ledgerPath);
  const currentLedgerSha = await sha256(ledgerPath).catch(error => error?.code === 'ENOENT' ? null : Promise.reject(error));
  if (!resume.path && (manifest.ledgerSha256Before || null) !== currentLedgerSha) {
    throw new Error('任务E去重台账已变化，必须重新生成候选');
  }
  const repeated = cards.filter(card => Object.hasOwn(initialLedger.submissions, card));
  const unexpectedRepeated = repeated.filter(card => !resume.ledgerCardDates.has(card));
  const missingResumeLedger = [...resume.ledgerCardDates].filter(([card, expireDate]) => {
    const entry = initialLedger.submissions[card];
    return !entry || entry.runId !== label || entry.expireDate !== expireDate;
  });
  if (unexpectedRepeated.length) throw new Error(`任务E候选包含非本断点已提交号码: ${unexpectedRepeated.slice(0, 20)}`);
  if (missingResumeLedger.length) throw new Error(`任务E续跑断点与永久台账不一致: ${missingResumeLedger.slice(0, 20)}`);
  if (resume.path && resume.ledgerCardDates.size && initialLedger.lastRunId !== label) {
    throw new Error('任务E续跑期间永久台账已被其它轮次修改');
  }

  const receipt = {
    createdAt: new Date().toISOString(),
    task: 'E',
    manifestPath,
    sourcePath,
    sourceSha256: manifest.sourceSha256,
    ledgerPath,
    ledgerDedupeKey: 'phone',
    eligibleExpiryStatuses: [...ELIGIBLE_STATUSES],
    candidateCount: cards.length,
    dateGroupCount: groups.length,
    markerBatchSize: MARKER_BATCH_SIZE,
    recoveryProgressPath: recovery.path,
    recoveryProgressSha256: recovery.sha256 || null,
    recoveryCandidateCount: recovery.cardDates.size,
    recoveryRequiresFreshManifestEquality: true,
    resumeProgressPath: resume.path,
    resumedProcessedCount: resume.cardDates.size,
    resumedConfirmedCount: resume.ledgerCardDates.size,
    validateOnly,
    verificationMode: 'synchronous_transactional_business_receipt_plus_persistent_local_ledger',
    independentReadbackAvailable: false,
    batches: [...resume.batches],
  };

  if (!cards.length) {
    receipt.status = 'no_candidate';
    receipt.inputCount = 0;
    receipt.matchedCardCount = 0;
    receipt.updatedChatCount = 0;
    receipt.submittedCount = 0;
    receipt.notWrittenCount = 0;
    receipt.notWrittenCards = [];
    receipt.matchedNoChangeCount = 0;
    receipt.matchedNoChangeCards = [];
    receipt.businessReceiptPassed = true;
    receipt.transactionalVerificationPassed = true;
    receipt.ledgerVerificationPassed = true;
    receipt.independentReadbackPassed = false;
    await writeAtomic(receiptPath, receipt);
    console.log(JSON.stringify({ ...receipt, path: receiptPath }, null, 2));
    return;
  }

  let lockHandle;
  let session;
  try {
    await fs.mkdir(path.dirname(ledgerPath), { recursive: true });
    lockHandle = await fs.open(lockPath, 'wx');
    await lockHandle.writeFile(`${JSON.stringify({ task: 'E', label, pid: process.pid, createdAt: new Date().toISOString() })}\n`);

    // Recheck after obtaining the exclusive lock. A different run may have
    // finished between manifest generation and this lock acquisition.
    const lockedLedgerSha = await sha256(ledgerPath).catch(error => error?.code === 'ENOENT' ? null : Promise.reject(error));
    if (!resume.path && (manifest.ledgerSha256Before || null) !== lockedLedgerSha) {
      throw new Error('任务E获得排他锁后发现去重台账已变化，必须重新生成候选');
    }
    const lockedLedger = await readLedger(ledgerPath);
    const lockedRepeated = cards.filter(card => Object.hasOwn(lockedLedger.submissions, card));
    const lockedUnexpectedRepeated = lockedRepeated.filter(card => !resume.ledgerCardDates.has(card));
    const lockedMissingResume = [...resume.ledgerCardDates].filter(([card, expireDate]) => {
      const entry = lockedLedger.submissions[card];
      return !entry || entry.runId !== label || entry.expireDate !== expireDate;
    });
    if (lockedUnexpectedRepeated.length || lockedMissingResume.length || (resume.path && resume.ledgerCardDates.size && lockedLedger.lastRunId !== label)) {
      throw new Error('任务E获得排他锁后发现永久台账与续跑断点不一致');
    }

    session = await createSession({ cdp: process.env.BITBROWSER_CDP, pathName: '/phoneConfig' });
    const remainingCards = cards.filter(card => !resume.cardDates.has(card));
    receipt.validation = await validateLivePhones(session.api, remainingCards);
    if (validateOnly) {
      receipt.status = 'validated_only';
      receipt.businessReceiptPassed = true;
      receipt.transactionalVerificationPassed = true;
      receipt.ledgerVerificationPassed = true;
      receipt.independentReadbackPassed = true;
      await writeAtomic(receiptPath, receipt);
      console.log(JSON.stringify({ ...receipt, path: receiptPath }, null, 2));
      return;
    }

    // Narrow /phone/config/batchSetChatExpire helper; never generic POST replay.
    const submitter = await createExpirySubmitter({ task: 'E', api: session.api, ...submissionRuntime,
      beforeRetry: async ({ cards: batchCards }) => {
        if (await sha256(sourcePath) !== manifest.sourceSha256) throw Error('EXPIRY_RETRY_SOURCE_CHANGED');
        const ledger = await readLedger(ledgerPath);
        if (batchCards.some(card => Object.hasOwn(ledger.submissions, card))) throw Error('EXPIRY_RETRY_LEDGER_CHANGED');
        return verifyExpiryRetryIdentity({ api: session.api, cards: batchCards,
          snapshot: receipt.validation.retryIdentitySnapshot, serverDate: manifest.serverDate,
          readDate: () => readDate(session.client) });
      },
    });
    receipt.submissionPolicy = submitter.policy;
    for (const group of groups.sort((a, b) => a.expireDate.localeCompare(b.expireDate))) {
      const partitions = [
        { recoveryRetry: true, cards: group.cards.filter(card => !resume.cardDates.has(card) && recovery.cardDates.get(card) === group.expireDate) },
        { recoveryRetry: false, cards: group.cards.filter(card => !resume.cardDates.has(card) && recovery.cardDates.get(card) !== group.expireDate) },
      ];
      for (const partition of partitions) {
        for (let index = 0; index < partition.cards.length; index += MARKER_BATCH_SIZE) {
        const batchCards = partition.cards.slice(index, index + MARKER_BATCH_SIZE);
        const liveLedger = await readLedger(ledgerPath);
        const ledgerConflict = batchCards.filter(card => Object.hasOwn(liveLedger.submissions, card));
        if (ledgerConflict.length) {
          throw new Error(`任务E提交前发现已入台账号码: ${ledgerConflict.slice(0, 20)}`);
        }
        const batchNumber = receipt.batches.length + 1;
        const pendingBatch = { batchNumber, cardNos: batchCards, expireDate: group.expireDate, recoveryRetry: partition.recoveryRetry, submission: {} };
        await writeAtomic(progressPath, {
          ...receipt,
          status: 'submitting',
          pendingBatch,
        });
        const response = await submitter.submit(
          { cardNos: batchCards, expireDate: group.expireDate }, pendingBatch.submission,
          () => writeAtomic(progressPath, { ...receipt, status: 'submitting', pendingBatch }),
        );
        pendingBatch.response = response;
        // Persist every response before validation, including error responses.
        await writeAtomic(progressPath, {
          ...receipt, status: 'submitted_response_received',
          pendingBatch,
        });
        const { inputCount, matchedCardCount, updatedChatCount, notFound, matchedCards }
          = validateExpiryReceipt(response, batchCards, { allowNotFound: true });
        await writeAtomic(progressPath, {
          ...receipt,
          status: 'submitted_transactionally_confirmed',
          pendingBatch,
        });

        const confirmedAt = new Date().toISOString();
        for (const card of matchedCards) {
          liveLedger.submissions[card] = {
            phone: card,
            firstSubmittedAt: confirmedAt,
            latestSubmittedAt: confirmedAt,
            expireDate: group.expireDate,
            runId: label,
            receiptPath,
            batchNumber,
          };
        }
        if (matchedCards.length) {
          liveLedger.schemaVersion = LEDGER_SCHEMA_VERSION;
          liveLedger.dedupeKey = 'phone';
          liveLedger.updatedAt = confirmedAt;
          liveLedger.lastRunId = label;
          await writeAtomic(ledgerPath, liveLedger);
        }

        receipt.batches.push({
          batchNumber,
          expireDate: group.expireDate,
          cardNos: batchCards,
          inputCount,
          matchedCardCount,
          updatedChatCount,
          notFound,
          serverResponse: response,
          submission: pendingBatch.submission,
          businessReceiptPassed: true,
          transactionalVerificationPassed: true,
          ledgerRecordedCount: matchedCards.length,
          notWrittenCount: notFound.length,
          notWrittenCards: notFound.map(card => ({ phone: card, expireDate: group.expireDate, reason: 'backend_not_found' })),
          matchedNoChangeCount: updatedChatCount === 0 ? matchedCards.length : 0,
          matchedNoChangeCards: updatedChatCount === 0
            ? matchedCards.map(card => ({ phone: card, expireDate: group.expireDate, reason: 'backend_matched_no_change' }))
            : [],
          recoveryRetry: partition.recoveryRetry,
          idempotentRecoveryNoChange: partition.recoveryRetry && matchedCards.length > 0 && updatedChatCount === 0,
          updateCountAcceptanceRule: partition.recoveryRetry
            ? 'input_equals_matched_plus_not_found; nonnegative_update_count_accepts_recovery_idempotent_no_change'
            : 'input_equals_matched_plus_not_found; nonnegative_update_count_accepts_idempotent_no_change',
        });
        await writeAtomic(progressPath, { ...receipt, status: 'in_progress' });
        }
      }
    }

    const finalLedger = await readLedger(ledgerPath);
    const notWrittenCards = receipt.batches.flatMap(item => item.notWrittenCards || []);
    const notWrittenSet = new Set(notWrittenCards.map(item => item.phone));
    const successfullyMatchedCards = cards.filter(card => !notWrittenSet.has(card));
    const missingFromLedger = successfullyMatchedCards.filter(card => !Object.hasOwn(finalLedger.submissions, card));
    if (missingFromLedger.length) throw new Error(`任务E提交成功但台账缺少号码: ${missingFromLedger.slice(0, 20)}`);
    receipt.status = notWrittenCards.length ? 'passed_with_not_found' : 'passed';
    receipt.inputCount = receipt.batches.reduce((sum, item) => sum + item.inputCount, 0);
    receipt.matchedCardCount = receipt.batches.reduce((sum, item) => sum + item.matchedCardCount, 0);
    receipt.updatedChatCount = receipt.batches.reduce((sum, item) => sum + item.updatedChatCount, 0);
    receipt.submittedCount = successfullyMatchedCards.length;
    receipt.notWrittenCount = notWrittenCards.length;
    receipt.notWrittenCards = notWrittenCards;
    receipt.matchedNoChangeCards = receipt.batches.flatMap(item => item.matchedNoChangeCards || []);
    receipt.matchedNoChangeCount = receipt.matchedNoChangeCards.length;
    receipt.businessReceiptPassed = true;
    receipt.transactionalVerificationPassed = true;
    receipt.ledgerVerificationPassed = true;
    receipt.recoveryRetriedCount = receipt.batches.filter(item => item.recoveryRetry).reduce((sum, item) => sum + item.inputCount, 0);
    receipt.idempotentRecoveryNoChangeCount = receipt.batches.filter(item => item.idempotentRecoveryNoChange).reduce((sum, item) => sum + item.ledgerRecordedCount, 0);
    receipt.ledgerSha256After = successfullyMatchedCards.length ? await sha256(ledgerPath) : null;
    receipt.independentReadbackPassed = false;
    receipt.independentReadbackUnavailableReason = 'batchSetChatExpire未提供到期标记查询接口；以同步事务回执和本地永久去重台账为准';
    await writeAtomic(receiptPath, receipt);
    console.log(JSON.stringify({ ...receipt, path: receiptPath, progressPath }, null, 2));
  } catch (error) {
    if (!lockHandle) throw error; // Never overwrite another process's evidence.
    const existing = await fs.readFile(progressPath, 'utf8').then(JSON.parse).catch(() => ({}));
    const pendingBatch = existing.pendingBatch || null;
    // Any unfinished submission/ledger commit blocks replay, not only errors
    // whose English message happens to contain "timeout" or "gateway".
    const gatewayOrNetworkUncertain = Array.isArray(pendingBatch?.cardNos)
      && ['submitting', 'submitted_response_received', 'submitted_transactionally_confirmed', 'business_receipt_rejected'].includes(existing.status);
    await writeAtomic(progressPath, {
      ...receipt,
      ...existing,
      status: 'failed',
      error: error.message,
      failureClass: gatewayOrNetworkUncertain ? 'submission_outcome_uncertain' : 'confirmed_pre_or_post_submit_failure',
      uncertainSubmission: gatewayOrNetworkUncertain,
      automaticRetryBlocked: gatewayOrNetworkUncertain,
      confirmedInputCount: (existing.batches || []).reduce((sum, item) => sum + Number(item.inputCount || 0), 0),
      uncertainBatch: gatewayOrNetworkUncertain ? {
        batchNumber: pendingBatch.batchNumber,
        expireDate: pendingBatch.expireDate,
        inputCount: pendingBatch.cardNos.length,
        cardNosSha256: crypto.createHash('sha256').update(pendingBatch.cardNos.join('\n')).digest('hex'),
      } : null,
      recoveryInstruction: gatewayOrNetworkUncertain
        ? '禁止自动重试；必须由当前真实用户明确再次授权任务E后，使用新鲜导出重新生成候选并按20个/批提交'
        : null,
    }).catch(() => {});
    throw error;
  } finally {
    if (session) session.client.close();
    if (lockHandle) await lockHandle.close().catch(() => {});
    if (lockHandle) await fs.unlink(lockPath).catch(() => {});
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => {
    console.error(error.stack || String(error));
    process.exit(1);
  });
}
