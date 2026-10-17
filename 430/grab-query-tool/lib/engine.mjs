import {EventEmitter} from 'node:events';
import {classify,now} from './model.mjs';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
export class Engine extends EventEmitter {
  constructor(store,provider,{delay=1000,loginPollMs=4000}={}){
    super();this.store=store;this.provider=provider;this.delay=delay;this.loginPollMs=loginPollMs;
    this.active=null;this.work=null;this.polling=false;this.shuttingDown=false;
  }
  async start(id){
    const job=this.store.get(id);
    if(job.exporting)throw new Error('正在导出此任务，请稍候');
    if(this.polling)throw new Error('正在检测登录，请稍候几秒');
    if(this.active&&this.active!==id)throw new Error('已有运行中或等待登录的任务，请先暂停或停止它');
    if(this.work)throw new Error('正在处理当前请求，请稍候');
    if(!['paused','failed','waiting_login'].includes(job.status))throw new Error('此任务不能继续；如需实时重查，请开始新任务');
    this.active=id;job.status='running';job.startedAt||=now();job.finishedAt=null;job.error='';job.message='正在检查登录并实时查询';
    job.report=null;job.events.push({at:now(),type:'start_or_resume'});
    this.work=this.run(job); // run() reaches an await before querying; the lock is synchronous.
    this.work.finally(()=>{this.work=null;}).catch(()=>{});
    return job;
  }
  async control(id,action){
    const job=this.store.get(id);
    if(action==='resume')return this.start(id);
    if(!['pause','stop'].includes(action))throw new Error('未知操作');
    if(['completed','stopped'].includes(job.status))throw new Error('任务已结束');
    if(job.exporting)throw new Error('正在导出，请稍候');
    if(action==='pause'){
      job.status=this.work&&this.active===id?'pausing':'paused';job.message='已请求暂停；当前只读请求收尾后暂停';
    }else{
      job.status=this.work&&this.active===id?'stopping':'stopped';job.message='已请求停止；当前请求收尾后停止，不再派发查询';
      if(job.status==='stopped')job.finishedAt=now();
    }
    job.events.push({at:now(),type:action});await this.store.save(job);
    if(['paused','stopped'].includes(job.status)&&this.active===id)this.active=null;
    return job;
  }
  async run(job){
    try{
      await this.store.save(job);
      if(job.status!=='running')return;
      await this.provider.ensureAuthenticated();
      if(job.status!=='running')return;
      const groups=new Map();
      for(const key of job.keys){const prefix=key.slice(0,15);if(!groups.has(prefix))groups.set(prefix,[]);groups.get(prefix).push(key);}
      for(const [prefix,keys]of groups){if(job.status!=='running')break;await this.batch(job,keys,prefix);}
      if(job.status==='running'){
        if(job.results.length!==job.uniqueIccids)throw new Error('结果条数未对齐，不能标为完成');
        job.status='completed';job.finishedAt=now();job.message='实时查询完成，可导出 Excel';job.events.push({at:now(),type:'completed'});
      }
    }catch(error){
      if(job.status==='running'){
        if(error.code==='LOGIN_REQUIRED'){
          job.status='waiting_login';job.message='请在登录窗口中手工登录；检测成功后自动从断点继续';
          job.events.push({at:now(),type:'login_required'});
          await this.store.save(job);
          try{await this.provider.openLogin();this.scheduleLogin();}catch(e){job.error=e.message;}
        }else{job.status='failed';job.finishedAt=now();job.error=error.message;job.message='查询中断，已保存结果；可点击继续重试未完成项';job.events.push({at:now(),type:'error',message:error.message});}
      }
    }finally{
      if(job.status==='pausing'){job.status='paused';job.message='已暂停，断点已保存';}
      if(job.status==='stopping'){job.status='stopped';job.finishedAt=now();job.message='已停止，已完成结果仍可查看与导出';}
      await this.store.save(job);
      if(job.status!=='waiting_login')this.active=null;
    }
  }
  async batch(job,keys,prefix){
    if(job.status!=='running')return;
    const done=new Set(job.results.map(r=>r.queryIccid));keys=keys.filter(k=>!done.has(k));if(!keys.length)return;
    const term=keys.length===1?keys[0]:prefix;
    const response=await this.provider.search(term);
    if(job.status==='stopping')return; // do not publish an in-flight response after Stop.
    if(!['running','pausing'].includes(job.status))return;
    if(response.searchValue!==term||response.rendered!==response.rows.length||response.total<response.rendered)throw new Error('网站查询回显或返回条数不一致');
    const map=new Map(response.rows.map(r=>[r.queryIccid,r]));if(map.size!==response.rows.length)throw new Error('网站返回重复 ICCID');
    const batch={id:job.batches.length+1,term,total:response.total,rendered:response.rendered,queriedAt:now()};
    const staged=[];
    for(const key of keys)if(map.has(key))staged.push(classify(key,map.get(key),batch,job.sourceRows));
    if(keys.length===1&&!map.has(keys[0]))staged.push(classify(keys[0],null,batch,job.sourceRows));
    job.batches.push(batch);job.results.push(...staged);
    const ordered=new Map(job.results.map(r=>[r.queryIccid,r]));job.results=job.keys.filter(k=>ordered.has(k)).map(k=>ordered.get(k));
    job.message=`已查询 ${job.results.length} / ${job.uniqueIccids}`;await this.store.save(job);
    await sleep(this.delay);if(job.status!=='running')return;
    const missing=keys.filter(k=>!ordered.has(k));if(!missing.length)return;
    if(response.total===response.rendered){for(const key of missing){if(job.status!=='running')break;await this.batch(job,[key],key);}return;}
    const next=new Map();for(const key of missing){const p=key.slice(0,prefix.length+1);if(!next.has(p))next.set(p,[]);next.get(p).push(key);}
    for(const [p,list]of next){if(job.status!=='running')break;await this.batch(job,list,p);}
  }
  scheduleLogin(){
    if(this.polling||this.shuttingDown)return;this.polling=true;
    const tick=async()=>{
      try{
        const job=this.active?this.store.get(this.active):null;
        if(!job||job.status!=='waiting_login'||this.shuttingDown)return;
        await this.provider.ensureAuthenticated();
        if(job.status==='waiting_login'&&!this.work){this.polling=false;await this.start(job.id);}
      }catch{}finally{
        const job=this.active?this.store.get(this.active):null;
        if(job?.status==='waiting_login'&&!this.shuttingDown){this.loginTimer=setTimeout(tick,this.loginPollMs);this.loginTimer.unref();}
        else this.polling=false;
      }
    };
    this.loginTimer=setTimeout(tick,this.loginPollMs);this.loginTimer.unref();
  }
  async checkLogin(){
    if(this.work||this.polling) return this.provider.state;
    await this.provider.ensureAuthenticated();
    const job=this.active?this.store.get(this.active):null;
    if(job?.status==='waiting_login')await this.start(job.id);
    return this.provider.state;
  }
  async shutdown(){
    this.shuttingDown=true;clearTimeout(this.loginTimer);
    if(this.active&&!['completed','stopped'].includes(this.store.get(this.active).status))await this.control(this.active,'pause');
    await this.work;
  }
}
