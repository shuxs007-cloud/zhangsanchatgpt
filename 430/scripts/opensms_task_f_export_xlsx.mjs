#!/usr/bin/env node

import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(path.join(process.cwd(), 'package.json'));
const { SpreadsheetFile, Workbook } = require('@oai/artifact-tool');

const JSON_CHUNK_SIZE = 30000;

function asText(value) {
  if (value === null || value === undefined) return '';
  return String(value).trim();
}

function columnName(index) {
  let value = index + 1;
  let result = '';
  while (value > 0) {
    const remainder = (value - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    value = Math.floor((value - 1) / 26);
  }
  return result;
}

function splitExcelText(value) {
  const text = asText(value);
  if (!text) return [''];
  const chunks = [];
  for (let index = 0; index < text.length; index += JSON_CHUNK_SIZE) {
    chunks.push(text.slice(index, index + JSON_CHUNK_SIZE));
  }
  return chunks;
}

function exactTextFormula(value) {
  return `="${asText(value).replaceAll('"', '""')}"`;
}

function addTable(sheet, values, tableName, widths) {
  const rowCount = values.length;
  const columnCount = values[0].length;
  const endColumn = columnName(columnCount - 1);
  sheet.showGridLines = false;
  sheet.getRange(`A1:${endColumn}${rowCount}`).values = values;
  sheet.getRange(`A1:${endColumn}1`).format = {
    fill: '#0F766E',
    font: { bold: true, color: '#FFFFFF' },
    horizontalAlignment: 'center',
    verticalAlignment: 'center',
    wrapText: true,
  };
  sheet.getRange(`A1:${endColumn}${rowCount}`).format.borders = {
    insideHorizontal: { style: 'thin', color: '#E2E8F0' },
  };
  sheet.getRange(`A1:${endColumn}${rowCount}`).format.verticalAlignment = 'top';
  sheet.getRange(`A1:${endColumn}${rowCount}`).format.wrapText = true;
  sheet.getRange(`A1:${endColumn}1`).format.rowHeight = 32;
  for (let index = 0; index < columnCount; index += 1) {
    sheet.getRange(`${columnName(index)}1:${columnName(index)}${rowCount}`).format.columnWidth = widths[index] || 18;
  }
  if (rowCount > 1) {
    sheet.getRange(`A2:${endColumn}${rowCount}`).format.rowHeight = 24;
  }
  sheet.freezePanes.freezeRows(1);
  sheet.freezePanes.freezeColumns(1);
  const table = sheet.tables.add(`A1:${endColumn}${rowCount}`, true, tableName);
  table.style = 'TableStyleMedium2';
  table.showFilterButton = true;
  table.showBandedRows = true;
  return { rowCount, columnCount, endColumn };
}

async function main() {
  const inputPath = process.argv[2];
  const outputPath = process.argv[3];
  const previewDir = process.argv[4];
  const receiptPath = process.argv[5];
  if (!inputPath || !outputPath || !previewDir || !receiptPath) {
    throw new Error('用法: opensms_task_f_export_xlsx.mjs INPUT.json OUTPUT.xlsx PREVIEW_DIR RECEIPT.json');
  }

  const payload = JSON.parse(await fs.readFile(inputPath, 'utf8'));
  const rows = Array.isArray(payload.sampleRows) ? payload.sampleRows : [];
  if (!rows.length) throw new Error('任务F Excel没有可导出的结果行');

  const resultHeaders = [
    '序号', '手机号', '卡槽', 'OPR', '当前运营商', '原始ICCID', '查询ICCID',
    'Purchase Date', 'Act. Request Date', 'Activation Date原值', '标准激活日期',
    '截止日', 'SIM Number', 'SKU', '查询状态', '线程', '异常', 'OpenSMS源记录数',
  ];
  const resultValues = [resultHeaders];
  rows.forEach((row, index) => {
    const occurrences = Array.isArray(row.sourceOccurrences) ? row.sourceOccurrences : [];
    resultValues.push([
      index + 1,
      asText(row.phone),
      asText(row.slot),
      asText(row.opr),
      asText(row.carrierName),
      asText(row.sourceIccid),
      asText(row.queryIccid),
      asText(row.purchaseDate),
      asText(row.actRequestDate),
      asText(row.activationDateRaw),
      asText(row.activationDate),
      asText(row.cutoffDate),
      asText(row.simNumber),
      asText(row.sku),
      asText(row.status),
      Number(row.workerId || 0),
      asText(row.error),
      occurrences.length || (row.sourceData && Object.keys(row.sourceData).length ? 1 : 0),
    ]);
  });

  const sourceItems = [];
  for (const row of rows) {
    const occurrences = Array.isArray(row.sourceOccurrences) && row.sourceOccurrences.length
      ? row.sourceOccurrences
      : [row.sourceData || {}];
    occurrences.forEach((occurrence, occurrenceIndex) => {
      const raw = occurrence && typeof occurrence === 'object' && occurrence.sourceData && typeof occurrence.sourceData === 'object'
        ? occurrence.sourceData
        : occurrence;
      const jsonChunks = splitExcelText(JSON.stringify(raw || {}, null, 2));
      sourceItems.push({ row, occurrence, occurrenceIndex, jsonChunks });
    });
  }
  const maxJsonChunks = Math.max(1, ...sourceItems.map(item => item.jsonChunks.length));
  const sourceHeaders = [
    '查询ICCID', '源记录序号', '手机号', '卡槽', 'OPR', '当前运营商', '原始ICCID',
    ...Array.from({ length: maxJsonChunks }, (_, index) => `完整源JSON_${index + 1}`),
  ];
  const sourceValues = [sourceHeaders];
  for (const item of sourceItems) {
    const occurrence = item.occurrence && typeof item.occurrence === 'object' ? item.occurrence : {};
    const sourceData = occurrence.sourceData && typeof occurrence.sourceData === 'object'
      ? occurrence.sourceData
      : occurrence;
    const chunks = [...item.jsonChunks];
    while (chunks.length < maxJsonChunks) chunks.push('');
    sourceValues.push([
      asText(item.row.queryIccid),
      item.occurrenceIndex + 1,
      asText(occurrence.phone ?? sourceData.phone ?? item.row.phone),
      asText(occurrence.slot ?? sourceData.slot ?? item.row.slot),
      asText(occurrence.opr ?? sourceData.opr ?? item.row.opr),
      asText(occurrence.carrierName ?? sourceData.carrierName ?? item.row.carrierName),
      asText(occurrence.sourceIccid ?? sourceData.sourceIccid ?? item.row.sourceIccid),
      ...chunks,
    ]);
  }

  const statusCounts = payload.statusCounts || {};
  const summaryValues = [
    ['项目', '结果'],
    ['运行编号', asText(payload.runId)],
    ['执行模式', '任务F 100条样例；未运行B/C/D/E'],
    ['浏览器', asText(payload.browser || 'BitBrowser 430（后台静默）')],
    ['请求线程数', Number(payload.requestedWorkers || 0)],
    ['实际线程数', Number(payload.effectiveWorkers || 0)],
    ['独立ASP.NET会话数', Number(payload.uniqueAspNetSessionCount || 0)],
    ['会话模式', asText(payload.workerSessionMode)],
    ['自适应并发', payload.adaptiveConcurrency ? '是' : '否'],
    ['共享会话强制单线程', payload.sharedSessionForcesSerial ? '是' : '否'],
    ['并发降级原因', asText(payload.concurrencyLimitedReason)],
    ['ICCID身份保护', asText(payload.strictIdentityGuard)],
    ['完整Kooda库存数', Number(payload.inventoryCount || 0)],
    ['数据库原有已完成数', Number(payload.alreadyQueriedCount || 0)],
    ['本轮完整待查数', Number(payload.fullPendingCount || 0)],
    ['本次查询数', rows.length],
    ['found', Number(statusCounts.found || 0)],
    ['not_found', Number(statusCounts.not_found || 0)],
    ['activation_missing', Number(statusCounts.activation_missing || 0)],
    ['transient_error', Number(statusCounts.transient_error || 0)],
    ['写入数据库终态数', Number(payload.terminalImportedCount || 0)],
    ['写入数据库found数', Number(payload.foundImportedCount || 0)],
    ['记录结构版本', Number(payload.recordSchemaVersion || 0)],
    ['剩余待查估算', Math.max(0, Number(payload.fullPendingCount || 0) - rows.length)],
    ['生成时间', asText(payload.generatedAt)],
  ];

  const workbook = Workbook.create();
  const summarySheet = workbook.worksheets.add('执行汇总');
  const resultSheet = workbook.worksheets.add('任务F结果');
  const sourceSheet = workbook.worksheets.add('OpenSMS完整源数据');

  summarySheet.showGridLines = false;
  summarySheet.getRange('A1:B25').values = summaryValues;
  summarySheet.getRange('A1:B1').format = {
    fill: '#0F766E',
    font: { bold: true, color: '#FFFFFF' },
    horizontalAlignment: 'center',
    verticalAlignment: 'center',
  };
  summarySheet.getRange('A2:A25').format = { font: { bold: true, color: '#334155' } };
  summarySheet.getRange('A1:B25').format.borders = { preset: 'inside', style: 'thin', color: '#CBD5E1' };
  summarySheet.getRange('A1:B25').format.wrapText = true;
  summarySheet.getRange('A1:A25').format.columnWidth = 28;
  summarySheet.getRange('B1:B25').format.columnWidth = 56;
  summarySheet.getRange('A1:B25').format.rowHeight = 23;
  summarySheet.freezePanes.freezeRows(1);

  const resultMeta = addTable(
    resultSheet,
    resultValues,
    'TaskFResults',
    [8, 18, 18, 12, 20, 27, 27, 19, 23, 23, 18, 18, 27, 20, 20, 9, 40, 16],
  );
  for (const index of [1, 5, 6, 12]) {
    const col = columnName(index);
    resultSheet.getRange(`${col}2:${col}${resultMeta.rowCount}`).format.numberFormat = '@';
  }
  rows.forEach((row, index) => {
    const excelRow = index + 2;
    resultSheet.getRange(`G${excelRow}`).formulas = [[exactTextFormula(row.queryIccid)]];
    resultSheet.getRange(`M${excelRow}`).formulas = [[exactTextFormula(row.simNumber)]];
  });
  if (resultMeta.rowCount > 1) {
    resultSheet.getRange(`Q2:Q${resultMeta.rowCount}`).format.rowHeight = 32;
  }

  const sourceWidths = [27, 12, 18, 18, 12, 20, 27, ...Array(maxJsonChunks).fill(70)];
  const sourceMeta = addTable(sourceSheet, sourceValues, 'TaskFOpenSmsSources', sourceWidths);
  for (const index of [0, 2, 6]) {
    const col = columnName(index);
    sourceSheet.getRange(`${col}2:${col}${sourceMeta.rowCount}`).format.numberFormat = '@';
  }
  sourceItems.forEach((item, index) => {
    sourceSheet.getRange(`A${index + 2}`).formulas = [[exactTextFormula(item.row.queryIccid)]];
  });
  if (sourceMeta.rowCount > 1) {
    sourceSheet.getRange(`A2:${sourceMeta.endColumn}${sourceMeta.rowCount}`).format.rowHeight = 55;
  }
  summarySheet.getRange('B25').formulas = [[exactTextFormula(payload.generatedAt)]];

  const summaryInspect = await workbook.inspect({
    kind: 'table', range: '执行汇总!A1:B25', include: 'values,formulas', tableMaxRows: 28, tableMaxCols: 4,
  });
  const resultInspect = await workbook.inspect({
    kind: 'table', range: `任务F结果!A1:${resultMeta.endColumn}${Math.min(resultMeta.rowCount, 12)}`,
    include: 'values,formulas', tableMaxRows: 12, tableMaxCols: resultMeta.columnCount,
  });
  const sourceInspect = await workbook.inspect({
    kind: 'table', range: `OpenSMS完整源数据!A1:${sourceMeta.endColumn}${Math.min(sourceMeta.rowCount, 6)}`,
    include: 'values,formulas', tableMaxRows: 6, tableMaxCols: Math.min(sourceMeta.columnCount, 12),
  });
  const formulaErrors = await workbook.inspect({
    kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A',
    options: { useRegex: true, maxResults: 100 }, summary: 'Task F final formula error scan',
  });

  await fs.mkdir(previewDir, { recursive: true });
  const previewSpecs = [
    ['执行汇总', 'A1:B25', '执行汇总.png', 1.5],
    ['任务F结果', `A1:${resultMeta.endColumn}${Math.min(resultMeta.rowCount, 24)}`, '任务F结果_前23条.png', 1.05],
    ['OpenSMS完整源数据', `A1:${sourceMeta.endColumn}${Math.min(sourceMeta.rowCount, 7)}`, 'OpenSMS完整源数据_前6条.png', 0.8],
  ];
  const previews = [];
  for (const [sheetName, range, fileName, scale] of previewSpecs) {
    const image = await workbook.render({ sheetName, range, scale, format: 'png' });
    const previewPath = path.resolve(previewDir, fileName);
    await fs.writeFile(previewPath, new Uint8Array(await image.arrayBuffer()));
    previews.push(previewPath);
  }

  await fs.mkdir(path.dirname(path.resolve(outputPath)), { recursive: true });
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(path.resolve(outputPath));

  const receipt = {
    status: 'passed',
    outputPath: path.resolve(outputPath),
    resultRowCount: rows.length,
    sourceOccurrenceRowCount: sourceItems.length,
    sourceJsonChunkColumns: maxJsonChunks,
    sheetNames: ['执行汇总', '任务F结果', 'OpenSMS完整源数据'],
    exactTextColumns: ['手机号', '原始ICCID', '查询ICCID', 'SIM Number'],
    summaryInspect: summaryInspect.ndjson,
    resultInspect: resultInspect.ndjson,
    sourceInspect: sourceInspect.ndjson,
    formulaErrorScan: formulaErrors.ndjson,
    previews,
  };
  await fs.mkdir(path.dirname(path.resolve(receiptPath)), { recursive: true });
  await fs.writeFile(path.resolve(receiptPath), `${JSON.stringify(receipt, null, 2)}\n`, 'utf8');
  console.log(JSON.stringify(receipt));
}

main().catch(error => {
  console.error(error?.stack || String(error));
  process.exit(1);
});
