const $=id=>document.getElementById(id);
const names={running:'查询中',pausing:'正在暂停',paused:'已暂停',stopping:'正在停止',stopped:'已停止',waiting_login:'等待登录',failed:'查询中断',completed:'已完成'};
const classes={'激活成功':'activated','激活中':'pending','未激活':'','不存在':'missing'};
let token='',selected=null,current=null,jobs=[],active=null,page=0,loading=false,queued=false,events=null;
const size=100;
const time=value=>value?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value)):'—';
function notice(text){$('notice').textContent=text;$('notice').hidden=!text;}
async function api(url,method='GET',data){const response=await fetch(url,{method,headers:method==='POST'?{'content-type':'application/json','x-tool-token':token}:{},...(data?{body:JSON.stringify(data)}:{})});const result=await response.json();if(!response.ok)throw new Error(result.error+(result.invalid?'\n'+result.invalid.map(r=>`第 ${r.sourceRow} 项：${r.originalIccid}`).join('\n'):''));return result;}
function auth(state){$('auth').textContent=state.status==='authenticated'?'Grab 已登录':state.status==='login_required'?'需要手工登录':'尚未检查登录';$('auth').className='pill '+(state.status==='authenticated'?'good':state.status==='login_required'?'warn':'');$('auth').title=state.message||'';}
function badge(status){const span=document.createElement('span');span.className='badge '+(classes[status]||'');span.textContent=status;return span;}
function history(){const list=$('history');list.replaceChildren();if(!jobs.length){const p=document.createElement('p');p.className='empty';p.textContent='还没有历史任务';list.append(p);return;}for(const job of jobs){
  const item=document.createElement('article');item.className='history-item'+(job.id===selected?' selected':'');item.tabIndex=0;item.setAttribute('role','button');item.setAttribute('aria-label',`查看 ${time(job.startedAt||job.createdAt)} 的查询`);
  const top=document.createElement('div');top.className='history-top';const title=document.createElement('span');title.textContent=`${job.total.toLocaleString()} 个 ICCID`;const state=document.createElement('span');state.className='pill';state.textContent=names[job.status]||job.status;top.append(title,state);
  const start=document.createElement('p');start.textContent='开始 '+time(job.startedAt||job.createdAt);const finish=document.createElement('p');finish.textContent='结束 '+time(job.finishedAt);
  const bottom=document.createElement('div');bottom.className='history-bottom';const progress=document.createElement('span');progress.textContent=`已查 ${job.completed} / ${job.total}`;bottom.append(progress);
  if(job.completed>0&&!['running','pausing','stopping','waiting_login'].includes(job.status)){const download=document.createElement('button');download.textContent=job.exporting?'生成中':'下载 Excel';download.disabled=job.exporting;download.onclick=e=>{e.stopPropagation();downloadJob(job.id);};bottom.append(download);}
  item.append(top,start,finish,bottom);item.onclick=()=>select(job.id);item.onkeydown=e=>{if(e.key==='Enter')select(job.id);};list.append(item);
}}
function render(){if(!current)return;const j=current;$('taskTitle').textContent=!j.imported&&jobs[0]?.id===j.id?'当前查询':'历史查询';$('taskStatus').textContent=names[j.status]||j.status;$('taskStatus').className='pill '+(j.status==='completed'?'good':j.status==='waiting_login'?'warn':'');
  $('taskTimes').textContent=`开始：${time(j.startedAt)}　结束：${time(j.finishedAt)}`;
  for(const key of ['total','completed','remaining'])$(key).textContent=j[key].toLocaleString();$('originalCount').textContent=`${j.originalRows} / ${j.duplicateRows}`;
  $('progress').value=j.total?j.completed/j.total*100:0;$('progressText').textContent=j.error?`${j.message}：${j.error}`:j.message;
  $('counts').replaceChildren(...Object.entries(j.counts).map(([status,n])=>{const el=badge(status);el.textContent=`${status} ${n}`;return el;}));
  $('resume').disabled=!['paused','failed','waiting_login'].includes(j.status)||!!j.exporting||!!(active&&active!==j.id);
  $('pause').disabled=!['running','waiting_login'].includes(j.status);$('stop').disabled=['completed','stopped','stopping'].includes(j.status)||!!j.exporting;
  $('export').disabled=!j.completed||!!j.exporting||['running','pausing','stopping','waiting_login'].includes(j.status);$('export').textContent=j.exporting?'正在生成 Excel…':j.remaining?'导出已查询部分':'导出 Excel';$('reuse').disabled=false;
  const filter=$('statusFilter').value,query=$('search').value.trim();const rows=j.results.filter(r=>(!filter||r.status===filter)&&(!query||r.originalIccid.includes(query)||r.queryIccid.includes(query)));
  const pages=Math.max(1,Math.ceil(rows.length/size));page=Math.min(page,pages-1);const tbody=$('results');tbody.replaceChildren();
  for(const r of rows.slice(page*size,(page+1)*size)){const tr=document.createElement('tr');for(const [i,v]of [r.originalIccid,r.queryIccid,r.status,r.activationDate||'—',r.actRequestDate||'—',r.purchaseDate||'—',r.occurrences].entries()){const td=document.createElement('td');i===2?td.append(badge(v)):td.textContent=v;tr.append(td);}tbody.append(tr);}
  if(!rows.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=7;td.className='empty';td.textContent=j.status==='waiting_login'?'等待你手工登录 Grab…':'暂时没有符合条件的结果';tr.append(td);tbody.append(tr);}
  $('pageLabel').textContent=`${rows.length} 条 · ${page+1} / ${pages} 页`;$('prev').disabled=page===0;$('next').disabled=page+1>=pages;
}
async function refresh(){if(loading){queued=true;return;}loading=true;try{const data=await api('/api/jobs');jobs=data.jobs;active=data.active;auth(data.auth);$('start').disabled=!!active;selected||=active||jobs[0]?.id;if(selected)current=await api(`/api/jobs/${selected}`);history();render();}catch(e){notice(e.message);}finally{loading=false;if(queued){queued=false;refresh();}}}
async function select(id){selected=id;page=0;await refresh();}
async function action(fn){try{notice('');await fn();}catch(e){notice(e.message);}finally{await refresh();}}
async function downloadJob(id){await action(async()=>{notice('正在准备 Excel，请稍候…');const data=await api(`/api/jobs/${id}/export`,'POST',{});const a=document.createElement('a');a.href=data.url;a.download='';a.click();notice('Excel 已生成，浏览器开始下载。');});}
$('start').onclick=()=>action(async()=>{if(!$('input').value.trim())throw new Error('请先输入 ICCID');$('start').disabled=true;const j=await api('/api/jobs','POST',{text:$('input').value});selected=j.id;page=0;});
for(const command of ['resume','pause','stop'])$(command).onclick=()=>action(async()=>{if(!selected)return;if(command==='stop'&&!confirm('停止当前任务？已完成结果会保留；停止后不再继续，需要新建任务才能重查。'))return;await api(`/api/jobs/${selected}/${command}`,'POST',{});});
$('export').onclick=()=>selected&&downloadJob(selected);$('login').onclick=()=>action(async()=>{const d=await api('/api/login','POST',{});auth(d.auth);notice('请在弹出的 Grab 窗口中手工登录。正在等待登录的任务会自动继续，也可点击“已登录，检查并继续”。');});$('checkLogin').onclick=()=>action(async()=>{const d=await api('/api/login/check','POST',{});auth(d.auth);notice(d.auth.status==='authenticated'?'登录已确认。':d.auth.message);});
$('clear').onclick=()=>{$('input').value='';count();};$('reuse').onclick=()=>{if(current){$('input').value=current.rawText;count();$('input').focus();notice('清单已复制。点击“开始新查询”会重新实时查询全部号码。');}};
$('refresh').onclick=refresh;$('prev').onclick=()=>{page--;render();};$('next').onclick=()=>{page++;render();};for(const id of ['statusFilter','search'])$(id).oninput=()=>{page=0;render();};
function count(){const tokens=$('input').value.split(/[\s,，;；、]+/).filter(Boolean);const keys=tokens.map(t=>t.replace(/[Ff]$/,''));const bad=keys.filter(k=>!/^\d{18,22}$/.test(k)).length;$('inputCount').textContent=tokens.length?`原始 ${tokens.length} 条 · 去重 ${new Set(keys).size} 条 · 无效 ${bad} 条`:'末尾 F / f 自动去除，同一任务内重复号码只查询一次。';}
$('input').oninput=count;
try{const data=await api('/api/bootstrap');token=data.token;auth(data.auth);await refresh();events=new EventSource('/api/events');events.addEventListener('update',refresh);events.addEventListener('ready',refresh);events.onerror=()=>notice('进度连接暂时中断，正在自动重连；后台任务与断点仍会保留。');events.onopen=()=>{if($('notice').textContent.startsWith('进度连接'))notice('');refresh();};}catch(e){notice(e.message);}
