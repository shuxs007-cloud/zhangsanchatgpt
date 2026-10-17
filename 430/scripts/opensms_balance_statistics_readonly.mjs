import { performance } from 'node:perf_hooks';
import { createReadOnlyApiLimiter } from './opensms_readonly_concurrency.mjs';

const ENDPOINT = '/admin/statisticsList';

function beijingDateOffset(now, days = 0) {
  const shifted = new Date(now.getTime() + days * 86400000);
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(shifted);
  const values = Object.fromEntries(parts.map(item => [item.type, item.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function validatedLatestDailyRow(response) {
  if (!response || response.code !== 200) throw new Error(`按日统计查询失败: ${JSON.stringify(response)}`);
  const rows = response.data?.data?.content || response.data?.content || [];
  if (!Array.isArray(rows) || !rows.length) throw new Error('最新统计行缺失');
  const latestDate = rows.map(row => String(row.days || '').trim()).filter(Boolean).sort().at(-1);
  const latestRows = rows.filter(row => String(row.days || '').trim() === latestDate);
  if (latestRows.length !== 1) throw new Error(`最新统计行数量为${latestRows.length}`);
  const dailyRow = latestRows[0];
  const raw = dailyRow.firstReplyRate;
  const replyRate = raw === undefined || raw === null || raw === ''
    ? null : Number.parseFloat(String(raw).replace('%', '').trim());
  if (replyRate === null || !Number.isFinite(replyRate)) throw new Error('首次回复率无法解析');
  return { dailyRow, latestDate, replyRate };
}

// The caller has already uniquely resolved carrier identities. This function
// never opens a session and its only permitted request is the statistics GET.
export async function collectBalanceStatisticsReadOnly(api, records, {
  concurrency = 4, now = new Date(), captureObservationTimes = false, continueOnDataErrors = false,
} = {}) {
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 4) {
    throw new Error(`余量统计只读并发数必须为1至4的整数: ${concurrency}`);
  }
  const started = performance.now();
  // Freeze one query window for every group, including across local midnight.
  const queryStartDate = beijingDateOffset(now, -31);
  const queryEndDate = beijingDateOffset(now);
  const dispatchedQueries = new WeakSet();
  const limiter = createReadOnlyApiLimiter(async (method, endpoint, data, query) => {
    dispatchedQueries.add(query);
    const response = await api(method, endpoint, data, query);
    let value;
    try {
      value = validatedLatestDailyRow(response);
    } catch (error) {
      // Optional display-only carriers may have no daily row. They must not
      // cancel other carriers. Transport/auth/business failures still stop.
      if (!continueOnDataErrors || response?.code !== 200) throw error;
      return { dataError: error.message };
    }
    return { ...value, ...(captureObservationTimes ? { observedAt: new Date().toISOString() } : {}) };
  }, { concurrency, allowedRequests: [`GET ${ENDPOINT}`] });
  let replyRates;
  try {
    replyRates = await Promise.all(records.map(async record => {
      const base = {
        group: record.group,
        carrierMatchCount: 1,
        carrierName: record.name,
        carrierIdentif: record.identif,
        metric: 'firstReplyRate',
        metricLabel: '首次回复率',
        sourceMethod: '按日统计 -> 运营商唯一精确选择 -> GET /api/admin/statisticsList',
        requestMethod: 'GET',
        endpoint: '/api/admin/statisticsList',
      };
      const query = {
        pageNum: 1,
        pageSize: 40,
        userId: -1,
        proxyUserId: -1,
        phoneId: -1,
        startTime: `${queryStartDate} 00:00:00`,
        endTime: `${queryEndDate} 23:59:59`,
        carrireIdentif: record.identif,
        dayliModel: -1,
      };
      try {
        const latest = await limiter.api('GET', ENDPOINT, null, query);
        if (latest.dataError) throw new Error(latest.dataError);
        return {
          ...base,
          statDate: latest.latestDate,
          queryStartDate,
          queryEndDate,
          replyRatePct: latest.replyRate,
          replyRateRaw: latest.dailyRow.firstReplyRate,
          dailyRow: latest.dailyRow,
          ...(captureObservationTimes ? { observedAt: latest.observedAt } : {}),
          status: 'ok',
          reason: null,
          requestDispatched: true,
          cancelledBeforeDispatch: false,
        };
      } catch (error) {
        const requestDispatched = dispatchedQueries.has(query);
        const reason = error?.message || String(error);
        return {
          ...base,
          statDate: null,
          replyRatePct: null,
          status: 'error',
          reason: requestDispatched ? reason : `其它统计查询失败，本组未派发: ${reason}`,
          requestDispatched,
          cancelledBeforeDispatch: !requestDispatched,
        };
      }
    }));
  } finally {
    // Preserve successful sibling results and wait for every dispatched read
    // before the caller can persist the receipt or close its browser session.
    await limiter.drain();
  }
  return {
    replyRates,
    performance: {
      ...limiter.stats,
      durationSeconds: (performance.now() - started) / 1000,
      queryStartDate,
      queryEndDate,
    },
  };
}
