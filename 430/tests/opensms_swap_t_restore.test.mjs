import test from 'node:test';
import assert from 'node:assert/strict';
import {buildPlan,liveIdentity,latestEvents,verifyRows,verifyOnlineBeforeWrite,decodeWrite,MANUAL_RESTORE_SCOPES} from '../scripts/opensms_swap_t_restore_engine.mjs';
import {assertPreview,combineZonePlans,parsePhoneSelection} from '../scripts/opensms_manual_swap_t_restore.mjs';
test('selected restoration normalizes exact phones and rejects invalid or empty lists',()=>{
  assert.deepEqual(parsePhoneSelection(Buffer.from('电话\r\n14165550000\r\n4165550000\r\n')),
    {phones:['4165550000'],inputCount:2,duplicateCount:1});
  assert.throws(()=>parsePhoneSelection(Buffer.from('电话\n')),/EMPTY/);
  assert.throws(()=>parsePhoneSelection(Buffer.from('电话\nnot-a-phone\n')));
});
import {options as recoveryOptions} from '../scripts/opensms_manual_swap_rest_restore.mjs';
const zone={name:'换号区T',identif:'99'},target={name:'B12',identif:'12'};
const row={id:'1234567890123456789',cardNo:'4165550000',slotIdentif:'1_02',cardStore:'8901000000000000001F',opr:'Koodo',carrireIdentif:'99'};
const event={phone:'4165550000',id:row.id,slot:'1_2',iccid:'8901000000000000001',opr:'Koodo',at:'2026-10-08T12:00:00Z',confirmed:true,targetIdentif:'99',original:target,evidence:[]};
const carriers=[{name:'换号区T',identif:'99'},{name:'B12',identif:'12'}];
test('exact identity eligible; no automatic threshold or unrelated zone',()=>{
  assert.equal(buildPlan([row],carriers,[event],zone).eligibleCount,1);
  assert.throws(()=>buildPlan([{...row,carrireIdentif:'98'}],carriers,[event],zone),/SCOPE/);
});
test('explicit original ID authority follows renamed carriers and retains historical names',()=>{
  const renamed=[carriers[0],{name:'B12V',identif:'12'},{name:'B12',identif:'13'}];
  assert.equal(buildPlan([row],renamed,[event],zone).eligibleCount,0);
  const p=buildPlan([row],renamed,[event],zone,{originalCarrierIdAuthoritative:true});
  assert.equal(p.eligibleCount,1);assert.deepEqual(p.groups[0].target,{name:'B12V',identif:'12'});
  assert.deepEqual(p.groups[0].rows[0].originalCarrier,target);
  assert.equal(buildPlan([row],[carriers[0],{name:'B12',identif:'13'}],[event],zone,
    {originalCarrierIdAuthoritative:true}).eligibleCount,0);
  for(const name of ['周末休息区TV','E-T休息区','B13'])assert.equal(buildPlan([row],
    [carriers[0],{name,identif:'12'}],[event],zone,{originalCarrierIdAuthoritative:true}).eligibleCount,0);
});
test('online-only restoration requires both live status fields and repeats check before write',()=>{
  for(const status of [{},{displayStatus:'未上架',userAble:false},{displayStatus:'已上架',userAble:false}])
    assert.equal(buildPlan([{...row,...status}],carriers,[event],zone,{onlineOnly:true}).excluded[0].reason,'not_confirmed_currently_online');
  const online={...row,displayStatus:'已上架',userAble:true};
  const p=buildPlan([online],carriers,[event],zone,{onlineOnly:true});assert.equal(p.eligibleCount,1);
  verifyOnlineBeforeWrite(p.groups[0].rows,{rows:[online]});
  assert.throws(()=>verifyOnlineBeforeWrite(p.groups[0].rows,{rows:[row]}),/ONLINE_STATUS_CHANGED/);
});
test('C and E scopes use exact recorded source; never restore into another rest zone',()=>{
  for(const name of ['换号区C','E-T休息区','E-C休息区']){
    const z={...zone,name};assert.equal(buildPlan([row],[...carriers,z],[event],z).eligibleCount,1);
  }
  assert.equal(buildPlan([row],[...carriers,{name:'E-T休息区',identif:'22'}],
    [{...event,original:{name:'E-T休息区',identif:'22'}}],zone).eligibleCount,0);
  const write={ts:event.at,target:{...zone,name:'换号区C'},before:{eligibleRows:[{...row,slot:'1_2',carrireIdentif:'12'}]}};
  assert.equal(decodeWrite(write,{},null,[]).length,0);
  assert.equal(decodeWrite(write,{},null,[],false,MANUAL_RESTORE_SCOPES).length,1);
});
test('latest deleted origin blocks earlier valid origin',()=>{
  const newer={...event,at:'2026-10-08T13:00:00Z',original:{name:'B14-T',identif:'14'}};
  const p=buildPlan([row],carriers,[event,newer],zone);assert.equal(p.eligibleCount,0);
  assert.equal(p.excluded[0].reason,'original_carrier_deleted_or_changed');
});
test('latest unverified entry blocks old origin; unknown is not a default',()=>{
  const later={...event,at:'2026-10-08T13:00:00Z',confirmed:false};
  assert.equal(buildPlan([row],carriers,[event,later],zone).excluded[0].reason,'latest_entry_not_independently_verified');
  assert.equal(buildPlan([row],carriers,[],zone).excluded[0].reason,'no_original_record');
});
test('card, populated slot or backend ID drift does not restore',()=>{
  for(const changed of [{cardStore:'8901000000000000002'},{slotIdentif:'1_3'},{id:'999'}])
    assert.equal(buildPlan([{...row,...changed}],carriers,[event],zone).eligibleCount,0);
  assert.throws(()=>liveIdentity({...row,id:1234567890123456789}),/ID_INVALID/);
  assert.throws(()=>liveIdentity({...row,iccid:'8901999999999999999'}),/CONFLICT/);
});
test('network label alone can change but must not hide an ICCID change',()=>{
  assert.equal(buildPlan([{...row,opr:'Kini'}],carriers,[event],zone).eligibleCount,1);
  assert.equal(buildPlan([{...row,opr:'Kini',cardStore:'8901000000000000002'}],carriers,[event],zone).eligibleCount,0);
});
test('empty current slot alone does not invent a different card; ICCID must still match',()=>{
  const empty={...row,slotIdentif:null};
  assert.equal(buildPlan([empty],carriers,[event],zone).eligibleCount,1);
  assert.equal(buildPlan([{...empty,cardStore:'8901000000000000002'}],carriers,[event],zone).eligibleCount,0);
  assert.equal(buildPlan([{...empty,cardStore:null}],carriers,[event],zone).eligibleCount,0);
});
test('duplicate population or conflicting latest proofs fail closed',()=>{
  assert.throws(()=>buildPlan([row,row],carriers,[event],zone),/DUPLICATED/);
  assert.equal(latestEvents([event,{...event,original:{name:'B1',identif:'1'}}]).get(event.phone).confirmed,false);
});
test('independent readback must retain full identity and exact target',()=>{
  const expected=[liveIdentity(row)];verifyRows(expected,{total:1,rows:[{...row,carrireIdentif:'12'}]},target);
  assert.throws(()=>verifyRows(expected,{total:1,rows:[row]},target),/CHANGED/);
  assert.throws(()=>verifyRows(expected,{total:0,rows:[]},target),/INCOMPLETE/);
});
test('preview expiry, date, scope and plan changes block writes',()=>{
  const plan=buildPlan([row],carriers,[event],zone),now=Date.now(),run='20261009T010000+0800';
  const p={runId:run,mode:'readonly-preview',scope:'换号区T',serverDate:'2026-10-08',checkedAt:new Date(now).toISOString(),zone,plan};
  assertPreview(p,plan,zone,p.serverDate,run,now);
  assert.throws(()=>assertPreview(p,plan,zone,'2026-10-09',run,now),/EXPIRED/);
  assert.throws(()=>assertPreview(p,plan,zone,p.serverDate,run,now+900001),/EXPIRED/);
});
test('combined recovery freezes all four zones and the online-only swap constraint',()=>{
  assert.deepEqual(recoveryOptions.scopes,MANUAL_RESTORE_SCOPES);
  assert.deepEqual(recoveryOptions.onlineOnlyScopes,['换号区T','换号区C']);
  const t=buildPlan([row],carriers,[event],zone),c={populationCount:2,eligibleCount:1,excludedCount:1,
    groups:[{target,rows:[{...t.groups[0].rows[0],id:'2',phone:'4165550001'}]}],excluded:[{id:'3',reason:'unknown'}]};
  const plan=combineZonePlans([t,c]);assert.equal(plan.populationCount,3);assert.equal(plan.eligibleCount,2);
  assert.equal(plan.groups.length,1);assert.equal(plan.groups[0].rows.length,2);
  const now=Date.now(),run='20261010T010000+0800',zones=[zone,{name:'换号区C',identif:'98'}];
  const preview={runId:run,mode:'readonly-preview',scope:recoveryOptions.scopes,serverDate:'2026-10-09',
    checkedAt:new Date(now).toISOString(),zone:zones,plan};
  assertPreview(preview,plan,zones,preview.serverDate,run,now,recoveryOptions.scopes);
  assert.throws(()=>assertPreview(preview,plan,zones,preview.serverDate,run,now,['换号区T']),/EXPIRED/);
});
test('write receipt without independent verification cannot prove original',()=>{
  const write={ts:event.at,target:zone,before:{eligibleRows:[{...row,slot:'1_2',carrireIdentif:'12'}]}};
  assert.equal(decodeWrite(write,{},null,[])[0].confirmed,false);
});
test('independent verify may use a hash-verified copied candidate, never basename alone',()=>{
  const write={ts:event.at,status:'written',target:zone,actualWriteCount:1,independentReadbackPerformed:true,
    validatedCandidatePath:'/local/candidate.json',sourceCarriers:[target],
    before:{eligibleCount:1,eligibleRows:[{...row,slot:'1_2',carrireIdentif:'12'}]}};
  const candidate={count:1,sourcePath:'/source.xlsx',sourceSha256:'a'.repeat(64),rows:[{'电话':row.cardNo,
    '后台ID':row.id,'原运营商ID':'12','精确原运营商':'B12','卡槽':'1_2',opr:'Koodo'}]};
  const verify={checkedAt:'2026-10-08T12:00:02Z',source:'/delivery/candidate.json',target:zone,
    passed:true,independentReadbackPerformed:true,total:1,targetCount:1,missingCount:0,
    stillNotTargetCount:0,duplicateBackendCount:0};
  assert.equal(decodeWrite(write,candidate,verify,[],false)[0].confirmed,false);
  assert.equal(decodeWrite(write,candidate,verify,[],true)[0].confirmed,true);
  assert.equal(decodeWrite(write,candidate,{...verify,targetCount:0},[],true)[0].confirmed,false);
});
