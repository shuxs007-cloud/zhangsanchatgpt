import fs from 'node:fs/promises';
import path from 'node:path';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {fileURLToPath} from 'node:url';

import {REPORT_DIR, SNAPSHOT_DIR, ensureArtifactDirs} from '../opensms_artifact_paths.mjs';
import {createRawOpenSmsApiSession} from '../opensms_protocol_client.mjs';
import {collectCarrierConfigPagesReadOnly} from './opensms_carriers_readonly.mjs';
import {acquireSharedOpenSmsSession, SHARED_BROWSER_NAME} from './opensms_shared_browser.mjs';

const runFile = fileURLToPath(import.meta.url);
const runService = promisify(execFile);
export const monitoredGroups = ['A1', 'A11', 'A12', 'B1', 'B11', 'B12',
  'B1V', 'B11V', 'B12V', 'A2', 'A21', 'A3', 'B3', 'C2'];
const combinationGroups = ['A3', 'B1', 'B11', 'B3'];

function beijingParts(date) {
  return Object.fromEntries(new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(date).map(part => [part.type, part.value]));
}

export function checkpointFor(date = new Date()) {
  const p = beijingParts(date);
  const hour = Number(p.hour);
  const minute = Number(p.minute);
  const second = Number(p.second);
  const slotMinute = minute < 30 ? 0 : 30;
  const elapsed = (minute - slotMinute) * 60 + second;
  const inWindow = hour >= 1 && (hour < 11 || hour === 11 && slotMinute === 0);
  if (!inWindow || elapsed > 300) return null;
  const datePart = `${p.year}-${p.month}-${p.day}`;
  const timePart = `${p.hour}:${String(slotMinute).padStart(2, '0')}`;
  return {date: datePart, time: timePart, label: `${datePart} ${timePart}`,
    key: `${p.year}${p.month}${p.day}_${p.hour}${String(slotMinute).padStart(2, '0')}`};
}

export function checkpointQueueRemainingMs(checkpoint, now = new Date()) {
  const started = Date.parse(`${checkpoint.date}T${checkpoint.time}:00+08:00`);
  return Math.max(0, started + 301000 - now.getTime());
}

export async function waitForSharedSession(checkpoint, {
  acquire = acquireSharedOpenSmsSession, now = () => new Date(),
} = {}) {
  const remaining = checkpointQueueRemainingMs(checkpoint, now());
  if (!remaining) throw new Error('OPENSMS_CHECKPOINT_QUEUE_EXPIRED');
  const lease = await acquire({timeoutMs: remaining});
  if (!checkpointQueueRemainingMs(checkpoint, now())) {
    lease.release();
    throw new Error('OPENSMS_CHECKPOINT_QUEUE_EXPIRED');
  }
  return lease;
}

function carrierName(row) {
  return String(row.name ?? row.carrierName ?? row.carrireName ?? row.carrier ?? '').trim();
}

function exactGroup(name) {
  const normalized = name.toUpperCase();
  return monitoredGroups.find(group => normalized === group || normalized.startsWith(`${group}-`)
    || normalized.startsWith(`${group} `)) ?? null;
}

function quantity(value) {
  if (value === null || value === undefined) return null;
  const cleaned = String(value).replaceAll(',', '').trim();
  if (!cleaned) return null;
  const number = Number(cleaned);
  return Number.isSafeInteger(number) && number >= 0 ? number : null;
}

export function carrierBalances(rows) {
  return monitoredGroups.map(group => {
    const matches = rows.filter(row => exactGroup(carrierName(row)) === group);
    if (matches.length !== 1) return {group, name: null, dailyRemaining: null,
      dailyTotal: null, ratio: null, status: 'unavailable',
      reason: `${group} 精确匹配数量为 ${matches.length}`};
    const row = matches[0];
    const remaining = quantity(row.dailyRemaining);
    const total = quantity(row.dailyTotal);
    if (remaining === null || total === null || remaining > total) {
      return {group, name: carrierName(row), dailyRemaining: null,
        dailyTotal: null, ratio: null, status: 'unavailable', reason: `${group} 余量数据无效`};
    }
    return {group, name: carrierName(row), dailyRemaining: remaining,
      dailyTotal: total, ratio: total ? remaining / total : null,
      status: total ? 'ok' : 'zero_total', reason: total ? null : '0/0，单组比例不可计算'};
  });
}

function weightedFromBalances(balances) {
  const records = combinationGroups.map(group => {
    const record = balances.find(row => row.group === group);
    if (record.status === 'unavailable') throw new Error(record.reason);
    return record;
  });
  const remaining = records.reduce((sum, row) => sum + row.dailyRemaining, 0);
  const total = records.reduce((sum, row) => sum + row.dailyTotal, 0);
  if (!Number.isSafeInteger(remaining) || !Number.isSafeInteger(total) || total <= 0) {
    throw new Error('四组合计数据无效');
  }
  return {records, dailyRemaining: remaining, dailyTotal: total,
    ratio: remaining / total, alert: remaining / total < 0.1};
}

export function weightedBalance(rows) {
  return weightedFromBalances(carrierBalances(rows));
}

export function readFailureDetails(error, phase) {
  const message = String(error?.message || '');
  if (message === 'OPENSMS_CHECKPOINT_QUEUE_EXPIRED'
      || message === 'OPENSMS_SHARED_SESSION_WAIT_TIMEOUT') {
    return {category: 'site_lock_timeout', phase, reason: '共用浏览器访问锁排队超出检查点五分钟窗口'};
  }
  if (/Timed out waiting for site lock:/.test(message)) {
    return {category: 'site_lock_timeout', phase, reason: '共用浏览器访问锁排队超出检查点五分钟窗口'};
  }
  if (/Abandoned site lock; inspect before removing:/.test(message)) {
    return {category: 'abandoned_site_lock', phase, reason: '共用浏览器访问锁疑似遗留，需人工核验'};
  }
  const diagnostic = error?.readDiagnostic;
  if (diagnostic) {
    const category = String(diagnostic.category || 'unknown');
    const attempts = Array.isArray(error.readAttempts) ? error.readAttempts.length : 1;
    const page = Number.isInteger(diagnostic.pageNum) ? `第${diagnostic.pageNum}页` : '';
    return {category, phase, attempts, pageNum: diagnostic.pageNum ?? null,
      reason: `运营商配置${page}只读接口失败（${category}，尝试${attempts}次）`};
  }
  const code = String(error?.code || message);
  if (/^OPENSMS_[A-Z0-9_]+$/.test(code)) {
    return {category: code, phase, reason: code};
  }
  return {category: 'unclassified_read_failure', phase, reason: '共用浏览器或只读接口检查失败'};
}

async function startSharedBrowser(action) {
  const service = path.join(path.dirname(runFile), 'opensms_new_browser.mjs');
  const {stdout} = await runService(process.execPath, [service, action], {timeout: 120000, maxBuffer: 32768});
  const status = JSON.parse(stdout.trim());
  return {status: status.status, headless: status.headless === true,
    loginWindowShown: status.loginWindowShown === true};
}

async function readBalance(checkpoint) {
  let lease;
  let phase = 'shared_session';
  try {
    try { lease = await waitForSharedSession(checkpoint); }
    catch (error) {
      if (error.message !== 'OPENSMS_SHARED_BROWSER_UNCONFIRMED') throw error;
      if (!checkpointQueueRemainingMs(checkpoint)) throw new Error('OPENSMS_CHECKPOINT_QUEUE_EXPIRED');
      const started = await startSharedBrowser('start');
      if (started.status !== 'ready') return {status: 'login_required', reason: '共用浏览器等待人工登录',
        browserForegroundActivationAttempted: started.loginWindowShown};
      lease = await waitForSharedSession(checkpoint);
    }
    if (lease.session.headless !== true) {
      phase = 'headless_transition';
      lease.release(); lease = null;
      if (!checkpointQueueRemainingMs(checkpoint)) throw new Error('OPENSMS_CHECKPOINT_QUEUE_EXPIRED');
      const background = await startSharedBrowser('background');
      if (background.status !== 'ready' || !background.headless) {
        return {status: 'login_required', reason: '等待人工登录后切回无头模式',
          browserForegroundActivationAttempted: background.loginWindowShown};
      }
      lease = await waitForSharedSession(checkpoint);
      if (lease.session.headless !== true) throw new Error('OPENSMS_HEADLESS_SESSION_UNCONFIRMED');
    }
    if (!checkpointQueueRemainingMs(checkpoint)) throw new Error('OPENSMS_CHECKPOINT_QUEUE_EXPIRED');
    phase = 'api_session';
    const session = await createRawOpenSmsApiSession({cdp: lease.session.cdp, pathName: '/index'});
    try {
      phase = 'carrier_config';
      const api = async (...args) => {
        const response = await session.api(...args);
        if (Number(response?.code) === 401) {
          throw Object.assign(new Error('OpenSMS API 登录失效'), {status: 401});
        }
        return response;
      };
      const carrierPage = await collectCarrierConfigPagesReadOnly(api, {concurrency: 1});
      const balances = carrierBalances(carrierPage.rows);
      const unavailable = balances.filter(row => row.status === 'unavailable').map(row => row.group);
      let balance;
      try {balance = weightedFromBalances(balances);}
      catch (error) {
        return {status: 'data_unavailable', reason: error.message, balances,
          unavailableGroups: unavailable, carrierPageCount: carrierPage.pageCount,
          headless: lease.session.headless === true};
      }
      return {status: unavailable.length ? 'partial' : 'ok', balance, balances,
        unavailableGroups: unavailable, carrierPageCount: carrierPage.pageCount,
        headless: lease.session.headless === true};
    } finally {
      await session.browser.close();
    }
  } catch (error) {
    if (error.status === 401) return {status: 'auth_expired', reason: 'OpenSMS API 返回 401'};
    if (error.message === '未从430当前页面目标获取到 Admin-Token') {
      return {status: 'auth_check_required', reason: '页面目标中未读取到 CK，待共用浏览器核验'};
    }
    const dataError = /精确匹配数量|余量数据无效|四组合计数据无效/.test(error.message);
    const failure = readFailureDetails(error, phase);
    return {status: dataError ? 'data_unavailable' : 'read_failed',
      ...failure, reason: dataError ? error.message : failure.reason};
  } finally {
    lease?.release();
  }
}

export function reportText(result) {
  const {checkpoint, balance, balances} = result;
  const lines = [
    `# OpenSMS 运营商余量监控（北京时间 ${checkpoint.label}）`, '',
    `- 状态：${result.status}`, `- 浏览器：${SHARED_BROWSER_NAME}（只读复用）`,
    `- silentBrowserMode=${result.silentBrowserMode}`,
    `- browserForegroundActivationAttempted=${result.browserForegroundActivationAttempted}`,
    '- 数据源：`GET /api/carrier/config/listNotDeleted`；未使用 BitBrowser 获取 CK。',
    `- 余量统计范围：${monitoredGroups.join('、')}；本监控脚本不写入绑定、加量、导出或号码。条件补绑如触发，以独立执行回执为准。`,
    '- 低于 10% 的组合提醒仅按 A3+B1+B11+B3 四组加权合计判断。', '',
    '- 共用浏览器访问锁按检查点排队；超出五分钟窗口不补查。', '',
  ];
  if (balances) {
    lines.push('| 运营商 | 剩余量 | 总量 | 余量比例 | 状态 |',
      '| --- | ---: | ---: | ---: | --- |');
    for (const row of balances) {
      const remaining = row.dailyRemaining ?? '不可用';
      const total = row.dailyTotal ?? '不可用';
      const ratio = row.ratio === null ? '不可计算' : `${(row.ratio * 100).toFixed(2)}%`;
      const status = row.status === 'unavailable' ? row.reason
        : row.status === 'zero_total' ? '0/0' : '正常';
      lines.push(`| ${row.group} | ${remaining} | ${total} | ${ratio} | ${status} |`);
    }
    lines.push('');
  } else {
    lines.push('本轮未取得运营商余量数据。', '');
  }
  if (balance) {
    lines.push(`四组加权合计：${balance.dailyRemaining}/${balance.dailyTotal} = ${(balance.ratio * 100).toFixed(2)}%。`,
      `严格低于 10%：${balance.alert ? '是，提醒' : '否'}。`);
  } else {
    lines.push(`四组加权比例不可用：${result.reason || '未取得有效数据'}。不依据旧值推断。`);
  }
  if (result.unavailableGroups?.length) lines.push(`不可用组：${result.unavailableGroups.join('、')}。`);
  if (result.loginRecovery) lines.push('', `登录处理：${result.loginRecovery}。`);
  lines.push('', `[本轮回执](${result.receipt})`, result.receipt);
  return `${lines.join('\n')}\n`;
}

async function main() {
  const checkpoint = checkpointFor();
  if (!checkpoint) return;
  const claims = path.join(path.dirname(SNAPSHOT_DIR), 'state', 'weighted_balance_monitor');
  await fs.mkdir(claims, {recursive: true, mode: 0o700});
  try {
    const claim = await fs.open(path.join(claims, `${checkpoint.key}.claim`), 'wx', 0o600);
    await claim.close();
  } catch (error) {
    if (error.code === 'EEXIST') return;
    throw error;
  }
  let result = {checkpoint, status: 'read_failed', silentBrowserMode: true,
    browserForegroundActivationAttempted: false, browser: SHARED_BROWSER_NAME};
  try {
    const read = await readBalance(checkpoint);
    result = {...result, ...read};
    if (read.browserForegroundActivationAttempted) result.silentBrowserMode = false;
    if (read.status === 'auth_check_required') {
      try {
        const verification = await startSharedBrowser('start');
        result.loginRecovery = verification.status === 'ready'
          ? '共用浏览器重新核验通过；本轮不使用旧数据，下轮复查'
          : '共用浏览器已确认需人工登录；本轮不使用旧数据';
        result.browserForegroundActivationAttempted = verification.loginWindowShown;
        if (verification.loginWindowShown) result.silentBrowserMode = false;
      } catch {
        result.loginRecovery = '共用浏览器核验失败；未在未确认失效时弹窗';
      }
    }
    if (read.status === 'auth_expired') {
      try {
        const recovery = await startSharedBrowser('login');
        result.loginRecovery = recovery.loginWindowShown || recovery.status !== 'ready'
          ? '已请求共用浏览器显示登录窗口，请人工完成登录；本轮不复用旧数据'
          : '共用浏览器已重新验证，请下轮复查';
        result.browserForegroundActivationAttempted = recovery.loginWindowShown;
        if (recovery.loginWindowShown) result.silentBrowserMode = false;
      } catch {
        result.loginRecovery = '登录窗口启动失败，请人工检查共用浏览器';
      }
    }
  } catch {
    result.status = 'read_failed';
    result.reason = '监控程序异常';
  }
  await ensureArtifactDirs();
  const suffix = `${checkpoint.key}_${Date.now()}`;
  const receipt = path.join(SNAPSHOT_DIR, `opensms_weighted_balance_${suffix}.json`);
  const report = path.join(REPORT_DIR, `opensms_weighted_balance_${suffix}.md`);
  result.receipt = receipt;
  result.report = report;
  await fs.writeFile(receipt, `${JSON.stringify(result, null, 2)}\n`, {flag: 'wx'});
  await fs.writeFile(report, reportText(result), {flag: 'wx'});
  console.log(JSON.stringify({checkpoint: checkpoint.label, status: result.status,
    balances: result.balances?.map(row => ({group: row.group,
      dailyRemaining: row.dailyRemaining, dailyTotal: row.dailyTotal,
      status: row.status})) || null,
    balance: result.balance ? {dailyRemaining: result.balance.dailyRemaining,
      dailyTotal: result.balance.dailyTotal, ratio: result.balance.ratio,
      alert: result.balance.alert} : null,
    reason: result.reason || null, loginRecovery: result.loginRecovery || null, report, receipt}));
  if (result.status !== 'ok') process.exitCode = 2;
}

if (process.argv[1] && path.resolve(process.argv[1]) === runFile) {
  main().catch(error => {
    console.error(JSON.stringify({status: 'fatal', reason: String(error.code || error.message).slice(0, 160)}));
    process.exitCode = 2;
  });
}
