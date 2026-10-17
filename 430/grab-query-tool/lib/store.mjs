import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {EventEmitter} from 'node:events';
import {now,parseInput,summary,SOURCE_URL} from './model.mjs';
export class Store extends EventEmitter {
  constructor(dir){super();this.dir=dir;this.jobs=new Map();this.writes=new Map();}
  file(id){if(!/^[a-zA-Z0-9_-]{8,80}$/.test(id))throw new Error('任务编号无效');return path.join(this.dir,id+'.json');}
  async init(){
    await fs.mkdir(this.dir,{recursive:true,mode:0o700});
    for(const name of await fs.readdir(this.dir)){
      if(!name.endsWith('.json'))continue;
      const job=JSON.parse(await fs.readFile(path.join(this.dir,name),'utf8'));
      if(!job.id||!Array.isArray(job.results)||!Array.isArray(job.sourceRows))throw new Error(`历史记录损坏：${name}`);
      this.jobs.set(job.id,job);
      if(['running','pausing','stopping','waiting_login'].includes(job.status)){
        job.status=job.status==='stopping'?'stopped':'paused';job.message='程序曾退出，断点已保留；点击继续只查未完成的号码。';
        if(job.status==='stopped')job.finishedAt=now();
        await this.save(job);
      }
      if(job.exporting){job.exporting=false;await this.save(job);}
    }
    return this;
  }
  get(id){const job=this.jobs.get(id);if(!job){const e=new Error('未找到历史任务');e.httpStatus=404;throw e;}return job;}
  list(){return [...this.jobs.values()].sort((a,b)=>b.createdAt.localeCompare(a.createdAt)).map(summary);}
  async create(text){
    const input=parseInput(text);
    const job={id:randomUUID(),version:1,createdAt:now(),startedAt:null,finishedAt:null,updatedAt:now(),status:'paused',
      rawText:text,...input,results:[],batches:[],events:[],error:'',message:'待开始',sourceUrl:SOURCE_URL,report:null};
    this.jobs.set(job.id,job);await this.save(job);return job;
  }
  async save(job){
    job.updatedAt=now();job.version=(job.version||0)+1;
    const content=JSON.stringify(job,null,2)+'\n';
    const previous=this.writes.get(job.id)||Promise.resolve();
    const write=previous.catch(()=>{}).then(async()=>{
      const file=this.file(job.id),tmp=file+'.tmp';
      const handle=await fs.open(tmp,'w',0o600);
      try{await handle.writeFile(content);await handle.sync();}finally{await handle.close();}
      await fs.rename(tmp,file);
    });
    this.writes.set(job.id,write);await write;this.emit('update',summary(job));
  }
  async importReceipt(receipt,report){
    const id='imported-'+receipt.startedAt.replace(/[^0-9]/g,'').slice(0,17);
    if(this.jobs.has(id))return this.get(id);
    const job={...receipt,id,createdAt:receipt.startedAt,status:'completed',keys:receipt.results.map(r=>r.queryIccid),
      rawText:receipt.sourceRows.map(r=>r.originalIccid).join('\n'),events:[],error:'',message:'已导入的历史查询，不作为新任务缓存',
      imported:true,report,exporting:false};
    this.jobs.set(id,job);await this.save(job);return job;
  }
}
