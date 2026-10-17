import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  compactPhoneRow,
  createOpenSmsApiSession,
  getCarrierConfigs,
  listPhonesByNumbers,
  shortCardNo,
  targetPhonesFromCandidate,
  updatePhoneCarrier,
} from './opensms_protocol_client.mjs';
import { SNAPSHOT_DIR, ensureArtifactDirs } from './opensms_artifact_paths.mjs';
import { assertTemporaryCarrierAllowed } from './scripts/opensms_temporary_carrier_hold.mjs';
import { retentionOprClass, validateFilteredRetentionProof } from './scripts/opensms_task_d_filtered_retention.mjs';
import { enforceUnexpiredRestore, validateUnexpiredLiveIdentity, isUnexpiredRestore } from './scripts/opensms_unexpired_a11_guard.mjs';
import { enforceVGroupBoundary } from './scripts/opensms_v_group_guard.mjs';

const CDP = process.env.BITBROWSER_CDP;
const BATCH_SIZE = 500;
const REPLY_RATE_SEND_THRESHOLD = 50;
const A1_KINI_ROUTE = 'a1_kini_latest_lt4_to_b12';
const A1_KINI_RULE_VERSION = 'a1-kini-latest-send50-rate-lt4-to-b12-v1';
const REST_ZONE_ENTRY_ENABLED = false;
const REPLY_RATE_SEND_GATED_AUTO_ROUTES = new Set([
  A1_KINI_ROUTE,
  'a_zone_lt4_to_a12',
  'a_zone_gte4_to_a1',
  'b1_b11_b12_today_gte50_zero_to_e',
  'chatr_to_e',
]);
const TASK_C_REPLY_RATE_SEND_GATED_ROUTES = new Set([
  'task_c_kooda_4day_reply_rate_lt5_to_swap',
  'task_c_chatr_unchanged_4day_reply_rate_lt4_to_swap',
]);
const V_ZONE_ZERO_ROUTE = 'b1_b11_b12_today_gte50_zero_to_e';
const B_STANDARD_USE2_ROUTE = 'b1_b11_use2_lt240_to_b11';
const B_STANDARD_GTE240_ROUTE = 'b1_b11_gte240_to_b12';
const B_STANDARD_USE2_THRESHOLD = 240;
const B_STANDARD_GTE240_THRESHOLD = 240;
const V_ZONE_USE2_ROUTE = 'b1v_b11v_use2_lt240_to_b11v';
const V_ZONE_GTE240_ROUTE = 'b1v_b11v_gte240_to_b12v';
const V_ZONE_TEN_DAY_THRESHOLD = 240;
const V_ZONE_USE_DAYS_FIELD_VALUE = '2天';
const NEVER_PROCESS_SOURCE_CARRIERS = new Set(['快过期续费C', '快过期续费T']);
const TASK_B_RETAINED_SOURCE_CARRIERS = new Set(['B14-C快下保留卡', 'B14-T快下保留卡']);
const TASK_B_PRIORITY_SOURCE_CARRIERS = new Set([
  '过期下架卡C', '过期下架卡T', '快过期隔离区C', '快过期隔离区T',
]);
const QUICK_RENEW_C_RESTORE_TARGET = '快过期续费C';

function stamp() {
  return new Date().toISOString().replace(/[-:]/g, '').replace(/\.d+Z$/, 'Z');
}

function carrierIdentif(row) {
  return String(row.carrireIdentif || row.carrierIdentif || row.identif || row.id || '');
}

function carrierName(row) {
  return String(row.name || row.carrierName || row.carrireName || row.carrier || '');
}

function backendPhone(row) {
  return shortCardNo(row.cardNo || row.phone || row.cardPhone || row.tel);
}

function safeLabel(value) {
  return String(value || 'carrier').replace(/[^A-Za-z0-9_.-]+/g, '_').replace(/^[_\.]+|[_\.]+$/g, '') || 'carrier';
}

function isRestCarrier(name) {
  const value = String(name || '').trim();
  return /^E(?:-|$)/i.test(value) && value.includes('休息');
}

export function enforceRestZoneEntryGate(candidate, targetCarrierName) {
  const route = String(candidate.route || '').trim();
  const restRoutes = new Map([
    ['b1_b11_b12_today_gte50_zero_to_e', 'E-T休息区'],
    ['chatr_to_e', 'E-C休息区'],
  ]);
  if (!isRestCarrier(targetCarrierName) && !restRoutes.has(route)) return;
  const rows = candidate.rows || candidate.candidates || [];
  if (!REST_ZONE_ENTRY_ENABLED && rows.length > 0) {
    throw new Error(
      `休息区转入功能已关闭，拒绝提交${rows.length}个号码到${targetCarrierName}`,
    );
  }
  if (candidate.restZoneEntryEnabled !== REST_ZONE_ENTRY_ENABLED) {
    throw new Error('休息区候选开关与当前程序不一致，必须用新鲜导出重新生成候选');
  }
  if (!restRoutes.has(route) || restRoutes.get(route) !== targetCarrierName
    || candidate.targetCarrierName !== targetCarrierName) {
    throw new Error('休息区转入路由或目标不匹配，拒绝提交');
  }
  const groups = route === 'chatr_to_e'
    ? ['A1', 'A11', 'A12', 'A3'] : ['B1', 'B11', 'B12'];
  for (const row of rows) {
    const send = row['发送窗口数'];
    const reply = row['回复窗口数'];
    if (!hasCarrierGroupPrefix(candidateSourceName(row), groups)
      || send === null || send === undefined || String(send).trim() === ''
      || !Number.isFinite(Number(send)) || Number(send) < REPLY_RATE_SEND_THRESHOLD
      || reply === null || reply === undefined || String(reply).trim() === ''
      || Number(reply) !== 0
      || (route === 'chatr_to_e' && !/chatr/i.test(String(row.opr || '')))) {
      throw new Error('休息区候选不满足允许来源、最新日发送>=50且回复=0条件，拒绝提交');
    }
  }
}

function isAbolishedB13Carrier(name) {
  return /^B13(?:-|\s|$)/i.test(String(name || '').trim());
}

function isProtectedSourceCarrier(name) {
  return /^(?:A21|A2)(?:-|\s|$)/i.test(String(name || '').trim());
}

function isNeverProcessSourceCarrier(name) {
  return NEVER_PROCESS_SOURCE_CARRIERS.has(String(name || '').trim());
}

function isTaskBRoute(candidate) {
  return /^task_b(?:_|$)/i.test(String(candidate.route || '').trim());
}

function isAllCarrierTaskB(candidate) {
  return isTaskB(candidate) && isTaskBRoute(candidate) && candidate.forceGate === true
    && candidate.sourceCarrierScope === 'all_carriers_v1'
    && Array.isArray(candidate.sourceCarrierExclusions) && candidate.sourceCarrierExclusions.length === 0;
}

export function enforceTaskBAllCarrierRouting(candidate, target, liveRows = null) {
  if (candidate.sourceCarrierScope === undefined) return; // Historical receipts retain their old guard.
  if (!isTaskB(candidate) && !isTaskBRoute(candidate)) return;
  if (!isAllCarrierTaskB(candidate)) throw new Error('任务B全运营商范围声明无效，必须从新鲜来源重新生成');
  enforceTaskBDeadlineGate(candidate, target);
  const keys = (candidate.rows || []).map(row => shortCardNo(row.phone || row['电话']));
  if (keys.some(key => !key) || new Set(keys).size !== keys.length) {
    throw new Error('任务B全运营商候选号码标准化后无效或重复，禁止静默折叠');
  }
  const family = /C$/.test(target) ? 'C' : /T$/.test(target) ? 'T' : '';
  const classify = opr => {
    const c = /chatr/i.test(String(opr || ''));
    const t = /kooda|koodo|kini|telus1|\blucky\b/i.test(String(opr || ''));
    return c === t ? '' : c ? 'C' : 'T';
  };
  const grouped = liveRows === null ? null : groupBackendRows(liveRows);
  for (const row of [...(candidate.rows || []), ...(candidate.candidates || [])]) {
    if (!family || !candidateSourceName(row) || classify(row.opr) !== family) {
      throw new Error('任务B全运营商候选缺来源或OPR分区不符，停止整组');
    }
    if (grouped) {
      const hits = grouped.get(shortCardNo(row.phone || row['电话'])) || [];
      if (hits.length !== 1 || classify(hits[0].opr) !== family) {
        throw new Error('任务B全运营商写前号码不唯一或实时OPR分区不符，停止整组');
      }
    }
  }
}

function isTaskBRetainedSourceCarrier(name) {
  return TASK_B_RETAINED_SOURCE_CARRIERS.has(String(name || '').trim());
}

function enforceTaskBRetainedDeclaredSources(candidate) {
  if (isAllCarrierTaskB(candidate)) return;
  if (!isTaskB(candidate) && !isTaskBRoute(candidate)) return;
  const sources = [
    ...(candidate.sourceCarrierNames || []),
    ...(candidate.configuredSourceCarrierNames || []),
  ];
  for (const row of [...(candidate.rows || []), ...(candidate.candidates || [])]) {
    for (const key of ['运营商', '原运营商', '精确原运营商', 'sourceCarrierName', 'sourceCarrier']) {
      sources.push(row[key]);
    }
  }
  const retained = [...new Set(sources.filter(isTaskBRetainedSourceCarrier).map(name => String(name).trim()))];
  if (retained.length) {
    throw new Error(`任务B保留卡保护：禁止将B14-C/T快下保留卡作为写入来源: ${retained.join('、')}`);
  }
}

function enforceTaskBRetainedLiveSources(candidate, rows, carriers) {
  if (isAllCarrierTaskB(candidate)) return;
  if (!isTaskB(candidate) && !isTaskBRoute(candidate)) return;
  const retainedIdentifs = new Set(carriers
    .filter(row => isTaskBRetainedSourceCarrier(carrierName(row)))
    .map(carrierIdentif)
    .filter(Boolean));
  const retained = rows.filter(row => (
    isTaskBRetainedSourceCarrier(carrierName(row))
    || retainedIdentifs.has(carrierIdentif(row))
  ));
  if (retained.length) {
    throw new Error(`任务B保留卡保护：后台实时来源中发现${retained.length}条B14-C/T快下保留卡记录，整组停止写入`);
  }
}

function isReplyRateSendGatedSourceCarrier(name) {
  return /^(?:A1|A11|A12|B1|B11|B12|B1V|B11V|B12V)(?:-|\s|$)/i.test(String(name || '').trim());
}

function isoDateText(value) {
  const match = String(value || '').trim().match(/^(20\d{2}-\d{2}-\d{2})/);
  return match ? match[1] : '';
}

function nextIsoDate(value) {
  const dateText = isoDateText(value);
  if (!dateText) return '';
  const parsed = new Date(`${dateText}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return '';
  parsed.setUTCDate(parsed.getUTCDate() + 1);
  return parsed.toISOString().slice(0, 10);
}

export function enforceTaskBDeadlineGate(candidate, targetCarrierName) {
  const target = String(targetCarrierName || '').trim();
  const expiryTarget = /^(?:快过期隔离区|过期下架卡)[CT]?$/.test(target);
  if (!isTaskB(candidate) && !isTaskBRoute(candidate) && !expiryTarget) return;
  if (!isTaskB(candidate) || !isTaskBRoute(candidate)) {
    throw new Error('隔离/过期目标必须有任务B截止日证明，禁止其它路由绕过');
  }
  const strictDay = value => {
    if (typeof value !== 'string' || !/^20\d{2}-\d{2}-\d{2}$/.test(value)) return null;
    const parsed = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value ? parsed : null;
  };
  const reference = strictDay(candidate.serverDate);
  if (!reference || candidate.tomorrowDate !== nextIsoDate(candidate.serverDate)) {
    throw new Error('任务B截止日校验：服务器日期或次日日期无效');
  }
  const expired = /^task_b_expired_to_offline(?:_[ct])?$/.test(candidate.route);
  const tomorrow = /^task_b_tomorrow_fast_to_isolation(?:_[ct])?$/.test(candidate.route);
  if ((!expired && !tomorrow) || target !== candidate.targetCarrierName
      || !(expired ? /^过期下架卡[CT]?$/.test(target) : /^快过期隔离区[CT]?$/.test(target))) {
    throw new Error('任务B截止日校验：日期分支与目标不一致');
  }
  for (const row of [...(candidate.rows || []), ...(candidate.candidates || [])]) {
    const cutoff = strictDay(row['截止日']);
    const remaining = cutoff ? (cutoff - reference) / 86400000 : null;
    if (remaining === null || !(expired ? remaining <= 0 : remaining === 1)
        || row['过期状态'] !== (expired ? '已过期' : '快过期')
        || (row['距截止日天数'] !== undefined && row['距截止日天数'] !== remaining)) {
      throw new Error(`任务B截止日校验失败：${row['电话'] || row.phone || '未知号码'}，截止日=${row['截止日'] ?? '空'}，服务器日期=${candidate.serverDate}，禁止提前隔离或下架`);
    }
  }
}

function enforceTaskBPriorityLockForTaskC(candidate) {
  if (candidate.taskBPriorityLockEnforced !== true) {
    throw new Error('任务C候选缺少任务B过期优先级锁定标记，拒绝提交');
  }
  const serverDate = isoDateText(candidate.serverDate);
  const tomorrowDate = isoDateText(candidate.tomorrowDate);
  if (!serverDate || tomorrowDate !== nextIsoDate(serverDate)) {
    throw new Error('任务C候选的服务器日期或明日日期无效，无法验证任务B优先级');
  }
  const invalid = [];
  for (const row of candidate.rows || candidate.candidates || []) {
    const source = String(row['运营商'] || row['原运营商'] || row.sourceCarrierName || '').trim();
    const expiryStatus = String(row['过期状态'] || '').trim();
    const deadline = isoDateText(row['截止日']);
    if (
      TASK_B_PRIORITY_SOURCE_CARRIERS.has(source)
      || expiryStatus === '已过期'
      || (expiryStatus === '快过期' && deadline === tomorrowDate)
    ) {
      invalid.push({
        phone: row.phone || row['电话'] || '', source, expiryStatus, deadline,
      });
    }
  }
  if (invalid.length) {
    throw new Error(`任务B已过期/明日到期优先级锁定：任务C包含${invalid.length}个禁止覆盖号码，整组停止写入`);
  }
}

export function enforceTaskCTenDaySendGate(candidate) {
  if (candidate.tenDaySendThresholdExclusive !== 360 || candidate.tenDaySendMetric !== '10日发送窗口数') {
    throw new Error('任务C候选缺少10日发送窗口数>360门禁，拒绝旧候选或不一致门槛');
  }
  const invalid = (candidate.rows || candidate.candidates || []).filter(row => {
    const raw = row['10日发送窗口数'];
    if (typeof raw !== 'number' && typeof raw !== 'string') return true;
    const value = Number(typeof raw === 'string' ? raw.trim().replaceAll(',', '') : raw);
    return !Number.isFinite(value) || value <= 360;
  });
  if (invalid.length) {
    throw new Error(`任务C候选中有${invalid.length}个号码未满足10日发送窗口数>360，整组停止写入`);
  }
}

export function enforceTaskCRecentDailyReplyGuard(candidate) {
  const threshold = new Map([
    ['task_c_kooda_4day_reply_rate_lt5_to_swap', 0.05],
    ['task_c_chatr_unchanged_4day_reply_rate_lt4_to_swap', 0.04],
  ]).get(candidate.route);
  const server = candidate.serverDate;
  const day = typeof server === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(server)
    ? new Date(`${server}T00:00:00Z`) : new Date(NaN);
  if (!threshold || !Number.isFinite(day.getTime()) || day.toISOString().slice(0, 10) !== server) {
    throw new Error('任务C近两日回复率保护：服务器日期或路由无效');
  }
  const previous = new Date(day.getTime() - 86400000).toISOString().slice(0, 10);
  const headers = [server, previous].map(value => `${value.slice(5)}回复率`);
  const gate = candidate.recentDailyReplyRateGuard;
  if (!gate || gate.version !== 1 || gate.serverDate !== server || gate.previousDate !== previous
      || JSON.stringify(gate.headers) !== JSON.stringify(headers)
      || gate.thresholdExclusive !== threshold || gate.comparison !== 'any_strictly_greater_than'
      || gate.unknownPolicy !== 'exclude') {
    throw new Error('任务C近两日回复率保护门禁缺失或不一致，拒绝旧候选');
  }
  for (const row of candidate.rows || candidate.candidates || []) {
    for (const header of headers) {
      const raw = row[header];
      const text = (typeof raw === 'number' || typeof raw === 'string') ? String(raw).trim() : '';
      const percent = text.endsWith('%');
      const numeric = percent ? text.slice(0, -1) : text;
      // Decimal Excel values only; no booleans, blanks, NaN, Infinity or hex.
      const number = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(numeric.trim())
        ? Number(numeric) : NaN;
      const rate = percent || number > 1 ? number / 100 : number;
      if (!Number.isFinite(rate) || rate < 0 || rate > 1 || rate > threshold) {
        throw new Error(`任务C近两日回复率保护：${header}缺失、无效或大于${threshold * 100}%，整组停止写入`);
      }
    }
  }
}

function enforceReplyRateSendGate(candidate) {
  const route = String(candidate.route || '').trim();
  const isLatestDayRoute = REPLY_RATE_SEND_GATED_AUTO_ROUTES.has(route);
  const isTaskCFourDayRoute = TASK_C_REPLY_RATE_SEND_GATED_ROUTES.has(route);
  if (!isLatestDayRoute && !isTaskCFourDayRoute) return;
  const configuredThreshold = Number(
    isTaskCFourDayRoute
      ? candidate.replyRateSendThresholdInclusive
      : candidate.replyRateLatestSendThresholdInclusive,
  );
  if (configuredThreshold !== REPLY_RATE_SEND_THRESHOLD) {
    throw new Error(
      `回复型候选缺少对应统计窗口发送>=${REPLY_RATE_SEND_THRESHOLD}门禁: route=${route}`,
    );
  }
  if (
    route === 'chatr_to_e'
    && String(candidate.targetCarrierName || '').trim() !== 'E-C休息区'
  ) {
    throw new Error('Chatr零回复候选目标必须为E-C休息区');
  }
  const invalid = [];
  for (const row of candidate.rows || candidate.candidates || []) {
    const source = row['原运营商'] || row['运营商'] || row.sourceCarrierName || row.sourceCarrier;
    if (route !== 'chatr_to_e' && !isReplyRateSendGatedSourceCarrier(source)) continue;
    const send = Number(row[isTaskCFourDayRoute ? '4日发送窗口数' : '发送窗口数']);
    if (!Number.isFinite(send) || send < REPLY_RATE_SEND_THRESHOLD) {
      invalid.push({
        phone: row.phone || row['电话'] || '',
        source,
        send: Number.isFinite(send) ? send : null,
      });
    }
  }
  if (invalid.length) {
    throw new Error(
      `回复型候选中有${invalid.length}个号码未满足对应统计窗口发送>=${REPLY_RATE_SEND_THRESHOLD}，整组停止写入`,
    );
  }
  if (isTaskCFourDayRoute) {
    enforceTaskCTenDaySendGate(candidate);
    enforceTaskCRecentDailyReplyGuard(candidate);
  }
}

function hasCarrierGroupPrefix(value, groups) {
  const text = String(value || '').trim();
  return groups.some(group => new RegExp(`^${group}(?:-|\\s|$)`, 'i').test(text));
}

function finiteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function isTSeriesOpr(value) {
  return /kooda|koodo|kini|telus1/i.test(String(value || ''));
}

function kiniWindowCount(value) {
  if (!['string', 'number'].includes(typeof value) || String(value).trim() === '') return null;
  const count = Number(value);
  return Number.isSafeInteger(count) && count >= 0 ? count : null;
}

// Pure guard: reject stale ordinary A1/Kini candidates before browser access,
// then recheck the live OPR before any write. Explicit B/C rules remain separate.
export function enforceA1KiniRule(candidate, targetCarrierName, liveRows = null) {
  const route = String(candidate.route || '').trim();
  if (/^task_[bcde]_/.test(route) || ['B', 'C', 'D', 'E'].includes(String(candidate.task || '').toUpperCase())) return;
  const rows = candidate.rows || candidate.candidates || [];
  const isKini = opr => /\bkini\b/i.test(String(opr || ''));
  const isA1 = source => hasCarrierGroupPrefix(source, ['A1']);
  const declaredKini = rows.filter(row => isA1(candidateSourceName(row)) && isKini(row.opr));
  const grouped = liveRows === null ? null : groupBackendRows(liveRows);
  if (route !== A1_KINI_ROUTE) {
    const liveKini = grouped && rows.some(row => {
      if (!isA1(candidateSourceName(row))) return false;
      return (grouped.get(shortCardNo(row.phone || row['电话'])) || []).some(hit => isKini(hit.opr));
    });
    if (declaredKini.length || liveKini) throw new Error('A1＋Kini必须使用最新日发送>=50且回复率<4%转B12的独立路由，拒绝旧候选');
    return;
  }
  const sources = candidate.sourceCarrierNames || [];
  if (candidate.a1KiniRuleVersion !== A1_KINI_RULE_VERSION
      || candidate.a1KiniReplyRateThresholdExclusive !== 0.04
      || candidate.replyRateLatestSendThresholdInclusive !== 50
      || candidate.replyRateWindow !== 'latest_day'
      || !/^\d{2}-\d{2}$/.test(String(candidate.latestDate || ''))
      || sources.length !== 1 || !isA1(sources[0])
      || candidate.targetCarrierName !== targetCarrierName
      || !hasCarrierGroupPrefix(targetCarrierName, ['B12'])) {
    throw new Error('A1＋Kini候选缺少独立规则门禁、精确A1来源或标准B12目标');
  }
  for (const row of rows) {
    const send = kiniWindowCount(row['发送窗口数']);
    const reply = kiniWindowCount(row['回复窗口数']);
    if (candidateSourceName(row) !== sources[0] || !isKini(row.opr) || /chatr/i.test(String(row.opr || ''))
        || row.latestDate !== candidate.latestDate || row['统计日期'] !== candidate.latestDate
        || send === null || reply === null || send < 50 || reply > send
        || BigInt(reply) * 100n >= BigInt(send) * 4n) {
      throw new Error('A1＋Kini候选未满足最新日发送>=50且回复率严格<4%，整组停止写入');
    }
    if (grouped) {
      const hits = grouped.get(shortCardNo(row.phone || row['电话'])) || [];
      if (hits.length !== 1 || !isKini(hits[0].opr) || /chatr/i.test(String(hits[0].opr || ''))) {
        throw new Error('A1＋Kini写前后台身份不唯一或OPR不再是唯一Kini，整组停止写入');
      }
    }
  }
}

// Lucky is a Task B expiry-routing exception; ordinary/C/D/F OPR scopes stay unchanged.
export function enforceTaskBLuckyRouting(candidate, targetCarrierName, liveRows = null) {
  if (!isTaskB(candidate) && !isTaskBRoute(candidate)) return;
  const lucky = value => /\blucky\b/i.test(String(value || ''));
  const rows = [...(candidate.rows || []), ...(candidate.candidates || [])];
  const declaredLucky = rows.filter(row => lucky(row.opr));
  const liveLucky = (liveRows || []).filter(row => lucky(row.opr));
  if (!declaredLucky.length && !liveLucky.length) return;
  const targets = new Map([
    ['task_b_expired_to_offline_t', '过期下架卡T'],
    ['task_b_tomorrow_fast_to_isolation_t', '快过期隔离区T'],
  ]);
  const expected = targets.get(candidate.route);
  if (!expected || expected !== targetCarrierName || candidate.targetCarrierName !== expected
      || [...declaredLucky, ...liveLucky].some(row => /chatr/i.test(String(row.opr || '')))) {
    throw new Error('任务B Lucky只允许唯一T系分区及对应T目标，拒绝错误或歧义路由');
  }
  if (liveRows !== null) {
    const grouped = groupBackendRows(liveRows);
    for (const row of declaredLucky) {
      const matches = grouped.get(shortCardNo(row.phone || row['电话'])) || [];
      const opr = String(matches[0]?.opr || '');
      if (matches.length !== 1 || /chatr/i.test(opr) || !(lucky(opr) || isTSeriesOpr(opr))) {
        throw new Error('任务B Lucky写前后台OPR缺失、身份不唯一或不再属于T系，停止整组');
      }
    }
  }
}

function enforceBStandardPriorityGate(candidate, targetCarrierName) {
  const route = String(candidate.route || '').trim();
  if (![B_STANDARD_USE2_ROUTE, B_STANDARD_GTE240_ROUTE].includes(route)) return;
  const configuredTarget = String(candidate.targetCarrierName || '').trim();
  const argumentTarget = String(targetCarrierName || '').trim();
  if (!configuredTarget || configuredTarget !== argumentTarget) {
    throw new Error(`B标准组候选目标与提交目标不一致: route=${route}`);
  }
  const expectedThreshold = route === B_STANDARD_USE2_ROUTE
    ? B_STANDARD_USE2_THRESHOLD
    : B_STANDARD_GTE240_THRESHOLD;
  if (Number(candidate.tenDaySendThreshold) !== expectedThreshold) {
    throw new Error(`B标准组候选阈值异常: route=${route}, expected=${expectedThreshold}`);
  }
  const rows = candidate.rows || candidate.candidates || [];
  const invalid = [];
  for (const row of rows) {
    const source = row['原运营商'] || row['运营商'] || row.sourceCarrierName || '';
    const tenDay = finiteNumber(row['10日发送窗口数']);
    const send = finiteNumber(row['发送窗口数']);
    const reply = finiteNumber(row['回复窗口数']);
    const useDays = String(row['koo是否2天'] || '').trim();
    if (!hasCarrierGroupPrefix(source, ['B1', 'B11'])) {
      invalid.push({ phone: row.phone || row['电话'] || '', reason: 'invalid_source', source });
    } else if (send !== null && reply !== null && send >= REPLY_RATE_SEND_THRESHOLD && reply === 0) {
      invalid.push({ phone: row.phone || row['电话'] || '', reason: 'priority_1_should_have_won', send, reply });
    } else if (route === B_STANDARD_USE2_ROUTE) {
      if (!hasCarrierGroupPrefix(configuredTarget, ['B11'])) {
        invalid.push({ phone: row.phone || row['电话'] || '', reason: 'invalid_b11_target' });
      } else if (candidate.useDaysFieldEquals !== '2天' || useDays !== '2天') {
        invalid.push({ phone: row.phone || row['电话'] || '', reason: 'use_days_not_exactly_2', useDays });
      } else if (!isTSeriesOpr(row.opr)) {
        invalid.push({ phone: row.phone || row['电话'] || '', reason: 'opr_not_t_series', opr: row.opr || '' });
      } else if (tenDay === null || tenDay >= B_STANDARD_USE2_THRESHOLD) {
        invalid.push({ phone: row.phone || row['电话'] || '', reason: 'ten_day_not_lt_240', tenDay });
      }
    } else if (!hasCarrierGroupPrefix(configuredTarget, ['B12'])) {
      invalid.push({ phone: row.phone || row['电话'] || '', reason: 'invalid_b12_target' });
    } else if (tenDay === null || tenDay < B_STANDARD_GTE240_THRESHOLD) {
      invalid.push({ phone: row.phone || row['电话'] || '', reason: 'ten_day_not_gte_240', tenDay });
    } else if (useDays === '2天' && tenDay < B_STANDARD_USE2_THRESHOLD) {
      invalid.push({ phone: row.phone || row['电话'] || '', reason: 'priority_2_should_have_won', tenDay });
    }
  }
  if (invalid.length) {
    throw new Error(`B标准组固定优先级候选校验失败${invalid.length}条，整组停止写入: ${JSON.stringify(invalid.slice(0, 20))}`);
  }
}

function enforceVZonePriorityGate(candidate, targetCarrierName) {
  const route = String(candidate.route || '').trim();
  if (![V_ZONE_ZERO_ROUTE, V_ZONE_USE2_ROUTE, V_ZONE_GTE240_ROUTE].includes(route)) return;
  const configuredTarget = String(candidate.targetCarrierName || '').trim();
  const argumentTarget = String(targetCarrierName || '').trim();
  if (!configuredTarget || configuredTarget !== argumentTarget) {
    throw new Error(`V组候选目标与提交目标不一致: route=${route}`);
  }
  const rows = candidate.rows || candidate.candidates || [];
  const invalid = [];

  if (route === V_ZONE_ZERO_ROUTE) {
    if (Number(candidate.vZonePriority) !== 1 || configuredTarget !== 'E-T休息区') {
      throw new Error('V组优先级1候选缺少优先级标记或目标不是E-T休息区');
    }
    for (const row of rows) {
      const source = row['原运营商'] || row['运营商'] || row.sourceCarrierName || '';
      if (!hasCarrierGroupPrefix(source, ['B1', 'B11', 'B12', 'B1V', 'B11V', 'B12V'])) {
        invalid.push({ phone: row.phone || row['电话'] || '', reason: 'invalid_source', source });
        continue;
      }
      const send = finiteNumber(row['发送窗口数']);
      const reply = finiteNumber(row['回复窗口数']);
      if (send === null || send < REPLY_RATE_SEND_THRESHOLD || reply !== 0) {
        invalid.push({ phone: row.phone || row['电话'] || '', reason: 'priority_1_condition', send, reply });
      }
    }
  } else if (route === V_ZONE_USE2_ROUTE) {
    if (
      Number(candidate.vZonePriority) !== 2
      || candidate.useDaysFieldEquals !== V_ZONE_USE_DAYS_FIELD_VALUE
      || !hasCarrierGroupPrefix(configuredTarget, ['B11V'])
    ) {
      throw new Error('V组优先级2候选缺少固定优先级、koo是否2天字段或B11V目标门禁');
    }
    for (const row of rows) {
      const source = row['原运营商'] || row['运营商'] || row.sourceCarrierName || '';
      const tenDay = finiteNumber(row['10日发送窗口数']);
      const send = finiteNumber(row['发送窗口数']);
      const reply = finiteNumber(row['回复窗口数']);
      if (!hasCarrierGroupPrefix(source, ['B1V', 'B11V'])) {
        invalid.push({ phone: row.phone || row['电话'] || '', reason: 'invalid_source', source });
      } else if (String(row['koo是否2天'] || '').trim() !== V_ZONE_USE_DAYS_FIELD_VALUE) {
        invalid.push({ phone: row.phone || row['电话'] || '', reason: 'use_days_not_exactly_2', value: row['koo是否2天'] });
      } else if (tenDay === null || tenDay >= V_ZONE_TEN_DAY_THRESHOLD) {
        invalid.push({ phone: row.phone || row['电话'] || '', reason: 'ten_day_not_lt_240', tenDay });
      } else if (send !== null && reply !== null && send >= REPLY_RATE_SEND_THRESHOLD && reply === 0) {
        invalid.push({ phone: row.phone || row['电话'] || '', reason: 'priority_1_should_have_won', send, reply });
      }
    }
  } else {
    if (Number(candidate.vZonePriority) !== 3 || !hasCarrierGroupPrefix(configuredTarget, ['B12V'])) {
      throw new Error('V组优先级3候选缺少固定优先级或B12V目标门禁');
    }
    for (const row of rows) {
      const source = row['原运营商'] || row['运营商'] || row.sourceCarrierName || '';
      const tenDay = finiteNumber(row['10日发送窗口数']);
      const send = finiteNumber(row['发送窗口数']);
      const reply = finiteNumber(row['回复窗口数']);
      if (!hasCarrierGroupPrefix(source, ['B1V', 'B11V'])) {
        invalid.push({ phone: row.phone || row['电话'] || '', reason: 'invalid_source', source });
      } else if (tenDay === null || tenDay < V_ZONE_TEN_DAY_THRESHOLD) {
        invalid.push({ phone: row.phone || row['电话'] || '', reason: 'ten_day_not_gte_240', tenDay });
      } else if (send !== null && reply !== null && send >= REPLY_RATE_SEND_THRESHOLD && reply === 0) {
        invalid.push({ phone: row.phone || row['电话'] || '', reason: 'priority_1_should_have_won', send, reply });
      }
    }
  }
  if (invalid.length) {
    throw new Error(`V组固定优先级候选校验失败${invalid.length}条，整组停止写入: ${JSON.stringify(invalid.slice(0, 20))}`);
  }
}

export function validateTaskDRetentionCarrierProof(candidate, targetCarrierName, receipt, actualReceiptSha256) {
  if (
    !/^[a-f0-9]{64}$/.test(String(candidate.retentionReceiptSha256 || ''))
    || candidate.retentionReceiptSha256 !== actualReceiptSha256
  ) throw new Error('任务D运营商候选缺少有效的完整分页回执SHA-256绑定');
  if (receipt.task !== 'D' || receipt.sourceProvenance !== 'phone_card_retain_all_pages') {
    throw new Error('任务D卡保留回执来源无效');
  }
  if (!Object.hasOwn(receipt, 'filteredQueryProof')) {
    const retained = new Set((receipt.cardNos || []).map(shortCardNo).filter(Boolean));
    if (uniquePhones(candidate).some(phone => !retained.has(shortCardNo(phone)))) {
      throw new Error('任务D候选包含卡保留全部分页回执之外的号码');
    }
    return null;
  }
  const proof = validateFilteredRetentionProof(receipt);
  if (!String(candidate.runId || '').trim() || candidate.runId !== receipt.runId) {
    throw new Error('任务D运营商候选与筛选查询回执runId不一致');
  }
  const oprClass = new Map([
    ['B14-C快下保留卡', 'chatr'], ['B14-T快下保留卡', 'kooda'],
  ]).get(targetCarrierName);
  if (!oprClass || candidate.targetCarrierName !== targetCarrierName) {
    throw new Error('任务D筛选查询证明目标运营商不一致');
  }
  validateCandidateStructure(candidate);
  for (const row of candidate.rows) assertTemporaryCarrierAllowed(candidateSourceName(row));
  const expectedRows = proof.actionableRows.filter(row => row.oprClass === oprClass);
  const expectedByPhone = new Map(expectedRows.map(row => [shortCardNo(row.cardNo), row]));
  const declaredPhones = candidate.rows.map(row => shortCardNo(row['电话'] || row.phone));
  if (
    declaredPhones.length !== expectedByPhone.size
    || new Set(declaredPhones).size !== expectedByPhone.size
    || declaredPhones.some(phone => !expectedByPhone.has(phone))
  ) throw new Error('任务D运营商候选必须精确覆盖该目标组全部可处理筛选命中号码');
  for (const rows of [candidate.rows, candidate.candidates || candidate.rows]) {
    for (const row of rows) {
      const cardNo = shortCardNo(row['电话'] || row.phone);
      const source = expectedByPhone.get(cardNo);
      const expectedSourceName = String(source?.sourceCarrierName || carrierName(source || {})).trim();
      if (
        !source
        || String(row.backendId ?? '') !== String(source.id)
        || String(row.sourceCarrierIdentif || '') !== carrierIdentif(source)
        || !expectedSourceName || candidateSourceName(row) !== expectedSourceName
        || String(row.opr || '') !== String(source.opr || '') || row.oprClass !== oprClass
        || row.expectedTargetCarrierName !== targetCarrierName
        || ['cardNo', 'phone', '电话'].some(key => row[key] !== undefined && shortCardNo(row[key]) !== cardNo)
      ) throw new Error(`任务D运营商候选后台身份、来源或OPR与筛选查询证明不一致: ${cardNo}`);
    }
  }
  return proof;
}

export function validateTaskDRetentionLiveIdentity(candidate, rows, target) {
  const expectedOpr = target.name === 'B14-C快下保留卡' ? 'chatr'
    : target.name === 'B14-T快下保留卡' ? 'kooda' : '';
  const grouped = groupBackendRows(rows);
  for (const declared of candidate.rows || []) {
    const phone = shortCardNo(declared['电话'] || declared.phone);
    const hits = grouped.get(phone) || [];
    // Missing/duplicate live records continue to the existing precise source
    // classification. A unique row must still be the proven backend identity.
    if (hits.length !== 1) continue;
    const row = hits[0];
    if (!expectedOpr || !String(declared.backendId ?? '').trim()
      || String(row.id ?? '') !== String(declared.backendId)
      || retentionOprClass(row.opr) !== expectedOpr
      || (carrierIdentif(row) !== target.identif
        && carrierIdentif(row) !== String(declared.sourceCarrierIdentif || ''))) {
      throw new Error(`任务D写前后台身份、来源或OPR已偏离筛选证明: ${phone}`);
    }
  }
}

export async function enforceTaskAuthorization(candidate, targetCarrierName) {
  if (isUnexpiredRestore(candidate)) {
    await enforceUnexpiredRestore(candidate, targetCarrierName);
    return;
  }
  const task = String(candidate.task || '').trim().toUpperCase();
  if (task === 'D' || String(candidate.route || '').startsWith('task_d_retain_')
      || [targetCarrierName, candidate.targetCarrierName].some(name => /^B14(?:-|$)/i.test(String(name || '').trim()))) {
    throw new Error('转入B14保留区工作已删除，任务D不再修改运营商；旧候选和强制参数均不得绕过');
  }
  if (isTaskBRoute(candidate) && task !== 'B') {
    throw new Error('任务B路由与task标记不一致，拒绝提交');
  }
  if (task === 'RESTORE_QUICK_RENEW_C') {
    if (
      process.env.OPENSMS_FORCE_QUICK_RENEW_C_RESTORE !== '1'
      || candidate.forceGate !== true
      || candidate.restoreGate !== true
      || candidate.route !== 'restore_quick_renew_c_from_20260902_forced_tasks'
      || String(candidate.targetCarrierName || '').trim() !== QUICK_RENEW_C_RESTORE_TARGET
      || String(targetCarrierName || '').trim() !== QUICK_RENEW_C_RESTORE_TARGET
    ) {
      throw new Error('快过期续费C恢复门禁、路由或目标不完整，拒绝提交');
    }
    return;
  }
  if (task === 'B') {
    if (process.env.OPENSMS_FORCE_TASK_B !== '1') {
      throw new Error('OPENSMS_FORCE_TASK_B 未严格等于 1，拒绝提交任务B候选');
    }
    if (candidate.forceGate !== true) {
      throw new Error('任务B候选缺少强制授权门禁标记，拒绝提交');
    }
    const target = String(candidate.targetCarrierName || '').trim();
    const allowedRoutes = new Map([
      ['task_b_expired_to_offline', '过期下架卡'],
      ['task_b_expired_to_offline_c', '过期下架卡C'],
      ['task_b_expired_to_offline_t', '过期下架卡T'],
      ['task_b_tomorrow_fast_to_isolation', '快过期隔离区'],
      ['task_b_tomorrow_fast_to_isolation_c', '快过期隔离区C'],
      ['task_b_tomorrow_fast_to_isolation_t', '快过期隔离区T'],
    ]);
    const expectedTarget = allowedRoutes.get(String(candidate.route || ''));
    if (!expectedTarget || target !== expectedTarget || target !== String(targetCarrierName || '').trim()) {
      throw new Error(`任务B路由或目标异常: route=${candidate.route || '空'}, candidate=${target || '空'}, argument=${targetCarrierName}`);
    }
    enforceTaskBDeadlineGate(candidate, targetCarrierName);
    enforceTaskBAllCarrierRouting(candidate, targetCarrierName);
    enforceTaskBLuckyRouting(candidate, targetCarrierName);
    return;
  }
  if (task !== 'C') return;
  if (process.env.OPENSMS_FORCE_TASK_C !== '1') {
    throw new Error('OPENSMS_FORCE_TASK_C 未严格等于 1，拒绝提交任务C候选');
  }
  if (candidate.forceGate !== true) {
    throw new Error('任务C候选缺少强制授权门禁标记，拒绝提交');
  }
  const configuredTarget = String(candidate.targetCarrierName || '').trim();
  const allowedRoutes = new Map([
    ['task_c_kooda_4day_reply_rate_lt5_to_swap', '换号区T'],
    ['task_c_chatr_unchanged_4day_reply_rate_lt4_to_swap', '换号区C'],
  ]);
  const expectedTarget = allowedRoutes.get(String(candidate.route || ''));
  if (!expectedTarget || configuredTarget !== expectedTarget) {
    throw new Error(
      `任务C路由或目标异常: route=${candidate.route || '空'}, target=${configuredTarget || '空'}`,
    );
  }
  if (!configuredTarget || configuredTarget !== String(targetCarrierName || '').trim()) {
    throw new Error(`任务C目标运营商与候选不一致: candidate=${configuredTarget || '空'}, argument=${targetCarrierName}`);
  }
  enforceTaskBPriorityLockForTaskC(candidate);
}

function isTaskC(candidate) {
  return String(candidate.task || '').trim().toUpperCase() === 'C';
}

function isTaskB(candidate) {
  return String(candidate.task || '').trim().toUpperCase() === 'B';
}

function isTaskD(candidate) {
  return String(candidate.task || '').trim().toUpperCase() === 'D';
}

function beijingParts(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map(item => [item.type, item.value]));
  return {
    date: `${values.year}-${values.month}-${values.day}`,
    hour: Number(values.hour),
  };
}

async function claimC2DailyWrite(candidate, candidatePath) {
  if (candidate.route !== 'c2_previous_day_reply_rate_lt5_to_a11') return null;
  const gate = candidate.c2DailyExecution;
  if (!gate || gate.eligible !== true || gate.status !== 'evaluation_completed') {
    throw new Error('C2每日门禁未授权本轮写入：候选不是北京时间21:00前的当日首次执行');
  }
  if (gate.timezone !== 'Asia/Shanghai' || Number(gate.cutoffHourExclusive) !== 21) {
    throw new Error('C2每日门禁配置异常：必须使用北京时间且21:00为排他截止时间');
  }
  const now = beijingParts();
  if (now.date !== gate.beijingDate || now.hour >= 21) {
    throw new Error(`C2写入已超过授权时段：当前北京时间日期=${now.date}, 小时=${now.hour}`);
  }
  const statePath = path.resolve(String(gate.stateReceiptPath || ''));
  if (!gate.stateReceiptPath) throw new Error('C2候选缺少每日执行回执路径');
  const state = JSON.parse(await fs.readFile(statePath, 'utf8'));
  if (
    state.runStamp !== gate.runStamp
    || state.beijingDate !== gate.beijingDate
    || state.status !== 'evaluation_completed'
  ) {
    throw new Error('C2每日执行回执与候选不一致，已停止写入');
  }
  const lockPath = `${statePath}.write.lock`;
  let lock;
  try {
    lock = await fs.open(lockPath, 'wx');
    await lock.writeFile(JSON.stringify({
      claimedAt: new Date().toISOString(),
      candidatePath: path.resolve(candidatePath),
      runStamp: gate.runStamp,
      beijingDate: gate.beijingDate,
    }, null, 2));
  } catch (error) {
    if (error?.code === 'EEXIST') {
      throw new Error('C2今日写入已被执行或占用，禁止同日第二次提交');
    }
    throw error;
  } finally {
    await lock?.close().catch(() => {});
  }
  const startedAt = new Date().toISOString();
  await fs.writeFile(statePath, JSON.stringify({
    ...state,
    status: 'write_started',
    writeStartedAt: startedAt,
    writeCandidatePath: path.resolve(candidatePath),
    writeLockPath: lockPath,
  }, null, 2));
  return { statePath, lockPath, state, startedAt };
}

async function completeC2DailyWrite(guard, receiptPath, before, after) {
  if (!guard) return;
  await fs.writeFile(guard.statePath, JSON.stringify({
    ...guard.state,
    status: 'write_completed',
    writeStartedAt: guard.startedAt,
    writeCompletedAt: new Date().toISOString(),
    writeReceiptPath: receiptPath,
    writeLockPath: guard.lockPath,
    actualWriteCount: before.eligibleCount,
    verifiedTargetCount: after?.targetCount ?? null,
    verifiedStillNotTargetCount: after?.stillNotTargetCount ?? null,
    independentReadbackPerformed: after !== null,
  }, null, 2));
}

function uniquePhones(candidate) {
  const byKey = new Map();
  for (const phone of targetPhonesFromCandidate(candidate)) {
    const key = shortCardNo(phone);
    if (key && !byKey.has(key)) byKey.set(key, String(phone));
  }
  return [...byKey.values()];
}

function exactCarrier(carriers, targetCarrierName) {
  const matches = carriers.filter(row => carrierName(row) === targetCarrierName);
  if (matches.length !== 1) {
    throw new Error(`目标运营商精确匹配异常: ${targetCarrierName}, count=${matches.length}`);
  }
  const identif = carrierIdentif(matches[0]);
  if (!identif) throw new Error(`目标运营商缺少ID: ${targetCarrierName}`);
  return {
    name: carrierName(matches[0]),
    identif,
    id: matches[0].id,
    delData: matches[0].delData ?? null,
  };
}

function resolveSourceCarriers(carriers, sourceCarrierNames, candidate) {
  const resolved = [];
  const hiddenProtectedSourceNames = [];
  for (const sourceName of sourceCarrierNames) {
    const matches = carriers.filter(row => carrierName(row) === sourceName);
    if (
      matches.length === 0
      && isProtectedSourceCarrier(sourceName)
      && (isTaskB(candidate) || isTaskD(candidate))
    ) {
      hiddenProtectedSourceNames.push(sourceName);
      continue;
    }
    if (matches.length !== 1) {
      throw new Error(`来源运营商精确匹配异常: ${sourceName}, count=${matches.length}`);
    }
    const identif = carrierIdentif(matches[0]);
    if (!identif) throw new Error(`来源运营商缺少ID: ${sourceName}`);
    resolved.push({ name: sourceName, identif, id: matches[0].id });
  }
  return { resolved, hiddenProtectedSourceNames };
}

async function listPhonesBatched(api, phones) {
  const rows = [];
  for (let index = 0; index < phones.length; index += BATCH_SIZE) {
    const chunk = phones.slice(index, index + BATCH_SIZE);
    const result = await listPhonesByNumbers(api, chunk, BATCH_SIZE);
    rows.push(...result.rows);
  }
  return rows;
}

function groupBackendRows(rows) {
  const grouped = new Map();
  for (const row of rows) {
    const key = backendPhone(row);
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(row);
  }
  return grouped;
}

function targetMatches(row, target) {
  return carrierIdentif(row) === target.identif || carrierName(row) === target.name;
}

function sourceRowByPhone(candidate) {
  const result = new Map();
  for (const row of candidate.rows || candidate.candidates || []) {
    const key = shortCardNo(row['电话'] || row.phone);
    if (key && !result.has(key)) result.set(key, row);
  }
  return result;
}

function candidateSourceName(row) {
  return String(
    row?.['运营商']
    || row?.sourceCarrierName
    || row?.sourceCarrier
    || row?.['原运营商']
    || '',
  ).trim();
}

function representedSourceNames(candidate) {
  const names = new Set();
  for (const row of candidate.rows || candidate.candidates || []) {
    const name = row['运营商'] || row.sourceCarrierName || row.sourceCarrier || row['原运营商'];
    if (name) names.add(String(name));
  }
  return names;
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
  for (const key of ['sourceCarrierNames', 'configuredSourceCarrierNames']) {
    if (candidate[key] !== undefined && !Array.isArray(candidate[key])) {
      throw new Error(`候选清单${key}必须为数组`);
    }
  }
}

async function saveReceipt(receipt, label) {
  const receiptPath = path.join(SNAPSHOT_DIR, `opensms_set_carrier_${label}_${stamp()}.json`);
  await fs.writeFile(receiptPath, JSON.stringify(receipt, null, 2));
  return receiptPath;
}

async function main() {
  const [candidatePath, targetCarrierName, rawLabel = 'set_carrier'] = process.argv.slice(2);
  if (!candidatePath || !targetCarrierName) {
    throw new Error('usage: node opensms_set_carrier_candidates.mjs candidates.json exactTargetCarrierName [label]');
  }
  if (isAbolishedB13Carrier(targetCarrierName)) {
    throw new Error(`B13已废除，禁止将任何号码写入该运营商: ${targetCarrierName}`);
  }

  await ensureArtifactDirs();
  const label = safeLabel(rawLabel);
  const candidate = JSON.parse(await fs.readFile(candidatePath, 'utf8'));
  validateCandidateStructure(candidate);
  enforceVGroupBoundary(candidate, targetCarrierName);
  enforceRestZoneEntryGate(candidate, targetCarrierName);
  for (const name of representedSourceNames(candidate)) {
    if (!isAllCarrierTaskB(candidate) && String(name || '').trim() === '三方隔离') throw new Error('三方隔离保护：禁止修改该区号码运营商');
  }
  for (const name of [...(candidate.sourceCarrierNames || []), ...(candidate.configuredSourceCarrierNames || [])]) {
    if (!isAllCarrierTaskB(candidate) && String(name || '').trim() === '三方隔离') throw new Error('三方隔离保护：禁止修改该区号码运营商');
  }
  enforceTaskBRetainedDeclaredSources(candidate);
  await enforceTaskAuthorization(candidate, targetCarrierName);
  if (!isTaskB(candidate)) enforceTaskBDeadlineGate(candidate, targetCarrierName);
  enforceA1KiniRule(candidate, targetCarrierName);
  enforceReplyRateSendGate(candidate);
  enforceBStandardPriorityGate(candidate, targetCarrierName);
  enforceVZonePriorityGate(candidate, targetCarrierName);
  const c2DailyWriteGuard = await claimC2DailyWrite(candidate, candidatePath);
  const allowedSources = new Set(
    (candidate.sourceCarrierNames || candidate.configuredSourceCarrierNames || []).map(String),
  );
  const phones = uniquePhones(candidate);
  if (phones.length && !allowedSources.size) {
    throw new Error('候选文件缺少精确允许来源 sourceCarrierNames，已停止写入');
  }
  const configuredProtectedSources = [...allowedSources].filter(isProtectedSourceCarrier);
  const configuredNeverProcessSources = [...allowedSources].filter(isNeverProcessSourceCarrier);
  const representedNeverProcessSources = [...representedSourceNames(candidate)].filter(isNeverProcessSourceCarrier);
  if (!isAllCarrierTaskB(candidate) && (configuredNeverProcessSources.length || representedNeverProcessSources.length)) {
    throw new Error(`快过期续费C/T永久保护：禁止作为写入来源: ${[...new Set([...configuredNeverProcessSources, ...representedNeverProcessSources])].join('、')}`);
  }
  if (configuredProtectedSources.length && !isTaskB(candidate) && !isTaskD(candidate)) {
    throw new Error(`A21/A2号码运营商受保护，禁止作为写入来源: ${configuredProtectedSources.join('、')}`);
  }

  if (!phones.length) {
    const validatedPath = path.join(SNAPSHOT_DIR, `opensms_validated_${label}_${stamp()}_candidates.json`);
    const excluded = { alreadyTargetCount: 0, missingCount: 0, duplicateBackendCount: 0, nonSourceCount: 0 };
    await fs.writeFile(validatedPath, JSON.stringify({
      ...candidate,
      sourceCandidate: path.resolve(candidatePath),
      validatedAt: new Date().toISOString(),
      validationStatus: 'no_candidate',
      targetCarrierName,
      originalCount: 0,
      count: 0,
      excluded,
      rows: [],
      candidates: [],
    }, null, 2));
    const before = {
      mode: 'not_required', apiTransport: null, candidateCount: 0, eligibleCount: 0,
      ...excluded, missingPhones: [], duplicateBackend: [], nonSource: [], alreadyTarget: [], eligibleRows: [],
    };
    const receipt = {
      ts: new Date().toISOString(), mode: 'protocol-api', apiTransport: null,
      status: 'no_candidate', noWrite: true, actualWriteCount: 0,
      independentReadbackRequired: false, independentReadbackPerformed: false,
      verificationStatus: 'not_required', stageReadbackRequired: false,
      candidatePath: path.resolve(candidatePath), validatedCandidatePath: validatedPath,
      originBackupPath: null, target: { name: targetCarrierName, identif: null, id: null, resolved: false },
      sourceCarriers: [], hiddenProtectedSourceCarriers: [], sourceCarrierNames: [...allowedSources],
      phones: [], before, prep: before, updateResponses: [], after: null,
    };
    const receiptPath = await saveReceipt(receipt, label);
    await completeC2DailyWrite(c2DailyWriteGuard, receiptPath, before, null);
    console.log(JSON.stringify({ path: receiptPath, ...receipt }, null, 2));
    return;
  }

  const session = await createOpenSmsApiSession({ cdp: CDP, pathName: '/phoneConfig' });
  try {
    const carriers = await getCarrierConfigs(session.api);
    const target = exactCarrier(carriers, targetCarrierName);
    if ((isAllCarrierTaskB(candidate) || isTaskC(candidate) || isTaskD(candidate)) && target.delData === true) {
      throw new Error('任务B/任务C/任务D目标运营商当前已停用或删除，拒绝写入');
    }
    const representedSources = representedSourceNames(candidate);
    const requiredSourceNames = representedSources.size
      ? [...allowedSources].filter(name => representedSources.has(name))
      : [...allowedSources];
    const sourceResolution = resolveSourceCarriers(carriers, requiredSourceNames, candidate);
    const visibleSourceCarriers = sourceResolution.resolved;
    const hiddenProtectedSourceNames = new Set(sourceResolution.hiddenProtectedSourceNames);
    const beforeRows = phones.length ? await listPhonesBatched(session.api, phones) : [];
    enforceA1KiniRule(candidate, targetCarrierName, beforeRows);
    enforceTaskBLuckyRouting(candidate, targetCarrierName, beforeRows);
    enforceTaskBAllCarrierRouting(candidate, targetCarrierName, beforeRows);
    validateUnexpiredLiveIdentity(candidate, beforeRows, carriers, target);
    if (isTaskD(candidate)) validateTaskDRetentionLiveIdentity(candidate, beforeRows, target);
    const grouped = groupBackendRows(beforeRows);
    const candidateRows = sourceRowByPhone(candidate);
    const visibleCarrierNameByIdentif = new Map(
      carriers.map(row => [carrierIdentif(row), carrierName(row)]),
    );
    const hiddenSourceIdentifs = new Map(
      [...hiddenProtectedSourceNames].map(name => [name, new Set()]),
    );
    for (const phone of phones) {
      const key = shortCardNo(phone);
      const declaredSource = candidateSourceName(candidateRows.get(key));
      if (!hiddenProtectedSourceNames.has(declaredSource)) continue;
      const hits = grouped.get(key) || [];
      if (hits.length !== 1 || targetMatches(hits[0], target)) continue;
      const liveIdentif = carrierIdentif(hits[0]);
      if (!liveIdentif) {
        throw new Error(`隐藏保护组来源缺少后台运营商ID: ${declaredSource}, phone=${phone}`);
      }
      const visibleName = visibleCarrierNameByIdentif.get(liveIdentif);
      if (visibleName && visibleName !== declaredSource) {
        throw new Error(
          `隐藏保护组来源与后台可见运营商冲突: candidate=${declaredSource}, live=${visibleName}, phone=${phone}`,
        );
      }
      hiddenSourceIdentifs.get(declaredSource).add(liveIdentif);
    }
    const hiddenSourceCarriers = [];
    for (const [name, identifs] of hiddenSourceIdentifs) {
      if (identifs.size > 1) {
        throw new Error(`隐藏保护组来源出现多个后台运营商ID: ${name}, count=${identifs.size}`);
      }
      if (identifs.size === 1) {
        hiddenSourceCarriers.push({
          name,
          identif: [...identifs][0],
          id: null,
          hiddenProtectedSource: true,
          evidence: 'fresh_candidate_plus_live_unique_phone_readback',
        });
      }
    }
    const sourceCarriers = [...visibleSourceCarriers, ...hiddenSourceCarriers];
    const resolvedSourceNames = new Set(sourceCarriers.map(row => row.name));
    const allowedSourceIdentifs = new Set(sourceCarriers.map(row => row.identif));
    const sourceNameByIdentif = new Map(sourceCarriers.map(row => [row.identif, row.name]));
    enforceTaskBRetainedLiveSources(candidate, beforeRows, carriers);
    enforceVGroupBoundary(candidate, targetCarrierName, beforeRows.map(row =>
      visibleCarrierNameByIdentif.get(carrierIdentif(row)) || carrierName(row)));
    const liveProtectedRows = [];
    const liveNeverProcessRows = [];
    for (const phone of phones) {
      const hits = grouped.get(shortCardNo(phone)) || [];
      if (hits.length === 1 && isProtectedSourceCarrier(carrierName(hits[0]))) {
        liveProtectedRows.push({ phone, row: compactPhoneRow(hits[0]) });
      }
      if (hits.length === 1 && isNeverProcessSourceCarrier(carrierName(hits[0]))) {
        liveNeverProcessRows.push({ phone, row: compactPhoneRow(hits[0]) });
      }
    }
    if (liveNeverProcessRows.length && !isAllCarrierTaskB(candidate)) {
      throw new Error(`快过期续费C/T永久保护：后台实时来源中发现${liveNeverProcessRows.length}个受保护号码，整组停止写入`);
    }
    if (liveProtectedRows.length && !isTaskB(candidate) && !isTaskD(candidate)) {
      throw new Error(`后台实时来源中发现${liveProtectedRows.length}个A21/A2受保护号码，整组停止写入`);
    }
    const eligible = [];
    const alreadyTarget = [];
    const missingPhones = [];
    const duplicateBackend = [];
    const nonSource = [];

    for (const phone of phones) {
      const key = shortCardNo(phone);
      const hits = grouped.get(key) || [];
      if (!hits.length) {
        missingPhones.push(phone);
      } else if (hits.length !== 1) {
        duplicateBackend.push({ phone, rows: hits.map(compactPhoneRow) });
      } else if (targetMatches(hits[0], target)) {
        alreadyTarget.push({ phone, row: compactPhoneRow(hits[0]) });
      } else if (
        !resolvedSourceNames.has(carrierName(hits[0]))
        && !allowedSourceIdentifs.has(carrierIdentif(hits[0]))
      ) {
        nonSource.push({ phone, row: compactPhoneRow(hits[0]) });
      } else if (!hits[0].id) {
        missingPhones.push(phone);
      } else {
        eligible.push({ phone, row: hits[0] });
      }
    }

    const validatedRows = eligible.map(({ phone, row }) => ({
      ...(candidateRows.get(shortCardNo(phone)) || { 电话: phone, phone }),
      后台ID: row.id,
      精确原运营商: carrierName(row) || sourceNameByIdentif.get(carrierIdentif(row)) || '',
      原运营商ID: carrierIdentif(row),
      目标运营商: target.name,
      目标运营商ID: target.identif,
    }));
    if (isAllCarrierTaskB(candidate)) {
      if (missingPhones.length || duplicateBackend.length || nonSource.length) {
        throw new Error(`任务B全运营商覆盖失败：缺失${missingPhones.length}、重复${duplicateBackend.length}、来源不符${nonSource.length}，禁止部分执行`);
      }
      for (const { phone, row } of eligible) {
        const actualSource = visibleCarrierNameByIdentif.get(carrierIdentif(row)) || carrierName(row)
          || sourceNameByIdentif.get(carrierIdentif(row));
        if (actualSource !== candidateSourceName(candidateRows.get(shortCardNo(phone)))) {
          throw new Error('任务B全运营商写前精确来源变化，停止整组，不借其它允许来源放行');
        }
      }
    }
    const validatedPath = path.join(
      SNAPSHOT_DIR,
      `opensms_validated_${label}_${stamp()}_candidates.json`,
    );
    const validatedCandidate = {
      ...candidate,
      sourceCandidate: path.resolve(candidatePath),
      validatedAt: new Date().toISOString(),
      targetCarrierName: target.name,
      targetCarrierIdentif: target.identif,
      sourceCarrierNames: [...allowedSources],
      hiddenProtectedSourceCarriers: hiddenSourceCarriers,
      originalCount: phones.length,
      count: validatedRows.length,
      excluded: {
        alreadyTargetCount: alreadyTarget.length,
        missingCount: missingPhones.length,
        duplicateBackendCount: duplicateBackend.length,
        nonSourceCount: nonSource.length,
      },
      rows: validatedRows,
      candidates: validatedRows,
    };
    await fs.writeFile(validatedPath, JSON.stringify(validatedCandidate, null, 2));

    let originBackupPath = null;
    if (isRestCarrier(target.name)) {
      const backupRows = validatedRows.map(row => ({
        ...row,
        原运营商: row.精确原运营商 || row.原运营商 || '',
        原运营商ID: row.原运营商ID || '',
      }));
      const byOrigin = {};
      for (const row of backupRows) {
        const origin = row.原运营商 || '归属未识别';
        byOrigin[origin] = (byOrigin[origin] || 0) + 1;
      }
      originBackupPath = path.join(
        SNAPSHOT_DIR,
        `opensms_e_origin_backup_${label}_${stamp()}.json`,
      );
      await fs.writeFile(originBackupPath, JSON.stringify({
        createdAt: new Date().toISOString(),
        rule: '写入E-休息前按后台唯一记录备份精确原运营商',
        sourceCandidate: path.resolve(candidatePath),
        validatedCandidatePath: validatedPath,
        sourceCarrierNames: [...allowedSources],
        targetCarrierName: target.name,
        targetCarrierIdentif: target.identif,
        count: backupRows.length,
        byOrigin,
        rows: backupRows,
        candidates: backupRows,
      }, null, 2));
    }

    const ids = eligible.map(item => item.row.id);
    for (const { row } of eligible) {
      if (!isAllCarrierTaskB(candidate)) assertTemporaryCarrierAllowed(carrierName(row) || sourceNameByIdentif.get(carrierIdentif(row)));
    }
    const updateResponses = [];
    for (let index = 0; index < ids.length; index += BATCH_SIZE) {
      updateResponses.push(await updatePhoneCarrier(
        session.api,
        ids.slice(index, index + BATCH_SIZE),
        target.identif,
      ));
    }
    const noWrite = ids.length === 0;
    // No submission means no per-group write readback. The stage collector must
    // still independently read all original candidates before refreshing files.
    const afterRows = noWrite ? [] : await listPhonesBatched(session.api, phones);
    const afterGrouped = groupBackendRows(afterRows);
    const atTarget = [];
    const stillNotTarget = [];
    const afterMissing = [];
    const afterDuplicate = [];
    for (const phone of noWrite ? [] : phones) {
      const hits = afterGrouped.get(shortCardNo(phone)) || [];
      if (!hits.length) {
        afterMissing.push(phone);
      } else if (hits.length !== 1) {
        afterDuplicate.push({ phone, rows: hits.map(compactPhoneRow) });
      } else if (targetMatches(hits[0], target)) {
        atTarget.push(phone);
      } else {
        stillNotTarget.push({ phone, row: compactPhoneRow(hits[0]) });
      }
    }

    const before = {
      mode: 'protocol-api',
      apiTransport: session.apiTransport,
      candidateCount: phones.length,
      eligibleCount: eligible.length,
      alreadyTargetCount: alreadyTarget.length,
      missingCount: missingPhones.length,
      duplicateBackendCount: duplicateBackend.length,
      nonSourceCount: nonSource.length,
      missingPhones,
      duplicateBackend,
      nonSource,
      alreadyTarget,
      eligibleRows: eligible.map(item => compactPhoneRow(item.row)),
    };
    const after = noWrite ? null : {
      mode: 'protocol-api',
      total: phones.length,
      targetCount: atTarget.length,
      stillNotTargetCount: stillNotTarget.length,
      missingCount: afterMissing.length,
      duplicateBackendCount: afterDuplicate.length,
      stillNotTarget,
      missingPhones: afterMissing,
      duplicateBackend: afterDuplicate,
      rows: afterRows.map(compactPhoneRow),
    };
    const receipt = {
      ts: new Date().toISOString(),
      mode: 'protocol-api',
      apiTransport: session.apiTransport,
      status: noWrite ? 'no_eligible_records' : 'written',
      noWrite,
      actualWriteCount: ids.length,
      independentReadbackRequired: !noWrite,
      independentReadbackPerformed: !noWrite,
      verificationStatus: noWrite ? 'not_required' : 'performed',
      stageReadbackRequired: phones.length > 0,
      candidatePath: path.resolve(candidatePath),
      validatedCandidatePath: validatedPath,
      originBackupPath,
      target,
      sourceCarriers,
      hiddenProtectedSourceCarriers: hiddenSourceCarriers,
      sourceCarrierNames: [...allowedSources],
      phones,
      before,
      prep: before,
      updateResponses,
      after,
    };
    const receiptPath = await saveReceipt(receipt, label);
    await completeC2DailyWrite(c2DailyWriteGuard, receiptPath, before, after);
    console.log(JSON.stringify({ path: receiptPath, validatedCandidatePath: validatedPath, ...receipt }, null, 2));
  } finally {
    await session.browser.close().catch(() => {});
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => {
    console.error(error.stack || String(error));
    process.exit(1);
  });
}
