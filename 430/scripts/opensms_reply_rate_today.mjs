import crypto from 'node:crypto';
import fssync from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { ensureArtifactDirs, SNAPSHOT_DIR } from '../opensms_artifact_paths.mjs';
import { loadPagePayloadCodec } from '../opensms_payload_codec.mjs';

const require = createRequire(import.meta.url);

function loadPlaywright() {
  try {
    return require('playwright');
  } catch {}
  return require('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
}

const { chromium } = loadPlaywright();

const CDP = process.env.BITBROWSER_CDP || 'http://127.0.0.1:49538';
const ADMIN_ORIGIN = 'https://adminsms.smsoug.com';
const ADMIN_API_BASE = `${ADMIN_ORIGIN}/api`;
const GROUPS = ['A1', 'A11', 'A12', 'B1', 'B11', 'B12', 'C2', 'A3', 'B1V', 'B11V', 'B12V'];

function zonedParts(date = new Date(), timeZone = 'Asia/Hong_Kong') {
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone,
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

function dateNewYork(date = new Date()) {
  const p = zonedParts(date, 'America/New_York');
  return `${p.year}-${p.month}-${p.day}`;
}

function stampBj(date = new Date()) {
  const p = zonedParts(date);
  return `${p.year}${p.month}${p.day}${p.hour}${p.minute}${p.second}`;
}

function localBj(date = new Date()) {
  const p = zonedParts(date);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
}

function parseRatePct(value) {
  if (value === undefined || value === null || value === '') return null;
  const parsed = Number.parseFloat(String(value).replace('%', '').trim());
  return Number.isFinite(parsed) ? parsed : null;
}

function carrierName(row) {
  return String(row.name || row.carrierName || row.carrireName || row.carrier || '').trim();
}

function carrierIdentif(row) {
  return String(row.identif || row.carrireIdentif || row.carrierIdentif || row.id || '').trim();
}

function groupFromCarrierName(name) {
  const normalized = String(name || '').trim().toUpperCase();
  return [...GROUPS]
    .sort((left, right) => right.length - left.length)
    .find(group => {
      const candidate = group.toUpperCase();
      return normalized === candidate
        || normalized.startsWith(`${candidate}-`)
        || normalized.startsWith(`${candidate} `);
    }) || null;
}

function readDevtoolsPorts() {
  const cacheDir = '/Users/zhangsan/Library/Application Support/BitBrowser/BrowserCache';
  const endpoints = [];
  try {
    for (const item of fssync.readdirSync(cacheDir, { withFileTypes: true })) {
      if (!item.isDirectory()) continue;
      try {
        const port = fssync.readFileSync(path.join(cacheDir, item.name, 'DevToolsActivePort'), 'utf8')
          .split(/\r?\n/)[0]
          .trim();
        if (port) endpoints.push(`http://127.0.0.1:${port}`);
      } catch {}
    }
  } catch {}
  return endpoints;
}

async function connectExistingOpenSmsTarget() {
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
      const pages = browser.contexts().flatMap(context => context.pages());
      const page = pages.find(candidate => {
        try { return new URL(candidate.url()).origin === ADMIN_ORIGIN; }
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
  throw new Error(`无法静默复用 430 已有 OpenSMS 页面目标: ${JSON.stringify(errors)}`);
}

async function getToken(context, page) {
  const pageToken = await page.evaluate(() => {
    const raw = document.cookie.split('; ').find(part => part.startsWith('Admin-Token='));
    return raw ? decodeURIComponent(raw.slice('Admin-Token='.length)) : '';
  }).catch(() => '');
  const cookies = await context.cookies([ADMIN_ORIGIN, `${ADMIN_ORIGIN}/`]);
  const token = pageToken || cookies.find(cookie => cookie.name === 'Admin-Token')?.value;
  if (!token) throw new Error('未获取到 Admin-Token');
  return token;
}

async function pageOriginGetJson(page, token, url) {
  const payloadCodec = await loadPagePayloadCodec(page);
  const response = await page.evaluate(async ({ requestUrl, authorization }) => {
    const result = await fetch(requestUrl, {
      method: 'GET',
      credentials: 'include',
      headers: {
        Accept: 'application/json, text/plain, */*',
        Authorization: authorization,
      },
    });
    return { ok: result.ok, status: result.status, text: await result.text() };
  }, {
    requestUrl: url,
    authorization: token.startsWith('Bearer ') ? token : `Bearer ${token}`,
  });
  const parsed = payloadCodec.parse(response.text);
  if (!response.ok || parsed?.code !== 200) {
    throw new Error(`页面同源接口读取失败: HTTP ${response.status} ${JSON.stringify(parsed)}`);
  }
  return parsed;
}

async function pageOriginGetJsonWithRetry(page, token, url) {
  try {
    return await pageOriginGetJson(page, token, url);
  } catch (firstError) {
    try {
      return await pageOriginGetJson(page, token, url);
    } catch (secondError) {
      throw new Error(`${secondError.message}; 后台同源重试1次仍失败; 首次错误=${firstError.message}`);
    }
  }
}

async function fetchCarrierConfigs(page, token) {
  const url = new URL(`${ADMIN_API_BASE}/carrier/config/listNotDeleted`);
  url.searchParams.set('pageNum', '1');
  url.searchParams.set('pageSize', '9999');
  url.searchParams.set('showDeleted', 'false');
  const parsed = await pageOriginGetJsonWithRetry(page, token, url.toString());
  return parsed.rows || parsed.data?.rows || parsed.data || [];
}

async function fetchTodayReplyRate(page, token, carrier, today) {
  const url = new URL(`${ADMIN_API_BASE}/admin/statisticsList`);
  const params = {
    pageNum: '1',
    pageSize: '100',
    userId: '-1',
    proxyUserId: '-1',
    phoneId: '-1',
    startTime: `${today} 00:00:00`,
    endTime: `${today} 23:59:59`,
    carrireIdentif: carrier.identif,
    dayliModel: '-1',
  };
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
  const parsed = await pageOriginGetJsonWithRetry(page, token, url.toString());
  const rows = parsed.data?.data?.content || parsed.data?.content || parsed.rows || [];
  const todayRows = Array.isArray(rows)
    ? rows.filter(row => String(row.days || '').trim() === today)
    : [];
  if (todayRows.length !== 1) throw new Error(`今日统计行数量为${todayRows.length}`);
  const dailyRow = todayRows[0];
  const raw = dailyRow.replyRate;
  const replyRatePct = parseRatePct(raw);
  if (replyRatePct === null) throw new Error('今日回复率无法唯一解析');
  return {
    replyRatePct,
    replyRateRaw: raw,
    firstReplyRateRaw: dailyRow.firstReplyRate ?? null,
    dailyRow,
  };
}

function errorRows(reason) {
  return GROUPS.map(group => ({
    group,
    carrierMatchCount: 0,
    carrierName: null,
    carrierIdentif: null,
    statDate: null,
    replyRatePct: null,
    replyRateRaw: null,
    status: 'error',
    reason,
  }));
}

async function main() {
  await ensureArtifactDirs();
  const checkedAtDate = new Date();
  const today = dateNewYork(checkedAtDate);
  let session;
  let results;
  let source;
  let fatalError = null;

  try {
    session = await connectExistingOpenSmsTarget();
    const token = await getToken(session.context, session.page);
    const carrierRows = await fetchCarrierConfigs(session.page, token);
    const candidates = carrierRows
      .map(row => ({ group: groupFromCarrierName(carrierName(row)), name: carrierName(row), identif: carrierIdentif(row) }))
      .filter(row => row.group);
    results = [];
    for (const group of GROUPS) {
      const matches = candidates.filter(candidate => candidate.group === group);
      if (matches.length !== 1 || !matches[0]?.identif) {
        results.push({
          group,
          carrierMatchCount: matches.length,
          carrierName: matches[0]?.name || null,
          carrierIdentif: matches[0]?.identif || null,
          statDate: today,
          replyRatePct: null,
          replyRateRaw: null,
          status: 'error',
          reason: `运营商唯一精确匹配数为${matches.length}`,
        });
        continue;
      }
      const carrier = matches[0];
      try {
        const value = await fetchTodayReplyRate(session.page, token, carrier, today);
        results.push({
          group,
          carrierMatchCount: 1,
          carrierName: carrier.name,
          carrierIdentif: carrier.identif,
          statDate: today,
          ...value,
          status: 'ok',
          reason: null,
        });
      } catch (error) {
        results.push({
          group,
          carrierMatchCount: 1,
          carrierName: carrier.name,
          carrierIdentif: carrier.identif,
          statDate: today,
          replyRatePct: null,
          replyRateRaw: null,
          status: 'error',
          reason: error.message,
        });
      }
    }
    source = {
      method: 'existing_page_origin_fetch',
      carrierEndpoint: '/api/carrier/config/listNotDeleted',
      replyRateEndpoint: '/api/admin/statisticsList',
      sourceMethod: '按日统计 -> 指定运营商 -> 查询',
      metric: 'replyRate',
      metricLabel: '今日回复率',
      cdpEndpoint: session.endpoint,
    };
  } catch (error) {
    fatalError = error.message;
    results = errorRows(fatalError);
    source = {
      method: 'blocked',
      carrierEndpoint: '/api/carrier/config/listNotDeleted',
      replyRateEndpoint: '/api/admin/statisticsList',
      sourceMethod: '按日统计 -> 指定运营商 -> 查询',
      metric: 'replyRate',
      metricLabel: '今日回复率',
      cdpEndpoint: session?.endpoint || null,
    };
  }

  const out = {
    checkedAt: checkedAtDate.toISOString(),
    checkedAtLocal: localBj(checkedAtDate),
    statDate: today,
    statTimezone: 'America/New_York',
    groups: GROUPS,
    silentBrowserMode: true,
    browserForegroundActivationAttempted: false,
    readOnly: true,
    source,
    successCount: results.filter(row => row.status === 'ok').length,
    failureCount: results.filter(row => row.status !== 'ok').length,
    results,
    fatalError,
  };
  const outPath = path.join(SNAPSHOT_DIR, `opensms_reply_rate_today_${stampBj(checkedAtDate)}.json`);
  out.outPath = outPath;
  await fs.writeFile(outPath, JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
  await session?.browser.close().catch(() => {});
  if (fatalError) process.exitCode = 1;
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});
