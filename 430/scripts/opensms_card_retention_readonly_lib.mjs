// Shared, read-only card-retention pagination for Task A reporting and D/E.

import { shortCardNo } from '../opensms_protocol_client.mjs';
import { mapReadOnlyBounded } from './opensms_readonly_concurrency.mjs';

export const CARD_RETENTION_PAGE_SIZE = 200;

function assertApi(label, response) {
  const code = Number(response?.code ?? 200);
  if (code !== 200) throw new Error(`${label}失败: ${JSON.stringify(response)}`);
}

export function normalizeCardRetentionDate(value) {
  const raw = String(value ?? '').trim();
  if (!raw) return '';
  const match = raw.match(/(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})/);
  if (!match) return '';
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  if (
    parsed.getUTCFullYear() !== year
    || parsed.getUTCMonth() !== month - 1
    || parsed.getUTCDate() !== day
  ) return '';
  return `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
}

function normalizeRecord(row) {
  return {
    cardNo: shortCardNo(row?.cardNo),
    expireBefore: normalizeCardRetentionDate(row?.expireBefore),
    expireAfter: normalizeCardRetentionDate(row?.expireAfter),
    chatId: String(row?.chatId ?? '').trim(),
    createTime: String(row?.createTime ?? '').trim(),
  };
}

export async function collectCardRetention(api) {
  let total;
  async function readPage(pageNum) {
    const response = await api('GET', '/phone/cardRetain/list', null, {
      pageNum,
      pageSize: CARD_RETENTION_PAGE_SIZE,
    });
    assertApi(`读取卡保留记录第${pageNum}页`, response);
    if (!Array.isArray(response?.rows) || response.total === undefined || response.total === null) {
      throw new Error(`卡保留记录第${pageNum}页缺少有效rows/total`);
    }
    const rawTotal = response.total;
    const pageTotal = Number(rawTotal);
    if (
      !['number', 'string'].includes(typeof rawTotal)
      || String(rawTotal).trim() === ''
      || !Number.isSafeInteger(pageTotal)
      || pageTotal < 0
    ) throw new Error(`卡保留记录第${pageNum}页total无效: ${rawTotal}`);
    if (total === undefined) total = pageTotal;
    if (pageTotal !== total) {
      throw new Error(`卡保留记录分页期间total变化: page=${pageNum}, expected=${total}, received=${pageTotal}`);
    }
    const expectedRows = Math.min(CARD_RETENTION_PAGE_SIZE, Math.max(0, total - (pageNum - 1) * CARD_RETENTION_PAGE_SIZE));
    if (response.rows.length !== expectedRows) {
      throw new Error(`卡保留记录第${pageNum}页行数不符: expected=${expectedRows}, received=${response.rows.length}`);
    }
    return { pageNum, rowCount: response.rows.length, rows: response.rows };
  }
  const first = await readPage(1);
  const pageCount = Math.max(1, Math.ceil(total / CARD_RETENTION_PAGE_SIZE));
  const pages = [first];
  if (pageCount > 1) {
    const rest = await mapReadOnlyBounded(
      Array.from({ length: pageCount - 1 }, (_, index) => index + 2),
      readPage,
      { concurrency: 8 },
    );
    pages.push(...rest);
  }
  const allRows = pages.flatMap(item => item.rows);
  if (allRows.length !== total) {
    throw new Error(`卡保留记录分页不完整: total=${total}, rows=${allRows.length}`);
  }
  const recordIds = new Set();
  for (const row of allRows) {
    const id = String(row?.id ?? '').trim();
    if (!id) continue;
    if (recordIds.has(id)) throw new Error(`卡保留记录存在重复后台ID: ${id}`);
    recordIds.add(id);
  }
  const records = allRows.map(normalizeRecord);
  const invalidCardRecordCount = records.filter(record => !/^\d{10}$/.test(record.cardNo)).length;
  if (invalidCardRecordCount) {
    throw new Error(`卡保留记录存在${invalidCardRecordCount}条无效卡号，停止处理`);
  }
  const cardNos = [...new Set(records.map(record => record.cardNo))];
  return {
    totalRecords: total,
    fetchedRecords: allRows.length,
    pageSize: CARD_RETENTION_PAGE_SIZE,
    pageCount,
    pages: pages.map(({ pageNum, rowCount }) => ({ pageNum, rowCount })),
    uniqueCardCount: cardNos.length,
    duplicateCardRecordCount: records.length - cardNos.length,
    invalidCardRecordCount,
    invalidExpireAfterRecordCount: records.filter(record => !record.expireAfter).length,
    cardNos,
    records,
  };
}
