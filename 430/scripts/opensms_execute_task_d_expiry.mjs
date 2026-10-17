#!/usr/bin/env node
// Receipt-gated Task D expiry-marker submission.  The backend does not expose
// a read API for chat expiry state: /phone/cardRetain/list is a user retention
// request source log, not an audit log for batchSetChatExpire.  Completion is
// therefore gated on the synchronous transactional response from the write
// endpoint and never inferred from the retention source log.

import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  createRawOpenSmsApiSession,
  listPhonesByNumbers,
  shortCardNo,
} from '../opensms_protocol_client.mjs';
import { getCarrierConfigsReadOnly } from './opensms_carriers_readonly.mjs';
import { mapReadOnlyBounded } from './opensms_readonly_concurrency.mjs';
import { retentionOprClass, validateFilteredRetentionProof } from './opensms_task_d_filtered_retention.mjs';
import { validateExpiryReceipt } from './opensms_expiry_receipt.mjs';
import { createExpirySubmitter, expiryIdentitySnapshot, verifyExpiryRetryIdentity, writeExpiryEvidence } from './opensms_expiry_submission.mjs';
import { readPlatformDate } from './opensms_expired_card_probe.mjs';

export const MARKER_BATCH_SIZE = 20;
const PHONE_BATCH_SIZE = 500;
const SWAP_CARRIERS = new Set(['换号区C', '换号区T']);
const NEVER_PROCESS_SOURCE_CARRIERS = new Set(['快过期续费C', '快过期续费T']);

function carrierName(row) {
  return String(row.name || row.carrierName || row.carrireName || row.carrier || '').trim();
}

function carrierIdentif(row) {
  return String(row.carrireIdentif || row.carrierIdentif || row.identif || row.id || '').trim();
}

function classifyOpr(value) {
  return retentionOprClass(value);
}

function addDays(dateText, days) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateText || ''));
  if (!match) throw new Error(`无效服务器日期: ${dateText}`);
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function assertApi(label, response) {
  const code = Number(response?.code ?? 200);
  if (code !== 200) throw new Error(`${label}失败: ${JSON.stringify(response)}`);
}

async function sha256(file) {
  const data = await fs.readFile(file);
  return crypto.createHash('sha256').update(data).digest('hex');
}

async function writeAtomic(file, payload) {
  await writeExpiryEvidence(file, payload);
}

function exactCarrier(carriers, name) {
  const matches = carriers.filter(item => carrierName(item) === name);
  if (matches.length !== 1) throw new Error(`任务D运营商精确匹配异常: ${name}, count=${matches.length}`);
  const identif = carrierIdentif(matches[0]);
  if (!identif) throw new Error(`任务D运营商缺少ID: ${name}`);
  if (matches[0].delData === true) throw new Error(`任务D运营商已停用或删除: ${name}`);
  return { name, identif };
}

function uniqueCards(manifest) {
  const cards = [];
  const seen = new Set();
  for (const value of manifest.cardNos || []) {
    const card = shortCardNo(value);
    if (!card) continue;
    if (seen.has(card)) throw new Error(`任务D到期标记候选存在重复卡号: ${card}`);
    seen.add(card);
    cards.push(card);
  }
  if (Number(manifest.count ?? cards.length) !== cards.length) {
    throw new Error(`任务D到期标记候选计数不一致: manifest=${manifest.count}, cards=${cards.length}`);
  }
  return cards;
}

async function listPhonesBatched(api, cards) {
  const chunks = [];
  for (let index = 0; index < cards.length; index += PHONE_BATCH_SIZE) {
    chunks.push(cards.slice(index, index + PHONE_BATCH_SIZE));
  }
  const results = await mapReadOnlyBounded(chunks, chunk => listPhonesByNumbers(api, chunk, PHONE_BATCH_SIZE));
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

export function validateTaskDRetentionSourceProof(manifest, cards, receipt, actualReceiptSha256) {
  if (
    !/^[a-f0-9]{64}$/.test(String(manifest.retentionReceiptSha256 || ''))
    || manifest.retentionReceiptSha256 !== actualReceiptSha256
  ) throw new Error('任务D卡保留通知缺少有效的完整分页回执SHA-256绑定');
  if (
    receipt.task !== 'D'
    || receipt.sourceProvenance !== 'phone_card_retain_all_pages'
    || receipt.endpoint !== '/phone/cardRetain/list'
    || !Array.isArray(receipt.cardNos)
    || !Array.isArray(receipt.neverProcessSourceExcludedRows)
    || !Array.isArray(manifest.neverProcessSourceExcludedCards)
  ) throw new Error('任务D卡保留通知缺少完整分页或逐号保护排除证明');
  if (Object.hasOwn(receipt, 'filteredQueryProof')) {
    const { actionableRows, unmatchedCards, protectedRows, unconfiguredRows, retiredRows, dataAnomalyRows } = validateFilteredRetentionProof(receipt);
    if (manifest.dataAnomalyPolicy !== receipt.dataAnomalyPolicy) throw new Error('任务D异常跳过契约与原始查询不一致');
    if (!String(manifest.runId || '').trim() || manifest.runId !== receipt.runId) {
      throw new Error('任务D卡保留通知与筛选查询回执runId不一致');
    }
    const exactCards = (declared, expected, count, label) => {
      if (!Array.isArray(declared)) throw new Error(`任务D卡保留${label}缺少逐号清单`);
      const normalized = declared.map(shortCardNo);
      const expectedSet = new Set(expected.map(shortCardNo));
      if (
        !Number.isInteger(count) || count !== expectedSet.size
        || normalized.length !== expectedSet.size || new Set(normalized).size !== expectedSet.size
        || normalized.some(card => !card || !expectedSet.has(card))
      ) throw new Error(`任务D卡保留${label}与完整筛选查询证明不一致`);
    };
    exactCards(manifest.neverProcessSourceExcludedCards, protectedRows.map(row => row.cardNo),
      manifest.neverProcessSourceExcludedCount, '保护排除');
    exactCards(manifest.filteredUnmatchedCards, unmatchedCards, manifest.filteredUnmatchedCount, '筛选未返回');
    exactCards(manifest.unconfiguredCarrierSkippedCards ?? [], unconfiguredRows.map(row => row.cardNo),
      manifest.unconfiguredCarrierSkippedCount ?? 0, '未配置运营商排除');
    exactCards(manifest.retiredCarrierSkippedCards ?? [], retiredRows.map(row => row.cardNo),
      manifest.retiredCarrierSkippedCount ?? 0, '已删除运营商排除');
    exactCards(manifest.dataAnomalySkippedCards ?? [], dataAnomalyRows.map(row => row.cardNo),
      manifest.dataAnomalySkippedCount ?? 0, '异常数据排除');
    exactCards(cards, actionableRows.map(row => row.cardNo), cards.length, '通知集合');
    return {
      sourceCount: receipt.cardNos.length,
      notificationCount: cards.length,
      protectedExcludedCount: protectedRows.length,
      filteredUnmatchedCount: unmatchedCards.length,
      unconfiguredCarrierSkippedCount: unconfiguredRows.length,
      retiredCarrierSkippedCount: retiredRows.length,
      ...(receipt.dataAnomalyPolicy ? {dataAnomalySkippedCount: dataAnomalyRows.length} : {}),
      filteredQueryProofValidated: true,
    };
  }
  const sourceCards = receipt.cardNos.map(shortCardNo);
  const sourceSet = new Set(sourceCards);
  if (sourceCards.some(card => !card) || sourceSet.size !== sourceCards.length || Number(receipt.uniqueCardCount) !== sourceSet.size) {
    throw new Error('任务D卡保留全部来源号码无效、重复或计数不一致');
  }
  const excludedRows = receipt.neverProcessSourceExcludedRows;
  const excludedCards = excludedRows.map(row => shortCardNo(row.cardNo));
  const excludedSet = new Set(excludedCards);
  if (
    excludedCards.some(card => !card)
    || excludedSet.size !== excludedCards.length
    || excludedRows.some(row => !NEVER_PROCESS_SOURCE_CARRIERS.has(row.sourceCarrierName)
      || !String(row.sourceCarrierIdentif || '').trim()
      || !String(row.backendId ?? '').trim())
  ) throw new Error('任务D卡保留保护排除来源不是精确续费保护区或缺少后台身份');
  const declaredExcludedCards = manifest.neverProcessSourceExcludedCards.map(shortCardNo);
  if (
    Number(manifest.neverProcessSourceExcludedCount) !== excludedSet.size
    || Number(receipt.neverProcessSourceExcludedCount) !== excludedSet.size
    || declaredExcludedCards.length !== excludedSet.size
    || new Set(declaredExcludedCards).size !== excludedSet.size
    || declaredExcludedCards.some(card => !excludedSet.has(card))
  ) throw new Error('任务D卡保留通知与逐号保护排除清单不一致');
  const notifiedSet = new Set(cards);
  if (notifiedSet.size !== cards.length || cards.some(card => excludedSet.has(card))) {
    throw new Error('任务D卡保留通知号码重复或与保护排除集合相交');
  }
  const partition = new Set([...cards, ...excludedCards]);
  if (partition.size !== sourceSet.size || [...partition].some(card => !sourceSet.has(card))) {
    throw new Error('任务D卡保留来源不满足全部来源=当前通知号码+精确保护排除号码');
  }
  return { sourceCount: sourceSet.size, notificationCount: notifiedSet.size, protectedExcludedCount: excludedSet.size };
}

export function assertTaskDNotificationOnly(manifest) {
  if (manifest.phase === 'retention_verified' && (
    manifest.retentionCarrierTransferEnabled !== false
    || manifest.retentionCarrierTransferStatus !== 'removed_by_user'
    || manifest.requiredCarrierTargets
  )) throw new Error('任务D转入保留区工作已删除，拒绝旧B14依赖清单');
}

export function validateTaskDRetentionNotificationIdentity(row, expected, liveCarrier) {
  if (!expected || !String(row.id ?? '').trim()
      || String(row.id) !== String(expected.id)
      || shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel) !== expected.cardNo
      || carrierIdentif(row) !== carrierIdentif(expected)
      || liveCarrier !== expected.sourceCarrierName
      || NEVER_PROCESS_SOURCE_CARRIERS.has(liveCarrier)
      || !classifyOpr(row.opr) || classifyOpr(row.opr) !== expected.oprClass
      || String(row.opr || '') !== String(expected.opr || '')) {
    throw new Error(`任务D通知写前后台身份、来源或OPR已偏离原始筛选证明: ${JSON.stringify({
      expected: expected && { id: expected.id, cardNo: expected.cardNo, carrier: carrierIdentif(expected), name: expected.sourceCarrierName, opr: expected.opr, oprClass: expected.oprClass },
      actual: { id: row.id, cardNo: shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel), carrier: carrierIdentif(row), name: liveCarrier, opr: row.opr, oprClass: classifyOpr(row.opr) },
    })}`);
  }
}

export async function validateManifest(manifest, cards, api, carriers) {
  assertTaskDNotificationOnly(manifest);
  if (manifest.task !== 'D' || manifest.forceGate !== true || manifest.automationGate !== true) {
    throw new Error('任务D到期标记清单缺少当前用户授权门禁或程序门禁');
  }
  if (!['swap_zero', 'retention_verified'].includes(manifest.phase)) {
    throw new Error(`任务D到期标记阶段无效: ${manifest.phase || '空'}`);
  }
  const serverDate = String(manifest.serverDate || '');
  let retentionSourceProof = null;
  let retentionRowsByCard;
  if (manifest.phase === 'swap_zero' && manifest.expireDate !== serverDate) {
    throw new Error('任务D换号区两日0回复到期时间必须等于本轮服务器日期');
  }
  if (manifest.phase === 'retention_verified' && manifest.expireDate !== addDays(serverDate, 7)) {
    throw new Error('任务D卡保留到期时间必须等于本轮服务器日期+7天');
  }

  if (manifest.phase === 'retention_verified') {
    if (manifest.sourceProvenance !== 'phone_card_retain_all_pages') {
      throw new Error('任务D卡保留清单缺少全部分页来源证明');
    }
    const retentionReceiptPath = path.resolve(manifest.retentionReceiptPath);
    const receiptBytes = await fs.readFile(retentionReceiptPath);
    const receipt = JSON.parse(receiptBytes.toString('utf8'));
    const actualReceiptSha256 = crypto.createHash('sha256').update(receiptBytes).digest('hex');
    retentionSourceProof = validateTaskDRetentionSourceProof(manifest, cards, receipt, actualReceiptSha256);
    retentionRowsByCard = new Map(validateFilteredRetentionProof(receipt).actionableRows.map(row => [row.cardNo, row]));
  } else {
    const source = path.resolve(manifest.sourcePath || '');
    if (!source || await sha256(source) !== manifest.sourceSha256) {
      throw new Error('任务D换号区指标源表SHA-256不一致');
    }
    const latest = String(manifest.latestReplyHeader || '');
    const previous = String(manifest.previousReplyHeader || '');
    const metricRows = new Map((manifest.rows || []).map(row => [shortCardNo(row.cardNo || row['电话']), row]));
    for (const card of cards) {
      const row = metricRows.get(card);
      if (!row || Number(row[latest]) !== 0 || Number(row[previous]) !== 0) {
        throw new Error(`任务D换号区卡号未证明连续两日总回复量为0: ${card}`);
      }
    }
  }

  if (manifest.phase === 'swap_zero') for (const name of SWAP_CARRIERS) exactCarrier(carriers, name);
  const nameByIdentif = new Map();
  for (const row of carriers) nameByIdentif.set(carrierIdentif(row), carrierName(row));
  const rows = cards.length ? await listPhonesBatched(api, cards) : [];
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
    if (manifest.phase === 'swap_zero') {
      if (!SWAP_CARRIERS.has(liveCarrier)) invalid.push({ card, reason: 'not_in_swap_zone', liveCarrier });
    } else {
      try {
        validateTaskDRetentionNotificationIdentity(row, retentionRowsByCard.get(card), liveCarrier);
      } catch (error) {
        // Repeat only the read, never a write. Both reads must still prove the
        // exact original identity; persistent drift remains a hard failure.
        const reread = await listPhonesBatched(api, [card]);
        try {
          if (reread.length !== 1) throw error;
          const fresh = reread[0];
          validateTaskDRetentionNotificationIdentity(fresh, retentionRowsByCard.get(card),
            nameByIdentif.get(carrierIdentif(fresh)) || carrierName(fresh));
          Object.assign(row, fresh); // Retry baseline must use the verified reread.
        } catch (recheckError) {
          invalid.push({ card, reason: 'retention_source_identity_changed', detail: error.message, recheck: recheckError.message });
        }
      }
    }
  }
  if (invalid.length) {
    throw new Error(`任务D到期标记写前独立验证失败${invalid.length}条: ${JSON.stringify(invalid.slice(0, 20))}`);
  }
  return { serverDate, backendVerifiedCount: cards.length, retentionSourceProof, retryIdentitySnapshot: expiryIdentitySnapshot(rows, cards, carriers) };
}

export async function assertNoTaskDReplay(progressPath, receiptPath) {
  for (const file of [progressPath, receiptPath].filter(Boolean)) {
    try {
      await fs.access(file);
    } catch (error) {
      if (error.code === 'ENOENT') continue;
      throw error;
    }
    const error = new Error(`任务D已有断点或提交回执，禁止自动重复提交: ${file}`);
    error.code = 'TASK_D_REPLAY_BLOCKED';
    throw error;
  }
}

// Explicit manual continuation is distinct from automatic HTTP retries. Keep
// the old failure immutable and import only its strictly verified prefix.
export function validateTaskDContinuation(prior, manifest, cards, manifestPath) {
  if (manifest.phase !== 'retention_verified' || prior.task !== 'D'
      || prior.phase !== manifest.phase || prior.status !== 'failed'
      || prior.failureClass !== 'submission_outcome_uncertain'
      || prior.uncertainSubmission !== true || prior.uncertainBatchCount !== 1
      || !/POST \/phone\/config\/batchSetChatExpire HTTP 502[；; ]/.test(prior.error || '')
      || path.resolve(prior.manifestPath || '') !== path.resolve(manifestPath)
      || prior.serverDate !== manifest.serverDate || prior.expireDate !== manifest.expireDate
      || prior.candidateCount !== cards.length || prior.markerBatchSize !== MARKER_BATCH_SIZE
      || !Array.isArray(prior.batches) || !prior.pendingBatch) {
    throw Error('TASK_D_CONTINUATION_SCOPE_INVALID');
  }
  let count = 0;
  for (const [index, batch] of prior.batches.entries()) {
    const expected = cards.slice(count, count + MARKER_BATCH_SIZE);
    const proof = validateExpiryReceipt(batch.serverResponse, expected);
    if (batch.batchNumber !== index + 1 || batch.expireDate !== manifest.expireDate
        || JSON.stringify(batch.cardNos) !== JSON.stringify(expected)
        || batch.businessReceiptPassed !== true || batch.transactionalVerificationPassed !== true
        || Object.entries(proof).some(([key, value]) => key !== 'matchedCards' && JSON.stringify(batch[key]) !== JSON.stringify(value))) {
      throw Error('TASK_D_CONTINUATION_CONFIRMED_PREFIX_INVALID');
    }
    count += expected.length;
  }
  const pending = prior.pendingBatch;
  const expected = cards.slice(count, count + MARKER_BATCH_SIZE);
  const digest = crypto.createHash('sha256').update(expected.join('\n')).digest('hex');
  if (expected.length === 0 || pending.batchNumber !== prior.batches.length + 1
      || pending.expireDate !== manifest.expireDate
      || pending.inputCount !== expected.length
      || JSON.stringify(pending.cardNos) !== JSON.stringify(expected)
      || pending.cardNosSha256 !== digest || prior.confirmedInputCount !== count
      || prior.uncertainBatch?.cardNosSha256 !== digest
      || prior.uncertainBatch?.inputCount !== expected.length) {
    throw Error('TASK_D_CONTINUATION_PENDING_BATCH_INVALID');
  }
  return { confirmedCount: count, remainingCards: cards.slice(count),
    batches: structuredClone(prior.batches), pendingCardNosSha256: digest };
}

export async function submitTaskDMarkerBatches({ api, cards, manifest, receipt, progressPath, submissionOptions = {} }) {
  await assertNoTaskDReplay(progressPath);
  const submitter = await createExpirySubmitter({ task: 'D', api, ...submissionOptions });
  receipt.submissionPolicy = submitter.policy;
  let pendingBatch = null;
  let submissionMayHaveStarted = false;
  try {
    for (let index = 0; index < cards.length; index += MARKER_BATCH_SIZE) {
      const batchCards = cards.slice(index, index + MARKER_BATCH_SIZE);
      pendingBatch = {
        batchNumber: receipt.batches.length + 1,
        cardNos: batchCards,
        inputCount: batchCards.length,
        cardNosSha256: crypto.createHash('sha256').update(batchCards.join('\n')).digest('hex'),
        expireDate: manifest.expireDate,
        submission: {},
      };
      await writeAtomic(progressPath, { ...receipt, status: 'submitting', pendingBatch });
      submissionMayHaveStarted = true;
      // Only confirmed HTTP 502 permits bounded, delayed, same-payload retries
      // within this live batch. Old checkpoints and other failures never replay.
      const response = await submitter.submit(
        { cardNos: batchCards, expireDate: manifest.expireDate }, pendingBatch.submission,
        () => writeAtomic(progressPath, { ...receipt, status: 'submitting', pendingBatch }),
      );
      pendingBatch.response = response;
      // Preserve the raw synchronous response even when strict validation fails.
      await writeAtomic(progressPath, { ...receipt, status: 'submitted_response_received', pendingBatch });
      assertApi('写入任务D到期标记', response);
      const { inputCount, matchedCardCount, updatedChatCount, notFound } = validateExpiryReceipt(response, batchCards);
      const batchReceipt = {
        batchNumber: receipt.batches.length + 1,
        cardNos: batchCards,
        expireDate: manifest.expireDate,
        inputCount, matchedCardCount, updatedChatCount, notFound,
        matchedNoChangeCount: updatedChatCount === 0 ? matchedCardCount : 0,
        updateCountAcceptanceRule: 'exact_full_match; nonnegative_update_count_accepts_idempotent_no_change',
        serverResponse: response,
        submission: pendingBatch.submission,
        businessReceiptPassed: true,
        transactionalVerificationPassed: true,
        independentReadbackPassed: false,
        independentReadbackUnavailableReason: 'batchSetChatExpire未提供查询接口；cardRetain/list是保留申请来源日志，不是本接口写入日志',
      };
      receipt.batches.push(batchReceipt);
      submissionMayHaveStarted = false;
      pendingBatch = null;
      await writeAtomic(progressPath, { ...receipt, status: 'in_progress' });
    }
  } catch (error) {
    const uncertainSubmission = submissionMayHaveStarted && pendingBatch !== null;
    await writeAtomic(progressPath, {
      ...receipt,
      status: 'failed',
      pendingBatch,
      error: error.message || String(error),
      failureClass: uncertainSubmission ? 'submission_outcome_uncertain' : 'confirmed_pre_or_post_submit_failure',
      uncertainSubmission,
      automaticRetryBlocked: true,
      confirmedInputCount: receipt.batches.reduce((sum, item) => sum + Number(item.inputCount || 0), 0),
      uncertainBatchCount: uncertainSubmission ? 1 : 0,
      uncertainBatch: uncertainSubmission ? {
        batchNumber: pendingBatch.batchNumber,
        expireDate: pendingBatch.expireDate,
        inputCount: pendingBatch.cardNos.length,
        cardNosSha256: pendingBatch.cardNosSha256,
      } : null,
      recoveryInstruction: '禁止自动重试或重复使用本断点；须核对同步事务回执并取得当前真实用户再次明确授权后，使用新鲜候选处理',
    }).catch(() => {});
    throw error;
  }
}

async function main() {
  const args = process.argv.slice(2);
  const validateOnly = args.includes('--validate-only');
  const resumePreSubmit = args.includes('--resume-pre-submit');
  const options = {};
  const positional = [];
  for (let index = 0; index < args.length; index++) {
    const arg = args[index];
    if (['--validate-only', '--resume-pre-submit'].includes(arg)) continue;
    if (['--continue-progress', '--continue-progress-sha256', '--authorization-source'].includes(arg)) {
      if (!args[index + 1] || args[index + 1].startsWith('--') || options[arg]) throw Error('TASK_D_CONTINUATION_ARGUMENT_INVALID');
      options[arg] = args[++index];
    } else if (arg.startsWith('--')) throw Error('TASK_D_UNKNOWN_OPTION');
    else positional.push(arg);
  }
  const [manifestArg, label = 'task_d_expiry', outputDirArg = '.'] = positional;
  if (!manifestArg) throw new Error('usage: opensms_execute_task_d_expiry.mjs manifest [label] [outputDir] [--validate-only]');
  if (process.env.OPENSMS_ENABLE_TASK_D !== '1') {
    throw new Error('OPENSMS_ENABLE_TASK_D 未严格等于 1，拒绝处理任务D到期标记');
  }
  if (process.env.OPENSMS_FORCE_TASK_D !== '1') {
    throw new Error('OPENSMS_FORCE_TASK_D 未严格等于 1，缺少当前真实用户授权，拒绝处理任务D到期标记');
  }
  if (!validateOnly && process.env.OPENSMS_EXECUTE_TASK_D !== '1') {
    throw new Error('OPENSMS_EXECUTE_TASK_D 未严格等于 1，拒绝写入任务D到期标记');
  }
  const manifestPath = path.resolve(manifestArg);
  const outputDir = path.resolve(outputDirArg);
  const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
  assertTaskDNotificationOnly(manifest);
  const cards = uniqueCards(manifest);
  const receiptPath = path.join(outputDir, `opensms_task_d_${manifest.phase}_${label}_receipt.json`);
  const progressPath = path.join(outputDir, `opensms_task_d_${manifest.phase}_${label}_progress.json`);
  const lockPath = `${progressPath}.lock`;
  let continuation;
  if (Object.keys(options).length) {
    const file = path.resolve(options['--continue-progress'] || '');
    if (validateOnly || resumePreSubmit || process.env.OPENSMS_TASK_D_CONTINUE_AUTHORIZED !== '1'
        || options['--authorization-source'] !== 'current-user'
        || !/^[a-f0-9]{64}$/.test(options['--continue-progress-sha256'] || '')
        || file === progressPath || path.dirname(file) === outputDir
        || await sha256(file) !== options['--continue-progress-sha256']) {
      throw Error('TASK_D_CONTINUATION_AUTHORIZATION_OR_HASH_INVALID');
    }
    await fs.access(`${file}.lock`).then(() => { throw Error('TASK_D_CONTINUATION_PRIOR_LOCK_PRESENT'); }, e => { if (e.code !== 'ENOENT') throw e; });
    continuation = validateTaskDContinuation(JSON.parse(await fs.readFile(file, 'utf8')), manifest, cards, manifestPath);
    continuation.source = file;
    continuation.sourceSha256 = options['--continue-progress-sha256'];
  }
  let session;
  let lockHandle;
  const receipt = {
    createdAt: new Date().toISOString(),
    task: 'D',
    phase: manifest.phase,
    route: manifest.route,
    manifestPath,
    serverDate: manifest.serverDate,
    expireDate: manifest.expireDate,
    candidateCount: cards.length,
    markerBatchSize: MARKER_BATCH_SIZE,
    validateOnly,
    verificationMode: 'synchronous_transactional_business_receipt',
    independentReadbackAvailable: false,
    batches: [],
  };
  try {
    await fs.mkdir(outputDir, { recursive: true });
    lockHandle = await fs.open(lockPath, 'wx');
    await lockHandle.writeFile(`${JSON.stringify({ task: 'D', label, pid: process.pid, createdAt: new Date().toISOString() })}\n`);
    if (resumePreSubmit) {
      const prior = JSON.parse(await fs.readFile(progressPath, 'utf8'));
      if (prior.status !== 'failed' || prior.task !== 'D' || prior.phase !== manifest.phase
          || path.resolve(prior.manifestPath || '') !== manifestPath
          || !Array.isArray(prior.batches) || prior.batches.length !== 0
          || prior.pendingBatch != null || prior.validation != null
          || prior.uncertainSubmission === true || prior.uncertainBatchCount > 0) {
        throw new Error('任务D只允许恢复已证明零提交的写前失败，拒绝重放批次');
      }
      await fs.access(receiptPath).then(() => { throw new Error('任务D已有提交回执，拒绝恢复'); }, e => { if (e.code !== 'ENOENT') throw e; });
      await fs.rename(progressPath, `${progressPath}.pre-submit-${Date.now()}.json`);
    }
    // Check under an exclusive process lock and before connecting the browser.
    await assertNoTaskDReplay(progressPath, receiptPath);
    session = await createRawOpenSmsApiSession({ cdp: process.env.BITBROWSER_CDP, pathName: '/phoneConfig' });
    const carriers = await getCarrierConfigsReadOnly(session.api);
    const validation = await validateManifest(manifest, cards, session.api, carriers);
    receipt.validation = validation;
    if (continuation) {
      const date = await readPlatformDate(session.client);
      if (date !== manifest.serverDate || await sha256(continuation.source) !== continuation.sourceSha256) {
        throw Error('TASK_D_CONTINUATION_DATE_OR_SOURCE_CHANGED');
      }
      const source = JSON.parse(await fs.readFile(manifest.retentionReceiptPath, 'utf8'));
      const originals = new Map(validateFilteredRetentionProof(source).actionableRows.map(row => [row.cardNo, row]));
      for (const card of continuation.remainingCards) {
        const original = originals.get(card), current = validation.retryIdentitySnapshot[card];
        if (!original || !current || current.iccid !== (original.cardStore ?? null)
            || current.slot !== (original.slotIdentif ?? null)) {
          throw Error('TASK_D_CONTINUATION_CARD_OR_SLOT_CHANGED');
        }
      }
      receipt.batches = continuation.batches;
      receipt.continuation = { authorizationSource: 'current-user', source: continuation.source,
        sourceSha256: continuation.sourceSha256, confirmedCountPreserved: continuation.confirmedCount,
        remainingCount: continuation.remainingCards.length,
        pendingCardNosSha256: continuation.pendingCardNosSha256, originalFailureOverwritten: false };
    }
    if (validateOnly || !cards.length) {
      receipt.status = cards.length ? 'validated_only' : 'no_candidate';
      receipt.businessReceiptPassed = cards.length === 0 || validateOnly;
      receipt.independentReadbackPassed = cards.length === 0 || validateOnly;
      receipt.transactionalVerificationPassed = cards.length === 0 || validateOnly;
      await writeAtomic(receiptPath, receipt);
      console.log(JSON.stringify({ ...receipt, path: receiptPath }, null, 2));
      return;
    }

    await submitTaskDMarkerBatches({ api: session.api, cards: continuation?.remainingCards ?? cards, manifest, receipt, progressPath,
      submissionOptions: { beforeRetry: async ({ cards: batchCards }) => {
        const proofPath = manifest.phase === 'retention_verified' ? manifest.retentionReceiptPath : manifest.sourcePath;
        const proofSha = manifest.phase === 'retention_verified' ? manifest.retentionReceiptSha256 : manifest.sourceSha256;
        if (await sha256(proofPath) !== proofSha) throw Error('EXPIRY_RETRY_SOURCE_CHANGED');
        return verifyExpiryRetryIdentity({
          api: session.api, cards: batchCards, snapshot: validation.retryIdentitySnapshot,
          serverDate: manifest.serverDate, readDate: () => readPlatformDate(session.client),
        });
      } },
    });

    receipt.status = 'passed';
    receipt.inputCount = receipt.batches.reduce((sum, item) => sum + item.inputCount, 0);
    receipt.matchedCardCount = receipt.batches.reduce((sum, item) => sum + item.matchedCardCount, 0);
    receipt.updatedChatCount = receipt.batches.reduce((sum, item) => sum + item.updatedChatCount, 0);
    receipt.matchedNoChangeCount = receipt.batches.reduce((sum, item) => sum + item.matchedNoChangeCount, 0);
    receipt.businessReceiptPassed = true;
    receipt.transactionalVerificationPassed = true;
    receipt.independentReadbackPassed = false;
    receipt.independentReadbackUnavailableReason = 'batchSetChatExpire未提供查询接口；以同步事务回执的输入、命中、更新会话和未命中字段为准';
    await writeAtomic(receiptPath, receipt);
    console.log(JSON.stringify({ ...receipt, path: receiptPath, progressPath }, null, 2));
  } catch (error) {
    // An existing checkpoint or another live process is evidence to preserve,
    // never a reason to overwrite it with a new failure record.
    if (error.code === 'TASK_D_REPLAY_BLOCKED' || !session) throw error;
    const existing = await fs.readFile(progressPath, 'utf8').then(JSON.parse).catch(() => ({}));
    await writeAtomic(progressPath, {
      ...receipt,
      ...existing,
      status: 'failed',
      error: error.message,
    }).catch(() => {});
    throw error;
  } finally {
    session?.client.close();
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
