// Independent carrier-configuration reads; no browser creation or writes here.
import { mapReadOnlyBounded } from './opensms_readonly_concurrency.mjs';

function carrierIdentif(row) {
  return String(row.carrireIdentif || row.carrierIdentif || row.identif || row.id || '').trim();
}

export async function collectCarrierConfigPagesReadOnly(api, { concurrency = 4 } = {}) {
  const startedAt = Date.now();
  const pageSize = 20;
  async function readPage(pageNum) {
    const response = await api('GET', '/carrier/config/listNotDeleted', null, { pageNum, pageSize });
    const code = Number(response?.code ?? 200);
    if (code !== 200) throw new Error(`读取运营商配置第${pageNum}页失败: ${JSON.stringify(response)}`);
    if (!Array.isArray(response.rows)) throw new Error(`运营商第${pageNum}页rows无效`);
    const total = Number(response.total);
    if (!Number.isInteger(total) || total < 0) throw new Error(`运营商total无效: ${response.total}`);
    return { pageNum, total, rows: response.rows };
  }
  const first = await readPage(1);
  const pageCount = Math.max(1, Math.ceil(first.total / pageSize));
  if (pageCount > 100) throw new Error(`运营商分页超出100页: ${pageCount}`);
  const rest = await mapReadOnlyBounded(
    Array.from({ length: pageCount - 1 }, (_, index) => index + 2),
    readPage,
    { concurrency },
  );
  const pages = [first, ...rest];
  for (const page of pages) {
    const expected = Math.min(pageSize, Math.max(0, first.total - (page.pageNum - 1) * pageSize));
    if (page.total !== first.total || page.rows.length !== expected) {
      throw new Error(`运营商分页不完整或查询期间变动: page=${page.pageNum}, total=${page.total}, expectedTotal=${first.total}, rows=${page.rows.length}, expectedRows=${expected}`);
    }
  }
  const rows = pages.flatMap(page => page.rows);
  const identifiers = rows.map(carrierIdentif);
  if (identifiers.some(id => !id) || new Set(identifiers).size !== identifiers.length) {
    throw new Error('运营商分页出现缺失或重复标识');
  }
  return {
    rows,
    pageSize,
    pageCount,
    totalRecords: first.total,
    pages: pages.map(page => ({ pageNum: page.pageNum, rowCount: page.rows.length })),
    elapsedMs: Date.now() - startedAt,
    requestedConcurrency: concurrency,
    effectiveRemainingPageConcurrency: Math.min(concurrency, pageCount - 1),
  };
}

export async function getCarrierConfigsReadOnly(api, options = {}) {
  return (await collectCarrierConfigPagesReadOnly(api, options)).rows;
}
