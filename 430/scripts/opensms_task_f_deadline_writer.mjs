// Pure XLSX writer; no browser, credentials, API, or database writes.
import fs from 'node:fs/promises';
import {FileBlob, SpreadsheetFile} from '@oai/artifact-tool';

const [template, source, output] = process.argv.slice(2);
if (!template || !source || !output) throw Error('template, source and output required');
const rows = JSON.parse(await fs.readFile(source, 'utf8'));
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(template));
const sheet = workbook.worksheets.getItem('截止日');
const expected = [['ICCID', '截止日', '激活日期']];
if (JSON.stringify(sheet.getRange('A1:C1').values) !== JSON.stringify(expected)) {
  throw Error('Template headers/order mismatch');
}
if (rows.length) {
  const last = rows.length + 1;
  sheet.getRange(`A2:A${last}`).setNumberFormat('@');
  sheet.getRange(`B2:C${last}`).setNumberFormat('yyyy-mm-dd');
  sheet.getRange(`A2:C${last}`).values = rows.map(([iccid, cutoff, activation]) => [
    iccid, cutoff ? new Date(`${cutoff}T00:00:00Z`) : null,
    activation ? new Date(`${activation}T00:00:00Z`) : null,
  ]);
}
workbook.recalculate();
// Do not render each automatic run: the unchanged template layout was visually verified.
const inspection = await workbook.inspect({kind: 'table', range: '截止日!A1:C3',
  tableMaxRows: 3, tableMaxCols: 3, maxChars: 1200});
const errors = await workbook.inspect({kind:'match',
  searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',
  options:{useRegex:true,maxResults:10}, maxChars:1500});
await (await SpreadsheetFile.exportXlsx(workbook)).save(output);
if(process.env.OPENSMS_RENEWAL_RENDER_PATH){
  const preview=await workbook.render({sheetName:'截止日',range:'A1:C12',scale:1.5,format:'png'});
  await fs.writeFile(process.env.OPENSMS_RENEWAL_RENDER_PATH,new Uint8Array(await preview.arrayBuffer()));
}
console.log(JSON.stringify({status:'success',recordCount:rows.length,
  inspection:inspection.ndjson,formulaErrorScan:errors.ndjson}));
