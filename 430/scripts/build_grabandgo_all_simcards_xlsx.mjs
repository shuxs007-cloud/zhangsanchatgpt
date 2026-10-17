#!/usr/bin/env node

import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(path.join(process.cwd(), 'package.json'));
const { SpreadsheetFile, Workbook } = require('@oai/artifact-tool');

const FIELD_LABELS = {
  lblPurchaseDate: 'Purchase Date',
  lblActivationRequestDate: 'Act. Request Date',
  lblActivateDate: 'Activation Date',
  lblSimCardNumber: 'SIM Number',
  lblSKU: 'SKU',
  lblCost: 'Cost',
  lnSimDetails: 'Details',
};

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

function asText(value) {
  return String(value ?? '').trim();
}

async function main() {
  const inputPath = process.argv[2];
  const outputPath = process.argv[3];
  const previewDir = process.argv[4] || path.join(path.dirname(outputPath || '.'), 'previews');
  if (!inputPath || !outputPath) {
    throw new Error('用法: build_grabandgo_all_simcards_xlsx.mjs INPUT.json OUTPUT.xlsx [PREVIEW_DIR]');
  }
  const payload = JSON.parse(await fs.readFile(inputPath, 'utf8'));
  if (payload.status !== 'passed') throw new Error(`抓取回执未通过: ${payload.status}`);
  if (!Array.isArray(payload.rows) || payload.rows.length !== payload.total) {
    throw new Error(`抓取行数与总数不一致: rows=${payload.rows?.length}, total=${payload.total}`);
  }

  const fields = payload.fieldNames || [];
  const headers = ['序号', ...fields.map(field => FIELD_LABELS[field] || field), '源页面'];
  const values = [headers];
  for (const row of payload.rows) {
    values.push([
      Number(row.ordinal),
      ...fields.map(field => field === 'lblSimCardNumber' ? null : asText(row.fields?.[field])),
      payload.sourceUrl || payload.interface?.endpoint || '',
    ]);
  }

  const workbook = Workbook.create();
  const summary = workbook.worksheets.add('抓取汇总');
  const data = workbook.worksheets.add('全部SIM数据');
  const endRow = values.length;
  const endCol = columnName(headers.length - 1);
  const finishedAt = payload.finishedAt ? new Date(payload.finishedAt) : null;

  summary.showGridLines = false;
  summary.mergeCells('A1:D1');
  summary.getRange('A1').values = [['Grab&Go Customer SIM Cards 完整导出']];
  summary.getRange('A1:D1').format = {
    fill: '#0F766E',
    font: { bold: true, color: '#FFFFFF' },
    verticalAlignment: 'center',
  };
  summary.getRange('A1:D1').format.rowHeight = 32;
  summary.getRange('A3:B14').values = [
    ['项目', '结果'],
    ['登录校验', payload.loginProof?.showAllPresent ? 'Show All 已确认' : '未确认'],
    ['后台会话', `${payload.browser} / 430`],
    ['接口类型', payload.interface?.type || ''],
    ['接口地址', payload.interface?.endpoint || payload.sourceUrl || ''],
    ['接口方法', payload.interface?.method || 'POST'],
    ['页面总记录数', payload.total],
    ['Excel 数据行数', null],
    ['字段数', fields.length],
    ['SIM Number 唯一数', payload.completeness?.simNumberUniqueCount ?? null],
    ['重复 SIM Number', payload.completeness?.duplicateSimNumberCount ?? null],
    ['抓取完成时间', finishedAt],
  ];
  summary.getRange('B10').formulas = [[`=ROWS('全部SIM数据'!$A$2:$A$${endRow})`]];
  summary.getRange('A3:B3').format = {
    fill: '#115E59',
    font: { bold: true, color: '#FFFFFF' },
  };
  summary.getRange('A4:A14').format.font = { bold: true, color: '#334155' };
  summary.getRange('A3:B14').format.borders = { preset: 'inside', style: 'thin', color: '#CBD5E1' };
  summary.getRange('A3:B14').format.wrapText = true;
  summary.getRange('B14').format.numberFormat = 'yyyy-mm-dd hh:mm:ss';
  summary.getRange('A1:A14').format.columnWidth = 22;
  summary.getRange('B1:B14').format.columnWidth = 68;
  summary.getRange('A16:C16').values = [['字段', '非空数量', '空值数量']];
  summary.getRange(`A17:C${16 + fields.length}`).values = fields.map(field => [
    FIELD_LABELS[field] || field,
    payload.completeness?.nonEmptyByField?.[field] ?? null,
    payload.completeness?.blankByField?.[field] ?? null,
  ]);
  summary.getRange('A16:C16').format = {
    fill: '#115E59',
    font: { bold: true, color: '#FFFFFF' },
  };
  summary.getRange(`A16:C${16 + fields.length}`).format.borders = { preset: 'inside', style: 'thin', color: '#CBD5E1' };
  summary.getRange(`A16:A${16 + fields.length}`).format.columnWidth = 28;
  summary.getRange(`B16:C${16 + fields.length}`).format.columnWidth = 16;
  summary.getRange(`B17:C${16 + fields.length}`).format.numberFormat = '#,##0';
  summary.freezePanes.freezeRows(1);

  data.showGridLines = false;
  data.getRange(`A1:${endCol}${endRow}`).values = values;
  data.getRange(`A1:${endCol}1`).format = {
    fill: '#0F766E',
    font: { bold: true, color: '#FFFFFF' },
    horizontalAlignment: 'center',
    verticalAlignment: 'center',
  };
  data.getRange(`A1:${endCol}${endRow}`).format.borders = {
    insideHorizontal: { style: 'thin', color: '#E2E8F0' },
  };
  data.getRange(`A2:A${endRow}`).format.numberFormat = '0';
  const simIndex = fields.indexOf('lblSimCardNumber');
  if (simIndex >= 0) {
    const simColumn = columnName(simIndex + 1);
    data.getRange(`${simColumn}2:${simColumn}${endRow}`).formulas = payload.rows.map(row => [
      `="${asText(row.fields?.lblSimCardNumber).replace(/"/g, '""')}"`,
    ]);
    data.getRange(`${simColumn}2:${simColumn}${endRow}`).format.numberFormat = '@';
  }
  data.getRange(`A1:A${endRow}`).format.columnWidth = 10;
  for (let index = 1; index < headers.length; index += 1) {
    const field = fields[index - 1];
    const col = columnName(index);
    let width = 20;
    if (field === 'lblSimCardNumber') width = 25;
    else if (field === 'lblActivationRequestDate' || field === 'lblActivateDate') width = 23;
    else if (field === 'lnSimDetails') width = 14;
    data.getRange(`${col}1:${col}${endRow}`).format.columnWidth = width;
  }
  const sourceColumn = columnName(headers.length - 1);
  data.getRange(`${sourceColumn}1:${sourceColumn}${endRow}`).format.columnWidth = 48;
  data.getRange(`A1:${endCol}${endRow}`).format.rowHeight = 20;
  data.freezePanes.freezeRows(1);
  data.freezePanes.freezeColumns(1);
  const table = data.tables.add(`A1:${endCol}${endRow}`, true, 'AllCustomerSimCards');
  table.style = 'TableStyleMedium2';
  table.showFilterButton = true;
  table.showBandedRows = true;

  const summaryInspect = await workbook.inspect({
    kind: 'table', range: `抓取汇总!A1:C${16 + fields.length}`, include: 'values,formulas', tableMaxRows: 30, tableMaxCols: 4,
  });
  const dataInspect = await workbook.inspect({
    kind: 'table', range: `全部SIM数据!A1:${endCol}${Math.min(endRow, 12)}`,
    include: 'values,formulas', tableMaxRows: 12, tableMaxCols: Math.min(headers.length, 12),
  });
  const errors = await workbook.inspect({
    kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A',
    options: { useRegex: true, maxResults: 100 }, summary: 'final formula error scan',
  });

  await fs.mkdir(previewDir, { recursive: true });
  const summaryPreview = await workbook.render({ sheetName: '抓取汇总', range: `A1:D${16 + fields.length}`, scale: 1.5, format: 'png' });
  await fs.writeFile(path.join(previewDir, '抓取汇总.png'), new Uint8Array(await summaryPreview.arrayBuffer()));
  const dataPreview = await workbook.render({
    sheetName: '全部SIM数据', range: `A1:${endCol}${Math.min(endRow, 28)}`, scale: 1.2, format: 'png',
  });
  await fs.writeFile(path.join(previewDir, '全部SIM数据_前27行.png'), new Uint8Array(await dataPreview.arrayBuffer()));

  await fs.mkdir(path.dirname(path.resolve(outputPath)), { recursive: true });
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(path.resolve(outputPath));
  console.log(JSON.stringify({
    status: 'passed',
    outputPath: path.resolve(outputPath),
    rowCount: payload.rows.length,
    fieldCount: fields.length,
    headers,
    summaryInspect: summaryInspect.ndjson,
    dataInspect: dataInspect.ndjson,
    formulaErrorScan: errors.ndjson,
    previewDir: path.resolve(previewDir),
  }));
}

main().catch(error => {
  console.error(error?.stack || String(error));
  process.exit(1);
});
