import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
const require = createRequire('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/package.json');
const {Workbook, SpreadsheetFile} = await import(require.resolve('@oai/artifact-tool'));
const [dataPath, outputPath] = process.argv.slice(2);
if (!dataPath || !outputPath) throw new Error('需要结果JSON和新输出XLSX路径');
try { await fs.access(outputPath); throw new Error('输出已存在，拒绝覆盖'); }
catch (e) { if (e.code !== 'ENOENT') throw e; }
const d = JSON.parse(await fs.readFile(dataPath, 'utf8'));
const manifest = JSON.parse(await fs.readFile(path.join(path.dirname(dataPath), 'scan_manifest.json'), 'utf8'));
let older={queries:[]};
try{older=JSON.parse(await fs.readFile(path.join(path.dirname(dataPath),'older_index_evidence.json'),'utf8'));}catch(e){if(e.code!=='ENOENT')throw e;}
const olderDates=new Map(older.queries.map(q=>[q.iccid,q.earliest[0].observed.slice(0,10)]));
if (!d.verification.passed) throw new Error('原文件独立核验未通过');
const wb = Workbook.create();
const col = n => {let s=''; for (;n;n=Math.floor((n-1)/26)) s=String.fromCharCode(65+(n-1)%26)+s; return s;};
const safe = v => typeof v === 'string' && /^[=+@]/.test(v) ? "'"+v : v;
const wallDate = value => new Date(value.slice(0,19)+'Z');
let tableNumber=0;
function sheet(name, title, note, headers, rows, widths) {
  const s = wb.worksheets.add(name), end = col(headers.length), last = rows.length + 5;
  s.showGridLines = false;
  s.getRange(`A1:${end}${Math.max(last,6)}`).format = {
    font:{name:'Arial',size:10,color:'#203047'},verticalAlignment:'center',rowHeight:22,
  };
  s.getRange('A2').values = [[title]];
  s.getRange('A2').format.font = {name:'Arial',size:14,bold:true,color:'#203047'};
  s.getRange('A3').values = [[note]];
  s.getRange('A3').format.font = {name:'Arial',size:10,italic:true,color:'#526174'};
  s.getRange(`A5:${end}${Math.max(last,5)}`).values = [headers,...rows.map(r=>r.map(safe))];
  s.getRange(`A5:${end}5`).format = {
    fill:'#29465D',font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},
    horizontalAlignment:'center',verticalAlignment:'center',rowHeight:30,
  };
  widths.forEach((w,i)=>s.getRange(`${col(i+1)}1:${col(i+1)}${Math.max(last,6)}`).format.columnWidth=w);
  if (rows.length>15) s.freezePanes.freezeRows(5);
  if (rows.length) s.tables.add(`A5:${end}${last}`,true,'FirstSeen'+(++tableNumber));
  return s;
}
const primaryRows=d.queries.map(q=>{
  const e=q.earliest[0], files=new Set(q.earliest.map(x=>x.path));
  const day=q.earliestDate?new Date(q.earliestDate+'T00:00:00Z'):null;
  const at=e?.precision==='second'?new Date(e.observed.slice(0,19)+'Z'):null;
  const status=olderDates.has(q.iccid)?'已有'+olderDates.get(q.iccid)+'更早证据，月内日期不是首次':q.status;
  return [q.input,day,at,e?path.basename(e.path):'未找到可定时文档',e?.sheet||'',e?.cell||'',files.size,q.matchedFiles,q.unknownTimeFiles,status];
});
const primary=sheet('逐卡首次出现',`${d.inputCount}个ICCID文档追溯（最近一个月）`,
  `扫描日期 ${d.since.slice(0,10)} 至 ${d.until.slice(0,10)}。日级和时间未知证据不强行排序；覆盖缺口见扫描范围。`,
  ['ICCID（原始）','月内最早可定时日期','时间（北京时间）','月内最早文档（首个候选）','工作表','单元格','同时段文档数','全部命中文档数','时间未知文档数','结论'],
  primaryRows,[25,23,24,76,22,12,18,18,19,52]);
const last=d.queries.length+5;
primary.getRange(`A6:A${last}`).setNumberFormat('@');
primary.getRange(`B6:B${last}`).setNumberFormat('yyyy-mm-dd');
primary.getRange(`C6:C${last}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');
primary.getRange(`G6:I${last}`).setNumberFormat('#,##0');
const evidenceItems=d.queries.flatMap(q=>q.earliest.map(e=>({input:q.input,...e})));
const evidenceRows=evidenceItems.map(e=>[e.input,wallDate(e.observed),e.precision==='day'?'日级':'秒级',e.basis,path.basename(e.path),e.sheet,e.cell,e.path,e.fileSha256,e.independentReadbackPassed?'通过':'未通过']);
const evidenceSheet=sheet('原文档证据','最早时段全部候选文档',
  '同日或同刻的原文件及副本全部保留。SHA256与单元格均已独立回读；不将归档时间当作激活时间。',
  ['ICCID','文档时间','时间精度','时间依据','文档名','工作表','单元格','原文件完整路径','文件SHA256','独立回读'],
  evidenceRows,[25,28,12,28,76,22,12,110,70,14]);
if(evidenceRows.length) evidenceSheet.getRange(`B6:B${evidenceRows.length+5}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');
evidenceItems.forEach((e,i)=>{if(e.precision==='day') evidenceSheet.getRange(`B${i+6}`).setNumberFormat('yyyy-mm-dd');});
const grouped=new Map();
for(const q of d.queries) for(const e of q.earliest){
  const k=e.path+'\u0000'+e.sheet;
  if(!grouped.has(k)) grouped.set(k,{...e,keys:new Set()});
  grouped.get(k).keys.add(q.iccid);
}
const groups=[...grouped.values()].sort((a,b)=>a.observed.localeCompare(b.observed)||a.path.localeCompare(b.path));
const groupSheet=sheet('最早文档分组','最早时段命中文档分组',
  '一张卡可同时出现在多份并列文档，因此本表各组人数不能直接相加作为总卡数。',
  ['文档时间','文档名','工作表','涉及ICCID数','原文件完整路径'],
  groups.map(g=>[wallDate(g.observed),path.basename(g.path),g.sheet,g.keys.size,g.path]),[28,76,22,18,110]);
if(groups.length) groupSheet.getRange(`A6:A${groups.length+5}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');
const coverage=[
  ['扫描开始',wallDate(d.until),''],['起始日期',wallDate(d.since),''],['输入ICCID',d.inputCount,'去掉末尾一个F/f后精确匹配'],
  ['发现表格',d.coverage.discovered,'全部指定根目录'],['本次已扫描',d.coverage.scannedFiles,'副本全部列入清单，重复内容只解析一次'],
  ['独立内容组',d.coverage.uniqueContents,'ZIP成员CRC及大小清单分组'],['日期范围外',d.coverage.outsideDateWindow,'有明确文档时间，早于起始日或晚于扫描开始'],
  ['无法读取',d.coverage.unreadableFiles,'不能当成未命中'],['命中ICCID',d.matchedCount,''],['有可定时命中',d.datedCount,''],
  ['旧索引更新时间',d.indexReference.indexedAt?wallDate(d.indexReference.indexedAt):'未知','旧索引只作参考，本次重新扫描磁盘'],
  ...d.coverage.roots.map(r=>['搜索目录',r,'']),
  ['文件类型',d.coverage.fileTypes.join('、'),''],['类型范围',d.coverage.excludedTypes,''],['结论边界',d.coverage.limitation,''],
  ...manifest.unreadable.map(x=>['无法读取',x.path,x.error||'未知错误']),
  ...d.invalidInputs.map(x=>['输入异常','第'+x.line+'行',x.reason]),
];
const coverageSheet=sheet('扫描范围与异常','扫描范围和未完成读取',
  '只读扫描原文件，不访问后台和任务F库；读取失败保留原因，不计作没有号码。',
  ['项目','值或路径','说明'],coverage,[25,115,90]);
coverageSheet.getRange('B6').setNumberFormat('yyyy-mm-dd hh:mm:ss');
coverageSheet.getRange('B7').setNumberFormat('yyyy-mm-dd');
if(d.indexReference.indexedAt) coverageSheet.getRange('B16').setNumberFormat('yyyy-mm-dd hh:mm:ss');
if(older.queries.length){
  const oldRows=older.queries.flatMap(q=>q.earliest.map(e=>[q.input,wallDate(e.observed),path.basename(e.path),e.sheet,e.cell,e.path,e.fileSha256]));
  const s=sheet('窗口外更早证据','已核验的窗口外更早文档',
    `${older.queries.length}个ICCID有早于本月窗口的证据；本页只核验索引阳性线索，不是窗口外全部文件扫描。`,
    ['ICCID','已核验文档时间','文档名','工作表','单元格','原文件完整路径','文件SHA256'],
    oldRows,[25,28,76,22,12,110,70]);
  s.getRange(`B6:B${oldRows.length+5}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');
}
wb.recalculate();
if(JSON.stringify(primary.getRange(`A6:A${last}`).values)!==JSON.stringify(d.queries.map(q=>[q.input]))) throw new Error('ICCID值或顺序不一致');
console.log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:20},maxChars:1500})).ndjson);
const previews=[['逐卡首次出现','A2:J11'],['原文档证据','A2:G11'],['最早文档分组','A2:D11'],['扫描范围与异常','A2:C13']];
if(older.queries.length)previews.push(['窗口外更早证据','A2:E11']);
for(const [name,range] of previews){
  const preview=await wb.render({sheetName:name,range,scale:1.25,format:'png'});
  await fs.writeFile(path.join(path.dirname(outputPath),name+'_preview.png'),new Uint8Array(await preview.arrayBuffer()));
}
const file=await SpreadsheetFile.exportXlsx(wb);await file.save(outputPath);
console.log(JSON.stringify({status:'passed',output:outputPath,identifiers:d.inputCount,sheets:older.queries.length?5:4,earliestEvidenceRows:evidenceRows.length,documentGroups:groups.length}));
