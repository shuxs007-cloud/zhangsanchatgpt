import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
const require=createRequire('/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/package.json');
const {Workbook,SpreadsheetFile,FileBlob}=await import(require.resolve('@oai/artifact-tool'));
const option=name=>{const i=process.argv.indexOf(name);return i>=0?process.argv[i+1]:null;};
const dataFile=option('--data');
const out=dataFile?path.dirname(path.resolve(dataFile)):'/Volumes/zs01/chatgpt/OpenSMS/task_i/reports/20261012_182';
const outputFile=option('--output')||`${out}/任务I_182个ICCID历史统计.xlsx`;
const generic=Boolean(dataFile);
if(process.argv.includes('--slot-table-help')){
  const wb=await SpreadsheetFile.importXlsx(await FileBlob.load(`${out}/slot_edit/before_slots.xlsx`));
  console.log(wb.help('table',{search:'resize|range',include:'index,examples,notes',maxChars:2400}).ndjson);
  for(const name of ['逐卡统计','逐手机号统计'])console.log(JSON.stringify(wb.worksheets.getItem(name).tables.items.map(t=>({name:t.name,style:t.style,showHeaders:t.showHeaders,showTotals:t.showTotals,showBandedColumns:t.showBandedColumns,showFilterButton:t.showFilterButton}))));
  process.exit(0);
}
if(process.argv.includes('--inspect-slots')||process.argv.includes('--add-slots')){
  await patchSlotColumns(process.argv.includes('--inspect-slots'));process.exit(0);
}

async function patchSlotColumns(inspectOnly){
  const filename=`${out}/任务I_182个ICCID历史统计.xlsx`;
  const w=await SpreadsheetFile.importXlsx(await FileBlob.load(`${out}/slot_edit/before_slots.xlsx`));
  console.log((await w.inspect({kind:'sheet,table',maxChars:3000,tableMaxRows:2,tableMaxCols:4})).ndjson);
  if(inspectOnly){
    console.log(w.help('table.resize',{include:'index,examples,notes',maxChars:2400}).ndjson);
    for(const [sheetName,range,file] of [['逐卡统计','K5:Q12','before_cards_positions'],['逐手机号统计','I5:N12','before_phones_positions']]){
      const blob=await w.render({sheetName,range,scale:1.5,format:'png'});await fs.writeFile(`${out}/slot_edit/${file}.png`,new Uint8Array(await blob.arrayBuffer()));
    }
    return;
  }
  const d=JSON.parse(await fs.readFile(`${out}/query_result.json`,'utf8'));
  const {cards,phones,phoneKeys,display}=appendSlotColumns(w,d);
  w.recalculate();
  if(JSON.stringify(cards.getRange('P7:R188').values)!==JSON.stringify(d.queries.map(display)))throw Error('逐卡位置核验失败');
  if(JSON.stringify(phones.getRange('M7:O569').values)!==JSON.stringify(phoneKeys.map(k=>display(d.phoneSlots[k]))))throw Error('逐手机号位置核验失败');
  console.log((await w.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:30},maxChars:1500})).ndjson);
  console.log((await w.inspect({kind:'table',range:'逐卡统计!P6:R12',include:'values',tableMaxRows:7,tableMaxCols:3,maxChars:2500})).ndjson);
  for(const [sheetName,range,file] of [['逐卡统计','N5:R12','cards_positions'],['逐手机号统计','K5:O12','phones_positions']]){
    const blob=await w.render({sheetName,range,scale:1.5,format:'png'});await fs.writeFile(`${out}/slot_edit/${file}.png`,new Uint8Array(await blob.arrayBuffer()));
  }
  const xlsx=await SpreadsheetFile.exportXlsx(w);await xlsx.save(filename);
  await fs.writeFile(`${out}/slot_edit/edit_receipt.json`,JSON.stringify({status:'passed',newCardColumns:'P:R',newPhoneColumns:'M:O',iccidRows:182,phoneRows:563,slotSourceReadback:d.slotVerification.sampleCount,currentPositionSource:d.currentPositionSource},null,2));
  console.log(JSON.stringify({status:'passed',output:filename,iccidRows:182,phoneRows:563,slotSourceReadback:d.slotVerification.sampleCount}));
}

function appendSlotColumns(w,d){
  const cards=w.worksheets.getItem('逐卡统计'),phones=w.worksheets.getItem('逐手机号统计');
  const current=s=>s.currentSlots?.length?s.currentSlots.join('、'):s.currentSlotStatus==='search-not-found'?'搜索未命中':s.currentSlotStatus==='slot-missing'?'最新导出卡槽为空':'未实时查询';
  const display=s=>[s.historicalSlots.join('、')||'未知',s.historicalLastSlot||(s.historicalLastSlots.length?'待核对：'+s.historicalLastSlots.join('、'):'未知'),current(s)];
  const nc=d.queries.length+6,np=new Set(d.daily.map(r=>r.phone)).size+6;
  cards.getRange(`P6:R${nc}`).values=[['历史卡槽位置（全部）','历史最后卡槽位置','最新卡槽位置（搜索导出）'],...d.queries.map(display)];
  const phoneKeys=np>6?phones.getRange(`A7:A${np}`).values.map(([s])=>String(s).slice(-10)):[];
  phones.getRange(`M6:O${np}`).values=[['历史卡槽位置（全部）','历史最后卡槽位置','最新卡槽位置（搜索导出）'],...phoneKeys.map(k=>display(d.phoneSlots[k]))];
  for(const [sheet,range,header,rows,hist,last] of [[cards,`P6:R${nc}`,'P6:R6',`P7:R${nc}`,`P6:P${nc}`,nc],[phones,`M6:O${np}`,'M6:O6',`M7:O${np}`,`M6:M${np}`,np]]){
    const priorHeights=Array.from({length:last-6},(_,i)=>sheet.getRange(`A${i+7}`).format.rowHeight);
    sheet.getRange(range).format={font:{name:'Arial',size:10,color:'#17253A'},verticalAlignment:'center',horizontalAlignment:'left',wrapText:true,columnWidth:38};
    sheet.getRange(range).setNumberFormat('@');
    sheet.getRange(hist).format.columnWidth=46;
    sheet.getRange(header).format={fill:'#263E60',font:{name:'Arial',size:10,color:'#FFFFFF',bold:true},horizontalAlignment:'center',verticalAlignment:'center',rowHeight:28};
    if(last>6)sheet.getRange(rows).format.autofitRows();
    for(let row=7;row<=last;row++){
      const height=sheet.getRange(`A${row}`).format.rowHeight,prior=priorHeights[row-7];
      if(typeof height!=='number'||typeof prior!=='number')throw Error('无法核验原行高，停止写出');
      sheet.getRange(`A${row}`).format.rowHeight=Math.max(height,prior);
    }
  }
  const at=d.currentPositionSource?new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Hong_Kong',dateStyle:'short',timeStyle:'medium'}).format(new Date(d.currentPositionSource.exportedAt)):'未采集';
  cards.getRange('A5').values=[[`历史最后位置与最新位置分列。最新位置仅取号码管理ICCID搜索导出，采集时间：${at}（北京时间）。`]];
  phones.getRange('A5').values=[[`最新位置只匹配此次号码管理ICCID搜索导出中的同手机号；未命中的历史号码不借卡的新号位置补齐。采集：${at}。`]];
  for(const sheet of [cards,phones])sheet.getRange('A5').format.font={name:'Arial',size:10,color:'#52647B',italic:true};
  // The documented API has no table resize operation. Extend only the two
  // affected tables through delete/add, retaining their names and settings.
  for(const [sheet,name,range] of [[cards,'CardHistory',`A6:R${nc}`],[phones,'PhoneHistory',`A6:O${np}`]]){
    const table=sheet.tables.items.find(t=>t.name===name);
    const settings={style:table.style,showHeaders:table.showHeaders,showTotals:table.showTotals,showBandedColumns:table.showBandedColumns,showFilterButton:table.showFilterButton};
    table.delete();const replacement=sheet.tables.add(range,true,name);
    Object.assign(replacement,settings);
  }
  return {cards,phones,phoneKeys,display};
}

const d=JSON.parse(await fs.readFile(dataFile||`${out}/query_result.json`,'utf8'));
const w=Workbook.create();
const cards=w.worksheets.add('逐卡统计');
const phones=w.worksheets.add('逐手机号统计');
const summary=w.worksheets.add('批次汇总');
const daily=w.worksheets.add('逐日统计');
const sources=w.worksheets.add('采用来源');
const revisions=w.worksheets.add('历史变更与缺失');
const cols=n=>{let r='';for(;n;n=Math.floor((n-1)/26))r=String.fromCharCode(65+(n-1)%26)+r;return r;};
function style(s,rows,columns,headerRow=1){
  s.showGridLines=false;
  const end=cols(columns);
  s.getRange(`A1:${end}${rows}`).format={font:{name:'Arial',size:10,color:'#17253A'},verticalAlignment:'center',rowHeight:22};
  s.getRange(`A${headerRow}:${end}${headerRow}`).format={fill:'#263E60',font:{name:'Arial',size:10,color:'#FFFFFF',bold:true},horizontalAlignment:'center',verticalAlignment:'center',rowHeight:28};
  s.getRange(`A1:${end}${rows}`).format.columnWidth=16;
  if(rows>25)s.freezePanes.freezeRows(headerRow);
}
const dayRows=d.daily.map(r=>[r.query,r.batch,new Date(`${r.date}T00:00:00Z`),r.phone,...r.values,r.partial?'当日快照，未结束':'已结束日快照',r.sourceId,r.sheet,r.row,r.observed,r.duplicateSnapshots,`ICCID:${r.query}`]);
const nh=Math.max(2,dayRows.length+1);
const positions=new Map();
dayRows.forEach((r,i)=>{const row=i+2;const prev=positions.get(r[0]);if(prev&&prev.last!==row-1)throw Error('同一ICCID逐日记录必须连续');positions.set(r[0],{first:prev?.first??row,last:row});});
const phonePositions=new Map(),phoneSeen=new Map();
dayRows.forEach((r,i)=>{const identity=JSON.stringify([d.daily[i].date,d.daily[i].iccid,d.daily[i].phone]);
  if(phoneSeen.has(identity)){if(JSON.stringify(phoneSeen.get(identity))!==JSON.stringify(d.daily[i].values))throw Error('交叉查询同一流量计数不一致');return;}
  phoneSeen.set(identity,d.daily[i].values);
  const row=i+2;let runs=phonePositions.get(r[3]);if(!runs){runs=[];phonePositions.set(r[3],runs);}const last=runs.at(-1);if(last?.last===row-1)last.last=row;else runs.push({first:row,last:row});});
daily.getRange(`A1:O${nh}`).values=[['查询标识','批次','平台统计日期','历史手机号','发送窗口数','回复窗口数','总发送量','总回复量','日期状态','来源编号','工作表','源表行号','采用采集时间','已合并快照数','查询文本匹配键'],...(dayRows.length?dayRows:[Array(15).fill(null)])];
style(daily,nh,15);daily.getRange(`A2:A${nh}`).setNumberFormat('@');daily.getRange(`C2:C${nh}`).setNumberFormat('yyyy-mm-dd');daily.getRange(`D2:D${nh}`).setNumberFormat('@');
daily.getRange(`E2:H${nh}`).setNumberFormat('#,##0');daily.getRange(`J2:J${nh}`).setNumberFormat('0');daily.getRange(`L2:L${nh}`).setNumberFormat('0');daily.getRange(`N2:N${nh}`).setNumberFormat('#,##0');
daily.getRange(`A1:A${nh}`).format.columnWidth=25;daily.getRange(`C1:C${nh}`).format.columnWidth=19;daily.getRange(`I1:I${nh}`).format.columnWidth=26;daily.getRange(`M1:M${nh}`).format.columnWidth=26;
daily.freezePanes.freezeColumns(1);daily.tables.add(`A1:O${nh}`,true,'DailyMetrics');daily.getRange(`O1:O${nh}`).format.columnWidth=33;

const phoneGroups=new Map();
const phoneCounted=new Set();
for(const r of d.daily){const id=JSON.stringify([r.date,r.iccid,r.phone]);if(phoneCounted.has(id))continue;phoneCounted.add(id);if(!phoneGroups.has(r.phone))phoneGroups.set(r.phone,{iccids:new Set(),dates:new Set(),partial:new Set(),totals:[0,0,0,0]});const g=phoneGroups.get(r.phone);if(r.iccid)g.iccids.add(r.iccid);g.dates.add(r.date);if(r.partial)g.partial.add(r.date);r.values.forEach((v,i)=>g.totals[i]+=v);}
const phoneEntries=[...phoneGroups].sort(([a],[b])=>a.localeCompare(b));const np=phoneEntries.length+6;
phones.getRange('A2').values=[[`${phoneEntries.length}个历史手机号逐号统计`]];
phones.getRange('A4').values=[['每个历史手机号单独一行，统计本次查询范围；交叉查询的同一流量不重复累计。']];
phones.getRange(`A6:L${np}`).values=[['手机号','关联ICCID','统计日数','历史总回复率','发送窗口数','总回复数','回复窗口数','总发送量','总回复量','指标起日','指标止日','未结束日数'],...phoneEntries.map(([key,g])=>['1'+key,[...g.iccids].join('、'),g.dates.size,null,null,null,null,null,null,new Date([...g.dates].sort()[0]+'T00:00:00Z'),new Date([...g.dates].sort().at(-1)+'T00:00:00Z'),g.partial.size])];
style(phones,np,12,6);phones.getRange('A2').format.font={name:'Arial',size:14,bold:true};phones.getRange('A4').format.font={name:'Arial',size:10,italic:true,color:'#52647B'};
for(let row=7;row<=np;row++){
  // Sum exact, compact source ranges rather than scanning 10,636 rows for each
  // phone. Formulas remain auditable and avoid long-number criteria coercion.
  const runs=phonePositions.get(phoneEntries[row-7][0]);
  for(const [dest,src] of [['E','E'],['G','F'],['H','G'],['I','H']])phones.getRange(`${dest}${row}`).formulas=[[`=SUM(${runs.map(p=>`'逐日统计'!$${src}$${p.first}:$${src}$${p.last}`).join(',')})`]];
  phones.getRange(`F${row}`).formulas=[[`=I${row}`]];phones.getRange(`D${row}`).formulas=[[`=IF(E${row}=0,"未知",G${row}/E${row})`]];
}
if(np>6){phones.getRange(`A7:B${np}`).setNumberFormat('@');phones.getRange(`D7:D${np}`).setNumberFormat('0.00%');phones.getRange(`E7:I${np}`).setNumberFormat('#,##0');phones.getRange(`J7:K${np}`).setNumberFormat('yyyy-mm-dd');phones.getRange(`B7:B${np}`).format.wrapText=true;phones.getRange(`B7:B${np}`).format.autofitRows();}
phones.getRange(`A1:A${np}`).format.columnWidth=20;phones.getRange(`B1:B${np}`).format.columnWidth=32;phones.freezePanes.freezeColumns(1);phones.tables.add(`A6:L${np}`,true,'PhoneHistory');

cards.getRange('A2').values=[[`${d.queries.length}个查询标识历史统计`]];
cards.getRange('A4').values=[['仅汇总可精确匹配的已保存资料。总回复数与总回复量均指短信回复条数。']];
const ch=['ICCID或手机号','历史手机号（全部）','批次','文档路径数','统计日数','历史总回复率','发送窗口数','总回复数','回复窗口数','总发送量','总回复量','指标起日','指标止日','未结束日期','缺失计数组'];
const cr=d.queries.map(q=>[q.query,q.historicalPhones.join('、'),q.batch,q.documentCount,q.dates.length,null,null,null,null,null,null,q.dates.length?new Date(q.dates[0]+'T00:00:00Z'):null,q.dates.length?new Date(q.dates.at(-1)+'T00:00:00Z'):null,q.partialDates.join('、'),q.unknownDailyRecords]);
const nc=cr.length+6;cards.getRange(`A6:O${nc}`).values=[ch,...cr];style(cards,nc,15,6);
cards.getRange('A2').format.font={name:'Arial',size:14,bold:true};cards.getRange('A4').format.font={name:'Arial',size:10,italic:true,color:'#52647B'};
for(let i=7;i<=nc;i++){
  cards.getRange(`F${i}`).formulas=[[`=IF(G${i}="未知","未知",IF(G${i}=0,"未知",I${i}/G${i}))`]];
  const p=positions.get(cr[i-7][0]);
  // Each exact ICCID owns a verified contiguous daily block. Direct SUM ranges
  // avoid numeric coercion of 19-digit SUMIFS criteria and repeated large scans.
  for(const [dest,src] of [['G','E'],['I','F'],['J','G'],['K','H']]){
    if(p)cards.getRange(`${dest}${i}`).formulas=[[`=SUM('逐日统计'!$${src}$${p.first}:$${src}$${p.last})`]];
    else cards.getRange(`${dest}${i}`).values=[['未知']];
  }
  cards.getRange(`H${i}`).formulas=[[`=K${i}`]];
}
cards.getRange(`A7:B${nc}`).setNumberFormat('@');cards.getRange(`D7:E${nc}`).setNumberFormat('#,##0');cards.getRange(`F7:F${nc}`).setNumberFormat('0.00%');cards.getRange(`G7:K${nc}`).setNumberFormat('#,##0');cards.getRange(`L7:M${nc}`).setNumberFormat('yyyy-mm-dd');cards.getRange(`O7:O${nc}`).setNumberFormat('#,##0');
cards.getRange(`A1:A${nc}`).format.columnWidth=25;cards.getRange(`B1:B${nc}`).format.columnWidth=32;cards.getRange(`N1:N${nc}`).format.columnWidth=23;cards.getRange(`L1:M${nc}`).format.columnWidth=16;
cards.getRange(`B7:B${nc}`).format.wrapText=true;cards.getRange(`N7:N${nc}`).format.wrapText=true;cards.getRange(`B7:N${nc}`).format.autofitRows();cards.freezePanes.freezeColumns(2);cards.tabColor='#263E60';
cards.getRange(`E7:E${nc}`).conditionalFormats.add('cellIs',{operator:'equal',formula:0,format:{fill:'#FFF0CB',font:{color:'#8B5C00'}}});
cards.getRange(`O7:O${nc}`).conditionalFormats.add('cellIs',{operator:'greaterThan',formula:0,format:{fill:'#FFF0CB',font:{color:'#8B5C00'}}});
cards.tables.add(`A6:O${nc}`,true,'CardHistory');

summary.getRange('A2').values=[['任务I历史汇总']];
summary.getRange('A4').values=[[`已保存指标日期：${d.dateStart||'未知'} 至 ${d.dateEnd||'未知'}。索引更新时间：${d.indexedAt}。`]];
const totalRow=7+d.batches.length,notesRow=totalRow+3,summaryEnd=notesRow+9;
summary.getRange(`A7:J${totalRow}`).values=[['批次','提交标识数','文档命中数','有逐日计数','总回复率','发送窗口数','回复窗口数','总回复数','总发送量','总回复量'],...d.batches.map(b=>[b.label,b.count,b.withDocuments,b.withDailyStats,null,null,null,null,null,null])];
style(summary,summaryEnd,10,7);summary.tabColor='#263E60';summary.getRange('A2').format.font={name:'Arial',size:14,bold:true};summary.getRange('A4').format.font={name:'Arial',size:10,italic:true,color:'#52647B'};
for(let row=8;row<totalRow;row++){
  for(const [dest,src] of [['F','G'],['G','I'],['H','H'],['I','J'],['J','K']]){
    if(d.batches[row-8].totals===null||!d.batches[row-8].withDailyStats)summary.getRange(`${dest}${row}`).values=[['未知']];
    else summary.getRange(`${dest}${row}`).formulas=[[`=SUMIFS('逐卡统计'!$${src}$7:$${src}$${nc},'逐卡统计'!$C$7:$C$${nc},A${row})`]];
  }
  summary.getRange(`E${row}`).formulas=[[`=IF(F${row}="未知","未知",IF(F${row}=0,"未知",G${row}/F${row}))`]];
}
for(const c of ['B','C','D','F','G','H','I','J']){
  if(['F','G','H','I','J'].includes(c)&&(d.totals===null||!d.daily.length))summary.getRange(`${c}${totalRow}`).values=[['未知']];
  else summary.getRange(`${c}${totalRow}`).formulas=[[`=SUM(${c}8:${c}${totalRow-1})`]];
}
summary.getRange(`E${totalRow}`).formulas=[[`=IF(F${totalRow}="未知","未知",IF(F${totalRow}=0,"未知",G${totalRow}/F${totalRow}))`]];summary.getRange(`B8:D${totalRow}`).setNumberFormat('#,##0');summary.getRange(`F8:J${totalRow}`).setNumberFormat('#,##0');summary.getRange(`E8:E${totalRow}`).setNumberFormat('0.00%');summary.getRange(`A${totalRow}:J${totalRow}`).format.font.bold=true;summary.getRange(`A${totalRow}:J${totalRow}`).format.borders={bottom:{style:'double',color:'#263E60'}};
summary.getRange(`A${notesRow}`).values=[['统计口径']];summary.getRange(`A${notesRow}`).format.font.bold=true;
const notes=[
  '总回复率＝回复窗口数合计÷发送窗口数合计，不平均日报百分比。',
  '总回复数＝总回复量，都是短信回复条数；不是回复窗口数。',
  '同一ICCID、手机号、平台日期去重，已结束日优先，再取最新可确定采集时间。',
  `采用 ${d.usedSources.length} 份来源，形成 ${d.daily.length.toLocaleString('en-US')} 条逐日记录。已结束日历史数值变更 ${d.conflicts.length} 组。`,
  `${d.daily.filter(x=>x.partial).length.toLocaleString('en-US')} 条为未结束日快照，已单独注明，不冒充整日最终值。`,
  `本地发现 ${d.indexReceipt.documentCount.toLocaleString('en-US')} 份表格/文本表。旧同步目录与不支持格式单列未索引。`,
  '没有ICCID的旧表不通过现在的手机号倒推卡历史。本统计不保证完整生命周期。',
  `独立原表回读 ${d.verification.sampleCount} 条，逐日与每个查询的计数勾稽通过；原文件未改动。`,
];notes.forEach((n,i)=>summary.getRange(`A${notesRow+1+i}`).values=[[n]]);
summary.getRange(`A${notesRow+9}`).values=[[d.portfolioWarning||`没有收发明细的标识：${d.queries.filter(q=>!q.dailyRecordCount).map(q=>q.query).join('、')||'无'}。`]];
summary.getRange('A1:A22').format.columnWidth=15;summary.getRange('B1:D22').format.columnWidth=16;summary.getRange('E1:J22').format.columnWidth=19;

const srcRows=d.usedSources.map(s=>[s.id,s.filename,s.path,s.selectedDailyRows]),ns=Math.max(2,srcRows.length+1);
sources.getRange(`A1:D${ns}`).values=[['来源编号','文档名称','完整文档路径','采用逐日记录数'],...(srcRows.length?srcRows:[[null,'没有采用来源',null,null]])];style(sources,ns,4);sources.getRange(`A1:A${ns}`).format.columnWidth=12;sources.getRange(`B1:B${ns}`).format.columnWidth=68;sources.getRange(`C1:C${ns}`).format.columnWidth=120;sources.getRange(`D1:D${ns}`).format.columnWidth=24;sources.getRange(`B2:C${ns}`).format.wrapText=true;sources.getRange(`B2:C${ns}`).format.autofitRows();
const revRows=[...d.conflicts.map(r=>[r.query,r.date,r.phone,'已结束日历史数值有变化',r.versions.map(v=>v.join('/')).join('；'),r.chosen.join('/'),r.path]),...d.unknown.map(r=>[r.query,r.date,r.phone,r.reason,'','',''])];
revisions.getRange(`A1:G${Math.max(2,revRows.length+1)}`).values=[['ICCID','平台日期','历史手机号','情况','历史四项计数版本','采用四项计数','采用来源路径'],...(revRows.length?revRows:[['','','','无变更或缺失','','','']])];style(revisions,Math.max(2,revRows.length+1),7);
revisions.getRange(`A1:A${revRows.length+1}`).format.columnWidth=25;revisions.getRange(`D1:D${revRows.length+1}`).format.columnWidth=38;revisions.getRange(`E1:F${revRows.length+1}`).format.columnWidth=45;revisions.getRange(`G1:G${revRows.length+1}`).format.columnWidth=120;revisions.getRange(`E2:G${Math.max(2,revRows.length+1)}`).format.wrapText=true;revisions.getRange(`E2:G${Math.max(2,revRows.length+1)}`).format.autofitRows();

appendSlotColumns(w,d);
w.recalculate();
for(let i=7;i<=nc;i++){const q=d.queries[i-7];const actual=cards.getRange(`G${i}:K${i}`).values[0];const expected=q.dailyRecordCount?[q.totals[0],q.totals[3],q.totals[1],q.totals[2],q.totals[3]]:['未知','未知','未知','未知','未知'];if(JSON.stringify(actual)!==JSON.stringify(expected))throw Error(`逐卡核验失败: ${q.query}`);}
for(let i=7;i<=np;i++){const [key,g]=phoneEntries[i-7];const actual=phones.getRange(`E${i}:I${i}`).values[0];if(JSON.stringify(actual)!==JSON.stringify([g.totals[0],g.totals[3],g.totals[1],g.totals[2],g.totals[3]]))throw Error(`逐手机号核验失败: ${key}`);}
const headline=summary.getRange(`F${totalRow}:J${totalRow}`).values[0];const expected=d.totals&&d.daily.length?[d.totals[0],d.totals[1],d.totals[3],d.totals[2],d.totals[3]]:Array(5).fill('未知');
if(JSON.stringify(headline)!==JSON.stringify(expected))throw Error(`总计不一致: ${JSON.stringify(headline)} vs ${JSON.stringify(expected)}`);
const errors=await w.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:50},maxChars:3000});
console.log(errors.ndjson);
console.log((await w.inspect({kind:'table',range:`批次汇总!A7:J${totalRow}`,include:'values,formulas',tableMaxRows:4,tableMaxCols:10,maxChars:2500})).ndjson);
for(const [sheetName,range,file] of [['批次汇总',`A1:J${summaryEnd}`,'summary'],['逐卡统计','A1:R12','cards'],['逐手机号统计','A1:O12','phones'],['逐日统计','A1:N8','daily'],['采用来源','A1:D5','sources'],['历史变更与缺失','A1:G6','revisions']]){
  const blob=await w.render({sheetName,range,scale:1.5,format:'png'});await fs.writeFile(`${out}/${file}.png`,new Uint8Array(await blob.arrayBuffer()));
}
const xlsx=await SpreadsheetFile.exportXlsx(w);await xlsx.save(outputFile);
await fs.writeFile(`${out}/workbook_check.json`,JSON.stringify({status:'passed',headline,expected,sheets:6,renderedSheets:6,phoneRows:phoneEntries.length,allCardRowsVerified:true,allPhoneRowsVerified:true},null,2));
console.log(JSON.stringify({status:'passed',output:outputFile,dailyRows:d.daily.length,queryRows:d.queries.length,phoneRows:phoneEntries.length}));
