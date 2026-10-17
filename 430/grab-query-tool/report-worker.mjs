import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {Workbook,SpreadsheetFile} from '@oai/artifact-tool';
import {summary,STATUSES,SOURCE_URL} from './lib/model.mjs';
const [input,output,previewDir]=process.argv.slice(2);
if(!input||!output)throw new Error('Usage: report-worker.mjs JOB.json OUTPUT.xlsx [PREVIEW_DIR]');
const job=JSON.parse(await fs.readFile(input,'utf8')),stat=summary(job),rows=job.results;
assert.ok(rows.length>0);assert.ok(['completed','paused','failed','stopped'].includes(job.status));
const byKey=new Map(rows.map(r=>[r.queryIccid,r]));assert.equal(byKey.size,rows.length);
for(const r of rows){
  assert.ok(job.keys.includes(r.queryIccid));assert.match(r.queryIccid,/^\d{18,22}$/);
  if(r.status==='不存在'){const b=job.batches.find(b=>b.id===r.batchId);assert.equal(b.term,r.queryIccid);assert.equal(b.total,0);}
  else assert.equal(r.simNumber.replace(/\s+/g,'').replace(/[Ff]$/,''),r.queryIccid);
}
const hk=v=>v?new Date(Date.parse(v)+8*3600000):null;
const date=v=>v?new Date(v.replace(' ','T')+(v.includes(' ')?(v.length===16?':00Z':'Z'):'T00:00:00Z')):null;
const basis={'激活成功':'Activation Date 有值','激活中':'有激活申请日期，激活日期为空','未激活':'申请日期与激活日期均为空','不存在':'完整 ICCID 查询返回 0 条'};
const wb=Workbook.create(),main=wb.worksheets.add('查询结果'),info=wb.worksheets.add('汇总说明'),original=wb.worksheets.add('原始清单');
const font='Helvetica Neue';
function style(s,last,cols){s.showGridLines=false;s.getRange(`A1:${cols}${last}`).format={font:{name:font,size:11,color:'#1F2937'},rowHeight:22,verticalAlignment:'center'};}
function title(s,text,note){s.getRange('A1').values=[[text]];s.getRange('A1').format.font={name:font,size:16,bold:true};s.getRange('A1').format.rowHeight=30;s.getRange('A2').values=[[note]];s.getRange('A2').format.font={name:font,size:10,color:'#64748B'};}
function header(s,address){s.getRange(address).format={fill:'#334155',font:{name:font,bold:true,color:'#FFFFFF'},rowHeight:34,wrapText:true};}
function widths(s,last,items){for(const [c,n]of Object.entries(items))s.getRange(`${c}1:${c}${last}`).format.columnWidth=n;}
function colors(s,range){for(const [text,fill]of [['激活成功','#E7F3EB'],['激活中','#FFF2CD'],['未激活','#E9EFF8'],['不存在','#FBE9E8'],['未查询','#FFF2CD']])s.getRange(range).conditionalFormats.add('containsText',{text,format:{fill}});}
const end=rows.length+4,rawEnd=job.sourceRows.length+4;
style(main,end,'O');title(main,'Grab ICCID 查询结果',`${stat.completed} / ${stat.total} 个 ICCID 已查询${stat.remaining?'；这是部分结果，未完成项见原始清单。':'；日期保留 Grab 原值，查询时间为香港时间。'}`);
main.getRange('A4:O4').values=[['序号','原始 ICCID','查询 ICCID（去 F）','状态','激活日期','激活申请日期','购买日期','Grab SIM Number','SKU','出现次数','来源','查询时间（香港）','来源链接','判定依据','Grab Cost（原值）']];
main.getRange(`A5:O${end}`).values=rows.map((r,i)=>[i+1,r.originalIccid,r.queryIccid,r.status,date(r.activationDate),date(r.actRequestDate),date(r.purchaseDate),r.simNumber,r.sku,r.occurrences,r.sources.join('、'),hk(r.queriedAt),SOURCE_URL,basis[r.status],r.rawFields.lblCost||'']);
main.getRange(`B5:C${end}`).setNumberFormat('@');main.getRange(`H5:I${end}`).setNumberFormat('@');main.getRange(`E5:F${end}`).setNumberFormat('yyyy-mm-dd hh:mm');main.getRange(`G5:G${end}`).setNumberFormat('yyyy-mm-dd');main.getRange(`L5:L${end}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');
widths(main,end,{A:8,B:28,C:28,D:13,E:22,F:22,G:15,H:28,I:32,J:10,K:16,L:23,M:68,N:36,O:20});
main.tables.add(`A4:O${end}`,true,'GrabResults');header(main,'A4:O4');colors(main,`D5:D${end}`);main.freezePanes.freezeRows(4);main.freezePanes.freezeColumns(3);
style(original,rawEnd,'I');title(original,'原始 ICCID 清单','保留重复项；未查询不代表不存在。');
original.getRange('A4:I4').values=[['总序号','来源','来源行号','原始 ICCID','查询 ICCID（去 F）','状态','出现次数','激活日期','查询时间（香港）']];
const frequencies=new Map();for(const r of job.sourceRows)frequencies.set(r.queryIccid,(frequencies.get(r.queryIccid)||0)+1);
original.getRange(`A5:I${rawEnd}`).values=job.sourceRows.map((r,i)=>{const d=byKey.get(r.queryIccid);return [i+1,r.source,r.sourceRow,r.originalIccid,r.queryIccid,d?.status||'未查询',frequencies.get(r.queryIccid),date(d?.activationDate),hk(d?.queriedAt)];});
original.getRange(`D5:E${rawEnd}`).setNumberFormat('@');original.getRange(`H5:H${rawEnd}`).setNumberFormat('yyyy-mm-dd hh:mm');original.getRange(`I5:I${rawEnd}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');
widths(original,rawEnd,{A:9,B:14,C:12,D:28,E:28,F:13,G:12,H:22,I:23});original.tables.add(`A4:I${rawEnd}`,true,'OriginalIccids');header(original,'A4:I4');colors(original,`F5:F${rawEnd}`);original.freezePanes.freezeRows(4);
style(info,25,'F');title(info,'查询汇总与口径',`任务状态：${{completed:'已完成',paused:'已暂停',stopped:'已停止',failed:'异常中断'}[job.status]}。每次新任务实时查询，不读取历史结果作为缓存。`);
info.getRange('A4:B4').values=[['输入统计','数量']];header(info,'A4:B4');info.getRange('A5:A9').values=[['原始行数'],['去重 ICCID 数'],['重复行数'],['已查询'],['未查询']];
info.getRange('B5:B9').formulas=[[`=COUNTA('原始清单'!$D$5:$D$${rawEnd})`],[`=${job.uniqueIccids}`],['=B5-B6'],[`=COUNTA('查询结果'!$C$5:$C$${end})`],['=B6-B8']];
info.getRange('D4:E4').values=[['状态','数量']];header(info,'D4:E4');info.getRange('D5:D9').values=[...STATUSES.map(s=>[s]),['合计']];info.getRange('E5:E8').formulas=STATUSES.map(s=>[`=COUNTIF('查询结果'!$D$5:$D$${end},"${s}")`]);info.getRange('E9').formulas=[['=SUM(E5:E8)']];
info.getRange('A12:B12').values=[['状态','判定依据']];header(info,'A12:B12');info.getRange('A13:B16').values=Object.entries(basis);
info.getRange('A18:B24').values=[['开始时间（香港）',hk(job.startedAt)],['完成 / 停止（香港）',hk(job.finishedAt)],['数据来源',SOURCE_URL],['ICCID 处理','去掉末尾一个 F/f；完整 ICCID 以文本保存，不使用科学计数法。'],['状态说明','激活成功表示有激活日期，不代表当前套餐有效；激活中依据申请日期存在且激活日期为空。'],['不存在的范围','仅表示当前 Grab 登录账号内，完整 ICCID 查询返回 0 条。'],['任务编号',job.id]];
info.getRange('B18:B19').setNumberFormat('yyyy-mm-dd hh:mm:ss');info.getRange('B20:B24').format.wrapText=true;info.getRange('A20:B24').format.rowHeight=38;
widths(info,25,{A:26,B:78,C:4,D:16,E:12,F:4});colors(info,'D5:D8');
assert.deepEqual(main.getRange(`C5:C${end}`).values.flat(),rows.map(r=>r.queryIccid));assert.equal(info.getRange('B5').values[0][0],job.originalRows);assert.equal(info.getRange('E9').values[0][0],rows.length);
for(let i=0;i<4;i++)assert.equal(info.getRange(`E${i+5}`).values[0][0],stat.counts[STATUSES[i]]);
console.log((await wb.inspect({kind:'table',range:'汇总说明!A4:E9',include:'values,formulas',tableMaxRows:6,tableMaxCols:5,maxChars:1800})).ndjson);
console.log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:10},maxChars:1000})).ndjson);
if(previewDir){
  await fs.mkdir(previewDir,{recursive:true});
  for(const [s,range,cols]of [[main,`A1:G${Math.min(end,14)}`,[`B5:C${end}`]],[info,'A1:E24',[]],[original,`A1:I${Math.min(rawEnd,12)}`,[`D5:E${rawEnd}`]]]){
    const old=cols.map(c=>[c,s.getRange(c).values]);
    try{for(const [c,v]of old)s.getRange(c).values=v.map(row=>row.map(x=>typeof x==='string'&&/^\d+$/.test(x)?'\u2060'+x:x));const p=await wb.render({sheetName:s.name,range,scale:1.3,format:'png'});await fs.writeFile(path.join(previewDir,s.name+'.png'),new Uint8Array(await p.arrayBuffer()));}
    finally{for(const [c,v]of old)s.getRange(c).values=v;}
  }
}
assert.deepEqual(main.getRange(`C5:C${end}`).values.flat(),rows.map(r=>r.queryIccid));
await fs.mkdir(path.dirname(output),{recursive:true});const blob=await SpreadsheetFile.exportXlsx(wb);await blob.save(output);console.log(JSON.stringify({output,completed:rows.length,total:job.uniqueIccids}));
