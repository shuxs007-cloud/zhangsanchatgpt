import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {OpenSmsAuthStore, scopedAuthState, authStateFingerprint, authStateFromTargetCookies, AUTH_ORIGIN} from '../scripts/opensms_browser_auth_store.mjs';

const state = () => ({cookies:[
  {name:'Admin-Token',value:'test-only-token',domain:'adminsms.smsoug.com',path:'/',expires:-1,httpOnly:false,secure:true,sameSite:'Lax'},
  {name:'password',value:'must-not-save',domain:'adminsms.smsoug.com'},
  {name:'Admin-Token',value:'grab-must-not-save',domain:'grabandgoplus.com'},
],origins:[{origin:AUTH_ORIGIN,localStorage:[{name:'password',value:'must-not-save'}]}]});

function memoryAdapter() {
  let saved;
  const calls=[];
  const adapter=async(action,value)=>{
    calls.push(action);
    if(action==='put') {saved=structuredClone(value);return {status:'saved'};}
    return saved ? {status:'found',data:Buffer.from(JSON.stringify(saved)).toString('base64')} : {status:'missing'};
  };
  return {adapter,calls,payload:()=>saved};
}

test('exact-target CDP cookies keep only the auth fields and use the browser default SameSite',()=>{
  const cookie={...state().cookies[0],size:99,priority:'Medium',session:true};
  delete cookie.sameSite;
  const result=authStateFromTargetCookies([null,cookie,...state().cookies.slice(1)]);
  assert.deepEqual(result,{cookies:[state().cookies[0]],origins:[]});
  assert.doesNotMatch(JSON.stringify(result),/priority|size|session|must-not-save/);
  assert.throws(()=>authStateFromTargetCookies(null),/STATE_INVALID/);
  assert.throws(()=>authStateFromTargetCookies([null]),/MANUAL_LOGIN_REQUIRED/);
  assert.throws(()=>authStateFromTargetCookies([{...cookie,httpOnly:undefined}]),/STATE_INVALID/);
});

test('scopes only OpenSMS Admin-Token and excludes passwords, other websites and localStorage',()=>{
  const scoped=scopedAuthState(state());
  assert.equal(scoped.cookies.length,1);
  assert.equal(scoped.cookies[0].value,'test-only-token');
  assert.deepEqual(scoped.origins,[]);
  assert.doesNotMatch(JSON.stringify(scoped),/must-not-save/);
  assert.throws(()=>scopedAuthState({cookies:[],origins:[]}),/MANUAL_LOGIN_REQUIRED/);
  assert.throws(()=>scopedAuthState({}),/STATE_INVALID/);
  const invalid=state();invalid.cookies[0].expires='yesterday';
  assert.throws(()=>scopedAuthState(invalid),/STATE_INVALID/);
  assert.throws(()=>scopedAuthState({cookies:[null],origins:[]}),/MANUAL_LOGIN_REQUIRED/);
});

test('missing cache does not write; save independently reads back exact scoped state',async()=>{
  const memory=memoryAdapter(),store=new OpenSmsAuthStore(memory.adapter);
  assert.equal(await store.load(),null);
  assert.deepEqual(memory.calls,['get']);
  const receipt=await store.save(state());
  assert.deepEqual(memory.calls,['get','put','get']);
  assert.deepEqual(receipt,{stored:true,store:'local-macOS-Keychain',synchronized:false,readbackPassed:true});
  assert.doesNotMatch(JSON.stringify(receipt),/test-only-token|must-not-save/);
  assert.deepEqual(await store.load(),scopedAuthState(state()));
  assert.equal(memory.payload().origin,AUTH_ORIGIN);
});

test('explicit expiry clears auth material and future loads are missing',async()=>{
  const memory=memoryAdapter(),store=new OpenSmsAuthStore(memory.adapter);
  await store.save(state());
  await store.invalidate();
  assert.equal(await store.load(),null);
  assert.equal(memory.payload().expired,true);
  assert.deepEqual(memory.payload().storageState,{cookies:[],origins:[]});
});

test('corrupt, cross-origin, wrong-version and absent token records fail closed',async()=>{
  const payload={version:1,origin:AUTH_ORIGIN,expired:false,storageState:state()};
  for(const raw of ['not-json',JSON.stringify({...payload,version:2}),
    JSON.stringify({...payload,origin:'https://grabandgoplus.com'}),JSON.stringify({...payload,expired:'false'}),
    JSON.stringify({...payload,storageState:{cookies:[],origins:[]}})]) {
    const store=new OpenSmsAuthStore(async()=>({status:'found',data:Buffer.from(raw).toString('base64')}));
    await assert.rejects(store.load(),/STATE_INVALID/);
  }
});

test('independent readback mismatch stops; transport error is not cached as expiration',async()=>{
  const memory=memoryAdapter();
  const store=new OpenSmsAuthStore(async(action,value)=>{
    const result=await memory.adapter(action,value);
    if(action==='get' && result.status==='found') {
      const copy=structuredClone(memory.payload());copy.storageState.cookies[0].value='changed';
      return {...result,data:Buffer.from(JSON.stringify(copy)).toString('base64')};
    }
    return result;
  });
  await assert.rejects(store.save(state()),/READBACK_FAILED/);
  const broken=new OpenSmsAuthStore(async()=>{throw new Error('KEYCHAIN_UNAVAILABLE');});
  await assert.rejects(broken.load(),/KEYCHAIN_UNAVAILABLE/);
  assert.equal(memory.payload().expired,false);
});

test('change detector ignores unrelated state and detects only scoped token changes',()=>{
  const a=state(),b=state();b.cookies[1].value='different';b.origins=[];
  assert.equal(authStateFingerprint(a),authStateFingerprint(b));
  b.cookies[0].value='updated';
  assert.notEqual(authStateFingerprint(a),authStateFingerprint(b));
});

test('Keychain adapter uses private pipes, refuses offline access, never passes secrets as args/env/files',async()=>{
  const source=await fs.readFile(new URL('../scripts/opensms_browser_auth_store.mjs',import.meta.url),'utf8');
  assert.match(source,/spawn\(python, \[helper, action\]/);
  assert.match(source,/child.stdin.end\(/);
  assert.match(source,/OPENSMS_FIXED_RULES_OFFLINE/);
  assert.doesNotMatch(source,/console\.|writeFile|appendFile|execFile|process.env.*value/);
  const helper=await fs.readFile(new URL('../scripts/opensms_auth_keychain.py',import.meta.url),'utf8');
  assert.match(helper,/'?"kSecAttrSynchronizable"?: symbol\(cf, "kCFBooleanFalse"\)/);
  assert.match(helper,/kSecUseAuthenticationUIFail/);
  assert.doesNotMatch(helper,/open\(|write_text|write_bytes|subprocess|os.environ/);
});
