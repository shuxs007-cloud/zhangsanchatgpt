import fs from 'node:fs/promises';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const spec = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
function literal(value) {
  // Excel dates are timezone-naive. Encode wall time in UTC; never let Node's
  // host timezone shift a source activation timestamp by eight hours.
  if (value && typeof value === 'object' && value.date) {
    return new Date(value.date.includes('T') ? `${value.date}Z` : `${value.date}T00:00:00Z`);
  }
  if (typeof value === 'string' && value.startsWith('=')) return `'${value}`;
  return value;
}
async function build(data, path, includeMatch) {
  try { await fs.access(path); throw new Error('禁止覆盖已有交付'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const wb = Workbook.create();
  const sheet = wb.worksheets.add('sheet1');
  const n = data.values.length, m = data.columnCount;
  sheet.getRangeByIndexes(0, 0, n, m).values = data.values.map(row => row.map(literal));
  // Contiguous style runs keep original number formats and date warning fills.
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < m;) {
      const id = data.styleIds[r][c];
      let end = c + 1;
      while (end < m && data.styleIds[r][end] === id) end++;
      sheet.getRangeByIndexes(r, c, 1, end-c).format = data.styles[id];
      c = end;
    }
  }
  for (let c = 0; c < m; c++) {
    sheet.getRangeByIndexes(0, c, n, 1).format.columnWidth = data.widths[c] || 14;
  }
  sheet.getRangeByIndexes(0, 0, 1, m).format.rowHeight = 34;
  sheet.getRangeByIndexes(0, 0, 1, m).format.wrapText = true;
  sheet.freezePanes.freezeRows(1);
  if (includeMatch) {
    const match = wb.worksheets.add('号码匹配核对');
    const values = [['卡号','匹配行数','匹配结果'], ...spec.phones.map(p =>
      [p, data.counts[p], data.counts[p] ? '已匹配' : '本轮源表未找到'])];
    match.getRangeByIndexes(0, 0, values.length, 3).values = values;
    match.getRangeByIndexes(0, 0, values.length, 1).setNumberFormat('@');
    match.getRangeByIndexes(0, 0, 1, 3).format.font.bold = true;
    match.getRangeByIndexes(0, 0, values.length, 3).format.columnWidth = 23;
    match.freezePanes.freezeRows(1);
  }
  const check = await wb.inspect({kind:'table', range:'sheet1!A1:D3', include:'values', tableMaxRows:3, tableMaxCols:4, maxChars:1200});
  const errors = await wb.inspect({kind:'match', searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!', options:{useRegex:true,maxResults:10},maxChars:2000});
  const output = await SpreadsheetFile.exportXlsx(wb);
  await output.save(path);
  return {path, rows:n-1, inspection:check.ndjson, formulaErrors:errors.ndjson};
}
const results = [];
results.push(await build(spec.source, spec.outputs.sourceWorkbook, false));
results.push(await build(spec.processed, spec.outputs.finalWorkbook, true));
console.log(JSON.stringify({status:'passed', results}));
