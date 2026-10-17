import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { acquireSiteLock } from './opensms_site_lock.mjs';

const moduleUrl = new URL('./opensms_site_lock.mjs', import.meta.url).href;
function child(directory, inherited = false) {
  const env = { ...process.env };
  if (!inherited) { delete env.OPENSMS_SITE_LOCK_TOKEN; delete env.OPENSMS_SITE_LOCK_PID; }
  const code = `import {acquireSiteLock} from ${JSON.stringify(moduleUrl)}; const lock=await acquireSiteLock({browserId:'test430',directory:${JSON.stringify(directory)},timeoutMs:600}); console.log(JSON.stringify({inherited:lock.inherited})); lock.release();`;
  return new Promise(resolve => {
    const p = spawn(process.execPath, ['--input-type=module', '-e', code], { env, cwd: os.tmpdir() });
    let stdout = '', stderr = '';
    p.stdout.on('data', b => stdout += b); p.stderr.on('data', b => stderr += b);
    p.on('close', status => resolve({ status, stdout, stderr }));
  });
}

test('cross-directory jobs wait, inherited children do not deadlock, release permits next job', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'opensms-lock-'));
  const lock = await acquireSiteLock({ browserId: 'test430', directory });
  try {
    const nested = await child(directory, true);
    assert.equal(nested.status, 0);
    assert.equal(JSON.parse(nested.stdout).inherited, true);
    assert.ok(fs.existsSync(lock.file));
    const blocked = await child(directory);
    assert.equal(blocked.status, 1);
    assert.match(blocked.stderr, /Timed out waiting/);
    const queued = child(directory);
    await new Promise(r => setTimeout(r, 100));
    lock.release();
    const completed = await queued;
    assert.equal(completed.status, 0);
    assert.equal(JSON.parse(completed.stdout).inherited, false);
    assert.equal(fs.existsSync(lock.file), false);
  } finally { lock.release(); fs.rmSync(directory, { recursive: true, force: true }); }
});
