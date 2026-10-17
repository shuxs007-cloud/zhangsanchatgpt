// Narrow user-authorized D/E expiry-marker pacing. Never a generic POST retry.
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { setTimeout as sleep } from 'node:timers/promises';
import { listPhonesByNumbers, shortCardNo } from '../opensms_protocol_client.mjs';
import { getCarrierConfigsReadOnly } from './opensms_carriers_readonly.mjs';

export const EXPIRY_SETTINGS_PATH = fileURLToPath(new URL('./opensms_expiry_submission_settings.json', import.meta.url));
const ENDPOINT = '/phone/config/batchSetChatExpire';
const hash = value => crypto.createHash('sha256').update(value).digest('hex');

export function validateExpirySubmissionSettings(settings) {
  if (settings?.version !== 1 || !isDeepStrictEqual(settings.tasks, ['D', 'E'])
      || settings.endpoint !== ENDPOINT || settings.retryHttpStatus !== 502
      || !Number.isSafeInteger(settings.completionGapMs) || settings.completionGapMs < 5000 || settings.completionGapMs > 60000
      || !Number.isSafeInteger(settings.maxAttempts) || settings.maxAttempts < 1 || settings.maxAttempts > 3
      || !Array.isArray(settings.retryDelayMs) || settings.retryDelayMs.length !== 2
      || settings.retryDelayMs.some((n, i) => !Number.isSafeInteger(n) || n < [60000, 120000][i] || n > 900000)
      || settings.maxRetryAfterMs !== 900000) throw Error('EXPIRY_SUBMISSION_SETTINGS_INVALID');
  return Object.freeze({ ...settings, tasks: Object.freeze([...settings.tasks]), retryDelayMs: Object.freeze([...settings.retryDelayMs]) });
}

export async function loadExpirySubmissionSettings() {
  const bytes = await fs.readFile(EXPIRY_SETTINGS_PATH);
  return { settings: validateExpirySubmissionSettings(JSON.parse(bytes)), settingsPath: EXPIRY_SETTINGS_PATH, settingsSha256: hash(bytes) };
}

// Write intent, each attempt, response, and ledger barriers are durable before
// continuing. A failed evidence write is never caught as a retryable HTTP error.
export async function writeExpiryEvidence(file, payload) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  const temp = `${file}.${crypto.randomUUID()}.tmp`;
  const handle = await fs.open(temp, 'wx', 0o600);
  try { await handle.writeFile(`${JSON.stringify(payload, null, 2)}\n`); await handle.sync(); }
  finally { await handle.close(); }
  await fs.rename(temp, file);
  const directory = await fs.open(path.dirname(file), 'r');
  try { await directory.sync(); } finally { await directory.close(); }
}

export function expiryIdentitySnapshot(rows, cards, configs = []) {
  const result = {}, ids = new Set(), requested = new Set(cards);
  for (const row of rows) {
    const phone = shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
    const id = String(row.id ?? '').trim();
    const carrierIdentif = String(row.carrireIdentif || row.carrierIdentif || row.identif || '').trim();
    if (!requested.has(phone) || Object.hasOwn(result, phone) || !id || ids.has(id) || !carrierIdentif) {
      throw Error('EXPIRY_RETRY_IDENTITY_UNCONFIRMED');
    }
    ids.add(id);
    const carrier = configs.find(c => String(c.carrireIdentif || c.carrierIdentif || c.identif || c.id || '').trim() === carrierIdentif);
    result[phone] = { id, carrierIdentif, carrierName: String(carrier?.name || carrier?.carrierName || carrier?.carrireName || carrier?.carrier || '').trim(),
      fullPhone: String(row.cardNo || row.phone || row.cardPhone || row.tel),
      opr: row.opr ?? null, iccid: row.cardStore ?? null, slot: row.slotIdentif ?? null };
  }
  if (Object.keys(result).length !== requested.size) throw Error('EXPIRY_RETRY_IDENTITY_UNCONFIRMED');
  return result;
}

export async function verifyExpiryRetryIdentity({ api, cards, snapshot, serverDate, readDate }) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(serverDate || '') || typeof readDate !== 'function') throw Error('EXPIRY_RETRY_DATE_UNCONFIRMED');
  const dateBefore = await readDate();
  if (dateBefore !== serverDate) throw Error('EXPIRY_RETRY_PLATFORM_DATE_CHANGED');
  const { rows } = await listPhonesByNumbers(api, cards, 500);
  const configs = await getCarrierConfigsReadOnly(api, { concurrency: 1 });
  const current = expiryIdentitySnapshot(rows, cards, configs);
  for (const phone of cards) {
    if (!isDeepStrictEqual(current[phone], snapshot?.[phone])) throw Error('EXPIRY_RETRY_IDENTITY_CHANGED');
    const matches = configs.filter(c => String(c.carrireIdentif || c.carrierIdentif || c.identif || c.id || '').trim() === current[phone].carrierIdentif);
    const name = String(matches[0]?.name || matches[0]?.carrierName || matches[0]?.carrireName || matches[0]?.carrier || '').trim();
    if (matches.length !== 1 || !name || matches[0].delData === true || ['快过期续费C', '快过期续费T'].includes(name)) throw Error('EXPIRY_RETRY_CARRIER_PROTECTED_OR_CHANGED');
  }
  if (await readDate() !== serverDate) throw Error('EXPIRY_RETRY_PLATFORM_DATE_CHANGED');
  return { passed: true, serverDate, checkedCount: cards.length, identitySha256: hash(JSON.stringify(current)), checkedAt: new Date().toISOString() };
}

export async function createExpirySubmitter({ task, api, beforeRetry, wait = sleep, clock = Date.now } = {}) {
  if (!['D', 'E'].includes(task) || typeof api !== 'function') throw Error('EXPIRY_SUBMISSION_SCOPE_INVALID');
  const policy = await loadExpirySubmissionSettings();
  let active = false;
  // Keep each wait chunk short; no second request/browser/worker is introduced.
  async function pause(ms) {
    const started = clock();
    while (clock() - started < ms) await wait(Math.min(ms - (clock() - started), 30000));
    return clock() - started;
  }
  async function submit({ cardNos, expireDate }, audit, evidence) {
    if (active) throw Error('EXPIRY_SUBMISSION_CONCURRENT_WRITE_FORBIDDEN');
    if (!Array.isArray(cardNos) || !cardNos.length || cardNos.length > 20
        || cardNos.some(p => typeof p !== 'string' || !/^\d{10}$/.test(p)) || new Set(cardNos).size !== cardNos.length
        || !/^\d{4}-\d{2}-\d{2}$/.test(expireDate || '') || typeof evidence !== 'function') throw Error('EXPIRY_SUBMISSION_PAYLOAD_INVALID');
    const payload = Object.freeze({ cardNos: Object.freeze([...cardNos]), expireDate });
    Object.assign(audit, { ...policy, task, method: 'POST', payloadSha256: hash(JSON.stringify(payload)), attempts: [], waits: [], retryCount: 0 });
    active = true;
    try {
      audit.phase = 'waiting_submission_gap';
      await evidence();
      const gap = { reason: 'submission_gap', requestedMs: policy.settings.completionGapMs, startedAt: clock() };
      audit.waits.push(gap);
      await evidence();
      gap.actualMs = await pause(gap.requestedMs);
      await evidence();
      for (let number = 1; number <= policy.settings.maxAttempts; number++) {
        const attempt = { number, startedAt: clock(), status: 'submitting' };
        audit.attempts.push(attempt); audit.phase = 'submitting'; audit.retryCount = number - 1;
        await evidence();
        let response, failure;
        // Only the actual API operation is inside this catch. Validation and
        // fsync/rename failures must not cause another business submission.
        try { response = await api('POST', ENDPOINT, payload, null, { timeoutMs: 180000 }); }
        catch (error) { failure = error; }
        attempt.completedAt = clock();
        if (!failure) {
          attempt.status = 'response_received'; audit.phase = 'response_received';
          audit.response = response;
          await evidence();
          return response;
        }
        attempt.status = 'request_failed';
        attempt.httpStatus = Number.isSafeInteger(failure.status) ? failure.status : null;
        attempt.requestCompletionUnconfirmed = failure.requestCompletionUnconfirmed === true;
        audit.phase = 'request_failed';
        await evidence();
        if (failure.status !== 502 || failure.requestCompletionUnconfirmed === true || number === policy.settings.maxAttempts) throw failure;
        if (typeof beforeRetry !== 'function') throw Error('EXPIRY_RETRY_RECHECK_MISSING');
        const serverWait = failure.retryAfterMs ?? 0;
        if (!Number.isFinite(serverWait) || serverWait < 0 || serverWait > policy.settings.maxRetryAfterMs) throw Error('EXPIRY_RETRY_AFTER_EXCESSIVE');
        const cooldown = { reason: 'http_502_retry', afterAttempt: number,
          requestedMs: Math.max(policy.settings.retryDelayMs[number - 1], serverWait), startedAt: clock() };
        audit.waits.push(cooldown); audit.phase = 'waiting_502_retry';
        await evidence();
        cooldown.actualMs = await pause(cooldown.requestedMs);
        audit.phase = 'rechecking_502_retry'; await evidence();
        const proof = await beforeRetry({ cards: [...payload.cardNos], expireDate, nextAttempt: number + 1 });
        if (proof?.passed !== true) throw Error('EXPIRY_RETRY_RECHECK_UNCONFIRMED');
        cooldown.recheck = proof; await evidence();
      }
      throw Error('EXPIRY_SUBMISSION_ATTEMPTS_EXHAUSTED');
    } finally { active = false; }
  }
  return { policy, submit };
}
