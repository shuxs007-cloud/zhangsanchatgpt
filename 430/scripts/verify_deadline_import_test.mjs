// Independent local verification of the explicitly authorized 263-row import.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
const audit='/Users/zhangsan/Downloads/430/opensms_artifacts/manual_deadline_import/2026-10-02T23-31-04-375Z';
const receipts='/Volumes/zs01/chatgpt/OpenSMS/20261003/20261003T070204+0800/receipts';
const read=async p=>JSON.parse(await fs.readFile(p,'utf8'));
const sha=async p=>crypto.createHash('sha256').update(await fs.readFile(p)).digest('hex');
const source=await read(receipts+'/opensms_system_missing_f_dates_20261003T070204+0800.json');
if(await sha(source.output)!==source.sha256||await sha(source.sourceDetail)!==source.sourceDetailSha256)throw Error('Source evidence changed');
const detail=await read(source.sourceDetail),before=await read(audit+'/before.json'),after=await read(audit+'/after.json');
const h=detail.headers,index=n=>{const i=h.indexOf(n);if(i<0||h.lastIndexOf(n)!==i)throw Error('Missing/duplicate source column');return i;};
const key=x=>String(x||'').replace(/[Ff]$/,'');
const expected=new Map(detail.rows.map(r=>[key(r[index('ICCID')]),{phone:String(r[index('电话')]).slice(-10),slot:r[index('卡槽')],activation:r[index('F库激活日期')],cutoff:r[index('F库截止日')]}]));
const original=new Map(before.rows.map(r=>[key(r.cardStore),r]));
const seen=new Set(),mismatches=[],verified=[];
for(const r of after.rows){
  const iccid=key(r.cardStore),e=expected.get(iccid),old=original.get(iccid);
  if(!e||!old||seen.has(iccid)||r.cardNo!==e.phone||r.id!==old.id||r.slotIdentif!==e.slot){mismatches.push({iccid,reason:'identity mismatch'});continue;}
  seen.add(iccid);
  if(r.chatrActivateDate!==e.activation||r.chatrPlanExpire!==e.cutoff){mismatches.push({iccid,reason:'date mismatch',expected:e,actual:{activation:r.chatrActivateDate,cutoff:r.chatrPlanExpire}});continue;}
  if((old.chatrActivateDate&&old.chatrActivateDate!==e.activation)||(old.chatrPlanExpire&&old.chatrPlanExpire!==e.cutoff))throw Error('Existing date overwritten');
  verified.push({iccid,id:r.id,phone:r.cardNo,slot:r.slotIdentif,activation:r.chatrActivateDate,cutoff:r.chatrPlanExpire});
}
const missing=[...expected.keys()].filter(k=>!seen.has(k));
const result={status:mismatches.length||missing.length||verified.length!==263?'failed':'passed',sourceRunId:source.runId,file:source.output,fileSha256:source.sha256,checkedAt:new Date().toISOString(),fileRows:263,backendAcceptedRows:263,backendMatchedRecords:after.rows.length,verifiedDatePairs:verified.length,missingCount:missing.length,mismatchCount:mismatches.length,independentReadbackPassed:mismatches.length===0&&missing.length===0&&verified.length===263,beforeSha256:await sha(audit+'/before.json'),afterSha256:await sha(audit+'/after.json'),mismatches,missing,verified};
await fs.writeFile(path.join(audit,'verification.json'),JSON.stringify(result,null,2),{flag:'wx',mode:0o600});
console.log(JSON.stringify({...result,verified:undefined,verificationPath:path.join(audit,'verification.json')}));
if(result.status!=='passed')process.exitCode=1;
