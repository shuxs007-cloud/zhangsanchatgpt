import test from 'node:test';
import assert from 'node:assert/strict';
import {verifiedRestOrigin} from '../scripts/opensms_swap_rest_origin_chain.mjs';
test('rest-to-swap chain requires exact identity and independently verified prior entry',()=>{
  const original={name:'B12',identif:'12'},rest={name:'周末休息区T',identif:'99'};
  const event={confirmed:true,phone:'4165550000',id:'123',slot:'1_2',iccid:'8901001',original:rest,at:'2026-10-11T14:00:00Z'};
  const record={key:'key',phone:event.phone,backendId:event.id,slot:event.slot,iccid:event.iccid,
    originalCarrier:original,restCarrier:rest,confirmedRunId:'20261011T210000+0800'};
  const before={phone:event.phone,id:event.id,slot:event.slot,iccid:event.iccid,carrierIdentif:'12'};
  const receipt={status:'passed',independentReadbackPassed:true,runId:record.confirmedRunId,
    batches:[{action:'enter',accepted:true,records:[record],before:[before],after:[{...before,carrierIdentif:'99'}]}]};
  assert.deepEqual(verifiedRestOrigin(event,record,receipt),original);
  assert.equal(verifiedRestOrigin(event,record,{...receipt,independentReadbackPassed:false}),null);
  assert.equal(verifiedRestOrigin({...event,id:'124'},record,receipt),null);
  assert.equal(verifiedRestOrigin({...event,at:'2026-10-11T12:00:00Z'},record,receipt),null);
  assert.equal(verifiedRestOrigin(event,record,{...receipt,batches:[{...receipt.batches[0],accepted:false}]}),null);
});
