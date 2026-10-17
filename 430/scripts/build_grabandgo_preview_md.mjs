#!/usr/bin/env node

import fs from 'node:fs/promises';
import path from 'node:path';

const args = process.argv.slice(2);
const valueOf = flag => {
  const index = args.indexOf(flag);
  return index >= 0 ? args[index + 1] : '';
};
const input = valueOf('--input-json');
const output = valueOf('--output-md');
if (!input || !output) throw new Error('用法: --input-json FILE --output-md FILE');

const payload = JSON.parse(await fs.readFile(path.resolve(input), 'utf8'));
if (payload.status !== 'passed' || !Array.isArray(payload.rows)) {
  throw new Error(`源数据未通过校验: status=${payload.status}`);
}

const labels = {
  lblPurchaseDate: 'Purchase Date',
  lblActivationRequestDate: 'Act. Request Date',
  lblActivateDate: 'Activation Date',
  lblSimCardNumber: 'SIM Number',
  lblSKU: 'SKU',
  lblCost: 'Cost',
  lnSimDetails: 'Details',
};
const fields = payload.fieldNames || Object.keys(labels);
const escapeCell = value => String(value ?? '')
  .replace(/\|/g, '\\|')
  .replace(/\r?\n/g, '<br>');
const rows = payload.rows;
const sampleIndexes = [
  ...Array.from({ length: Math.min(30, rows.length) }, (_, index) => index),
  ...Array.from({ length: Math.min(10, Math.max(0, rows.length - 30)) }, (_, index) => rows.length - 10 + index),
];
const uniqueIndexes = [...new Set(sampleIndexes)].sort((a, b) => a - b);

const lines = [
  '# Grab&Go Customer SIM Cards 下载数据预览',
  '',
  '> 这是确认用预览；最终 Excel 尚未生成。数据只保存在本机。',
  '',
  `- 抓取状态：${payload.status}`,
  `- 网页总数：${payload.total}`,
  `- 已抓取：${payload.extractedRowCount}`,
  `- SIM Number 唯一数：${payload.completeness?.simNumberUniqueCount ?? ''}`,
  `- 完整字段：${fields.map(field => labels[field] || field).join('、')}`,
  `- 抓取开始：${payload.startedAt || ''}`,
  `- 抓取完成：${payload.finishedAt || ''}`,
  '',
  '## 字段完整性',
  '',
  '| 字段 | 非空 | 空值 |',
  '| --- | ---: | ---: |',
  ...fields.map(field => `| ${escapeCell(labels[field] || field)} | ${payload.completeness?.nonEmptyByField?.[field] ?? ''} | ${payload.completeness?.blankByField?.[field] ?? ''} |`),
  '',
  '## 数据样例（前30条 + 后10条）',
  '',
  `| 序号 | ${fields.map(field => escapeCell(labels[field] || field)).join(' | ')} |`,
  `| ---: | ${fields.map(() => '---').join(' | ')} |`,
  ...uniqueIndexes.map(index => {
    const row = rows[index];
    return `| ${row.ordinal} | ${fields.map(field => escapeCell(row.fields?.[field] || '')).join(' | ')} |`;
  }),
  '',
  '## 说明',
  '',
  '- 预览只展示前30条和后10条；最终确认后，Excel 将写入全部 9,041 条。',
  '- 页面空白日期保持空白，不做推断或补值。',
  '- SIM Number 在最终 Excel 中按文本保存，避免科学计数法或末位精度丢失。',
  '',
];

const resolved = path.resolve(output);
await fs.mkdir(path.dirname(resolved), { recursive: true });
const temporary = `${resolved}.${process.pid}.tmp`;
await fs.writeFile(temporary, lines.join('\n'), 'utf8');
await fs.rename(temporary, resolved);
console.log(JSON.stringify({ status: 'passed', outputMd: resolved, sampleRows: uniqueIndexes.length, total: rows.length }));
