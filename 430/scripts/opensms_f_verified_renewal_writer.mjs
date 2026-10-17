// Offline artifact writer. Source payload excludes all account/password columns.
import fs from 'node:fs/promises';
import path from 'node:path';
import {FileBlob, SpreadsheetFile, Workbook} from '@oai/artifact-tool';

const [template, inputPath, directory] = process.argv.slice(2);
const input = JSON.parse(await fs.readFile(inputPath, 'utf8'));
for (const target of Object.values(input.outputs)) {
  try { await fs.access(target); throw new Error('Output exists; no overwrite'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
}
const toDate = value => value ? new Date(`${value}T00:00:00Z`) : null;
const deadline = await SpreadsheetFile.importXlsx(await FileBlob.load(template));
const sheet = deadline.worksheets.getItem('截止日');
if (JSON.stringify(sheet.getRange('A1:C1').values) !== JSON.stringify([['ICCID', '截止日', '激活日期']])) {
  throw new Error('Deadline template mismatch');
}
if (input.importRows.length) {
  const last = input.importRows.length + 1;
  sheet.getRange(`A2:A${last}`).setNumberFormat('@');
  sheet.getRange(`B2:C${last}`).setNumberFormat('yyyy-mm-dd');
  sheet.getRange(`A2:C${last}`).values = input.importRows.map(([iccid, cutoff, activation]) =>
    [iccid, toDate(cutoff), toDate(activation)]);
}
deadline.recalculate();
await deadline.inspect({kind:'table',range:'截止日!A1:C6',tableMaxRows:6,tableMaxCols:3,maxChars:1200});
const preview = await deadline.render({sheetName:'截止日',range:'A1:C10',scale:1.5,format:'png'});
await fs.writeFile(path.join(directory, 'import_preview.png'), new Uint8Array(await preview.arrayBuffer()));
await (await SpreadsheetFile.exportXlsx(deadline)).save(input.outputs.importWorkbook);

const review = Workbook.create();
const detail = review.worksheets.add('续费核对');
detail.showGridLines = false;
detail.getRange('A2').values = [[`任务F续费核对（${input.rows.length}个号码）`]];
detail.getRange('A2').format.font = {name:'Arial',size:14,bold:true};
detail.getRange('A3').values = [[`来源：${input.sourceName}。临时推算行待人工核查。本次未提交后台。`]];
detail.getRange('A3').format.font = {name:'Arial',size:10,italic:true};
const headers = ['号码','ICCID','余额（CAD）','套餐类型','表内截止日期','更新前F截止日',
  '更新后F截止日','截止日来源','激活日期','注册状态','源表行号','核验结果'];
detail.getRange('A5:L5').values = [headers];
const values = input.rows.map(r => [r.phone, r.iccid, Number(r.balanceCad), r.planType,
  toDate(r.sourceCutoff), toDate(r.beforeCutoff), toDate(r.cutoff),
  r.provisional ? '临时推算：库内有效截止日+29天' : (r.cutoff ? '表内日期' : '缺日期：保持原值'),
  toDate(r.activation), r.status, r.sourceRow, r.cutoff ? '本地入库并独立回读通过' : '余额/套餐已更新，日期未改']);
const last = values.length + 5;
if (values.length) detail.getRange(`A6:L${last}`).values = values;
detail.getRange(`A5:L${last}`).format.font = {name:'Arial',size:10};
detail.getRange(`A5:L${last}`).format.verticalAlignment = 'center';
detail.getRange('A5:L5').format = {fill:'#233F5D',font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},
  horizontalAlignment:'center',rowHeight:24};
detail.getRange(`A6:B${last}`).setNumberFormat('@');
detail.getRange(`C6:C${last}`).setNumberFormat('0.00');
detail.getRange(`D6:D${last}`).setNumberFormat('0');
detail.getRange(`E6:G${last}`).setNumberFormat('yyyy-mm-dd');
detail.getRange(`I6:I${last}`).setNumberFormat('yyyy-mm-dd');
detail.getRange(`K6:K${last}`).setNumberFormat('0');
detail.getRange(`A6:L${last}`).format.rowHeight = 21;
for (const [column,width] of [['A',16],['B',25],['C',15],['D',12],['E',17],['F',18],['G',18],
  ['H',43],['I',17],['J',44],['K',12],['L',34]]) {
  detail.getRange(`${column}5:${column}${last}`).format.columnWidth = width;
}
detail.getRange(`H6:H${last}`).conditionalFormats.add('containsText',
  {text:'临时推算',format:{fill:'#FFF2CC',font:{color:'#9C6500',bold:true}}});
detail.freezePanes.freezeRows(5);
detail.freezePanes.freezeColumns(2);
detail.tabColor = '#233F5D';
review.recalculate();
await review.inspect({kind:'table',range:'续费核对!A5:L10',tableMaxRows:6,tableMaxCols:12,maxChars:1800});
const reviewPreview = await review.render({sheetName:'续费核对',range:'A2:L10',scale:1,format:'png'});
await fs.writeFile(path.join(directory, 'review_preview.png'), new Uint8Array(await reviewPreview.arrayBuffer()));
await (await SpreadsheetFile.exportXlsx(review)).save(input.outputs.reviewWorkbook);
console.log(JSON.stringify({status:'passed',importCount:input.importRows.length,reviewCount:input.rows.length}));
