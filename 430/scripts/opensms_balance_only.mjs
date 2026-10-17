import crypto from 'node:crypto';
import fssync from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { execFile as execFileCb } from 'node:child_process';
import { promisify } from 'node:util';
import { createRequire } from 'node:module';
import { LOG_DIR, SNAPSHOT_DIR, ensureArtifactDirs } from '../opensms_artifact_paths.mjs';
import { loadPagePayloadCodec } from '../opensms_payload_codec.mjs';

const require = createRequire(import.meta.url);
const execFile = promisify(execFileCb);

function loadPlaywright() {
  try {
    return require('playwright');
  } catch {}
  return require('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
}

const { chromium } = loadPlaywright();

const BASE = '/Users/zhangsan/Downloads/430';
const CDP = process.env.BITBROWSER_CDP || 'http://127.0.0.1:49538';
const CONFIG_URL = 'https://adminsms.smsoug.com/config';
const DAY_URL = 'https://adminsms.smsoug.com/day';
const LOGIN_URL = 'https://adminsms.smsoug.com/index';
const ADMIN_API_BASE = 'https://adminsms.smsoug.com/api';
const OUT_PREFIX = 'opensms_balance_only';
const REQUIRED_GROUPS = ['A1', 'A12', 'A11', 'B1', 'B11', 'B12', 'C2', 'A3', 'B3'];
const ALERT_GROUPS = ['A1', 'A12', 'A11', 'B1', 'B11', 'B12', 'C2', 'A3', 'B3'];
const A3_GROUP = 'A3';
const A3_BINDING_TARGET_GROUPS = ['A1', 'A11'];
let payloadCodec;
const SILENT_BROWSER_MODE = true;

class LoginRecoveryRequiredError extends Error {
  constructor(loginRecovery) {
    super('OpenSMS 登录状态失效，需要确认本次验证码后登录');
    this.name = 'LoginRecoveryRequiredError';
    this.loginRecovery = loginRecovery;
  }
}

function bjParts(date = new Date()) {
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Hong_Kong',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(date).reduce((acc, part) => {
    acc[part.type] = part.value;
    return acc;
  }, {});
}

function stampBj(date = new Date()) {
  const p = bjParts(date);
  return `${p.year}${p.month}${p.day}${p.hour}${p.minute}${p.second}`;
}

function localBj(date = new Date()) {
  const p = bjParts(date);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
}

function dateBj(date = new Date()) {
  const p = bjParts(date);
  return `${p.year}-${p.month}-${p.day}`;
}

function dateBjOffset(date, days) {
  return dateBj(new Date(date.getTime() + days * 86400000));
}

function toNumber(value) {
  if (value === undefined || value === null || value === '') return null;
  const n = Number(String(value).replace(/,/g, '').trim());
  return Number.isFinite(n) ? n : null;
}

function pct(dailyRemaining, dailyTotal) {
  if (!dailyTotal) return null;
  return dailyRemaining / dailyTotal * 100;
}

function parseRatePct(value) {
  if (value === undefined || value === null || value === '') return null;
  const parsed = Number.parseFloat(String(value).replace('%', '').trim());
  return Number.isFinite(parsed) ? parsed : null;
}

function groupFromName(name) {
  const text = String(name ?? '').trim();
  return REQUIRED_GROUPS.find(group => (
    text === group
    || text.startsWith(`${group}-`)
    || text.startsWith(`${group} `)
  )) || null;
}

function encryptPayload(payload) {
  if (!payloadCodec) throw new Error('未校验当前页面AES参数');
  return payloadCodec.encrypt(payload);
}

function parseResponseText(text) {
  if (!payloadCodec) throw new Error('未校验当前页面AES参数');
  return payloadCodec.parse(text);
}

async function pageOriginGetJson(page, token, url) {
  const response = await page.evaluate(async ({ requestUrl, authorization }) => {
    const result = await fetch(requestUrl, {
      method: 'GET',
      credentials: 'include',
      headers: {
        Accept: 'application/json, text/plain, */*',
        Authorization: authorization,
      },
    });
    return {
      ok: result.ok,
      status: result.status,
      text: await result.text(),
    };
  }, {
    requestUrl: url,
    authorization: token.startsWith('Bearer ') ? token : `Bearer ${token}`,
  });
  const parsed = parseResponseText(response.text);
  if (!response.ok || parsed?.code !== 200) {
    throw new Error(`页面同源接口读取失败: HTTP ${response.status} ${JSON.stringify(parsed)}`);
  }
  return parsed;
}

function shouldRefreshAfterCarrierError(error) {
  const text = String(error?.stack || error?.message || error || '');
  return /Cloudflare|Bad gateway|origin_bad_gateway|HTTP 502|HTTP 403|"code":403|code['"]?\s*[:=]\s*403|Unexpected token '<'|系统接口(?:403|502)异常|数据正在处理/.test(text);
}

async function isOpenSmsLoginPage(page) {
  return page.evaluate(() => {
    const hasAccount = Boolean(document.querySelector('input[placeholder="账号"]'));
    const hasPassword = Boolean(document.querySelector('input[placeholder="密码"]'));
    const hasCaptcha = Boolean(document.querySelector('input[placeholder="验证码"]'));
    const hasLoginButton = [...document.querySelectorAll('button')]
      .some(button => (button.innerText || '').replace(/\s+/g, '') === '登录');
    return location.pathname === '/login' || (hasAccount && hasPassword && hasCaptcha && hasLoginButton);
  }).catch(() => false);
}

async function inspectLoginRecovery(page, checkedAtDate = new Date()) {
  await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
  await page.waitForLoadState('networkidle', { timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(1200);

  const state = await page.evaluate(() => {
    const account = document.querySelector('input[placeholder="账号"]');
    const password = document.querySelector('input[placeholder="密码"]');
    const captcha = document.querySelector('input[placeholder="验证码"]');
    const captchaImage = document.querySelector('img.login-code-img');
    const loginButtonCount = [...document.querySelectorAll('button')]
      .filter(button => (button.innerText || '').replace(/\s+/g, '') === '登录').length;
    return {
      url: location.href,
      title: document.title,
      accountPresent: Boolean(account?.value),
      passwordPresent: Boolean(password?.value),
      captchaInputPresent: Boolean(captcha),
      captchaImagePresent: Boolean(captchaImage),
      captchaDataUrl: captchaImage?.src || null,
      loginButtonCount,
    };
  });

  let captchaImagePath = null;
  if (state.captchaImagePresent) {
    const match = state.captchaDataUrl?.match(/^data:[^;]+;base64,(.+)$/s);
    if (match) {
      const image = Buffer.from(match[1], 'base64');
      const extension = image[0] === 0xff && image[1] === 0xd8 && image[2] === 0xff
        ? 'jpg'
        : image[0] === 0x89 && image[1] === 0x50 && image[2] === 0x4e && image[3] === 0x47
          ? 'png'
          : 'img';
      captchaImagePath = path.join(LOG_DIR, `opensms_login_captcha_${stampBj(checkedAtDate)}.${extension}`);
      await fs.writeFile(captchaImagePath, image);
    } else {
      captchaImagePath = path.join(LOG_DIR, `opensms_login_captcha_${stampBj(checkedAtDate)}.png`);
      await page.locator('img.login-code-img').screenshot({ path: captchaImagePath });
    }
  }

  delete state.captchaDataUrl;

  return {
    required: true,
    confirmationRequired: true,
    submitted: false,
    retryCount: 0,
    captchaImagePath,
    ...state,
    reason: 'login_expired_waiting_for_current_captcha_confirmation',
  };
}

async function refreshConfigPage(page) {
  await page.goto(CONFIG_URL, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
  await page.waitForLoadState('networkidle', { timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(3000);
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
  await page.waitForLoadState('networkidle', { timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(3000);
}

function carrierName(row) {
  return row.name || row.carrierName || row.carrireName || row.carrier || '';
}

function carrierIdentif(row) {
  return String(row.identif || row.carrireIdentif || row.carrierIdentif || row.id || '');
}

function readDevtoolsPorts() {
  const cacheDir = '/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache';
  const ports = [];
  try {
    for (const item of fssync.readdirSync(cacheDir, { withFileTypes: true })) {
      if (!item.isDirectory()) continue;
      const portFile = path.join(cacheDir, item.name, 'DevToolsActivePort');
      try {
        const port = fssync.readFileSync(portFile, 'utf8').split(/\r?\n/)[0].trim();
        if (port) ports.push(`http://127.0.0.1:${port}`);
      } catch {}
    }
  } catch {}
  return ports;
}

async function connectOpenSmsBrowser() {
  const endpoints = process.env.OPENSMS_EXACT_CDP_ONLY === '1'
    ? [CDP]
    : [CDP, ...readDevtoolsPorts()];
  const seen = new Set();
  const errors = [];
  for (const endpoint of endpoints) {
    if (!endpoint || seen.has(endpoint)) continue;
    seen.add(endpoint);
    try {
      const browser = await chromium.connectOverCDP(endpoint);
      const contexts = browser.contexts();
      const pages = contexts.flatMap(context => context.pages());
      const page = pages.find(candidate => {
        try { return new URL(candidate.url()).origin === 'https://adminsms.smsoug.com'; }
        catch { return false; }
      });
      if (!page) {
        await browser.close().catch(() => {});
        continue;
      }
      return { browser, context: page.context(), page, endpoint };
    } catch (error) {
      errors.push({ endpoint, error: error.message });
    }
  }
  throw new Error(`无法连接 430 已登录 BitBrowser OpenSMS 会话: ${JSON.stringify(errors)}`);
}

async function getToken(context, page, adminBase) {
  const pageToken = await page.evaluate(() => {
    const raw = document.cookie.split('; ').find(part => part.startsWith('Admin-Token='));
    return raw ? decodeURIComponent(raw.slice('Admin-Token='.length)) : '';
  }).catch(() => '');
  const cookies = await context.cookies([
    adminBase,
    `${adminBase}/`,
    'https://adminsms.smsoug.com',
  ]);
  const tokenCookie = cookies.find(cookie => cookie.name === 'Admin-Token' && cookie.domain.includes('adminsms.smsoug.com'))
    || cookies.find(cookie => cookie.name === 'Admin-Token');
  const token = pageToken || tokenCookie?.value;
  if (!token) throw new Error('未获取到 Admin-Token');
  return token;
}

async function fetchCarrierConfigs(page, token) {
  const url = new URL(`${ADMIN_API_BASE}/carrier/config/listNotDeleted`);
  url.searchParams.set('pageNum', '1');
  url.searchParams.set('pageSize', '9999');
  url.searchParams.set('showDeleted', 'false');
  const parsed = await pageOriginGetJson(page, token, url.toString());
  return parsed.rows || parsed.data?.rows || parsed.data || [];
}

async function fetchDailyReplyRate(page, token, record, checkedAtDate) {
  const queryStartDate = dateBjOffset(checkedAtDate, -31);
  const queryEndDate = dateBj(checkedAtDate);
  const url = new URL(`${ADMIN_API_BASE}/admin/statisticsList`);
  const params = {
    pageNum: '1',
    pageSize: '40',
    userId: '-1',
    proxyUserId: '-1',
    phoneId: '-1',
    startTime: `${queryStartDate} 00:00:00`,
    endTime: `${queryEndDate} 23:59:59`,
    carrireIdentif: record.identif,
    dayliModel: '-1',
  };
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);

  const parsed = await pageOriginGetJson(page, token, url.toString());
  const rows = parsed.data?.data?.content || parsed.data?.content || [];
  if (!Array.isArray(rows) || rows.length === 0) {
    throw new Error('最新统计行缺失');
  }
  const latestDate = rows
    .map(row => String(row.days || '').trim())
    .filter(Boolean)
    .sort()
    .at(-1);
  const latestRows = rows.filter(row => String(row.days || '').trim() === latestDate);
  if (latestRows.length !== 1) {
    throw new Error(`最新统计行数量为${latestRows.length}`);
  }
  const dailyRow = latestRows[0];
  const replyRatePct = parseRatePct(dailyRow.firstReplyRate);
  if (replyRatePct === null) throw new Error('首次回复率无法解析');
  return {
    group: record.group,
    carrierMatchCount: 1,
    carrierName: record.name,
    carrierIdentif: record.identif,
    statDate: latestDate,
    queryStartDate,
    queryEndDate,
    replyRatePct,
    replyRateRaw: dailyRow.firstReplyRate,
    metric: 'firstReplyRate',
    metricLabel: '首次回复率',
    sourceMethod: '按日统计 -> 运营商选择 -> 搜索',
    requestMethod: 'GET',
    endpoint: '/api/admin/statisticsList',
    dailyRow,
    status: 'ok',
    reason: null,
  };
}

async function fetchDailyReplyRates(page, token, records, apiRecords, checkedAtDate) {
  const counts = new Map();
  for (const record of apiRecords) {
    counts.set(record.group, (counts.get(record.group) || 0) + 1);
  }
  const results = [];
  for (const group of REQUIRED_GROUPS) {
    const record = records.find(item => item.group === group);
    const carrierMatchCount = counts.get(group) || 0;
    if (carrierMatchCount !== 1 || !record?.identif) {
      results.push({
        group,
        carrierMatchCount,
        carrierName: record?.name || null,
        carrierIdentif: record?.identif || null,
        statDate: null,
        replyRatePct: null,
        metric: 'firstReplyRate',
        metricLabel: '首次回复率',
        sourceMethod: '按日统计 -> 运营商选择 -> 搜索',
        requestMethod: 'GET',
        endpoint: '/api/admin/statisticsList',
        status: 'error',
        reason: `运营商唯一精确匹配数为${carrierMatchCount}`,
      });
      continue;
    }
    try {
      results.push(await fetchDailyReplyRate(page, token, record, checkedAtDate));
    } catch (error) {
      results.push({
        group: record.group,
        carrierMatchCount,
        carrierName: record.name,
        carrierIdentif: record.identif,
        statDate: null,
        replyRatePct: null,
        metric: 'firstReplyRate',
        metricLabel: '首次回复率',
        sourceMethod: '按日统计 -> 运营商选择 -> 搜索',
        requestMethod: 'GET',
        endpoint: '/api/admin/statisticsList',
        status: 'error',
        reason: error.message,
      });
    }
  }
  return results;
}

function buildC2ReplyRateRule(replyRates) {
  const byGroup = new Map(replyRates.map(item => [item.group, item]));
  const c2 = byGroup.get('C2');
  const comparisons = ['A3', 'A1'].map(targetGroup => {
    const target = byGroup.get(targetGroup);
    const comparable = c2?.status === 'ok'
      && target?.status === 'ok'
      && c2.statDate === target.statDate;
    const thresholdPct = comparable ? target.replyRatePct * 1.2 : null;
    return {
      targetGroup,
      comparable,
      reason: comparable ? null : '首次回复率缺失、查询失败或统计日期不一致',
      c2ReplyRatePct: c2?.replyRatePct ?? null,
      c2StatDate: c2?.statDate ?? null,
      targetReplyRatePct: target?.replyRatePct ?? null,
      targetStatDate: target?.statDate ?? null,
      thresholdPct,
      triggered: comparable ? c2.replyRatePct < thresholdPct : null,
    };
  });
  return {
    metric: 'firstReplyRate',
    metricLabel: '首次回复率',
    sourceMethod: '按日统计 -> 运营商选择 -> 搜索',
    c2Status: c2?.status || 'error',
    c2ReplyRatePct: c2?.replyRatePct ?? null,
    comparisons,
    triggeredTargets: comparisons.filter(item => item.triggered).map(item => item.targetGroup),
  };
}

function proxyCarrierIdentif(row) {
  return String(row.carrireIdentif || row.carrierIdentif || '');
}

function proxyUserId(row) {
  return String(row.proxyUserId || row.userId || '');
}

async function fetchProxyCarrierBindings(context, token, adminBase) {
  const url = new URL(`${ADMIN_API_BASE}/proxycarrier/list`);
  url.searchParams.set('pageNum', '1');
  url.searchParams.set('pageSize', '9999');
  const response = await context.request.fetch(url.toString(), {
    method: 'GET',
    timeout: 120000,
    headers: {
      Accept: 'application/json, text/plain, */*',
      Authorization: token.startsWith('Bearer ') ? token : `Bearer ${token}`,
      Origin: adminBase,
      Referer: `${adminBase}/proxyCarrierConfig`,
    },
  });
  const text = await response.text();
  const parsed = parseResponseText(text);
  if (!response.ok() || parsed?.code !== 200) {
    throw new Error(`读取代理运营商绑定失败: HTTP ${response.status()} ${JSON.stringify(parsed)}`);
  }
  return parsed.rows || parsed.data?.rows || parsed.data || [];
}

function bindingPayload(sourceRow, targetCarrierIdentif) {
  return {
    carrireIdentif: targetCarrierIdentif,
    proxyUserId: sourceRow.proxyUserId,
    smsCost: sourceRow.smsCost ?? 0,
    smsRetail: sourceRow.smsRetail ?? 0,
    mmsCost: sourceRow.mmsCost ?? 0,
    mmsRetail: sourceRow.mmsRetail ?? 0,
    monthlyCost: sourceRow.monthlyCost ?? 0,
    remark: sourceRow.remark ?? null,
  };
}

async function createProxyCarrierBinding(context, token, adminBase, payload) {
  const response = await context.request.fetch(`${ADMIN_API_BASE}/proxycarrier`, {
    method: 'POST',
    timeout: 120000,
    data: encryptPayload(JSON.stringify(payload)),
    headers: {
      Accept: 'application/json, text/plain, */*',
      'Content-Type': 'text/plain',
      Authorization: token.startsWith('Bearer ') ? token : `Bearer ${token}`,
      Origin: adminBase,
      Referer: `${adminBase}/proxyCarrierConfig`,
    },
  });
  const text = await response.text();
  const parsed = parseResponseText(text);
  if (!response.ok() || parsed?.code !== 200) {
    throw new Error(`新增代理运营商绑定失败: HTTP ${response.status()} ${JSON.stringify(parsed)}`);
  }
  return parsed;
}

async function syncBindingsToTargets(context, token, adminBase, records, sourceGroup, targetGroups) {
  const byGroup = new Map(records.map(record => [record.group, record]));
  const sourceCarrier = byGroup.get(sourceGroup);
  const targets = targetGroups.map(group => ({ group, record: byGroup.get(group) }));
  const missingCarrierGroups = [
    ...(sourceCarrier ? [] : [sourceGroup]),
    ...targets.filter(target => !target.record?.identif).map(target => target.group),
  ];
  if (missingCarrierGroups.length) {
    throw new Error(`绑定同步缺少运营商标识: ${missingCarrierGroups.join('、')}`);
  }

  const rows = await fetchProxyCarrierBindings(context, token, adminBase);
  const sourceByAgent = new Map();
  const existingPairs = new Set();
  for (const row of rows) {
    const carrier = proxyCarrierIdentif(row);
    const agent = proxyUserId(row);
    if (!carrier || !agent) continue;
    existingPairs.add(`${carrier}:${agent}`);
    if (carrier === sourceCarrier.identif && !sourceByAgent.has(agent)) sourceByAgent.set(agent, row);
  }

  const created = [];
  const alreadyBound = [];
  const failures = [];
  for (const target of targets) {
    for (const [agent, sourceRow] of sourceByAgent) {
      const pair = `${target.record.identif}:${agent}`;
      if (existingPairs.has(pair)) {
        alreadyBound.push({ group: target.group, proxyUserId: agent });
        continue;
      }
      try {
        const response = await createProxyCarrierBinding(
          context,
          token,
          adminBase,
          bindingPayload(sourceRow, target.record.identif),
        );
        existingPairs.add(pair);
        created.push({ group: target.group, proxyUserId: agent, response });
      } catch (error) {
        failures.push({ group: target.group, proxyUserId: agent, error: error.message });
      }
    }
  }

  const readbackRows = await fetchProxyCarrierBindings(context, token, adminBase);
  const readbackPairs = new Set(readbackRows.map(row => {
    const carrier = proxyCarrierIdentif(row);
    const agent = proxyUserId(row);
    return carrier && agent ? `${carrier}:${agent}` : '';
  }).filter(Boolean));
  const expected = targets.flatMap(target => [...sourceByAgent.keys()].map(agent => ({
    group: target.group,
    carrierIdentif: target.record.identif,
    proxyUserId: agent,
  })));
  const readbackMissing = expected.filter(item => !readbackPairs.has(`${item.carrierIdentif}:${item.proxyUserId}`));

  return {
    enabled: true,
    attempted: true,
    trigger: `${sourceGroup} < 10%`,
    sourceGroup,
    targetGroups,
    sourceBindingCount: sourceByAgent.size,
    created,
    alreadyBound,
    failures,
    independentReadback: {
      expectedCount: expected.length,
      confirmedCount: expected.length - readbackMissing.length,
      missingCount: readbackMissing.length,
      missing: readbackMissing,
      passed: readbackMissing.length === 0,
    },
    error: failures.length || readbackMissing.length
      ? `代理运营商绑定失败 ${failures.length} 项，独立复核缺失 ${readbackMissing.length} 项`
      : null,
  };
}

async function syncA3BindingsToTargets(context, token, adminBase, records) {
  return syncBindingsToTargets(context, token, adminBase, records, A3_GROUP, A3_BINDING_TARGET_GROUPS);
}

function emptyBindingResult(reason = 'disabled_by_current_policy') {
  return {
    enabled: false,
    attempted: false,
    trigger: null,
    sourceGroup: null,
    targetGroups: [],
    reason,
    sourceBindingCount: 0,
    created: [],
    alreadyBound: [],
    failures: [],
    rules: [],
    error: null,
  };
}

function combineBindingResults(results, reason = 'no_binding_trigger_low10') {
  if (!results.length) return emptyBindingResult(reason);
  const failures = results.flatMap(result => result.failures || []);
  return {
    enabled: true,
    attempted: results.some(result => result.attempted),
    trigger: results.map(result => result.trigger).join('; '),
    sourceGroup: results.map(result => result.sourceGroup).join('; '),
    targetGroups: [...new Set(results.flatMap(result => result.targetGroups || []))],
    sourceBindingCount: Object.fromEntries(results.map(result => [result.sourceGroup, result.sourceBindingCount || 0])),
    created: results.flatMap(result => (result.created || []).map(item => ({ sourceGroup: result.sourceGroup, ...item }))),
    alreadyBound: results.flatMap(result => (result.alreadyBound || []).map(item => ({ sourceGroup: result.sourceGroup, ...item }))),
    failures,
    rules: results,
    error: failures.length ? `代理运营商绑定失败 ${failures.length} 项` : null,
  };
}

function recordFromApiRow(row) {
  const name = carrierName(row);
  const group = groupFromName(name);
  if (!group) return null;
  const dailyRemaining = toNumber(row.dailyRemaining);
  const dailyTotal = toNumber(row.dailyTotal);
  if (dailyRemaining === null || dailyTotal === null) return null;
  const phoneCount = toNumber(row.phoneCount ?? row.userAblePhoneCount ?? row.bindPhoneCount);
  return {
    group,
    name,
    identif: carrierIdentif(row) || null,
    dailyRemaining,
    dailyTotal,
    percentRemaining: pct(dailyRemaining, dailyTotal),
    phoneCount,
    userAblePhoneCount: toNumber(row.userAblePhoneCount ?? phoneCount),
    updateTime: row.updateTime || row.lastUpdateTime || row.createTime || null,
    source: 'carrier_config_api',
    raw: row,
  };
}

function recordFromTableCells(cells) {
  const group = groupFromName(cells[0]);
  if (!group) return null;
  const dailyTotal = toNumber(cells[5]);
  const dailyRemaining = toNumber(cells[6]);
  if (dailyRemaining === null || dailyTotal === null) return null;
  const phoneCount = toNumber(cells[2]);
  return {
    group,
    name: cells[0],
    identif: cells[1] || null,
    dailyRemaining,
    dailyTotal,
    percentRemaining: pct(dailyRemaining, dailyTotal),
    phoneCount,
    userAblePhoneCount: phoneCount,
    updateTime: cells[cells.length - 1] || null,
    source: 'carrier_config_table',
    sourceCells: cells,
  };
}

async function scrapeCarrierTable(page, checkedAtDate = new Date()) {
  await page.goto(CONFIG_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForLoadState('networkidle', { timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(1500);
  if (await isOpenSmsLoginPage(page)) {
    throw new LoginRecoveryRequiredError(await inspectLoginRecovery(page, checkedAtDate));
  }
  const data = await page.evaluate(() => {
    const rows = [];
    for (const table of document.querySelectorAll('table')) {
      for (const tr of table.querySelectorAll('tr')) {
        const cells = [...tr.querySelectorAll('th,td')]
          .map(td => (td.innerText || '').trim())
          .filter(Boolean);
        if (cells.length) rows.push(cells);
      }
    }
    return {
      url: location.href,
      title: document.title,
      rows,
      bodySample: (document.body?.innerText || '').slice(0, 6000),
    };
  });
  const byGroup = new Map();
  for (const cells of data.rows) {
    const record = recordFromTableCells(cells);
    if (record && !byGroup.has(record.group)) byGroup.set(record.group, record);
  }
  return { data, records: REQUIRED_GROUPS.map(group => byGroup.get(group)).filter(Boolean) };
}

function buildSets(records) {
  const byGroup = new Map(records.map(record => [record.group, record]));
  const ordered = REQUIRED_GROUPS.map(group => byGroup.get(group)).filter(Boolean);
  const missingRequiredGroups = REQUIRED_GROUPS.filter(group => !byGroup.has(group));
  const low20 = ordered.filter(record => record.percentRemaining !== null && record.percentRemaining < 20);
  const low10 = ordered.filter(record => record.percentRemaining !== null && record.percentRemaining < 10);
  const alertLow10 = low10.filter(record => ALERT_GROUPS.includes(record.group));
  return { records: ordered, missingRequiredGroups, low20, low10, alertLow10 };
}

function alertText(alertLow10, systemIssues = []) {
  const lowBalance = alertLow10
    .map(row => `${row.group} ${row.percentRemaining?.toFixed(2)}% ${row.dailyRemaining}/${row.dailyTotal}`)
    .join('；');
  const issues = systemIssues.map(issue => `系统错误：${issue}`).join('；');
  return [lowBalance, issues].filter(Boolean).join('；');
}

async function runAppleScript(script) {
  const { stdout } = await execFile('osascript', ['-e', script], { timeout: 20000 });
  return stdout.trim();
}

async function ensureCalendarRunning() {
  await execFile('open', ['-gj', '-a', 'Calendar'], { timeout: 20000 });
  await new Promise(resolve => setTimeout(resolve, 2500));
}

async function cleanupCalendar() {
  await ensureCalendarRunning();
  const script = `
set deletedCount to 0
tell application "Calendar"
  repeat with cal in calendars
    try
      set oldEvents to (every event of cal whose summary starts with "OpenSMS 余量闹钟" and end date is less than ((current date) - 3600))
      repeat with ev in oldEvents
        delete ev
        set deletedCount to deletedCount + 1
      end repeat
    end try
  end repeat
end tell
return deletedCount
`;
  const stdout = await runAppleScript(script);
  return Number(stdout) || 0;
}

async function scheduleLocalAlarm(alertLow10, systemIssues) {
  const message = alertText(alertLow10, systemIssues);
  const script = `display notification "${message.replace(/"/g, '\\"')}" with title "OpenSMS 余量闹钟" sound name "Sosumi"`;
  await runAppleScript(script);
  return {
    attempted: true,
    scheduled: true,
    suppressed: false,
    reason: 'scheduled',
    startsAtLocal: localBj(new Date()),
  };
}

async function scheduleCalendarAlarm(alertLow10, systemIssues) {
  await ensureCalendarRunning();
  const message = alertText(alertLow10, systemIssues).replace(/"/g, '\\"');
  const script = `
set startDate to (current date) + 5
set endDate to startDate + 60
tell application "Calendar"
  set targetCalendar to first calendar whose writable is true
  set ev to make new event at end of events of targetCalendar with properties {summary:"OpenSMS 余量闹钟：${message}", start date:startDate, end date:endDate}
  make new display alarm at end of display alarms of ev with properties {trigger interval:0}
  return (name of targetCalendar) & "|" & (startDate as string)
end tell
`;
  const stdout = await runAppleScript(script);
  const [calendarName, startAtLocalRaw] = stdout.split('|');
  return {
    attempted: true,
    created: true,
    suppressed: false,
    reason: 'scheduled',
    startAtLocal: startAtLocalRaw || null,
    calendarName: calendarName || null,
  };
}

async function buildAlarmFields(alertLow10, systemIssues = []) {
  const hasAlert = alertLow10.length > 0 || systemIssues.length > 0;
  const reminderAlarm = {
    attempted: false,
    created: false,
    disabled: true,
    reason: 'cancelled_by_user',
  };

  if (!hasAlert) {
    return {
      calendarCleanup: { attempted: false, deleted: 0, reason: 'no_low10_or_system_error' },
      reminderAlarm,
      localAlarm: { attempted: false, scheduled: false, suppressed: false, reason: 'no_low10_or_system_error' },
      calendarAlarm: {
        attempted: false,
        created: false,
        suppressed: false,
        reason: 'no_low10_or_system_error',
        startAtLocal: null,
        calendarName: null,
      },
    };
  }

  if (process.env.OPENSMS_BALANCE_DISABLE_ALARMS === '1') {
    return {
      calendarCleanup: { attempted: false, deleted: 0, reason: 'disabled_by_env' },
      reminderAlarm,
      localAlarm: { attempted: false, scheduled: false, suppressed: true, reason: 'disabled_by_env' },
      calendarAlarm: {
        attempted: false,
        created: false,
        suppressed: true,
        reason: 'disabled_by_env',
        startAtLocal: null,
        calendarName: null,
      },
    };
  }

  let calendarCleanup = { attempted: true, deleted: 0 };
  try {
    calendarCleanup.deleted = await cleanupCalendar();
  } catch (error) {
    calendarCleanup.error = error.message;
  }

  let localAlarm;
  try {
    localAlarm = await scheduleLocalAlarm(alertLow10, systemIssues);
  } catch (error) {
    localAlarm = {
      attempted: true,
      scheduled: false,
      suppressed: false,
      reason: 'error',
      error: error.message,
    };
  }

  let calendarAlarm;
  try {
    calendarAlarm = await scheduleCalendarAlarm(alertLow10, systemIssues);
  } catch (error) {
    calendarAlarm = {
      attempted: true,
      created: false,
      suppressed: false,
      reason: 'error',
      startAtLocal: null,
      calendarName: null,
      error: error.message,
    };
  }

  return { calendarCleanup, reminderAlarm, localAlarm, calendarAlarm };
}

async function main() {
  await ensureArtifactDirs();
  const checkedAtDate = new Date();
  let session;
  let source;
  let sets;
  let rawRows;
  let bodySample;
  let protocolError;
  let loginRecovery = null;
  let carrierRows = [];
  let apiRecords = [];
  let replyRates = REQUIRED_GROUPS.map(group => ({
    group,
    carrierMatchCount: 0,
    carrierName: null,
    carrierIdentif: null,
    statDate: null,
    replyRatePct: null,
    metric: 'firstReplyRate',
    metricLabel: '首次回复率',
    sourceMethod: '按日统计 -> 运营商选择 -> 搜索',
    requestMethod: 'GET',
    endpoint: '/api/admin/statisticsList',
    status: 'error',
    reason: '运营商余量查询未完成',
  }));
  let context;
  let page;
  let token;
  const adminBase = 'https://adminsms.smsoug.com';

  try {
    session = await connectOpenSmsBrowser();
    context = session.context;
    page = session.page;
    if (await isOpenSmsLoginPage(page)) {
      loginRecovery = await inspectLoginRecovery(page, checkedAtDate);
      throw new LoginRecoveryRequiredError(loginRecovery);
    }
    token = await getToken(context, page, adminBase);
    payloadCodec = await loadPagePayloadCodec(page);
    try {
      carrierRows = await fetchCarrierConfigs(page, token);
    } catch (error) {
      if (!shouldRefreshAfterCarrierError(error)) throw error;
      token = await getToken(context, page, adminBase);
      carrierRows = await fetchCarrierConfigs(page, token);
    }
    apiRecords = carrierRows.map(recordFromApiRow).filter(Boolean);
    sets = buildSets(apiRecords);
    source = {
      url: CONFIG_URL,
      method: 'carrier_config_api',
      endpoint: '/api/carrier/config/listNotDeleted',
      apiBase: ADMIN_API_BASE,
      cdpEndpoint: session.endpoint,
    };
    rawRows = carrierRows;
    replyRates = [];
  } catch (error) {
    protocolError = error.stack || String(error);
    if (error instanceof LoginRecoveryRequiredError) {
      loginRecovery = error.loginRecovery;
      sets = buildSets([]);
      source = {
        url: loginRecovery.url,
        title: loginRecovery.title,
        method: 'login_recovery_required',
        intendedEndpoint: '/api/carrier/config/listNotDeleted',
        protocolError,
        cdpEndpoint: session?.endpoint || null,
      };
      rawRows = [];
      bodySample = 'OpenSMS 登录状态失效；验证码已保存，等待本次确认后登录。';
    } else {
      sets = buildSets([]);
      source = {
        url: CONFIG_URL,
        method: 'carrier_config_error',
        intendedEndpoint: '/api/carrier/config/listNotDeleted',
        protocolError,
        cdpEndpoint: session?.endpoint || null,
      };
      rawRows = [];
      bodySample = '静默后台接口查询失败；为避免复用或切换用户当前可见页面，未执行页面读取回退。';
    }
  }

  const autoBinding = emptyBindingResult();

  const systemIssues = [];
  if (process.env.OPENSMS_PREFLIGHT_ISSUE) {
    systemIssues.push(process.env.OPENSMS_PREFLIGHT_ISSUE);
  }
  if (source.method === 'login_recovery_required') {
    systemIssues.push('OpenSMS 登录状态失效，需要确认本次验证码后登录');
  } else if (source.method !== 'carrier_config_api') {
    systemIssues.push('运营商配置接口查询失败；静默模式已禁止复用用户当前可见页面');
  }
  if (sets.missingRequiredGroups.length > 0) {
    systemIssues.push(`运营商配置缺失固定分组：${sets.missingRequiredGroups.join('、')}`);
  }
  if (autoBinding.error) systemIssues.push(autoBinding.error);
  const alarmFields = await buildAlarmFields(sets.alertLow10, systemIssues);
  const out = {
    checkedAt: checkedAtDate.toISOString(),
    checkedAtLocal: localBj(checkedAtDate),
    silentBrowserMode: SILENT_BROWSER_MODE,
    browserForegroundActivationAttempted: false,
    fallback: source.method !== 'carrier_config_api',
    fallbackReason: source.method === 'carrier_config_api'
      ? null
      : source.method === 'login_recovery_required'
        ? 'OpenSMS login expired; CAPTCHA captured; submission waits for per-challenge confirmation'
        : 'carrier config API failed; visible-page fallback is disabled by silent mode',
    source,
    requiredGroups: REQUIRED_GROUPS,
    alertGroups: ALERT_GROUPS,
    ...sets,
    systemIssues,
    loginRecovery,
    autoBinding,
    autoQuota: {
      enabled: false,
      action: '已关闭，未执行',
    },
    replyRates,
    c2ReplyRateRule: buildC2ReplyRateRule(replyRates),
    ...alarmFields,
    rawRows,
    bodySample,
  };

  const outPath = path.join(SNAPSHOT_DIR, `${OUT_PREFIX}_${stampBj(checkedAtDate)}.json`);
  out.outPath = outPath;
  await fs.writeFile(outPath, JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
  await session?.browser.close().catch(() => {});
}

async function writeFatalErrorResult(error) {
  const checkedAtDate = new Date();
  const issue = `余量查询失败：${error.message || String(error)}`;
  const alarmFields = await buildAlarmFields([], [issue]);
  const replyRates = REQUIRED_GROUPS.map(group => ({
    group,
    carrierMatchCount: 0,
    carrierName: null,
    carrierIdentif: null,
    statDate: null,
    replyRatePct: null,
    metric: 'firstReplyRate',
    metricLabel: '首次回复率',
    sourceMethod: '按日统计 -> 运营商选择 -> 搜索',
    requestMethod: 'GET',
    endpoint: '/api/admin/statisticsList',
    status: 'error',
    reason: issue,
  }));
  const out = {
    checkedAt: checkedAtDate.toISOString(),
    checkedAtLocal: localBj(checkedAtDate),
    silentBrowserMode: SILENT_BROWSER_MODE,
    browserForegroundActivationAttempted: false,
    fallback: false,
    fatalError: issue,
    source: {
      url: CONFIG_URL,
      intendedEndpoint: '/api/carrier/config/listNotDeleted',
      method: 'carrier_config_error',
    },
    requiredGroups: REQUIRED_GROUPS,
    alertGroups: ALERT_GROUPS,
    records: [],
    missingRequiredGroups: REQUIRED_GROUPS,
    low20: [],
    low10: [],
    alertLow10: [],
    systemIssues: [issue],
    autoBinding: emptyBindingResult('query_failed_before_balance_read'),
    autoQuota: {
      enabled: false,
      action: '已关闭，未执行',
    },
    replyRates,
    c2ReplyRateRule: buildC2ReplyRateRule(replyRates),
    ...alarmFields,
  };
  const outPath = path.join(SNAPSHOT_DIR, `${OUT_PREFIX}_${stampBj(checkedAtDate)}.json`);
  out.outPath = outPath;
  await fs.writeFile(outPath, JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
}

main().catch(async error => {
  console.error(error.stack || String(error));
  try {
    await writeFatalErrorResult(error);
  } catch (fatalError) {
    console.error(fatalError.stack || String(fatalError));
  }
  process.exit(1);
});
