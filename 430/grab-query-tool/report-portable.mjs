// Standalone Windows has no Codex/@oai artifact runtime (not a public npm package).
// Keep the same workbook contract with a redistributable, pure-JavaScript XLSX writer.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import ExcelJS from 'exceljs';
import {summary,STATUSES,SOURCE_URL} from './lib/model.mjs';

export async function exportPortable(job,output){
  const rows=job.results,stat=summary(job),end=rows.length+4,rawEnd=job.sourceRows.length+4;
  assert.ok(rows.length>0&&['completed','paused','failed','stopped'].includes(job.status));
  const byKey=new Map(rows.map(r=>[r.queryIccid,r]));assert.equal(byKey.size,rows.length);
  for(const r of rows){
    assert.ok(job.keys.includes(r.queryIccid));assert.match(r.queryIccid,/^\d{18,22}$/);
    assert.ok(STATUSES.includes(r.status));
    if(r.status==='不存在'){const b=job.batches.find(b=>b.id===r.batchId);assert.equal(b.term,r.queryIccid);assert.equal(b.total,0);assert.equal(b.rendered,0);}
    else assert.equal(r.simNumber.replace(/\s+/g,'').replace(/[Ff]$/,''),r.queryIccid);
  }
  const hk=v=>v?new Date(Date.parse(v)+8*3600000):null;
  const date=v=>v?new Date(v.replace(' ','T')+(v.includes(' ')?(v.length===16?':00Z':'Z'):'T00:00:00Z')):null;
  const basis={'激活成功':'Activation Date 有值','激活中':'有激活申请日期，激活日期为空','未激活':'申请日期与激活日期均为空','不存在':'完整 ICCID 查询返回 0 条'};
  const wb=new ExcelJS.Workbook();wb.creator='Grab ICCID 查询工具';wb.calcProperties.fullCalcOnLoad=true;
  const main=wb.addWorksheet('查询结果'),info=wb.addWorksheet('汇总说明'),original=wb.addWorksheet('原始清单');
  function decorate(sheet,title,note,widths){
    sheet.views=[{showGridLines:false}];sheet.columns=widths.map(width=>({width}));
    sheet.eachRow(row=>{row.height=22;row.eachCell(cell=>{cell.font={name:'Arial',size:11,color:{argb:'FF1F2937'}};cell.alignment={vertical:'middle'};});});
    sheet.getCell('A1').value=title;sheet.getCell('A1').font={name:'Arial',size:16,bold:true,color:{argb:'FF1F2937'}};sheet.getRow(1).height=30;
    sheet.getCell('A2').value=note;sheet.getCell('A2').font={name:'Arial',size:10,color:{argb:'FF64748B'}};
  }
  function header(sheet,row,from,to){sheet.getRow(row).height=34;for(let c=from;c<=to;c++){const cell=sheet.getCell(row,c);cell.font={name:'Arial',size:11,bold:true,color:{argb:'FFFFFFFF'}};cell.fill={type:'pattern',pattern:'solid',fgColor:{argb:'FF334155'}};cell.alignment={vertical:'middle',wrapText:true};}}
  function table(sheet,name,columns,data){sheet.addTable({name,ref:'A4',headerRow:true,totalsRow:false,style:{theme:'TableStyleMedium2',showRowStripes:true},columns:columns.map(name=>({name,filterButton:true})),rows:data});}
  function formats(sheet,columns,start,finish,format){for(const c of columns)for(let r=start;r<=finish;r++)sheet.getCell(r,c).numFmt=format;}
  function colors(sheet,range,anchor){sheet.addConditionalFormatting({ref:range,rules:[['激活成功','FFE7F3EB'],['激活中','FFFFF2CD'],['未激活','FFE9EFF8'],['不存在','FFFBE9E8'],['未查询','FFFFF2CD']].map(([s,argb])=>({type:'expression',formulae:[`${anchor}="${s}"`],style:{fill:{type:'pattern',pattern:'solid',bgColor:{argb},fgColor:{argb}}}}))});}
  table(main,'GrabResults',['序号','原始 ICCID','查询 ICCID（去 F）','状态','激活日期','激活申请日期','购买日期','Grab SIM Number','SKU','出现次数','来源','查询时间（香港）','来源链接','判定依据','Grab Cost（原值）'],rows.map((r,i)=>[i+1,r.originalIccid,r.queryIccid,r.status,date(r.activationDate),date(r.actRequestDate),date(r.purchaseDate),r.simNumber,r.sku,r.occurrences,r.sources.join('、'),hk(r.queriedAt),SOURCE_URL,basis[r.status],r.rawFields.lblCost||'']));
  decorate(main,'Grab ICCID 查询结果',`${stat.completed} / ${stat.total} 个 ICCID 已查询${stat.remaining?'；这是部分结果，未完成项见原始清单。':'；日期保留 Grab 原值，查询时间为香港时间。'}`,[8,28,28,13,22,22,15,28,32,10,16,23,68,36,20]);
  header(main,4,1,15);formats(main,[2,3,8,9],5,end,'@');formats(main,[5,6],5,end,'yyyy-mm-dd hh:mm');formats(main,[7],5,end,'yyyy-mm-dd');formats(main,[12],5,end,'yyyy-mm-dd hh:mm:ss');colors(main,`D5:D${end}`,'D5');main.views=[{state:'frozen',ySplit:4,xSplit:3,topLeftCell:'D5',showGridLines:false}];
  const frequencies=new Map();for(const r of job.sourceRows)frequencies.set(r.queryIccid,(frequencies.get(r.queryIccid)||0)+1);
  table(original,'OriginalIccids',['总序号','来源','来源行号','原始 ICCID','查询 ICCID（去 F）','状态','出现次数','激活日期','查询时间（香港）'],job.sourceRows.map((r,i)=>{const d=byKey.get(r.queryIccid);return [i+1,r.source,r.sourceRow,r.originalIccid,r.queryIccid,d?.status||'未查询',frequencies.get(r.queryIccid),date(d?.activationDate),hk(d?.queriedAt)];}));
  decorate(original,'原始 ICCID 清单','保留重复项；未查询不代表不存在。',[9,14,12,28,28,13,12,22,23]);header(original,4,1,9);formats(original,[4,5],5,rawEnd,'@');formats(original,[8],5,rawEnd,'yyyy-mm-dd hh:mm');formats(original,[9],5,rawEnd,'yyyy-mm-dd hh:mm:ss');colors(original,`F5:F${rawEnd}`,'F5');original.views=[{state:'frozen',ySplit:4,topLeftCell:'A5',showGridLines:false}];
  info.getRow(4).values=['输入统计','数量',null,'状态','数量'];
  ['原始行数','去重 ICCID 数','重复行数','已查询','未查询'].forEach((s,i)=>info.getCell(i+5,1).value=s);
  const inputFormulas=[[`COUNTA('原始清单'!$D$5:$D$${rawEnd})`,job.originalRows],[`${job.uniqueIccids}`,job.uniqueIccids],['B5-B6',job.originalRows-job.uniqueIccids],[`COUNTA('查询结果'!$C$5:$C$${end})`,rows.length],['B6-B8',stat.remaining]];
  inputFormulas.forEach(([formula,result],i)=>info.getCell(i+5,2).value={formula,result});
  STATUSES.forEach((s,i)=>{info.getCell(i+5,4).value=s;info.getCell(i+5,5).value={formula:`COUNTIF('查询结果'!$D$5:$D$${end},"${s}")`,result:stat.counts[s]};});info.getCell('D9').value='合计';info.getCell('E9').value={formula:'SUM(E5:E8)',result:rows.length};
  info.getRow(12).values=['状态','判定依据'];Object.entries(basis).forEach((r,i)=>info.getRow(i+13).values=r);
  [['开始时间（香港）',hk(job.startedAt)],['完成 / 停止（香港）',hk(job.finishedAt)],['数据来源',SOURCE_URL],['ICCID 处理','去掉末尾一个 F/f；完整 ICCID 以文本保存，不使用科学计数法。'],['状态说明','激活成功表示有激活日期，不代表当前套餐有效；激活中依据申请日期存在且激活日期为空。'],['不存在的范围','仅表示当前 Grab 登录账号内，完整 ICCID 查询返回 0 条。'],['任务编号',job.id]].forEach((r,i)=>info.getRow(i+18).values=r);
  decorate(info,'查询汇总与口径',`任务状态：${{completed:'已完成',paused:'已暂停',stopped:'已停止',failed:'异常中断'}[job.status]}。每次新任务实时查询，不读取历史结果作为缓存。`,[26,78,4,16,12,4]);
  header(info,4,1,2);header(info,4,4,5);header(info,12,1,2);formats(info,[2],18,19,'yyyy-mm-dd hh:mm:ss');for(let r=20;r<=24;r++){info.getRow(r).height=38;info.getCell(r,2).alignment={wrapText:true,vertical:'middle'};}colors(info,'D5:D8','D5');
  await fs.mkdir(path.dirname(output),{recursive:true});await wb.xlsx.writeFile(output);
  // Reopen the saved file, rather than trusting only the in-memory workbook.
  const saved=new ExcelJS.Workbook();await saved.xlsx.readFile(output);
  assert.deepEqual(saved.worksheets.map(s=>s.name),['查询结果','汇总说明','原始清单']);
  rows.forEach((r,i)=>{assert.equal(saved.getWorksheet('查询结果').getCell(i+5,3).value,r.queryIccid);assert.equal(saved.getWorksheet('查询结果').getCell(i+5,3).numFmt,'@');});
  job.sourceRows.forEach((r,i)=>assert.equal(saved.getWorksheet('原始清单').getCell(i+5,5).value,r.queryIccid));
  assert.equal(saved.getWorksheet('汇总说明').getCell('E9').result,rows.length);assert.equal(saved.getWorksheet('汇总说明').getCell('B5').result,job.sourceRows.length);
  for(const s of saved.worksheets)s.eachRow(row=>row.eachCell(c=>assert.ok(!c.value?.error,`公式错误 ${s.name}!${c.address}`)));
  return {output,completed:rows.length,total:job.uniqueIccids,verifiedTextIds:true};
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const [input,output]=process.argv.slice(2);if(!input||!output)throw new Error('Usage: report-portable.mjs JOB.json OUTPUT.xlsx');
  console.log(JSON.stringify(await exportPortable(JSON.parse(await fs.readFile(input,'utf8')),output)));
}
