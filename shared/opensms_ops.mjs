import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { ADMIN_ORIGIN } from './opensms_binding_protocol.mjs';
import { acquireSharedOpenSmsSession } from '/Users/zhangsan/Downloads/430/scripts/opensms_shared_browser.mjs';
import { createRawOpenSmsApiSession } from '/Users/zhangsan/Downloads/430/opensms_protocol_client.mjs';
import { collectCarrierConfigPagesReadOnly } from '/Users/zhangsan/Downloads/430/scripts/opensms_carriers_readonly.mjs';
import { checkpointFor, weightedBalance } from '/Users/zhangsan/Downloads/430/scripts/opensms_weighted_balance_monitor.mjs';
import { split, carrier, exactUser, checkAccount, checkSets, validateBatch, exactCreatedBindings, beijing, assertCancelTime, validCheck } from './opensms_ops_core.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const rules = JSON.parse(fs.readFileSync(path.join(root, 'opensms_rules.json')));
if (rules.origin !== ADMIN_ORIGIN || rules.browserId !== 'a53ce74e6c9e4fa08aec35f81ce1e2a3') throw new Error('Rules and protocol environment disagree');
const argv = process.argv.slice(2);
const apply = argv.includes('--apply');
const idIndex = argv.indexOf('--request-id');
const requestId = idIndex < 0 ? new Date().toISOString().replace(/[:.]/g, '-') : argv[idIndex + 1];
if (!/^[A-Za-z0-9_-]+$/.test(requestId || '')) throw new Error('Invalid request ID');
const args = argv.filter((x, i) => x !== '--apply' && i !== idIndex && i !== idIndex + (idIndex < 0 ? 0 : 1));
if (args.length > 3 || args.some(x => x.startsWith('--'))) throw new Error('Unexpected argument or option');
let [command, first, second] = args;
const help = `OpenSMS commands (preview by default; add --apply to write):
  add-users NAME[,NAME] CODE[,CODE]
  only-users NAME[,NAME] CODE[,CODE]
  add-groups SOURCE[,SOURCE] TARGET[,TARGET]
  only-group SOURCE TARGET[,TARGET]
  clear-groups CODE[,CODE]
  add-prefix PREFIX[,PREFIX] CODE[,CODE]
  mirror-users SOURCE_USER DEST_USER[,DEST_USER]
  query-user NAME | query-group CODE
  preset jingliao|stock NAME[,NAME]
  preset a3-only|c2-only|b1-b12
  auto-checkpoint YYYYMMDD_HHMM --apply
  auto-a3-b12 YYYYMMDD_HHMM --apply
  rollback-batch FILE.json --apply
  cancel-today YYYYMMDD --apply
  restore-users SNAPSHOT.json --apply
Options: --request-id UNIQUE_ID (never replay a completed/uncertain write)
Rules: ${path.join(root, 'opensms_rules.json')}`;
if (!command || command === 'help') { console.log(help); process.exit(0); }
if (command === 'rules') { console.log(JSON.stringify(rules, null, 2)); process.exit(0); }
let preset;
if (command === 'preset') {
  preset = rules.presets[first];
  if (!preset) throw new Error(`Unknown preset: ${first}`);
  command = preset.kind;
  first = preset.sources ? preset.sources.join(',') : second;
  second = preset.targets.join(',');
}
const commands = ['add-users','only-users','add-groups','only-group','clear-groups','add-prefix','mirror-users','query-user','query-group','auto-checkpoint','auto-a3-b12','rollback-batch','cancel-today','restore-users'];
if (!commands.includes(command) || !first) throw new Error(help);
if (command === 'auto-a3-b12' && checkpointFor()?.key !== first) {
  throw new Error('A3/B12 binding requires the current monitoring checkpoint');
}
if (command === 'rollback-batch') {
  const batch = validateBatch(JSON.parse(fs.readFileSync(first)));
  if (['c2-b12-12h-exact-id', 'a3-b12-12h-exact-id'].includes(batch.kind) && beijing() < batch.cancelAtBeijing) {
    throw new Error('B12 batch has not reached its 12-hour cancellation time');
  }
}
const runDir = path.join(root, 'opensms_runs', `${requestId}${apply ? '' : '_preview'}`);
if (fs.existsSync(runDir)) throw new Error(`Request already recorded; inspect ${runDir}, do not replay`);
const lockPath = path.join(root, '.opensms_ops.lock');
const siteLock = await acquireSharedOpenSmsSession();
let lock;
try { lock = fs.openSync(lockPath, 'wx'); } catch { throw new Error(`Another operation or stale lock exists: ${lockPath}. Inspect before retrying.`); }
fs.writeFileSync(lock, JSON.stringify({pid: process.pid, requestId, command, at: beijing()}));
fs.mkdirSync(runDir, {recursive: true});
const result = {requestId, command, apply, startedAtBeijing: beijing(), status:'running', failures:[], silentBrowserMode:true, browserForegroundActivationAttempted:false, calls:[]};
let seq = 0;
function save(name, data) { const p = path.join(runDir, name); fs.writeFileSync(p, JSON.stringify(data,null,2)); return p; }
function invoke(script, params, writable = false, batchFolder = root) {
  if (writable && !apply) throw new Error('Writes require --apply');
  const p = path.join(runDir, `${String(++seq).padStart(3,'0')}_${params[0] || 'call'}.json`);
  const err = `${p}.stderr`;
  const fd = fs.openSync(p, 'wx');
  const ef = fs.openSync(err, 'wx');
  let child;
  try {
    child = spawnSync(process.execPath, [path.join(root,script), ...params], {
      cwd:root, env:{...process.env, OPENSMS_SILENT_BROWSER:'1', OPENSMS_STRICT_MATCH:'1', OPENSMS_OUTPUT_DIR:runDir, OPENSMS_BATCH_DIR:batchFolder},
      stdio:['ignore',fd,ef], timeout:7200000,
    });
  } finally { fs.closeSync(fd); fs.closeSync(ef); }
  result.calls.push({params, path:p, stderrPath:err, write:writable, exitCode:child.status});
  save('result.json', result);
  if (child.error || child.status !== 0) throw new Error(`Command incomplete/failed; do not retry blindly. See ${p} and ${err}`);
  const data = JSON.parse(fs.readFileSync(p,'utf8'));
  if (data.ok === false) throw new Error(`Business failure: ${p}`);
  return data;
}
const raw = (params, write = false) => invoke('opensms_raw_cdp_binding_tmp.mjs', params, write);
const group = c => raw(['query-source-identif',String(c.identif)]);
const readUser = name => raw(['query-account-bindings',name]);
let carriers;
function resolve(codes) {
  const list=split(codes);
  if(!list.length) throw new Error('Empty carrier selector');
  return list.map(c => carrier(carriers,c));
}
function accounts(names) {
  const wanted = split(names);
  if (!wanted.length) throw new Error('Empty account selector');
  const found = raw(['query-agents-matching',wanted.join(',')]).users;
  return wanted.map(n => exactUser(found,n));
}
function independentUsers(before, targets, only) {
  const rows = before.map(b => {
    const after = readUser(b.user.proxyUserName);
    if (String(after.user.proxyUserId) !== String(b.user.proxyUserId)) throw new Error('Account identity changed');
    return {user:after.user,bindings:after.bindings,...checkAccount(b.bindings,after.bindings,targets.map(x=>x.identif),only)};
  });
  save('independent_accounts.json', rows);
  if (!rows.every(validCheck)) throw new Error('Independent account verification failed');
  result.verifiedCount = rows.length;
  result.finalCarriers = Object.fromEntries(rows.map(r=>[r.user.proxyUserName,r.bindings.map(b=>b.carrierName)]));
}
function counts(business) {
  const list = Array.isArray(business) ? business : [business];
  result.created = list.reduce((n,r)=>n+(r.addCount||r.created||0),0);
  result.deleted = list.reduce((n,r)=>n+(r.deleteCount||r.deletedCount||0),0);
  result.alreadyBound = list.reduce((n,r)=>n+(r.alreadyCount||r.alreadyBound||0),0);
}
function rollback(file) {
  const batch = validateBatch(JSON.parse(fs.readFileSync(file)));
  if (['c2-b12-12h-exact-id', 'a3-b12-12h-exact-id'].includes(batch.kind)) {
    if (batch.status === 'completed') throw new Error('B12 batch already completed');
    if (!/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(batch.cancelAtBeijing || '') || beijing() < batch.cancelAtBeijing) {
      throw new Error('B12 batch has not reached its 12-hour cancellation time');
    }
    if (!batch.target?.identif || batch.bindings.some(row => row.carrireIdentif !== batch.target.identif)) {
      throw new Error('B12 batch contains a non-target binding');
    }
  }
  const resolved = new Map(carriers.map(c=>[String(c.identif),c]));
  const before = new Map();
  for (const b of batch.bindings) {
    if (!resolved.has(b.carrireIdentif)) throw new Error(`Batch carrier no longer resolvable: ${b.carrireIdentif}`);
    if (!before.has(b.carrireIdentif)) before.set(b.carrireIdentif, group(resolved.get(b.carrireIdentif)).bindings);
  }
  if (!apply) return {requested:batch.bindings.length};
  const business = raw(['remove-binding-ids',path.resolve(file)],true);
  const requested = new Set(batch.bindings.map(b=>b.id));
  for (const [ident,old] of before) {
    const final = group(resolved.get(ident)).bindings;
    const finalIds = new Set(final.map(b=>String(b.id)));
    if ([...requested].some(id=>finalIds.has(id))) throw new Error('Requested ID still present');
    if (old.some(b=>!requested.has(String(b.id))&&!finalIds.has(String(b.id)))) throw new Error('Unrequested binding disappeared');
  }
  return business;
}
try {
  if (command === 'auto-a3-b12') {
    if (!/^\d{8}_(?:0[1-9]|10|11)(?:00|30)$/.test(first) || first.endsWith('1130')) {
      throw new Error('Invalid A3/B12 monitoring checkpoint');
    }
    if (checkpointFor()?.key !== first) {
      throw new Error('A3/B12 binding requires the current monitoring checkpoint');
    }
    const apiSession = await createRawOpenSmsApiSession({cdp: siteLock.session.cdp, pathName: '/index'});
    let balance;
    try {
      const page = await collectCarrierConfigPagesReadOnly(apiSession.api, {concurrency: 1});
      balance = weightedBalance(page.rows);
    } finally { await apiSession.browser.close(); }
    result.balance = {dailyRemaining: balance.dailyRemaining, dailyTotal: balance.dailyTotal, ratio: balance.ratio};
    result.triggered = balance.ratio < 0.1;
    if (!result.triggered) {
      result.plan = {reason: '四组加权余量不低于10%'};
    } else {
      carriers = raw(['query-carriers','*']).matches;
      const source = resolve('A3')[0], target = resolve('B12')[0];
      const beforeSource = group(source), beforeTarget = group(target);
      save('before_groups.json', {sources:[beforeSource], targets:[beforeTarget]});
      const sourceIds = new Set(beforeSource.bindings.map(row => String(row.proxyUserId)));
      result.candidateCount = sourceIds.size;
      result.plan = {sources:[source], targets:[target], checkpoint:first};
      if (apply) {
        if (checkpointFor()?.key !== first) throw new Error('A3/B12 write window expired before dispatch');
        let business, mutationError;
        try { business = raw(['add-source', source.name, target.name], true); }
        catch (error) {
          mutationError = error;
          try { business = JSON.parse(fs.readFileSync(result.calls.at(-1).path, 'utf8')); }
          catch { business = null; }
        }
        const afterSource = group(source), afterTarget = group(target);
        const currentSourceIds = new Set(afterSource.bindings.map(row => String(row.proxyUserId)));
        const allSourceIds = new Set([...sourceIds, ...currentSourceIds]);
        const {bindings:createdBindings, unexplainedNewTargetIds} = exactCreatedBindings(
          beforeTarget.bindings, afterTarget.bindings, allSourceIds, business?.results, target.identif);
        if (createdBindings.length) {
          const batch = {
            kind:'a3-b12-12h-exact-id', status:'pending', checkpoint:first,
            createdAtBeijing:beijing(), cancelAtBeijing:beijing(new Date(Date.now() + 12 * 60 * 60 * 1000)),
            sourceRun:runDir,
            sourceCarrier:{name:source.name, identif:String(source.identif)},
            target:{name:target.name, identif:String(target.identif)},
            sourceCount:allSourceIds.size, created:createdBindings.length, bindings:createdBindings,
          };
          validateBatch(batch);
          result.batchPath = save('a3_b12_12h_batch.json', batch);
          result.cancelAtBeijing = batch.cancelAtBeijing;
          result.needsRollbackScheduling = true;
        }
        const verification = {
          missing:checkSets([...allSourceIds], afterTarget.bindings),
          lost:[...beforeSource.bindings, ...beforeTarget.bindings].filter(row => !(
            row.carrireIdentif === String(source.identif) ? afterSource.bindings : afterTarget.bindings
          ).some(current => String(current.id) === String(row.id))),
          createdCount:createdBindings.length,
          expectedCreated:business?.addCount ?? null,
          unexplainedNewTargetIds,
        };
        save('independent_groups.json', {verification, sources:[afterSource], targets:[afterTarget]});
        result.created = createdBindings.length;
        result.deleted = 0;
        result.alreadyBound = business?.results?.filter(row => row.added.length === 0).length ?? null;
        result.verifiedCount = allSourceIds.size;
        result.missing = verification.missing;
        if (mutationError) throw mutationError;
        if (verification.missing.length || verification.lost.length || verification.unexplainedNewTargetIds.length || verification.createdCount !== verification.expectedCreated) {
          throw new Error('Independent A3/B12 verification failed; inspect batch and receipt');
        }
      }
    }
  } else if (command === 'auto-checkpoint') {
    if (!apply) { result.plan = {checkpoint:first,rule:rules.automaticB12}; }
    else {
      const receipt = path.join(root,`opensms_guarded_b12_${first}.json`);
      // Checkpoint receipts are durable idempotency keys, even if a previous run failed.
      const business = fs.existsSync(receipt) ? JSON.parse(fs.readFileSync(receipt)) : invoke('opensms_guarded_b12_checkpoint.mjs',[first],true);
      if (!business.ok) throw new Error(`Checkpoint incomplete: ${receipt}; inspect, do not replay`);
      carriers = raw(['query-carriers','*']).matches;
      const sources = resolve(rules.automaticB12.sources.join(','));
      const target = resolve('B12')[0];
      const users = new Set(); result.sourceBindingCounts={};
      for (const c of sources) { const rows=group(c).bindings; const ids=new Set(rows.map(r=>String(r.proxyUserId))); result.sourceBindingCounts[c.name]=ids.size; ids.forEach(id=>users.add(id)); }
      const final = group(target).bindings;
      const finalIds = new Set(final.map(r=>String(r.id)));
      result.missing = checkSets([...users],final);
      result.originalB12Missing = business.initialB12Ids.filter(id=>!finalIds.has(String(id)));
      if (result.missing.length || result.originalB12Missing.length) throw new Error('Independent automatic verification failed');
      counts(business); result.candidateCount=users.size; result.batchPath=business.batchPath; result.cancelAtBeijing=business.batchPath?`${beijing().slice(0,10)} 13:00:00`:null;
      if (business.created>0) {
        const batch=validateBatch(JSON.parse(fs.readFileSync(business.batchPath)));
        if(batch.bindings.length!==business.created||batch.bindings.some(b=>!finalIds.has(b.id))) throw new Error('New batch verification failed');
      }
      result.schedulerStatus='External heartbeat must be confirmed by Codex; no timer created or modified';
    }
  } else {
    carriers = raw(['query-carriers','*']).matches;
    if (command === 'query-user') { result.query=readUser(first); }
    else if (command === 'query-group') { const cs=resolve(first);if(cs.length!==1)throw new Error('query-group requires exactly one source');result.query=group(cs[0]); }
    else if (command === 'rollback-batch') { const b=rollback(first); result.business=b; result.batchPath=path.resolve(first); counts(b); }
    else if (command === 'cancel-today') {
      assertCancelTime(first);
      const files=fs.readdirSync(root).filter(n=>n.startsWith(`opensms_b12_union_batch_${first}`)&&n.endsWith('.json'));
      const b12=resolve('B12')[0];
      for(const f of files){const b=validateBatch(JSON.parse(fs.readFileSync(path.join(root,f)))); if(b.cancelAtBeijing!==`${beijing().slice(0,10)} 13:00:00`||b.bindings.some(r=>r.carrireIdentif!==String(b12.identif))) throw new Error(`Wrong-day/non-B12 batch: ${f}`);}
      result.batches=files.map(f=>({file:path.join(root,f),business:rollback(path.join(root,f))})); counts(result.batches.map(x=>x.business));
    } else if (command === 'restore-users') {
      const snapshot=JSON.parse(fs.readFileSync(first));
      if(!Array.isArray(snapshot)||!snapshot.length) throw new Error('Expected account snapshot array');
      const jobs=snapshot.map(b=>{const u=accounts(b.user.proxyUserName)[0];if(u.proxyUserId!==b.user.proxyUserId)throw new Error('Snapshot account identity mismatch');if(!b.bindings.length)throw new Error('Empty restore set requires explicit review');const ids=[...new Set(b.bindings.map(x=>String(x.carrireIdentif)))];const cs=ids.map(id=>{const c=carriers.find(x=>String(x.identif)===id);if(!c)throw new Error('Snapshot carrier missing');return c;});return {u,cs,before:readUser(u.proxyUserName)};});
      result.candidateCount=jobs.length;result.plan=jobs.map(j=>({user:j.u,targets:j.cs}));
      if(apply){const business=[];for(const j of jobs){business.push(raw(['only-accounts',j.u.proxyUserName,j.cs.map(c=>c.name).join(',')],true));independentUsers([j.before],j.cs,true);}counts(business);}
    } else if (command === 'clear-groups') {
      const targets=resolve(first); result.plan={targets};const before=targets.map(c=>group(c));result.candidateCount=new Set(before.flatMap(r=>r.bindings.map(b=>b.proxyUserId))).size;save('before_groups.json',before);
      if(apply){const business=targets.map(c=>raw(['remove-source-identif',String(c.identif)],true));counts(business);const after=targets.map(c=>group(c));save('independent_groups.json',after);if(after.some(r=>r.count!==0))throw new Error('Carrier still has bindings');result.remaining=0;}
    } else if (command === 'add-groups') {
      const sources=resolve(first),targets=resolve(second);if(!targets.length)throw new Error('Empty target');
      const beforeSources=sources.map(c=>group(c)),beforeTargets=targets.map(c=>group(c));save('before_groups.json',{sources:beforeSources,targets:beforeTargets});
      result.candidateCount=new Set(beforeSources.flatMap(r=>r.bindings.map(b=>String(b.proxyUserId)))).size;result.plan={sources,targets};
      if(apply){const business=raw(['add-sources-union',sources.map(c=>c.name).join(','),targets.map(c=>c.name).join(',')],true);counts(business);
        const afterSources=sources.map(c=>group(c)),afterTargets=targets.map(c=>group(c));const users=[...new Set([...beforeSources,...afterSources].flatMap(r=>r.bindings.map(b=>String(b.proxyUserId))))];
        const verification={missing:afterTargets.flatMap((r,i)=>checkSets(users,r.bindings).map(id=>({id,carrier:targets[i].name}))),lost:[]};
        for(const [olds,news]of [[beforeSources,afterSources],[beforeTargets,afterTargets]])olds.forEach((r,i)=>{const ids=new Set(news[i].bindings.map(x=>String(x.id)));verification.lost.push(...r.bindings.filter(x=>!ids.has(String(x.id))));});
        save('independent_groups.json',{verification,sources:afterSources,targets:afterTargets});if(verification.missing.length||verification.lost.length)throw new Error('Independent union verification failed');result.verifiedCount=users.length;result.missing=[];
      }
    } else if (command === 'only-group') {
      const sources=resolve(first),targets=resolve(second);if(sources.length!==1||!targets.some(t=>t.identif===sources[0].identif))throw new Error('only-group must retain its source');
      const before=raw(['query-source-users',sources[0].name,'--with-bindings']);save('before_accounts.json',before.accountBindings.map(b=>({user:{proxyUserId:b.proxyUserId,proxyUserName:b.proxyUserName},bindings:b.bindings})));result.candidateCount=before.count;result.plan={sources,targets};
      if(apply){const business=raw(['only-source',sources[0].name,targets.map(c=>c.name).join(',')],true);counts(business);const after=raw(['query-source-users',sources[0].name,'--with-bindings']);const current=new Set(after.users.map(x=>String(x.proxyUserId)));if(before.users.some(x=>!current.has(String(x.proxyUserId))))throw new Error('Source membership lost');const checks=after.accountBindings.map(b=>({...b,...checkAccount([],b.bindings,targets.map(t=>t.identif),true)}));save('independent_accounts.json',checks);if(!checks.every(validCheck))throw new Error('Keep-only verification failed');result.verifiedCount=checks.length;result.deletedByCarrier={};for(const r of business.results)for(const c of r.deleted||[])result.deletedByCarrier[c]=(result.deletedByCarrier[c]||0)+1;}
    } else {
      let targets,users;
      if(command==='mirror-users'){const reference=readUser(first);save('reference_user.json',reference);targets=reference.bindings.map(b=>carriers.find(c=>String(c.identif)===String(b.carrireIdentif)));if(!targets.length||targets.some(x=>!x))throw new Error('Reference target set unavailable');users=accounts(second);}
      else {targets=resolve(second);if(!targets.length)throw new Error('Empty target');if(command==='add-prefix'){const prefixes=split(first).map(x=>x.toLowerCase());users=raw(['query-agents-matching',split(first).join(',')]).users.filter(u=>prefixes.some(p=>u.proxyUserName.toLowerCase().startsWith(p)));}else users=accounts(first);}
      if(!users.length)throw new Error('No matching accounts');const before=users.map(u=>readUser(u.proxyUserName));save('before_accounts.json',before);result.candidateCount=users.length;result.plan={users,targets};
      if(apply){const only=command==='only-users';const business=raw([only?'only-accounts':'add-accounts',users.map(u=>u.proxyUserName).join(','),targets.map(c=>c.name).join(',')],true);counts(business);independentUsers(before,targets,only);}
    }
  }
  result.status = apply ? 'verified' : 'preview';
} catch (error) { result.status='failed';result.failures.push({error:error.message});process.exitCode=1; }
finally {
  result.finishedAtBeijing=beijing();const receipt=save('result.json',result);
  if (command === 'rollback-batch' && apply && result.status === 'verified' && result.batchPath) {
    try {
      const batch=JSON.parse(fs.readFileSync(result.batchPath));
      if (['c2-b12-12h-exact-id', 'a3-b12-12h-exact-id'].includes(batch.kind)) {
        const completed={...batch,status:'completed',completedAtBeijing:result.finishedAtBeijing,rollbackReceipt:receipt};
        const temporary=`${result.batchPath}.${process.pid}.tmp`;
        fs.writeFileSync(temporary,JSON.stringify(completed,null,2));
        fs.renameSync(temporary,result.batchPath);
      }
    } catch (error) {
      result.status='verification_completed_batch_status_failed';
      result.failures.push({error:`Batch completion receipt failed: ${error.message}`});
      save('result.json',result);
      process.exitCode=1;
    }
  }
  const report=path.join(runDir,'报告.md');
  const targets=(result.plan?.targets||[]).map(c=>c.name).join('、');
  const removed=Object.entries(result.deletedByCarrier||{}).map(([c,n])=>`- ${c}：${n}条`).join('\n');
  const finalAccounts=Object.entries(result.finalCarriers||{}).map(([n,cs])=>`- ${n}：${cs.join('、')}`).join('\n');
  fs.writeFileSync(report,`# OpenSMS 执行报告\n\n状态：${result.status}\n\n目标运营商：${targets||'-'}\n\n| 项目 | 数量 |\n|---|---:|\n| 候选账号 | ${result.candidateCount??'-'} |\n| 新增绑定 | ${result.created??0} |\n| 取消绑定 | ${result.deleted??0} |\n| 已有绑定 | ${result.alreadyBound??'-'} |\n| 独立复核账号 | ${result.verifiedCount??'-'} |\n| 失败 | ${result.failures.length} |\n\n执行时间：${result.startedAtBeijing} 至 ${result.finishedAtBeijing}（北京时间）\n\n${result.balance?`四组加权余量：${result.balance.dailyRemaining}/${result.balance.dailyTotal} = ${(result.balance.ratio*100).toFixed(2)}%。\n\n`:''}${result.cancelAtBeijing?`本轮新增绑定最早撤销时间：${result.cancelAtBeijing}（北京时间）；是否已安排定时任务须另行确认。\n\n`:''}${result.batchPath?`[精确 ID 撤销批次](${result.batchPath})\n\n${result.batchPath}\n\n`:''}${removed}\n\n${finalAccounts}\n\n${result.failures.map(e=>e.error).join('\n')}\n\n[详细回执](${receipt})\n\n${receipt}\n`);
  fs.closeSync(lock);fs.unlinkSync(lockPath);
  siteLock.release();
  console.log(JSON.stringify({status:result.status,candidates:result.candidateCount,created:result.created,deleted:result.deleted,batchPath:result.batchPath,cancelAtBeijing:result.cancelAtBeijing,failures:result.failures,receiptPath:receipt,reportPath:report},null,2));
}
