import fs from 'node:fs/promises';
import path from 'node:path';

import { createOpenSmsApiSession, getCarrierConfigs } from '../opensms_protocol_client.mjs';
import { SNAPSHOT_DIR, ensureArtifactDirs } from '../opensms_artifact_paths.mjs';
import { collectBalanceStatisticsReadOnly } from './opensms_balance_statistics_readonly.mjs';

const REQUIRED_GROUPS = ['A1', 'A12', 'A11', 'B1', 'B11', 'B12', 'C2', 'A3', 'B3', 'B1V', 'B11V', 'B12V'];

function stamp() {
  return new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
}

function groupFromName(name) {
  const text = String(name || '').trim();
  return REQUIRED_GROUPS.find(group => text === group || text.startsWith(`${group}-`) || text.startsWith(`${group} `)) || null;
}

function carrierName(row) {
  return String(row.name || row.carrierName || row.carrireName || row.carrier || '');
}

function carrierIdentif(row) {
  return String(row.identif || row.carrireIdentif || row.carrierIdentif || row.id || '');
}

function number(value) {
  if (value === undefined || value === null || value === '') return null;
  const parsed = Number(String(value).replace(/,/g, '').trim());
  return Number.isFinite(parsed) ? parsed : null;
}

function balanceRecord(row, group) {
  const dailyRemaining = number(row.dailyRemaining);
  const dailyTotal = number(row.dailyTotal);
  return {
    group,
    name: carrierName(row),
    identif: carrierIdentif(row),
    dailyRemaining,
    dailyTotal,
    percentRemaining: dailyTotal > 0 && dailyRemaining !== null ? dailyRemaining / dailyTotal * 100 : null,
    phoneCount: number(row.phoneCount ?? row.userAblePhoneCount ?? row.bindPhoneCount),
    userAblePhoneCount: number(row.userAblePhoneCount ?? row.phoneCount),
    source: 'carrier_config_api_raw_page_target',
  };
}

async function main() {
  await ensureArtifactDirs();
  const session = await createOpenSmsApiSession({ cdp: process.env.BITBROWSER_CDP, pathName: '/day' });
  try {
    const carrierRows = await getCarrierConfigs(session.api);
    const grouped = new Map(REQUIRED_GROUPS.map(group => [group, []]));
    for (const row of carrierRows) {
      const group = groupFromName(carrierName(row));
      if (group) grouped.get(group).push(row);
    }
    const records = [];
    const missingRequiredGroups = [];
    const unresolvedReplyRates = [];
    for (const group of REQUIRED_GROUPS) {
      const matches = grouped.get(group) || [];
      if (matches.length !== 1) {
        missingRequiredGroups.push(group);
        unresolvedReplyRates.push({
          group,
          carrierMatchCount: matches.length,
          carrierName: null,
          carrierIdentif: null,
          statDate: null,
          replyRatePct: null,
          metric: 'firstReplyRate',
          metricLabel: '首次回复率',
          sourceMethod: '按日统计 -> 运营商唯一精确选择 -> GET /api/admin/statisticsList',
          requestMethod: 'GET',
          endpoint: '/api/admin/statisticsList',
          status: 'error',
          reason: `运营商唯一精确匹配数为${matches.length}`,
        });
        continue;
      }
      const record = balanceRecord(matches[0], group);
      records.push(record);
    }
    const statistics = await collectBalanceStatisticsReadOnly(session.api, records, { captureObservationTimes: true });
    const replyRateByGroup = new Map([...unresolvedReplyRates, ...statistics.replyRates].map(item => [item.group, item]));
    const replyRates = REQUIRED_GROUPS.map(group => replyRateByGroup.get(group));
    // Reuse the twelve existing reads; all other configured carriers get one
    // bounded read through the same globally paced protocol client. These
    // display-only rows never enter quota alerts or business-routing rules.
    const carrierIds = new Map();
    for (const row of carrierRows) {
      const identif = carrierIdentif(row).trim();
      carrierIds.set(identif, (carrierIds.get(identif) || 0) + 1);
    }
    const knownRates = new Map(statistics.replyRates.map(row => [row.carrierIdentif, row]));
    const additionalRecords = carrierRows.map(row => ({
      name: carrierName(row).trim(), identif: carrierIdentif(row).trim(),
    })).filter(row => row.identif && row.name && carrierIds.get(row.identif) === 1 && !knownRates.has(row.identif));
    const additional = await collectBalanceStatisticsReadOnly(session.api, additionalRecords, {
      captureObservationTimes: true, continueOnDataErrors: true,
    });
    for (const row of additional.replyRates) knownRates.set(row.carrierIdentif, row);
    const carrierReplyRates = carrierRows.map(row => {
      const identif = carrierIdentif(row).trim();
      const name = carrierName(row).trim();
      if (!identif || !name || carrierIds.get(identif) !== 1) return {
        carrierIdentif: identif, carrierName: name, metric: 'firstReplyRate',
        status: 'error', reason: '运营商名称/标识缺失或标识重复', replyRatePct: null,
      };
      return { ...knownRates.get(identif), carrierName: name, carrierIdentif: identif };
    });
    const low20 = records.filter(record => record.percentRemaining !== null && record.percentRemaining < 20);
    const low10 = records.filter(record => record.percentRemaining !== null && record.percentRemaining < 10);
    const c2Rate = replyRates.find(item => item.group === 'C2');
    const comparisons = ['A3', 'A1'].map(targetGroup => {
      const target = replyRates.find(item => item.group === targetGroup);
      const comparable = c2Rate?.status === 'ok' && target?.status === 'ok' && c2Rate.statDate === target.statDate;
      return {
        targetGroup,
        comparable,
        c2ReplyRatePct: c2Rate?.replyRatePct ?? null,
        c2StatDate: c2Rate?.statDate ?? null,
        targetReplyRatePct: target?.replyRatePct ?? null,
        targetStatDate: target?.statDate ?? null,
        thresholdPct: comparable ? target.replyRatePct * 1.2 : null,
        triggered: comparable ? c2Rate.replyRatePct < target.replyRatePct * 1.2 : null,
        reason: comparable ? null : '首次回复率缺失、查询失败或统计日期不一致',
      };
    });
    const c2Daily = replyRates.find(item => item.group === 'C2')?.dailyRow || null;
    const out = {
      checkedAt: new Date().toISOString(),
      fallback: false,
      source: {
        method: 'carrier_config_and_statistics_api_raw_page_target',
        carrierEndpoint: '/api/carrier/config/listNotDeleted',
        statisticsEndpoint: '/api/admin/statisticsList',
        apiTransport: session.apiTransport,
      },
      requiredGroups: REQUIRED_GROUPS,
      alertGroups: REQUIRED_GROUPS,
      records,
      missingRequiredGroups,
      low20,
      low10,
      alertLow10: low10,
      replyRates,
      carrierReplyRates,
      carrierReplyRateScope: 'all_current_carrier_configs',
      carrierReplyRatePerformance: additional.performance,
      performance: { statisticsRead: statistics.performance },
      c2ReplyRateRule: { comparisons },
      c2LatestDailySendSize: number(c2Daily?.sendSize),
      c2LatestDailyDate: c2Daily?.days || null,
      systemIssues: replyRates.filter(item => item.status !== 'ok').map(item => `${item.group}:${item.reason}`),
      autoBinding: { enabled: false, attempted: false, reason: '本轮专项导出及任务B只读余量查询，不执行客户绑定' },
      autoQuota: { enabled: false, action: '已关闭，未执行' },
      alarms: { enabled: false, reason: '本轮专项执行不创建系统提醒' },
    };
    const outputPath = path.join(SNAPSHOT_DIR, `opensms_balance_readonly_raw_${stamp()}.json`);
    out.outPath = outputPath;
    await fs.writeFile(outputPath, `${JSON.stringify(out, null, 2)}\n`, { flag: 'wx' });
    console.log(JSON.stringify(out, null, 2));
    if (missingRequiredGroups.length || out.systemIssues.length) process.exitCode = 2;
  } finally {
    await session.browser.close().catch(() => {});
  }
}

main().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
