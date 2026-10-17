export const STATUSES=['激活成功','激活中','未激活','不存在'];
export const SOURCE_URL='https://www.grabandgoplus.com/Retailer_CustomerSIMCards';
export const now=()=>new Date().toISOString();
export class LoginRequired extends Error { constructor(message='登录已失效，请在弹出的 Grab 浏览器中手工登录'){super(message);this.code='LOGIN_REQUIRED';} }
export function parseInput(text){
  if(typeof text!=='string'||Buffer.byteLength(text)>1_000_000)throw new Error('清单不能为空，且不能超过 1 MB');
  const tokens=text.split(/[\s,，;；、]+/).filter(Boolean);
  if(!tokens.length||tokens.length>20000)throw new Error('请输入 1 至 20,000 条 ICCID');
  const sourceRows=tokens.map((value,i)=>({source:'文本框',sourceRow:i+1,originalIccid:value,queryIccid:value.replace(/[Ff]$/,'')}));
  const invalid=sourceRows.filter(r=>!/^\d{18,22}$/.test(r.queryIccid));
  if(invalid.length){const error=new Error(`有 ${invalid.length} 项不是有效 ICCID，请修改后再开始（18–22 位数字，可带末尾 F）。`);error.invalid=invalid.slice(0,30);throw error;}
  const keys=[...new Set(sourceRows.map(r=>r.queryIccid))];
  return {sourceRows,keys,originalRows:tokens.length,uniqueIccids:keys.length,duplicateRows:tokens.length-keys.length};
}
export function summary(job){
  const counts=Object.fromEntries(STATUSES.map(s=>[s,0]));
  for(const row of job.results)counts[row.status]++;
  return {id:job.id,status:job.status,createdAt:job.createdAt,startedAt:job.startedAt,finishedAt:job.finishedAt,
    updatedAt:job.updatedAt,total:job.uniqueIccids,completed:job.results.length,remaining:job.uniqueIccids-job.results.length,
    originalRows:job.originalRows,duplicateRows:job.duplicateRows,counts,error:job.error||'',message:job.message||'',
    reportReady:!!job.report,exporting:!!job.exporting,imported:!!job.imported};
}
export function classify(key,match,batch,sourceRows){
  const originals=sourceRows.filter(r=>r.queryIccid===key);
  if(!originals.length)throw new Error('拒绝清单外 ICCID');
  const f=match?.fields||{};
  let status='不存在';
  if(match){
    for(const field of ['lblPurchaseDate','lblActivationRequestDate','lblActivateDate','lblSimCardNumber','lblSKU']){
      if(!Object.hasOwn(f,field))throw new Error(`网站缺少字段 ${field}，未将此项标为不存在`);
    }
    if(f.lblSimCardNumber.replace(/\s+/g,'').replace(/[Ff]$/,'')!==key)throw new Error('ICCID 身份校验失败');
    for(const value of [f.lblActivateDate,f.lblActivationRequestDate,f.lblPurchaseDate]){
      if(value&&!/^\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?$/.test(value))throw new Error('网站日期格式异常，保留为未查询');
      if(value){const d=new Date(value.slice(0,10)+'T00:00:00Z');if(!Number.isFinite(+d)||d.toISOString().slice(0,10)!==value.slice(0,10))throw new Error('网站日期无效');}
    }
    status=f.lblActivateDate?'激活成功':f.lblActivationRequestDate?'激活中':'未激活';
  }else if(batch.term!==key||batch.total!==0||batch.rendered!==0)throw new Error('只有完整 ICCID 查询返回 0 条时才允许标记不存在');
  return {queryIccid:key,originalIccid:originals[0].originalIccid,status,occurrences:originals.length,
    sources:[...new Set(originals.map(r=>r.source))],purchaseDate:f.lblPurchaseDate||'',actRequestDate:f.lblActivationRequestDate||'',
    activationDate:f.lblActivateDate||'',simNumber:f.lblSimCardNumber||'',sku:f.lblSKU||'',rawFields:f,
    queriedAt:batch.queriedAt,batchId:batch.id,sourceUrl:SOURCE_URL};
}
