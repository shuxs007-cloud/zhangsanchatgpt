import test from 'node:test';
import assert from 'node:assert/strict';
import {validateSearchRows,validateSearchExport} from '../scripts/opensms_task_i_current_slots.mjs';
const key='8912230200278094459',scope=new Set([key]);
test('search ICCID scope exact, F suffix allowed',()=>{
  validateSearchRows({code:200,total:1,rows:[{cardStore:key+'F',slotIdentif:'101_35.01'}]},scope);
});
test('out-of-scope ICCID stops before export',()=>{
  assert.throws(()=>validateSearchRows({code:200,total:1,rows:[{cardStore:'8912230200278094400',slotIdentif:'101_35.01'}]},scope),/OUTSIDE_ICCID_SCOPE/);
});
test('numeric ICCID and slot types are never guessed',()=>{
  assert.throws(()=>validateSearchRows({code:200,total:1,rows:[{cardStore:8912230200278094459,slotIdentif:'101_35.01'}]},scope),/OUTSIDE_ICCID_SCOPE/);
  assert.throws(()=>validateSearchRows({code:200,total:1,rows:[{cardStore:key,slotIdentif:12}]},scope),/SLOT_TYPE_INVALID/);
});
test('HTML, business-error JSON and HTTP failures are not Excel downloads',()=>{
  assert.throws(()=>validateSearchExport({status:200,ok:true,base64:Buffer.from('{"code":500}').toString('base64')}),/NOT_XLSX/);
  assert.throws(()=>validateSearchExport({status:502,ok:false,base64:Buffer.from('PKbad').toString('base64')}),/HTTP_502/);
});
