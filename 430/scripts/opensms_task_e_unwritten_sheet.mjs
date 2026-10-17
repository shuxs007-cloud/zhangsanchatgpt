// Build only the new tab; Python grafts it without rewriting existing sheets.
import fs from 'node:fs/promises';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const [input, output, preview] = process.argv.slice(2);
const rows = JSON.parse(await fs.readFile(input, 'utf8'));
const wb = Workbook.create();
const sheet = wb.worksheets.add('任务E未写入');
sheet.getRange(`A1:C${rows.length + 1}`).values = [
  ['手机号', '截止日', '未写入原因'],
  ...rows.map(row => [row.phone, row.expireDate, '后台未命中（未写入）']),
];
// Match the report's ISO text-date convention and preserve phone identifiers.
sheet.getRange(`A1:C${rows.length + 1}`).setNumberFormat('@');
sheet.getRange(`A1:C${rows.length + 1}`).format.rowHeight = 22;
sheet.getRange('A1:C1').format = {
  fill: '#D9EAF7', font: { bold: true, color: '#000000' },
};
sheet.getRange('A:A').format.columnWidth = 20;
sheet.getRange('B:B').format.columnWidth = 18;
sheet.getRange('C:C').format.columnWidth = 32;
sheet.freezePanes.freezeRows(1);
if (preview) {
  const check = await wb.inspect({kind:'region', sheetId:'任务E未写入', range:'A1:C8', maxChars:1800});
  console.log(check.ndjson);
  const blob = await wb.render({sheetName:'任务E未写入', range:`A1:C${Math.min(rows.length + 1, 12)}`, scale:1.5, format:'png'});
  await fs.writeFile(preview, new Uint8Array(await blob.arrayBuffer()));
}
await (await SpreadsheetFile.exportXlsx(wb)).save(output);
