import fs from 'node:fs/promises';
import path from 'node:path';
import {Workbook,SpreadsheetFile,FileBlob} from '@oai/artifact-tool';
const [input,out]=process.argv.slice(2);if(!input||!out)throw new Error('Usage: inspect-portable.mjs INPUT.xlsx PREVIEWS_DIR');
const wb=await SpreadsheetFile.importXlsx(await FileBlob.load(input));await fs.mkdir(out,{recursive:true});
console.log((await wb.inspect({kind:'table',range:'汇总说明!A4:E9',include:'values,formulas',tableMaxRows:6,tableMaxCols:5,maxChars:1600})).ndjson);
console.log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:10},maxChars:800})).ndjson);
for(const [name,range,ids] of [['查询结果','A1:G8',['B5:C8']],['汇总说明','A1:E24',[]],['原始清单','A1:I9',['D5:E9']]]){
  const sheet=wb.worksheets.getItem(name);
  // Preview-only workaround for the renderer; no workbook is re-exported.
  for(const r of ids){const data=sheet.getRange(r).values;sheet.getRange(r).values=data.map(row=>row.map(v=>typeof v==='string'&&/^\d+$/.test(v)?'\u2060'+v:v));}
  const image=await wb.render({sheetName:name,range,scale:1.2,format:'png'});await fs.writeFile(path.join(out,name+'.png'),new Uint8Array(await image.arrayBuffer()));
}
