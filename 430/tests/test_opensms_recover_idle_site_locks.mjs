import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {recoverIdleSiteLocks} from '../scripts/opensms_recover_idle_site_locks.mjs';

const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const id = 'a53ce74e6c9e4fa08aec35f81ce1e2a3';
function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'opensms-idle-lock-test-'));
  t.after(() => fs.rmSync(dir, {recursive:true, force:true}));
  const site = path.join(dir, `${id}.lock`), report = path.join(dir, 'opensms-new-browser-report.lock');
  for (const file of [site, report]) fs.writeFileSync(file, JSON.stringify({pid:100001, token:path.basename(file)}));
  fs.writeFileSync(path.join(dir, 'status.json'), JSON.stringify({status:'ready', headless:true,
    authentication:'verified-readonly-api', pid:100002, port:12345, sessionId:'session',
    webSocketPath:'/devtools/browser/exact', lastVerifiedTargetId:'baseline'}));
  fs.writeFileSync(path.join(dir, 'identity.json'), JSON.stringify({pid:100002, browserSessionId:'session', token:'private'}));
  const options = {authorizationSource:'current-user', siteSha256:hash(fs.readFileSync(site)), reportSha256:hash(fs.readFileSync(report))};
  let acquisitions = 0;
  const deps = {lockDirectory:dir, stateDirectory:dir, requestLock:path.join(dir, 'request.lock'),
    evidenceBase:path.join(dir, 'evidence'), alive:pid => pid === 100002,
    localJson:async (_endpoint, suffix) => suffix === '/json/version'
      ? {webSocketDebuggerUrl:'ws://127.0.0.1:12345/devtools/browser/exact'}
      : [{type:'page', id:'baseline', url:'https://adminsms.smsoug.com/index'}],
    acquire:async key => {
      acquisitions++;
      const file = path.join(dir, `${key}.lock`);
      fs.writeFileSync(file, 'new owner', {flag:'wx'});
      return {inherited:false, release() {fs.unlinkSync(file);}};
    }};
  return {dir, site, report, options, deps, count:() => acquisitions};
}
test('dead idle pair is archived by exact hash, never business-replayed', async t => {
  const f = fixture(t), result = await recoverIdleSiteLocks(f.options, f.deps);
  assert.equal(result.status, 'recovered');
  assert.equal(result.businessRequests, 0);
  assert.equal(result.businessReplay, false);
  assert.equal(f.count(), 2);
  assert.equal(fs.existsSync(f.site), false);
  assert.equal(fs.existsSync(f.report), false);
  assert.equal(hash(fs.readFileSync(path.join(result.evidenceDirectory, 'preserved-site.lock'))), f.options.siteSha256);
});
for (const scenario of ['authorization', 'live-owner', 'request-lane', 'wrong-hash', 'extra-target', 'browser-changed', 'symlink']) {
  test(`${scenario} refuses without moving locks`, async t => {
    const f = fixture(t);
    if (scenario === 'authorization') f.options.authorizationSource = 'timer';
    if (scenario === 'live-owner') f.deps.alive = () => true;
    if (scenario === 'request-lane') fs.mkdirSync(f.deps.requestLock);
    if (scenario === 'wrong-hash') f.options.siteSha256 = '0'.repeat(64);
    if (scenario === 'extra-target') {
      const original = f.deps.localJson;
      f.deps.localJson = async (...args) => args[1] === '/json/list'
        ? [...await original(...args), {type:'other', id:'orphan', url:'https://adminsms.smsoug.com/day'}]
        : original(...args);
    }
    if (scenario === 'browser-changed') {
      const original = f.deps.localJson;
      f.deps.localJson = async (...args) => {fs.appendFileSync(path.join(f.dir, 'status.json'), ' '); return original(...args);};
    }
    if (scenario === 'symlink') {fs.renameSync(f.site, f.site + '.old'); fs.symlinkSync(f.site + '.old', f.site);}
    await assert.rejects(recoverIdleSiteLocks(f.options, f.deps));
    assert.equal(f.count(), 0);
    assert.equal(fs.existsSync(f.site), true);
    assert.equal(fs.existsSync(f.report), true);
  });
}
test('changed lock during asynchronous checks is retained', async t => {
  const f = fixture(t), original = f.deps.localJson;
  f.deps.localJson = async (...args) => {fs.appendFileSync(f.report, ' '); return original(...args);};
  await assert.rejects(recoverIdleSiteLocks(f.options, f.deps), /changed/);
  assert.equal(f.count(), 0);
  assert.equal(fs.existsSync(f.site), true);
});
test('competing acquisition is never overwritten or removed', async t => {
  const f = fixture(t);
  f.deps.acquire = async () => {fs.writeFileSync(f.site, 'competing'); throw new Error('busy');};
  await assert.rejects(recoverIdleSiteLocks(f.options, f.deps), /busy/);
  assert.equal(fs.readFileSync(f.site, 'utf8'), 'competing');
  assert.equal(fs.existsSync(f.report), true);
});
